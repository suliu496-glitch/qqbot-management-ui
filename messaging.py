#!/usr/bin/env python3
"""QQ Bot 通信原语层 — log, _api, _send_text"""
import json, os, sys, threading, logging, time
from logging.handlers import RotatingFileHandler
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from collections import OrderedDict

from retry import with_retry

# 确保 Windows 终端 UTF-8 输出
try:
    if hasattr(sys.stdout, 'reconfigure') and sys.stdout.encoding != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, 'reconfigure') and sys.stderr.encoding != "utf-8":
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env", override=False)

NAP_CAT_HTTP = os.environ.get("NAP_CAT_HTTP", "http://127.0.0.1:3000").strip()

_print_lock = threading.Lock()
# Phase 4.3: _processed_ids 改为 LRU (OrderedDict)，精确淘汰最旧
_processed_ids: "OrderedDict[str, None]" = OrderedDict()
_MAX_PROCESSED_IDS = 10000
# Phase 4.3: 最新一条消息的 reply 引用信息缓存（供 bot.py 提取）
_last_reply_info: list[dict] = []
_memory_locks: dict[str, threading.Lock] = {}
_memory_locks_guard = threading.Lock()

def log(msg: str):
    with _print_lock:
        try:
            print(f"[qq-bot] {msg}", flush=True)
        except Exception:
            sys.stderr.write(f"[qq-bot] {msg}\n"); sys.stderr.flush()
        try:
            _file_logger.info(msg)
        except Exception:
            pass


# ==================== Phase 1.3: 分级日志 ====================
_LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40, "FATAL": 50}

class Logger:
    """结构化日志 + 内存缓冲 + 错误聚合。

    兼容旧 log(msg) 函数；新代码建议用 Logger.info()/Logger.error() 等。
    """
    _buffer: list[dict] = []
    _max_buffer: int = 500
    _buff_lock = threading.Lock()

    @classmethod
    def log(cls, level: str, msg: str, **extra):
        """分级日志：stdout + 文件 + 内存缓冲"""
        if _LOG_LEVELS.get(level, 20) < _LOG_LEVELS.get(_min_level, 10):
            return
        with _print_lock:
            try:
                print(f"[qq-bot][{level}] {msg}", flush=True)
            except Exception:
                sys.stderr.write(f"[qq-bot][{level}] {msg}\n"); sys.stderr.flush()
            try:
                _file_logger.info(f"[{level}] {msg}")
            except Exception:
                pass
        entry = {"level": level, "msg": msg, "time": time.time()}
        if extra:
            entry["extra"] = extra
        with cls._buff_lock:
            cls._buffer.append(entry)
            if len(cls._buffer) > cls._max_buffer:
                cls._buffer = cls._buffer[-cls._max_buffer // 2:]

    @classmethod
    def debug(cls, msg, **extra): cls.log("DEBUG", msg, **extra)
    @classmethod
    def info(cls, msg, **extra): cls.log("INFO", msg, **extra)
    @classmethod
    def warn(cls, msg, **extra): cls.log("WARN", msg, **extra)
    @classmethod
    def error(cls, msg, **extra): cls.log("ERROR", msg, **extra)
    @classmethod
    def fatal(cls, msg, **extra): cls.log("FATAL", msg, **extra)

    @classmethod
    def get_recent_errors(cls, minutes: int = 5) -> list[dict]:
        """最近 N 分钟的 ERROR/FATAL 错误聚合，供健康检查报告"""
        cutoff = time.time() - minutes * 60
        with cls._buff_lock:
            return [e for e in cls._buffer
                    if e["level"] in ("ERROR", "FATAL") and e["time"] >= cutoff]

    @classmethod
    def flush_on_crash(cls):
        """崩溃前 flush：把缓冲写入 panic dump"""
        try:
            with cls._buff_lock:
                if not cls._buffer:
                    return
                dump_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), "data", "logs",
                    f"crash_dump_{int(time.time())}.log")
                with open(dump_path, "w", encoding="utf-8") as f:
                    for e in cls._buffer:
                        f.write(f"[{time.strftime('%H:%M:%S', time.localtime(e['time']))}]"
                                f"[{e['level']}] {e['msg']}\n")
        except Exception:
            pass

_min_level = "DEBUG"

_file_logger = None
def _init_file_logger(data_dir: str = ""):
    global _file_logger
    if data_dir:
        log_dir = os.path.join(data_dir, "logs")
    else:
        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "bot.log")
    _file_logger = logging.getLogger("qq-bot-file")
    _file_logger.setLevel(logging.INFO)
    _file_logger.propagate = False
    h = RotatingFileHandler(log_path, maxBytes=10*1024*1024, backupCount=5, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s [qq-bot] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    _file_logger.handlers.clear()
    _file_logger.addHandler(h)

@with_retry(max_retries=2, base_delay=1.0, backoff=2.0,
            retryable_exceptions=(URLError, TimeoutError, ConnectionError, OSError))
def _api(action: str, params: dict | None = None) -> dict:
    data = json.dumps(params or {}, ensure_ascii=False).encode("utf-8")
    req = Request(f"{NAP_CAT_HTTP}/{action}", data=data,
                  headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    try:
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        # Phase 1.2: API 调用失败计数 + 分级日志
        from monitor import count_error
        count_error(f"api_{action}")
        return {"status": "error", "message": str(e)}

def _send_text(msg_type: str, target_id: str, text: str, image_urls: list[str] | None = None) -> bool:
    text = text.strip()
    if not text and not image_urls:
        return False
    message = text
    if image_urls:
        for url in image_urls:
            message += f"[CQ:image,file={url}]"
    if msg_type == "group":
        action = "send_group_msg"
        params = {"group_id": int(target_id), "message": message}
    else:
        action = "send_private_msg"
        params = {"user_id": int(target_id), "message": message}
    r = _api(action, params)
    if r.get("status") != "ok":
        msg = r if isinstance(r, dict) else {}
        error_msg = msg.get("message", msg.get("wording", ""))
        # HTTP 502 意味着 NapCat 已发送但网关响应丢失 → 不算失败，避免重复发送
        if "502" in str(error_msg) or "Bad Gateway" in str(error_msg):
            log(f"CATION] 发送可能已成功但响应 502: target={target_id}, {action}")
            return True
        log(f"SEND ERROR {action} target={target_id}: {r}")
        return False
    return True


def _send_image(msg_type: str, target_id: str, image_url: str) -> bool:
    """通过 CQ 码发送图片。image_url 可以是 http/https URL 或 base64://..."""
    cq = f"[CQ:image,file={image_url}]"
    if msg_type == "group":
        action = "send_group_msg"
        params = {"group_id": int(target_id), "message": cq}
    else:
        action = "send_private_msg"
        params = {"user_id": int(target_id), "message": cq}
    r = _api(action, params)
    if r.get("status") != "ok":
        log(f"SEND IMAGE ERROR {action} target={target_id}: {r}")
        return False
    return True


def _send_file(msg_type: str, target_id: str, file_url: str, file_name: str = "") -> bool:
    """通过 CQ 码发送文件。file_url 可以是 http/https URL 或本地文件路径。"""
    name_part = f",name={file_name}" if file_name else ""
    cq = f"[CQ:file,file={file_url}{name_part}]"
    if msg_type == "group":
        action = "send_group_msg"
        params = {"group_id": int(target_id), "message": cq}
    else:
        action = "send_private_msg"
        params = {"user_id": int(target_id), "message": cq}
    r = _api(action, params)
    if r.get("status") != "ok":
        log(f"SEND FILE ERROR {action} target={target_id}: {r}")
        return False
    return True


def _parse_message_segments(data: dict) -> tuple[str, bool, set, list]:
    """将 OneBot v11 WebSocket 消息解析为人类可读文本。

    返回 (text_content, has_media, media_types, image_urls)。
    Phase 4.3: 同时提取 reply 引用信息并缓存到 _last_reply_info。
    """
    segments = data.get("message", [])
    if not isinstance(segments, list) or not segments:
        raw = data.get("raw_message", "") or ""
        _last_reply_info.clear()
        return (raw, False, set(), [])

    parts: list[str] = []
    media_types: set[str] = set()
    image_urls: list[str] = []
    file_urls: list[str] = []
    file_names: list[str] = []
    reply_info: list[dict] = []

    for seg in segments:
        if not isinstance(seg, dict):
            continue
        stype = seg.get("type", "")
        sdata = seg.get("data", {})

        if stype == "text":
            t = sdata.get("text", "")
            if t:
                parts.append(t)
        elif stype == "image":
            media_types.add("image")
            parts.append("[图片]")
            # NapCat 兼容：图片 URL 可能在 url 或 file 字段
            url = sdata.get("url", "") or sdata.get("file", "")
            if url:
                image_urls.append(url)
        elif stype == "face":
            media_types.add("face")
        elif stype == "mface":
            media_types.add("sticker")
            parts.append("[贴纸]")
        elif stype == "at":
            parts.append("@某人")
        elif stype == "reply":
            parts.append("[回复]")
            # Phase 4.3: 提取 reply 引用的原始消息 ID
            reply_id = sdata.get("id") or sdata.get("message_id")
            if reply_id:
                reply_info.append({"message_id": int(reply_id)})
        elif stype == "video":
            media_types.add("video")
            parts.append("[视频]")
        elif stype == "record":
            media_types.add("record")
            parts.append("[语音]")
        elif stype == "file":
            file_url = sdata.get("file", "")
            file_name = sdata.get("name", "")
            media_types.add("file")
            file_urls.append(file_url)
            file_names.append(file_name)
        else:
            parts.append(f"[{stype}]")

    text_content = "".join(parts).strip()
    # 缓存 reply 信息供 bot.py 获取
    _last_reply_info.clear()
    _last_reply_info.extend(reply_info)
    return (text_content, bool(media_types), media_types, image_urls)
