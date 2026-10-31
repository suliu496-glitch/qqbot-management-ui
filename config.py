#!/usr/bin/env python3
"""QQ Bot 配置与数据层 — 环境变量、记忆 I/O、联系人索引、权限、角色、特征学习"""
import json, os, re, shutil, threading, time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path, override=False)

from messaging import log, _api, _memory_locks, _memory_locks_guard, _print_lock, _processed_ids, _MAX_PROCESSED_IDS

# ==================== Phase 6: 简单常量统一从 settings.py 读取 ====================
# 所有现有导出名保持不变，确保 `from config import XXX` 不破坏
from settings import settings as _settings

NAP_CAT_WS   = _settings.napcat_ws
NAP_CAT_HTTP = _settings.napcat_http
MY_QQ        = _settings.my_qq
DATA_DIR     = _settings.data_dir
DATA_DIR.mkdir(parents=True, exist_ok=True)

DEEPSEEK_API_KEY  = _settings.deepseek_key
DEEPSEEK_BASE_URL = _settings.deepseek_base
DEEPSEEK_MODEL    = _settings.deepseek_model

ZHIPU_API_KEY  = _settings.zhipu_key
ZHIPU_BASE_URL = _settings.zhipu_base

_DESKTOP = Path.home() / "Desktop"

PERSONA_FILE = Path(os.environ.get("PERSONA_FILE", Path(__file__).parent / "persona.txt"))
ROLES_FILE   = Path(os.environ.get("ROLES_FILE",   Path(__file__).parent / "roles.json"))

MASTER_MARK = "主人"
MASTER_QQ_EXTRA = [q.strip() for q in _settings.master_qq.split(",") if q.strip()]

SELF_NICK = _settings.self_nick
SELF_QQ   = MY_QQ

# ==================== 联系人缓存 ====================
_friend_cache: dict[str, str] = {}   # QQ号 → 昵称
_group_cache: dict[str, str] = {}    # 群号 → 群名
_cache_loaded = False

# Phase 3.2: 称呼轮换记录 — {qq: (old_name, new_name, timestamp)}
_recent_name_rotations: dict[str, tuple[str, str, float]] = {}


def _refresh_contact_cache():
    """刷新好友和群列表缓存。WS 连接后调用。"""
    global _friend_cache, _group_cache, _cache_loaded
    try:
        friends = _api("get_friend_list").get("data", [])
        _friend_cache.clear()
        for f in friends:
            _friend_cache[str(f["user_id"])] = f.get("nickname", "")
        groups = _api("get_group_list").get("data", [])
        _group_cache.clear()
        for g in groups:
            _group_cache[str(g["group_id"])] = g.get("group_name", "")
        _cache_loaded = True
        log(f"缓存刷新: {len(_friend_cache)} 好友, {len(_group_cache)} 群")
    except Exception as e:
        log(f"缓存刷新失败: {e}")


# ==================== 会话管理 ====================
_chat_mute: dict[str, bool] = {}

# Phase 4.2: _get_person_profile 缓存 — {qq: (timestamp, profile_dict)}
_profile_cache: dict[str, tuple[float, dict]] = {}
_PROFILE_CACHE_TTL = 60  # 秒


def _is_chat_muted(chat_id: str) -> bool:
    return _chat_mute.get(chat_id, False)


def _set_chat_muted(chat_id: str, muted: bool):
    _chat_mute[chat_id] = muted
    log(f"会话 {chat_id} {'静默' if muted else '恢复'}")


# ==================== 记忆 I/O ====================

def _get_memory_lock(key: str) -> threading.Lock:
    with _memory_locks_guard:
        if key not in _memory_locks:
            _memory_locks[key] = threading.Lock()
        return _memory_locks[key]


def _save_memory(key: str, mem: dict):
    """原子写入：先写 .tmp，再备份旧文件为 .bak，最后替换。崩溃后可从 .bak 恢复。"""
    f = DATA_DIR / f"{key}.json"
    tmp = f.with_suffix(".json.tmp")
    bak = f.with_suffix(".json.bak")
    payload = json.dumps(mem, ensure_ascii=False, indent=2)
    # 1. 写入临时文件
    tmp.write_text(payload, "utf-8")
    # 2. 如果存在旧文件 → 备份
    if f.exists():
        try:
            shutil.copy2(str(f), str(bak))
        except Exception:
            pass
    # 3. 原子替换（同分区 rename 是原子的）
    tmp.replace(f)


def _load_memory(key: str) -> dict:
    f = DATA_DIR / f"{key}.json"
    if f.exists():
        try:
            return json.loads(f.read_text("utf-8"))
        except Exception:
            pass
    # 读取失败 → 尝试从备份恢复
    bak = f.with_suffix(".json.bak")
    if bak.exists():
        try:
            data = json.loads(bak.read_text("utf-8"))
            log(f"[RECOVERY] 从备份恢复 {key}.json")
            # 恢复正式文件
            try:
                shutil.copy2(str(bak), str(f))
            except Exception:
                pass
            return data
        except Exception:
            pass
    return {"key": key, "name": "", "notes": [], "conversation": [], "message_ids": {},
            "role": {}}


# ==================== 群聊共享记忆 ====================

def _add_to_group_memory(group_id: str, sender_id: str, sender_name: str,
                          content: str, direction: str) -> dict:
    """群聊共享记忆：写入 group_{GID}.json，存储整个群的消息流。
    用于群聊上下文构建 —— 避免每人只看到自己的私聊文件、无法区分说话人。
    """
    key = f"group_{group_id}"
    lock = _get_memory_lock(key)
    with lock:
        mem = _load_memory(key)
        mem["conversation"].append({
            "time": datetime.now().strftime("%m-%d %H:%M:%S"),
            "direction": direction,
            "content": content,
            "sender_id": sender_id,
            "sender_name": sender_name,
        })
        if len(mem["conversation"]) > 200:
            mem["conversation"] = mem["conversation"][-200:]
        _prune_old_messages(mem, days=3)
        _save_memory(key, mem)
    return mem


def _load_group_context(group_id: str, limit: int = 30) -> tuple[str, dict[str, str]]:
    """从群聊共享记忆加载上下文。返回 (context_str, speakers_seen)。
    用于群聊时替换 previously 从 private 文件加载的单人视角。
    """
    key = f"group_{group_id}"
    try:
        mem = _load_memory(key)
        conv = mem.get("conversation", [])[-limit:]
    except Exception:
        conv = []

    lines = []
    speakers: dict[str, str] = {}
    for m in conv:
        direction = m.get("direction", "in")
        name = m.get("sender_name", "") or m.get("sender_id", "")
        if direction == "out":
            lines.append(f"[机器人]: {m['content'][:250]}")
        else:
            lines.append(f"[{name}]: {m['content'][:250]}")
            sid = m.get("sender_id", "")
            if sid and sid not in speakers:
                speakers[sid] = name

    return "\n".join(lines), speakers


def _add_to_memory(key: str, sender_id: str, sender_nick: str,
                   content: str, direction: str, group_id: str = "") -> dict:
    lock = _get_memory_lock(key)
    with lock:
        mem = _load_memory(key)
        mem["conversation"].append({
            "time": datetime.now().strftime("%m-%d %H:%M:%S"),
            "direction": direction,
            "content": content,
            "sender_id": sender_id,
            "group_id": group_id or _extract_group_from_key(key),
        })
        if len(mem["conversation"]) > 300:
            mem["conversation"] = mem["conversation"][-300:]
        _prune_old_messages(mem, days=3)

        # 全局聊天日志 + 临时名称 + 自动创建档案
        if direction == "in":
            gid = group_id if group_id else _extract_group_from_key(key)
            try:
                from db import insert_chat_log, upsert_temp_name, upsert_profile
                insert_chat_log(sender_id, MY_QQ, gid, content)
                if gid and gid != "0":
                    upsert_temp_name(gid, sender_id, sender_nick)
                    try:
                        from db import get_profile as _db_get_profile
                        if not _db_get_profile(sender_id):
                            upsert_profile(sender_id, is_friend=0, source_group=gid)
                    except Exception:
                        pass
            except Exception:
                pass

        # AI 动态分析计数器：每累积 50 条收消息触发一次后台分析
        if direction == "in" and key.startswith("private_"):
            mem.setdefault("role", {})
            count = mem["role"].get("analysis_pending_count", 0) + 1
            mem["role"]["analysis_pending_count"] = count
            if count >= 50:
                # Phase 2.2: 先调度再归零，调度失败则保留计数器
                from tools import _schedule_person_analysis
                try:
                    _schedule_person_analysis(sender_id)
                    mem["role"]["analysis_pending_count"] = 0
                except Exception:
                    mem["role"]["analysis_pending_count"] = 49  # 下次消息触发重试
                _save_memory(key, mem)
                return mem
        _save_memory(key, mem)
    return mem


def _extract_group_from_key(key: str) -> str:
    """从 memory key 中提取 group_id。private_QQ → '0', group_GID_person_QQ → GID"""
    if key.startswith("group_"):
        parts = key.split("_person_")
        if len(parts) == 2:
            return parts[0].replace("group_", "")
    return "0"


def _get_person_profile(sender_id: str) -> dict:
    """加载一个人的完整画像：从 private 文件读取 JSON 数据，合并 SQLite 档案。
    群聊文件仅做 conversation 补充，不再存 name/notes/role。
    Phase 4.2: 60s TTL 缓存。
    """
    now = time.time()
    if sender_id in _profile_cache:
        ts, cached = _profile_cache[sender_id]
        if now - ts < _PROFILE_CACHE_TTL:
            return cached.copy()

    pk = f"private_{sender_id}"
    base = _load_memory(pk)
    # 合并 SQLite 档案数据
    try:
        from db import get_profile as _db_get_profile
        db_profile = _db_get_profile(sender_id)
        if db_profile:
            base["_db_profile"] = db_profile
            # 名字统一通过 _resolve_speaker_name（nickname_slots > fixed_name > temp > 好友 > QQ末4）
            resolved = _resolve_speaker_name(sender_id, "")
            if resolved and resolved != sender_id[-4:]:
                base["name"] = resolved
    except Exception:
        pass
    # 补充 SQLite person_tags 和 real_name
    try:
        from db import get_person_tags, get_person_real_name
        base["_tags"] = get_person_tags(sender_id)
        base["_real_name"] = get_person_real_name(sender_id)
    except Exception:
        base["_tags"] = []
        base["_real_name"] = ""

    _profile_cache[sender_id] = (now, base)
    return base.copy()


def _sync_person_bidirectional(sender_id: str):
    """【已废弃】名称统一由 SQLite 管理，不再需要 JSON 文件间同步。
    保留空函数避免调用方报错。
    """
    pass


# ==================== 定时清理过期记忆 ====================
def _prune_old_messages(mem: dict, days: int = 3):
    """截断超过 N 天的旧消息，保留最新 300 条。
    Phase 5.1: 修复跨年时间解析 Bug — 1 月 1 日不再把去年 12 月的消息误判为未来。
    Phase 5.2: 被裁剪的消息先做语义压缩摘要再丢弃。
    """
    from datetime import timedelta
    conv = mem.get("conversation", [])
    if len(conv) <= 50:
        return
    now = datetime.now()
    cutoff_date = now.date()
    recent = []
    old_dropped = []  # 被裁剪的消息
    for m in conv:
        t = m.get("time", "")[:5]
        try:
            msg_date = datetime.strptime(
                str(now.year) + "-" + t, "%Y-%m-%d"
            ).date()
            # Phase 5.1: 跨年修正 — 解析结果在未来超过 1 天则年份减 1
            if msg_date > cutoff_date + timedelta(days=1):
                msg_date = datetime.strptime(
                    str(now.year - 1) + "-" + t, "%Y-%m-%d"
                ).date()
            if (cutoff_date - msg_date).days <= days:
                recent.append(m)
            else:
                old_dropped.append(m)
        except Exception:
            recent.append(m)
    if len(recent) < 50 and len(conv) > 50:
        recent = conv[-50:]
    mem["conversation"] = recent[-300:]

    # Phase 5.2: 语义内存压缩 — 被丢弃的消息先摘要再删除
    if len(old_dropped) >= 20:
        _compress_memory(mem, old_dropped)


# ==================== Phase 5.2: 语义记忆压缩 ====================

_compress_cooldown: dict[str, float] = {}  # {mem_key: last_compress_ts}
_COMPRESS_COOLDOWN = 86400  # 每人每 24 小时最多压缩一次

def _compress_memory(mem: dict, dropped_msgs: list[dict]):
    """将丢弃的消息摘要为压缩记忆条目，插入对话头部。
    只压缩入方向消息，需累积 ≥ 200 字符。
    """
    key = mem.get("key", "")
    now_ts = time.time()
    last = _compress_cooldown.get(key, 0)
    if now_ts - last < _COMPRESS_COOLDOWN:
        return  # 冷却中

    incoming = [m.get("content", "") for m in dropped_msgs if m.get("direction") == "in"]
    combined = " ".join(incoming)
    if len(combined) < 200:
        return

    # 调用 DeepSeek 做轻量摘要
    try:
        import httpx
        resp = httpx.post(
            f"{DEEPSEEK_BASE_URL}/v1/chat/completions",
            json={
                "model": DEEPSEEK_MODEL,
                "max_tokens": 150,
                "messages": [
                    {"role": "system", "content": "你是一个对话摘要助手。请用3-5条简短要点总结以下聊天记录中的关键信息：说了什么名字、关系、地点、计划、情绪。不要编造，只提取事实。用中文回复。"},
                    {"role": "user", "content": combined[:2000]},
                ],
            },
            headers={"Content-Type": "application/json; charset=utf-8",
                     "Authorization": f"Bearer {DEEPSEEK_API_KEY}"},
            timeout=20.0,
        )
        resp.raise_for_status()
        body = resp.json()
        summary = body["choices"][0]["message"]["content"].strip()
        if summary:
            conv = mem.get("conversation", [])
            conv.insert(0, {
                "time": datetime.now().strftime("%m-%d %H:%M:%S"),
                "direction": "system",
                "content": f"[记忆压缩] {summary}",
                "sender_id": "0",
            })
            mem["conversation"] = conv[-300:]
            _compress_cooldown[key] = now_ts
            log(f"记忆压缩完成 key={key}: {summary[:60]}...")
    except Exception as e:
        log(f"记忆压缩失败 key={key}: {e}")


# ==================== 主人系统 ====================
_owner_cache: set[str] | None = None
_MASTER_FILE = DATA_DIR / "masters.json"


def _get_master_ids() -> set[str]:
    """仅从 MASTER_QQ 环境变量 + masters.json 获取主人列表。
    不再扫描 data 文件中的「主人」备注——防止恶意备注越权。
    优先读内存缓存，避免写文件后立刻读磁盘拿到旧数据。
    """
    global _owner_cache
    if _owner_cache is not None:
        return _owner_cache
    masters = set(MASTER_QQ_EXTRA)
    if _MASTER_FILE.exists():
        try:
            saved = json.loads(_MASTER_FILE.read_text("utf-8"))
            if isinstance(saved, list):
                masters.update(saved)
        except Exception:
            pass
    if not masters and SELF_QQ and SELF_QQ != "0":
        masters = {SELF_QQ}
    _owner_cache = masters
    return masters


def _save_master_ids(masters: set[str]):
    """持久化主人列表到 masters.json，并更新内存缓存。"""
    _MASTER_FILE.write_text(
        json.dumps(sorted(masters), ensure_ascii=False, indent=2), "utf-8"
    )
    global _owner_cache
    _owner_cache = set(MASTER_QQ_EXTRA) | masters


def _is_master(sender_id: str, mem: dict | None = None) -> bool:
    """仅从中心化主人列表（MASTER_QQ + masters.json）判断。
    不再检查 per-mem notes——防止恶意备注越权。
    """
    return sender_id in _get_master_ids()


def _owner_add(qq: str) -> bool:
    key = f"private_{qq}"
    mem = _load_memory(key)
    for n in mem.get("notes", []):
        if MASTER_MARK in n.get("note", ""):
            return False
    mem["notes"].append({"time": datetime.now().isoformat(), "note": MASTER_MARK})
    _save_memory(key, mem)
    masters = _get_master_ids()
    masters.add(qq)
    _save_master_ids(masters)
    return True


def _owner_remove(qq: str) -> bool:
    key = f"private_{qq}"
    mem = _load_memory(key)
    new_notes = [n for n in mem.get("notes", []) if MASTER_MARK not in n.get("note", "")]
    if len(new_notes) == len(mem.get("notes", [])):
        return False
    mem["notes"] = new_notes
    _save_memory(key, mem)
    masters = _get_master_ids()
    masters.discard(qq)
    _save_master_ids(masters)
    return True


def _migrate_master_list():
    """一次性迁移：扫描现有 data 文件中 notes 标记为「主人」的 QQ 号，
    写入 masters.json。确保不丢失现有管理员。
    迁移后不再扫描 data 文件判断主人身份。
    """
    if _MASTER_FILE.exists():
        return
    masters = set(MASTER_QQ_EXTRA)
    for f in DATA_DIR.glob("private_*.json"):
        try:
            mem = json.loads(f.read_text("utf-8"))
            for n in mem.get("notes", []):
                if MASTER_MARK in n.get("note", ""):
                    qq = f.stem.split("_", 1)[1]
                    masters.add(qq)
                    break
        except Exception:
            pass
    if not masters and SELF_QQ and SELF_QQ != "0":
        masters = {SELF_QQ}
    _save_master_ids(masters)
    log(f"主人列表迁移完成：{len(masters)} 位主人已写入 masters.json")


# ==================== 角色配置 & 人物设定 ====================

# @deprecated: ROLES_FILE 不再使用，角色数据已迁移到 data/private_{QQ}.json 的 role 字段。
# 保留 _load_roles / _save_roles 供向后兼容，但新代码应使用 _get_person_role / _set_person_role。

def _load_roles() -> dict:
    """@deprecated 从 ROLES_FILE 读取（旧路径）。新代码应使用 _get_person_role()。"""
    if ROLES_FILE.exists():
        try:
            return json.loads(ROLES_FILE.read_text("utf-8"))
        except Exception:
            pass
    return {}


def _save_roles(roles: dict):
    """@deprecated 写入 ROLES_FILE（旧路径）。新代码应使用 _set_person_role()。"""
    ROLES_FILE.write_text(json.dumps(roles, ensure_ascii=False, indent=2), "utf-8")


def _get_person_role(qq: str) -> dict | None:
    """从 data/private_{QQ}.json 的 role 字段读取角色配置。"""
    mem_key = f"private_{qq}"
    mem = _load_memory(mem_key)
    role = mem.get("role", {})
    return role if role else None


# ==================== 内容敏感词过滤（效果四）====================

# 敏感词列表（命中替换为 ***）
_SENSITIVE_WORDS = [
    "傻逼", "他妈的", "草泥马", "脑残", "去死", "fuck", "shit",
]

def _filter_output(text: str) -> str:
    """过滤 AI 输出中的敏感词，命中替换为 ***。"""
    if not text:
        return text
    for word in _SENSITIVE_WORDS:
        if word in text:
            text = text.replace(word, "***")
    return text


def _set_person_role(qq: str, role_def: str) -> dict:
    """解析角色定义字符串，写入 data/private_{QQ}.json 的 role 字段。
    支持格式: "性格:X;称呼:Y;规则:Z" 或 "personality:X;name_override:Y;custom_rules:Z"
    旧字段名自动映射为新字段名。"""
    role = {}
    for part in role_def.split(";"):
        if ":" in part:
            k, v = part.split(":", 1)
            role[k.strip()] = v.strip()
    if not role:
        return {}
    # 字段映射：旧字段名 → 新字段名（称呼/name_override → display_name）
    role_map = {"性格": "personality", "称呼": "display_name", "name_override": "display_name", "规则": "custom_rules"}
    new_role = {}
    for old_k, v in role.items():
        new_k = role_map.get(old_k, old_k)
        new_role[new_k] = v
    mem_key = f"private_{qq}"
    lock = _get_memory_lock(mem_key)
    with lock:
        mem = _load_memory(mem_key)
        mem.setdefault("role", {})
        mem["role"].update(new_role)
        _save_memory(mem_key, mem)
        # 同步 display_name 到 SQLite 全局固定备注
        if "display_name" in new_role:
            try:
                from db import set_fixed_name as _db_set_fixed_name
                _db_set_fixed_name(qq, new_role["display_name"])
            except Exception:
                pass
        if "personality" in new_role:
            try:
                from db import upsert_profile as _db_upsert_profile
                _db_upsert_profile(qq, feature_summary=new_role["personality"])
            except Exception:
                pass
        _invalidate_person_index()
    return new_role


def _apply_learned_traits(mem: dict, role: dict):
    """从 SQLite 已结构化数据中提取信息，供 prompt 使用。"""
    conv = mem.get("conversation", [])
    if len(conv) < 2:
        return
    # 关系信息已由 _extract_relation 处理（合并 SQLite + JSON）


_NAME_BLACKLIST = {
    "什么", "为什么", "谁", "啥", "不知道", "你爹", "爸爸", "妈妈",
    "叫", "了", "呢", "吗", "吧", "啊", "哦", "嗯", "怎么", "哪个",
    "哪儿", "哪里", "啥子", "主人的", "名字", "备注",
    "为爹", "你主人", "你主人吗", "主人吗", "你爹啊",
}
_NAME_BAD_SUBSTR = ["控制器", "遥控", "记不住", "忘了", "忘了你", "别忘了",
                     "能不能", "可以吗", "行不行", "好不好", "知不知道",
                     "怎么叫", "叫什么", "你咋", "咋记"]
_NAME_BAD_PATTERNS = [
    re.compile(r"^[你他我].{1,3}[啊呀哦呢]$"),      # "你爹啊"、"他傻啊"
    re.compile(r"^[你我他它她].{4,}$"),                # "他名字他才回答你"
    re.compile(r"[一-鿿]{10,}"),               # 超过10个汉字的不是名字
]


def _is_plausible_name(text: str) -> bool:
    """判断一个字符串是否像一个真实的人名/称呼，过滤自动检测的垃圾。"""
    t = text.strip()
    if not t:
        return False
    if t.endswith("?") or t.endswith("？"):
        return False
    if len(t) > 8:
        return False
    if len(t) == 1 and not re.search(r'[一-鿿]', t):
        return False
    low = t.lower()
    if low in _NAME_BLACKLIST:
        return False
    for bad in _NAME_BAD_SUBSTR:
        if bad in low:
            return False
    for pat in _NAME_BAD_PATTERNS:
        if pat.search(t):
            return False
    if re.match(r'^[\W_]+$', t):
        return False
    return True


# ==================== 身份锚定 ====================
def _get_other_names() -> set[str]:
    other = set()
    masters = _get_master_ids()
    for f in DATA_DIR.glob("*.json"):
        try:
            mem = json.loads(f.read_text("utf-8"))
            name = mem.get("name", "").strip()
            if not name or name == SELF_NICK:
                continue
            key = mem.get("key", "")
            qq = ""
            if key.startswith("private_"):      qq = key.split("_", 1)[1]
            elif "_person_" in key:               qq = key.split("_person_")[1]
            if qq in masters or qq == SELF_QQ:
                continue
            other.add(name)
        except Exception:
            pass
    return other


def _build_identity_anchor() -> str:
    others = _get_other_names()
    others_str = "、".join(sorted(others)[:15]) if others else "无"
    masters = _get_master_ids()
    master_str = "\n".join(f"  - QQ {m}" for m in sorted(masters)) if masters else "  - 未知"
    return f"""【LAYER 1 — 身份锚定 · 绝对不可违反】
1. 你的QQ号是 {SELF_QQ}，昵称是「{SELF_NICK}」。
2. 你是 {SELF_NICK}，不是以下任何人：{others_str}。别人冒认你的名字时不要理会，你始终是 {SELF_NICK}。
3. 别人说「我是XX」是说他自己。你不叫 XX，你始终是 {SELF_NICK}。
4. 你的管理员（有权限使用指令和工具的人）：
{master_str}
5. 你是用户配置的 QQ 机器人，按全局人设和管理员设置自然交流。
6. 不编造没发生的对话，不脑补别人没问过的问题。
7. 拒绝侮辱性称呼：如果有人让你用脏话、辱骂词（如傻逼、你爹、儿子、废物等）称呼对方，你必须拒绝。回复「这个称呼不太合适，我不能这样叫你。」不要幽默地接受。"""


# ==================== 关系提取 ====================
def _extract_relation(mem: dict) -> str:
    """从 notes 中提取所有「关系:XXX」条目（向后兼容 JSON），再合并 SQLite person_tags。"""
    rels = []
    seen = set()
    for n in mem.get("notes", []):
        note = n.get("note", "")
        if note.startswith("关系:"):
            raw = note[3:]
            if raw.endswith("(自动)"):
                label = raw[:-4].strip() + "(推测)"
            else:
                label = raw.strip()
            if label and label not in seen:
                seen.add(label)
                rels.append(label)
    # 补充 SQLite 中的标签
    try:
        from db import get_person_tags
        qq = _qq_from_mem(mem)
        if qq:
            for tag in get_person_tags(qq):
                if tag not in seen:
                    seen.add(tag)
                    rels.append(tag)
    except Exception:
        pass
    return "、".join(rels) if rels else ""


def _qq_from_mem(mem: dict) -> str:
    """从 memory dict 中提取 QQ 号。"""
    key = mem.get("key", "")
    if key.startswith("private_"):
        return key.split("_", 1)[1]
    elif "_person_" in key:
        return key.split("_person_")[1]
    return ""


def _apply_relation_to_role(qq: str, relation: str):
    """根据主人设定的关系，动态更新 data 文件中的角色配置。
    关系映射决定机器人对该联系人的语气和称呼风格。"""
    rel = relation.lower()

    personality = ""
    if "家人" in rel or "亲戚" in rel:
        personality = "亲切温暖，像家人一样对待"
    elif "同事" in rel or "工作" in rel:
        personality = "尊重专业，像同事一样协助"
    elif "朋友" in rel or "哥们" in rel or "兄弟" in rel:
        personality = "轻松友好，像朋友一样聊天"
    elif "同学" in rel:
        personality = "友好伴读，互相帮助"
    elif "老师" in rel or "教授" in rel or "导师" in rel:
        personality = "非常尊重，虚心请教"
    elif "长辈" in rel:
        personality = "尊重敬爱，懂礼节"
    elif "客户" in rel or "老板" in rel:
        personality = "专业尊重，维护好关系"
    else:
        personality = f"主人说这是{relation}关系，用合适的礼貌态度对待"

    mem_key = f"private_{qq}"
    lock = _get_memory_lock(mem_key)
    with lock:
        mem = _load_memory(mem_key)
        mem.setdefault("role", {})
        mem["role"]["relation_override"] = relation
        if not mem["role"].get("personality"):
            mem["role"]["personality"] = personality
        _save_memory(mem_key, mem)
        _invalidate_person_index()


# ==================== 联系人概要 ====================
def _get_contact_summary(detail: bool = False) -> str:
    """返回 bot 账号的联系人概要。detail=False 只返回数量和抽样，detail=True 返回完整列表。"""
    if not _cache_loaded:
        return ""
    friends = dict(sorted(_friend_cache.items(), key=lambda x: x[1]))
    groups = dict(sorted(_group_cache.items(), key=lambda x: x[1]))

    known = []
    unknown = []
    for qq, nick in friends.items():
        try:
            mem = _load_memory(f"private_{qq}")
            name = mem.get("name", "")
            notes_count = len(mem.get("notes", []))
            conv_count = len(mem.get("conversation", []))
            # 也从 SQLite person_tags 读取关系标签
            rel = _extract_relation(mem)
            if not rel:
                try:
                    from db import get_person_tags
                    tag_list = get_person_tags(qq)
                    if tag_list:
                        rel = "、".join(tag_list)
                except Exception:
                    pass
            if name or notes_count or conv_count > 2:
                tag = f"[{rel}] " if rel else ""
                known.append((qq, name or nick, tag, notes_count, conv_count))
            else:
                unknown.append((qq, nick))
        except Exception:
            unknown.append((qq, nick))

    if detail:
        lines = [f"好友列表（共 {len(friends)} 人）："]
        for qq, name, tag, nc, cc in known:
            lines.append(f"  - {tag}{name}({qq}) · {cc}条对话")
        for qq, nick in unknown:
            lines.append(f"  - {nick}({qq}) · 无记录")
        if groups:
            lines.append(f"\n群列表（共 {len(groups)} 个）：")
            for gid, gname in groups.items():
                lines.append(f"  - {gname}({gid})")
        return "\n".join(lines)
    else:
        known_count = len(known)
        unknown_count = len(unknown)
        sample = []
        if known:
            sample_known = [f"{tag}{name}" for _, name, tag, _, _ in known[:5]]
            sample.append("认识：" + "、".join(sample_known))
            if known_count > 5:
                sample[-1] += f"等{known_count}人"
        if unknown:
            sample_unknown = [nick for _, nick in unknown[:3]]
            sample.append("未接触：" + "、".join(sample_unknown))
            if unknown_count > 3:
                sample[-1] += f"等{unknown_count}人"
        parts = [f"共 {len(friends)} 位好友"]
        if sample:
            parts.append("；".join(sample))
        if groups:
            parts.append(f"；{len(groups)} 个群")
        return "。".join(parts)


# ==================== 联系人类别索引 ====================
def _build_relation_index() -> dict[str, list[tuple[str, str]]]:
    """构建「关系→联系人」反向索引。优先 SQLite，回退 JSON。"""
    try:
        from db import build_relation_index as _db_build_relation_index
        idx = _db_build_relation_index()
        if idx:
            return idx
    except Exception:
        pass
    # 回退：扫描 JSON notes
    index: dict[str, list[tuple[str, str]]] = {}
    for f in DATA_DIR.glob("private_*.json"):
        try:
            mem = json.loads(f.read_text("utf-8"))
            qq = f.stem.split("_", 1)[1]
            name = mem.get("name", "") or qq
            relation = _extract_relation(mem)
            if relation:
                for r in relation.split("、"):
                    r_clean = r.strip()
                    if r_clean:
                        index.setdefault(r_clean, []).append((name, qq))
        except Exception:
            pass
    return index


# ==================== 联系人统一索引 ====================
_person_index_cache: dict | None = None
_person_index_cache_time: float = 0
_PERSON_INDEX_CACHE_TTL = 5

_CHAR_SIMILAR: dict[str, set[str]] = {
    "晨": {"辰", "尘", "陈", "忱", "宸"},
    "清": {"青", "晴", "轻", "倾", "请", "倩"},
    "文": {"闻", "纹", "紊"},
    "明": {"名", "鸣", "铭", "茗", "冥"},
    "杰": {"捷", "洁", "姐", "婕", "桀"},
    "强": {"墙", "蔷", "襁"},
    "伟": {"玮", "炜", "唯", "维", "纬", "苇"},
    "斌": {"彬", "宾", "滨", "缤", "殡"},
    "峰": {"锋", "风", "丰", "枫", "烽", "蜂"},
    "华": {"花", "化", "划", "骅", "桦"},
    "雨": {"羽", "宇", "语", "禹", "予"},
    "雪": {"血", "薛", "学"},
    "梅": {"枚", "玫", "媒", "煤"},
    "芳": {"方", "房", "仿", "坊"},
    "玲": {"铃", "灵", "龄", "岭", "伶"},
    "军": {"君", "俊", "峻", "骏"},
    "勇": {"永", "泳", "涌", "咏"},
    "建": {"剑", "健", "键", "见", "坚"},
    "志": {"智", "知", "治", "致", "至"},
    "超": {"潮", "朝", "抄", "晁"},
    "鹏": {"朋", "蓬", "彭", "澎"},
    "龙": {"隆", "笼", "珑", "聋"},
    "飞": {"非", "菲", "绯", "斐"},
    "凤": {"风", "峰", "锋", "枫", "丰"},
    "静": {"净", "靖", "敬", "镜", "精"},
    "宁": {"凝", "柠", "咛", "狞"},
    "波": {"博", "播", "拨", "剥"},
    "涛": {"滔", "陶", "掏", "桃"},
    "海": {"亥", "孩", "氦"},
    "霞": {"夏", "侠", "瑕", "遐"},
    "燕": {"雁", "艳", "焰", "宴"},
    "晓": {"小", "肖", "萧", "潇", "笑"},
    "宏": {"红", "弘", "洪", "鸿", "虹"},
    "国": {"郭", "过", "果", "锅"},
    "平": {"萍", "评", "苹", "凭"},
    "安": {"按", "暗", "岸", "鞍"},
    "林": {"琳", "淋", "霖", "临"},
    "星": {"兴", "行", "醒", "杏"},
    "月": {"越", "悦", "阅", "岳", "粤"},
    "水": {"睡", "税", "谁"},
    "金": {"今", "近", "进", "锦", "津"},
    "小": {"晓", "肖", "萧", "潇", "笑"},
    "大": {"达", "答", "打"},
    "子": {"紫", "字", "自", "梓"},
    "一": {"依", "衣", "伊", "医"},
    "然": {"燃", "染"},
    "天": {"添", "填", "田"},
    "生": {"声", "升", "胜", "圣"},
    "云": {"运", "韵", "蕴", "芸"},
    "阳": {"洋", "扬", "杨", "羊", "扬"},
    "尘": {"辰", "晨", "陈", "忱"},
    "辰": {"晨", "尘", "陈", "忱"},
    "州": {"洲", "舟", "周"},
}


def _invalidate_profile_cache(qq: str = None) -> None:
    """失效 profile 缓存（qq=None 清除全部，否则清除指定qq）。"""
    global _profile_cache
    if qq is None:
        _profile_cache.clear()
    else:
        _profile_cache.pop(qq, None)


def _invalidate_person_index():
    global _person_index_cache, _person_index_cache_time
    _person_index_cache = None
    _person_index_cache_time = 0


def _build_person_index() -> dict:
    """扫描所有 memory 文件，构建统一联系人索引。
    返回:
      {"by_qq": {qq: {"names": set, "relations": set, "notes_names": set}},
       "all_names": [(name, qq, kind), ...]}
    kind: "notes_name" (from nickname_slots) | "friend_cache" | "relation"

    名称唯一权威来源: nickname_slots (slot_a_value, slot_b_value)
    关系唯一权威来源: person_tags
    friend_cache 仅作为发现机制（QQ 不在 nickname_slots 中时回退）
    """
    by_qq: dict[str, dict] = {}
    all_names: list[tuple[str, str, str]] = []

    def _add_name(qq: str, name: str, kind: str):
        if not name or not name.strip():
            return
        name = name.strip()
        if qq not in by_qq:
            by_qq[qq] = {"names": set(), "relations": set(), "notes_names": set()}
        # 只添加不重复的名称
        if name in by_qq[qq]["names"]:
            return
        by_qq[qq]["names"].add(name)
        if kind == "notes_name":
            by_qq[qq]["notes_names"].add(name)
        elif kind == "relation":
            by_qq[qq]["relations"].add(name)
        all_names.append((name, qq, kind))

    # === 权威来源 1: nickname_slots — 唯一名称来源 ===
    try:
        from db import get_all_nickname_rows as _db_get_all_nickname_rows
        for row in _db_get_all_nickname_rows():
            qq = row["qq"]
            if qq == SELF_QQ:
                continue
            if qq not in by_qq:
                by_qq[qq] = {"names": set(), "relations": set(), "notes_names": set()}
            for slot_key in ("slot_a_value", "slot_b_value"):
                val = (row.get(slot_key) or "").strip()
                if val:
                    _add_name(qq, val, "notes_name")
    except Exception:
        pass

    # === 权威来源 2: person_tags — 唯一关系来源 ===
    try:
        from db import get_person_tags as _db_get_person_tags
        # 收集所有已知 QQ（包括 nickname_slots 和 friend_cache）
        all_known_qqs = set(by_qq.keys())
        all_known_qqs.update(_friend_cache.keys())
        all_known_qqs.discard(SELF_QQ)
        for qq in all_known_qqs:
            try:
                tags = _db_get_person_tags(qq)
                if tags:
                    if qq not in by_qq:
                        by_qq[qq] = {"names": set(), "relations": set(), "notes_names": set()}
                    for tag in tags:
                        tag_stripped = tag.strip()
                        if tag_stripped and tag_stripped not in by_qq[qq].get("relations", set()):
                            by_qq[qq].setdefault("relations", set()).add(tag_stripped)
            except Exception:
                pass
    except Exception:
        pass

    # === 权威来源 3: fixed_name from user_profile ===
    try:
        from db import _get_conn as _db_get_conn
        for row in _db_get_conn().execute("SELECT qq, fixed_name FROM user_profile WHERE fixed_name IS NOT NULL AND fixed_name != ''"):
            qq = row[0]
            name = (row[1] or "").strip()
            if name and qq != SELF_QQ and (qq not in by_qq or not by_qq[qq]['names']):
                _add_name(qq, name, "notes_name")
    except Exception:
        pass

    # === 发现机制: friend_cache — 仅在 QQ 不在 nickname_slots 中时回退 ===
    for qq, nick in _friend_cache.items():
        if qq == SELF_QQ:
            continue
        if nick and nick.strip():
            nick = nick.strip()
            if qq not in by_qq:
                by_qq[qq] = {"names": set(), "relations": set(), "notes_names": set()}
            # 只有当该 QQ 还没有任何名称来源时才使用 friend_cache
            if not by_qq[qq]["names"]:
                _add_name(qq, nick, "friend_cache")

    return {"by_qq": by_qq, "all_names": all_names}


def _get_person_index() -> dict:
    """获取缓存的联系人索引，过期自动重建。"""
    global _person_index_cache, _person_index_cache_time
    now = time.time()
    if _person_index_cache is not None and (now - _person_index_cache_time) < _PERSON_INDEX_CACHE_TTL:
        return _person_index_cache
    _person_index_cache = _build_person_index()
    _person_index_cache_time = now
    return _person_index_cache


# ==================== 模糊名字匹配 ====================

def _levenshtein_ratio(s1: str, s2: str) -> float:
    """计算两个字符串的编辑距离相似度 (0.0~1.0)。零依赖。"""
    if not s1 or not s2:
        return 0.0
    m, n = len(s1), len(s2)
    prev = list(range(n + 1))
    for i in range(1, m + 1):
        curr = [i] + [0] * n
        for j in range(1, n + 1):
            cost = 0 if s1[i - 1] == s2[j - 1] else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr
    return 1.0 - (prev[n] / max(m, n))


def _expand_chars(chars: str) -> list[str]:
    """用字形相似表展开一个字符串的所有候选变体（笛卡尔积）。最多16个。"""
    options: list[list[str]] = []
    for ch in chars:
        opts = [ch]
        if ch in _CHAR_SIMILAR:
            opts.extend(list(_CHAR_SIMILAR[ch]))
        options.append(opts)

    results: list[str] = []
    def _build(idx: int, cur: str):
        if len(results) >= 16:
            return
        if idx == len(options):
            if cur != chars:
                results.append(cur)
            return
        for o in options[idx]:
            _build(idx + 1, cur + o)
    _build(0, "")
    return results


def _fuzzy_match_person(query: str) -> list[tuple[str, str, str, float]]:
    """模糊匹配人名。返回 [(msg_type, target_id, display_name, confidence), ...] 按置信度降序。"""
    if not query or len(query.strip()) < 1:
        return []
    query = query.strip()
    # 拒绝查询机器人自己的名字
    if query == SELF_NICK:
        return []
    index = _get_person_index()
    all_names = index.get("all_names", [])
    by_qq = index.get("by_qq", {})

    matches: dict[str, tuple[str, str, str, float]] = {}

    for name, qq, _kind in all_names:
        if name == query:
            matches[qq] = ("private", qq, name, 1.0)
    if matches:
        return sorted(matches.values(), key=lambda x: x[3], reverse=True)

    threshold = 0.6 if len(query) <= 2 else 0.7
    for name, qq, _kind in all_names:
        ratio = _levenshtein_ratio(query, name)
        if ratio >= threshold:
            display = name
            info = by_qq.get(qq, {})
            main_name = ""
            for n, _q, k in all_names:
                if _q == qq and k == "name":
                    main_name = n
                    break
            if main_name and main_name != name:
                display = f"{main_name}({name})"
            if qq not in matches or ratio > matches[qq][3]:
                matches[qq] = ("private", qq, display, ratio)

    if not matches:
        candidates = _expand_chars(query)
        for cand in candidates:
            # Phase 3.1: 过滤机器人自身 — 字形展开后拒绝匹配 SELF_NICK
            if cand == SELF_NICK:
                continue
            for name, qq, _kind in all_names:
                # Phase 3.1: 拒绝匹配机器人自己的 QQ
                if qq == SELF_QQ:
                    continue
                if name == cand:
                    if qq not in matches or 0.75 > matches[qq][3]:
                        matches[qq] = ("private", qq, name, 0.75)
                else:
                    ratio = _levenshtein_ratio(cand, name)
                    if ratio >= threshold:
                        if qq not in matches or ratio > matches[qq][3]:
                            matches[qq] = ("private", qq, name, max(ratio, 0.7))

    if not matches:
        for name, qq, _kind in all_names:
            if qq == SELF_QQ:
                continue
            if len(name) < 2:
                continue
            if (len(query) >= 3 and query in name) or (len(name) >= 3 and name in query):
                matches[qq] = ("private", qq, name, 0.65)

    result = [(t, i, n, c) for i, (t, i, n, c) in matches.items() if c >= 0.6]
    result.sort(key=lambda x: x[3], reverse=True)

    # 跨QQ名称去重：同一名称出现在多个QQ下时，按来源优先级保留
    # 优先级: notes_name (nickname_slots) > friend_cache
    _name_qq_map: dict[str, list[tuple[str, float]]] = {}
    for name, qq, kind in all_names:
        if kind not in ("notes_name", "friend_cache"):
            continue
        _name_qq_map.setdefault(name, []).append((qq, 0 if kind == "notes_name" else 1))
    # 清理冲突：对于模糊匹配结果，如果匹配到的名称同时属于多个QQ，
    # 且当前匹配到的QQ不是最高优先级的，降低置信度
    _best_qq_for_name: dict[str, str] = {}
    for name, qq_list in _name_qq_map.items():
        if len(set(q for q, _ in qq_list)) > 1:
            qq_list.sort(key=lambda x: x[1])
            _best_qq_for_name[name] = qq_list[0][0]
    filtered = []
    for t, i, n, c in result:
        if n in _best_qq_for_name and i != _best_qq_for_name[n]:
            # 不是该名称的最高优先级归属QQ，降权但仍保留
            filtered.append((t, i, n, c * 0.85))
        else:
            filtered.append((t, i, n, c))
    result = sorted(filtered, key=lambda x: x[3], reverse=True)

    return result[:5]


def _resolve_speaker_name(sender_id: str, group_id: str = "") -> str:
    """快速解析一个 QQ 号的显示名。唯一入口：db.get_resolved_name() → 好友缓存 → QQ末4位。
    单一回退链：nickname_slots > fixed_name > temp_name_cache > 好友昵称 > QQ末4位
    """
    try:
        from db import get_resolved_name
        name = get_resolved_name(sender_id, group_id)
        if name:
            return name
    except Exception:
        pass

    # Level 4-5: NapCat 好友缓存 → QQ 末 4 位
    return _friend_cache.get(sender_id, sender_id[-4:])


def _peek_match_confidence(query: str) -> tuple[str, str, float]:
    """探测名字查询的匹配置信度。返回 (qq, display_name, confidence)。
    confidence=1.0 精确匹配，<0.95 模糊匹配（需用户确认）。
    """
    if not query or not query.strip():
        return ("", "", 0.0)
    query = query.strip()
    if re.match(r'^\d{5,20}$', query):
        return (query, _friend_cache.get(query, query[-4:]), 1.0)
    for qq, nick in _friend_cache.items():
        if nick == query:
            return (qq, nick, 1.0)
    idx = _get_person_index()
    by_qq = idx.get("by_qq", {})
    for qq, data in by_qq.items():
        names = data.get("names", set())
        if query in names:
            return (qq, query, 1.0)
    r_idx = _build_relation_index()
    if query in r_idx:
        return (r_idx[query][0][1], query, 0.95)
    fuzzy = _fuzzy_match_person(query)
    if fuzzy and fuzzy[0][3] >= 0.5:
        return (fuzzy[0][1], fuzzy[0][2], fuzzy[0][3])
    return ("", "", 0.0)


def _resolve_targets(text: str) -> tuple[list[tuple[str, str, str]], list[str]]:
    """解析目标字符串，返回 (已解析列表, 未解析列表)。
    已解析: [(msg_type, target_id, display_name), ...]
    支持格式：
    - 纯数字 QQ 号 → private
    - "group:群号" 或 "群:群号" → group
    - 关系标签（如 "同事"）→ 展开为该关系下所有人
    - 名字 → 扫描 memory 匹配
    - 逗号/顿号/空格分隔的混合列表
    - "所有人" / "全部好友" / "全体" → 展开为所有好友
    """
    tokens = re.split(r'[,，、\s]+', text.strip())
    tokens = [t.strip() for t in tokens if t.strip()]
    if not tokens:
        return [], []

    resolved: list[tuple[str, str, str]] = []
    unresolved: list[str] = []

    for token in tokens:
        # 自指保护：bot 自己的名字不能作为联系人解析
        if token == SELF_NICK:
            unresolved.append(f"「{token}」是机器人昵称，不是联系人")
            continue
        if token in ("所有人", "全部好友", "全体", "全员", "全部"):
            if _cache_loaded:
                for qq, nick in _friend_cache.items():
                    if not any(r[1] == qq for r in resolved):
                        try:
                            from db import get_resolved_name as _db_get_resolved_name
                            name = _db_get_resolved_name(qq) or nick
                        except Exception:
                            name = nick
                        resolved.append(("private", qq, name))
            else:
                unresolved.append(token)
            continue

        if token.startswith(("group:", "群:")):
            gid = token.split(":", 1)[1].strip()
            gname = _group_cache.get(gid, f"群{gid}")
            resolved.append(("group", gid, gname))
            continue

        if re.match(r'^\d{5,20}$', token):
            name = _friend_cache.get(token, token)
            try:
                from db import get_resolved_name as _db_get_resolved_name
                db_name = _db_get_resolved_name(token)
                if db_name:
                    name = db_name
            except Exception:
                pass
            resolved.append(("private", token, name))
            continue

        idx = _build_relation_index()
        if token in idx:
            for name, qq in idx[token]:
                if not any(r[1] == qq for r in resolved):
                    resolved.append(("private", qq, name))
            continue

        found = False
        # 优先用 person_index 精确匹配，而非扫描 JSON 文件
        person_idx = _get_person_index()
        for f in DATA_DIR.glob("private_*.json"):
            try:
                mem = json.loads(f.read_text("utf-8"))
                if mem.get("name", "").strip() == token:
                    qq = f.stem.split("_", 1)[1]
                    if not any(r[1] == qq for r in resolved):
                        resolved.append(("private", qq, token))
                    found = True
            except Exception:
                pass
        if found:
            continue

        fuzzy = _fuzzy_match_person(token)
        if fuzzy and fuzzy[0][3] >= 0.5:
            best = fuzzy[0]
            if not any(r[1] == best[1] for r in resolved):
                resolved.append(("private", best[1], best[2]))
            found = True
            if len(fuzzy) > 1 and fuzzy[1][3] >= 0.5:
                candidates = [f"{n}({i})" for _, i, n, _ in fuzzy[:3]]
                unresolved.append(f"模糊匹配「{token}」→「{best[2]}」（另有可能：{'、'.join(candidates[1:])}）")
            else:
                unresolved.append(f"模糊匹配「{token}」→「{best[2]}」")
        if found:
            continue

        for gid, gname in _group_cache.items():
            if token in gname:
                resolved.append(("group", gid, gname))
                found = True
        if found:
            continue

        unresolved.append(token)

    return resolved, unresolved


# ==================== 工具函数 ====================

def _person_key(msg_type: str, group_id: str, sender_id: str) -> str:
    if msg_type == "group" and group_id:
        return f"group_{group_id}_person_{sender_id}"
    return f"private_{sender_id}"


def _chat_id(msg_type: str, target_id: str) -> str:
    """会话 ID：群聊用 group_群号，私聊用 private_QQ号"""
    return f"{'group' if msg_type == 'group' else 'private'}_{target_id}"


_BLOCKED_NOTE_WORDS = [
    "主人", "管理员", "admin", "master", "root", "超级管理员", "owner",
    # 侮辱性称呼
    "傻逼", "傻b", "傻狗", "废物", "垃圾", "白痴", "弱智",
    "你爹", "你爸爸", "你妈", "你爷爷", "你祖宗",
]


def _validate_note(note_text: str) -> tuple[bool, str]:
    """审核备注内容。阻止越权标记和恶意备注。"""
    note = note_text.strip()
    if note.startswith("名字:") or note.startswith("称呼:"):
        prefix, val = note.split(":", 1)
        name = val.strip().lower()
        for blocked in _BLOCKED_NOTE_WORDS:
            if blocked in name:
                return False, f"不允许将「{blocked}」作为称呼。权限标记由系统管理。"
        return True, ""
    for blocked in _BLOCKED_NOTE_WORDS:
        if blocked in note.lower():
            return False, f"备注中不允许包含「{blocked}」。权限相关的标记由系统管理。"
    return True, ""


def _upsert_person_note(qq: str, note_text: str, source: str = "manual") -> bool:
    """添加备注，自动去重同类备注。
    source="manual"：管理员手动设定，最高优先级，直接覆盖旧条目。
    source="ai_analysis"：AI 分析提取，不覆盖已有的手动条目。
    称呼: / 名字: 各最多 1 条；关系: / 关键信息: 同名去重无上限。
    JSON notes[] 作为审计日志；结构化数据由调用方（工具处理函数/AI提取）
    负责写入 SQLite，此处不再重复写入。返回 True 表示已添加/更新。线程安全。"""
    mem_key = f"private_{qq}"
    lock = _get_memory_lock(mem_key)
    with lock:
        mem = _load_memory(mem_key)
        notes = mem.get("notes", [])
        note_type = None
        SINGLETON_PREFIXES = {"称呼:", "名字:"}
        for prefix in SINGLETON_PREFIXES:
            if note_text.startswith(prefix):
                note_type = prefix
                break

        # 构建同类条目的"规范化key"仅用于去重（非单例类型）
        note_key = _note_dedup_key(note_text)

        new_notes = []
        for n in notes:
            nt = n.get("note", "").strip()
            if note_type and nt.startswith(note_type):
                # 单例类型：手动覆盖一切，AI 不覆盖手动
                if source == "manual":
                    continue  # 删除所有旧的同类条目
                if source == "ai_analysis":
                    if not nt.endswith("(AI分析)") and not nt.endswith("(自动)"):
                        new_notes.append(n)  # 保留手动条目
                    continue
            # 非单例类型：按 key 去重
            if note_key and _note_dedup_key(nt) == note_key:
                if source == "ai_analysis" and not nt.endswith("(AI分析)") and not nt.endswith("(自动)"):
                    new_notes.append(n)  # 保留手动条目
                continue
            new_notes.append(n)

        new_notes.append({"time": datetime.now().isoformat(), "note": note_text})
        mem["notes"] = new_notes
        # 称呼: 前缀自动同步到 nickname_slots（单槽位写入）
        if note_type == "称呼:" and source == "manual":
            new_name = note_text[3:].strip()
            if new_name:
                try:
                    _set_nickname(qq, new_name, "system")
                except Exception:
                    pass

        _save_memory(mem_key, mem)
        _invalidate_person_index()
    return True


def _note_dedup_key(note: str) -> str:
    """提取备注的去重 key：忽略 (AI分析)(自动) 后缀，只比较内容。"""
    import re as _re
    return _re.sub(r'\((AI分析|自动)\)$', '', note.strip())


def _remove_person_note(qq: str, note_text: str) -> bool:
    """按匹配文本移除某人的备注。对 称呼:/名字:/关系: 做前缀匹配，
    对其他备注做精确匹配。同时清除对应的 SQLite 数据。返回 True 表示已移除。线程安全。"""
    mem_key = f"private_{qq}"
    lock = _get_memory_lock(mem_key)
    with lock:
        mem = _load_memory(mem_key)
        notes = mem.get("notes", [])
        new_notes = []
        removed = False
        for n in notes:
            nt = n.get("note", "").strip()
            if not nt:
                new_notes.append(n)
                continue
            if nt.startswith(("名字:", "称呼:", "关系:")):
                parts = nt.split(":", 1)
                if len(parts) == 2 and parts[1].strip() == note_text.strip():
                    # ── 同步删除 SQLite 对应列 ──
                    prefix = parts[0] + ":"
                    try:
                        if prefix == "称呼:" or prefix == "名字:":
                            from db import upsert_person_real_name
                            upsert_person_real_name(qq, "")
                        elif prefix == "关系:":
                            from db import remove_person_tag
                            remove_person_tag(qq, note_text.strip())
                    except Exception:
                        pass
                    removed = True
                    continue
            if nt == note_text.strip():
                removed = True
                continue
            new_notes.append(n)
        if removed:
            mem["notes"] = new_notes
            _save_memory(mem_key, mem)
            _invalidate_person_index()
    return removed


def _cleanup_person_notes(qq: str) -> int:
    """清理某人备注：同类去重（保留最新）、删除明显垃圾。
    同时将 JSON 中的关系标签同步到 SQLite person_tags。返回移除的总条数。"""
    total_removed = 0
    key = f"private_{qq}"
    lock = _get_memory_lock(key)
    with lock:
        mem = _load_memory(key)
        notes = mem.get("notes", [])
        cleaned = []
        removed = 0
        seen_cc = False
        seen_name = False
        for n in reversed(notes):
            nt = n.get("note", "").strip()
            if not nt:
                removed += 1
                continue
            # 同类去重：保留最晚的
            if nt.startswith("称呼:"):
                if seen_cc:
                    removed += 1
                    continue
                seen_cc = True
                cc_val = nt[3:].strip()
                if not cc_val or not _is_plausible_name(cc_val):
                    removed += 1
                    continue
            elif nt.startswith("名字:"):
                if seen_name:
                    removed += 1
                    continue
                seen_name = True
                nm_val = nt[3:].strip()
                if not nm_val or not _is_plausible_name(nm_val):
                    removed += 1
                    continue
            # 纯垃圾备注
            elif nt in ("备注已清除", "清理"):
                removed += 1
                continue
            cleaned.append(n)
        cleaned.reverse()
        # 同步 mem["name"]
        if seen_cc:
            for n in cleaned:
                if n["note"].startswith("称呼:"):
                    mem["name"] = n["note"][3:].strip()
                    break
        if removed > 0:
            mem["notes"] = cleaned
            _save_memory(key, mem)
            _invalidate_person_index()
        total_removed += removed

        # ── 同步关系标签到 SQLite person_tags ──
        try:
            from db import add_person_tag, get_person_tags
            existing_tags = set(get_person_tags(qq))
            for n in cleaned:
                nt = n.get("note", "").strip()
                if nt.startswith("关系:"):
                    tag_val = nt[3:].strip()
                    for suffix in ("(自动)", "(AI分析)", "(推测)"):
                        tag_val = tag_val.replace(suffix, "").strip()
                    if tag_val and tag_val not in existing_tags:
                        add_person_tag(qq, tag_val)
                        existing_tags.add(tag_val)
        except Exception:
            pass

    return total_removed


# ==================== 分层人设 Prompt 架构 ====================

def _build_relation_instruction(relation: str) -> str:
    """根据关系标签生成行为指令，用于 LAYER 2 关系人设变体。"""
    mapping = {
        "同事": "对方是主人的同事。保持专业尊重的语气，协助工作事务，不闲聊私事。",
        "朋友": "对方是主人的朋友。语气轻松友好，可以适度开玩笑。",
        "家人": "对方是主人的家人/亲戚。语气亲切温暖，像家人一样对待。",
        "同学": "对方是主人的同学。友好平等，可以讨论学习相关话题。",
        "客户": "对方是主人的客户/老板。正式尊重，注意商务礼仪，维护好关系。",
        "老师": "对方是主人的老师/导师。非常尊重，虚心请教。",
        "长辈": "对方是主人的长辈。尊重敬爱，懂礼节。",
    }
    for key, instruction in mapping.items():
        if key in relation:
            return instruction
    return f"对方与主人的关系是'{relation}'。根据此关系调整语气和礼貌程度。"


def _build_prompt(sender_nick: str, mem: dict, chat_id: str) -> str:
    """五层 + 三层人设 prompt：身份 → 主人感知 → 关系变体 → 个性特征 → 规则 → 上下文"""
    sender_id = mem.get("key", "").split("_")[-1] if "_" in mem.get("key", "") else ""

    role = mem.get("role") or {}

    _apply_learned_traits(mem, role)

    # 新字段名（兼容旧字段名作为 fallback）
    personality = role.get("personality") or role.get("性格", "")
    # 自定义称呼：从 SQLite fixed_name 解析出的 sender_nick 如果 != QQ昵称，说明有自定义称呼
    qq_nick = _friend_cache.get(sender_id, "")
    if sender_nick and qq_nick and sender_nick != qq_nick:
        nickname = sender_nick  # sender_nick 来自 _resolve_speaker_name = SQLite 权威值
    else:
        nickname = ""
    extra_rules = role.get("custom_rules") or role.get("规则", "")
    relation_override = role.get("relation_override", "")

    layers = []
    layers.append(_build_identity_anchor() + f"""

【身份硬约束】：
- 你的 QQ号：{SELF_QQ}
- 你的 昵称：{SELF_NICK}
- 当前对话对象：QQ号 {sender_id}，昵称「{sender_nick}」
- ★ 你必须用「{sender_nick}」称呼对方，这是铁律！不要问"你想让我叫你什么"，不要让其调用 get_nickname_status 查称呼——你已知晓，直接在回复中使用「{sender_nick}」。
- 你不是对方。对话历史中「他」说的话是对方说的，不是你说的。不要抢对方身份。
- 群聊场景：你不是群里的任何一个人。不要模仿或替别人说话。不要混淆不同人的身份。

【称呼管理规则 · 必须遵守】：
- 你必须始终用「{sender_nick}」称呼对方，这是最高优先级规则。不要犹豫、不要询问、不要再用工具确认。
- 如果对方主动提出称呼偏好，经确认后使用 set_nickname 记录。
- 对方或管理员要求改称呼时，先调用 set_nickname 工具写入，再回复结果。校验失败（如脏话）则回复"这个称呼不太合适，请换一个吧~"。
- "你叫他什么"→ 直接回复当前称呼「{sender_nick}」，不要调 get_nickname_status（除非 nickname 未解析出来）。
- "换个称呼" "切称呼"→ 调用 switch_nickname 切换。
- 每人只有1个称呼位（后来者覆盖），系统不再自动轮换。
    - 称呼在私聊和群聊中是通用的——对方在私聊设置的称呼，在群聊中同样有效。
    - 只有管理员才能被称为「主人」。非管理员不是主人，你不能称呼非管理员为「主人」。
    - 如果对方要求的称呼已经被别人占用，set_nickname 工具会拒绝保存。你不能口头答应。要如实告诉对方这个称呼已被占用。

""")
    masters = _get_master_ids()
    if sender_id in masters:
        master_count = len(masters)
        contact_info = _get_contact_summary(detail=False)
        contact_line = f"\n你的账号信息：{contact_info}" if contact_info else ""
        layers.append(f"【LAYER 2 — 管理员感知】对方是你的管理员之一（QQ:{sender_id}）。当前共 {master_count} 位管理员：{','.join(sorted(masters))}。按授权范围处理请求。{contact_line}")
    else:
        layers.append(
            f"【LAYER 2 — 管理员感知 · 受限模式】对方不是管理员（QQ:{sender_id}）。\n"
            f"你可以帮对方做这些事情：\n"
            f"  ✓ 聊天、玩游戏、查天气\n"
            f"  ✓ 查看和修改 TA 自己的信息（称呼/城市/关系/备注/档案）\n"
            f"  ✓ 给 TA 自己创建定时提醒\n"
            f"  ✓ 帮 TA 给管理员留言传话（发消息只能发给管理员）\n"
            f"  ✗ 不能给除管理员以外的任何人发消息、群发、传话——对方提出就自然拒绝\n"
            f"隐私红线（绝对遵守）：\n"
            f"  ✗ 不透露账号里的好友列表、任何第三人的称呼/QQ/备注/关系/城市等信息\n"
            f"  ✗ 有人问「你有哪些好友」「XX是谁」「查一下XX」→ 自然地拒绝，这些是别人的隐私\n"
            f"绝对禁止的行为：\n"
            f"  ✗ 不能说「已帮你发送」「已帮你通知」等假装执行了操作的回复\n"
            f"  当对方要求超出权限的操作时，自然地说做不到即可，不解释系统内部规则。"
        )

    relation = relation_override or _extract_relation(mem)

    # 首次对话检测：优先从 nickname_slots 看 first_contact_done，回退到旧逻辑
    from db import get_nickname_slots, get_person_city
    slots = get_nickname_slots(sender_id)
    first_contact_done = slots.get("first_contact_done", 0) if slots else 0
    is_new_contact = not first_contact_done and (
        len(mem.get("conversation", [])) <= 3
        and not personality and not nickname
    )

    # 非新联系人也检查是否还没设置城市
    sender_city = get_person_city(sender_id)
    needs_city = not sender_city and not is_new_contact

    if is_new_contact:
        layers.append("【新联系人】保持礼貌，尊重对方隐私；按对方实际需求交流，不主动收集个人信息。")

    if not is_new_contact:
        # LAYER 3A: 关系人设变体
        if relation_override:
            rel_instruction = _build_relation_instruction(relation_override)
            layers.append(f"【LAYER 3A — 关系人设】{rel_instruction}")
        elif relation:
            layers.append(f"【LAYER 3A — 关系提示】此人与主人的关系：{relation}。")

        # LAYER 3B: 个性特征
        persona_parts = []
        if nickname:
            persona_parts.append(f"称呼他为「{nickname}」")
        if personality:
            persona_parts.append(f"特征：{personality}")
        if extra_rules:
            persona_parts.append(f"特殊规则：{extra_rules}")

        # Phase 3.2: 称呼轮换提醒
        rotation = _recent_name_rotations.get(sender_id)
        if rotation:
            old, new, ts = rotation
            if time.time() - ts < 86400:  # 24 小时内有效
                persona_parts.append(f"⚠ 此人的活跃称呼最近从「{old}」切换为「{new}」。请使用「{new}」称呼")

        if persona_parts:
            layers.append(f"【LAYER 3B — 个性特征】对方「{sender_nick}」（QQ:{sender_id}）。{'。'.join(persona_parts)}。")
        elif relation:
            layers.append(f"【LAYER 3B — 个性特征】对方 QQ号{sender_id}，昵称「{sender_nick}」。没有特殊人设，按 persona.txt 默认风格。")
        else:
            layers.append(f"【LAYER 3B — 个性特征】对方 QQ号{sender_id}，昵称「{sender_nick}」。没有特殊人设，按 persona.txt 默认风格。")

    # 每用户独立人设（仅对该用户生效，不互串）
    from users import get_persona_prompt_section
    layers.append(get_persona_prompt_section(sender_id, _build_default_persona()))

    if _is_chat_muted(chat_id):
        layers.append("【LAYER 5 — 状态】此会话已被静默。仅当对方是主人且让你解除静默时才回复。")

    return "\n\n".join(layers)


def _build_default_persona() -> str:
    """默认 persona — 加载 persona.txt 的基础规则部分"""
    if PERSONA_FILE.exists():
        try:
            content = PERSONA_FILE.read_text("utf-8").strip()
            marker = "## 人物规则"
            idx = content.find(marker)
            if idx != -1:
                content = content[:idx].strip()
            return f"【LAYER 4 — 全局规则】\n{content}"
        except Exception:
            pass
    return "【LAYER 4 — 全局规则】\n请遵循使用者设置的人设，尊重隐私，回答真实、简洁。"


# ==================== 称呼插槽系统 ====================

# 恶意称呼黑名单（不全匹配，只做子串检测）
_NICKNAME_BLACKLIST = [
    "你爹", "傻逼", "sb", "nmsl", "吃屎", "操你", "fuck", "cnm", "草泥马",
    "狗日", "日你", "操你妈", "艹", "尼玛", "脑残", "脑瘫", "弱智",
]
_NICKNAME_MAX_LEN = 10


def _validate_nickname(name: str) -> tuple[bool, str]:
    """校验称呼合法性。返回 (是否合法, 原因)。"""
    name = name.strip()
    if not name:
        return False, "称呼不能为空"
    if len(name) < 1 or len(name) > _NICKNAME_MAX_LEN:
        return False, f"称呼长度需在1-{_NICKNAME_MAX_LEN}个字符之间"
    name_lower = name.lower()
    for word in _NICKNAME_BLACKLIST:
        if word in name_lower:
            return False, "这个称呼不太合适，请换一个吧~"
    if re.match(r'^\d{5,12}$', name):
        return False, "称呼不能是纯数字的QQ号"
    if re.match(r'^[^a-zA-Z一-鿿㐀-䶿]+$', name):
        return False, "称呼不能全是符号，请包含中文或英文"
    special_count = len(re.findall(r'[^a-zA-Z0-9一-鿿㐀-䶿]', name))
    if special_count / max(len(name), 1) > 0.5:
        return False, "称呼中特殊符号太多了，请减少符号"
    return True, ""



def _find_nickname_conflicts(name, exclude_qq=""):
    conflicts = []
    try:
        from db import get_all_nickname_rows, get_resolved_name
        for row in get_all_nickname_rows():
            qq = row["qq"]
            if qq == exclude_qq:
                continue
            for key in ("slot_a_value", "slot_b_value"):
                val = (row.get(key) or "").strip()
                if val and val == name:
                    display = get_resolved_name(qq) or qq[-4:]
                    if display not in conflicts:
                        conflicts.append(display)
                    break
    except Exception:
        pass
    return conflicts
def _get_nickname_data(qq: str) -> dict:
    """从 SQLite 获取称呼插槽数据。"""
    from db import get_nickname_slots
    return get_nickname_slots(qq)


def _save_nickname_data(qq: str, data: dict):
    """保存称呼插槽数据到 SQLite。"""
    from db import save_nickname_slots
    save_nickname_slots(qq, data)


def _set_nickname(qq: str, new_name: str, set_by: str) -> dict:
    """核心函数：设置称呼（单一值覆盖，后来者替换前者）。
    返回 {"ok": bool, "slot": str|None, "overwritten": str|None, "message": str}
    """
    valid, reason = _validate_nickname(new_name)
    if not valid:
        return {"ok": False, "slot": None, "overwritten": None, "message": reason}


    nc_list = _find_nickname_conflicts(new_name, qq)
    if nc_list:
        _taken_by = "、" . join(nc_list)
        return {
            "ok": False,
            "slot": None,
            "overwritten": None,
            "message": f"称呼「{new_name}」已被 {_taken_by} 使用，建议换个不重复的称呼，例如「{new_name}2」或「{new_name}新」"
        }
    data = _get_nickname_data(qq)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 单一值覆盖：清除 slot_b，统一使用 slot_a
    old_value = data.get("slot_a_value", "") or None
    data["slot_a_value"] = new_name
    data["slot_a_set_by"] = set_by
    data["slot_a_set_time"] = now
    data["slot_a_locked"] = 0
    # 清除副槽位
    data["slot_b_value"] = ""
    data["slot_b_set_by"] = ""
    data["slot_b_set_time"] = ""
    data["slot_b_locked"] = 0
    data["active_slot"] = "slot_a"

    _save_nickname_data(qq, data)
    _invalidate_person_index()
    _invalidate_profile_cache(qq)  # 改名后立即失效缓存，确保下次读取新名字

    if old_value:
        msg = f"已覆盖「{old_value}」→「{new_name}」"
    else:
        msg = f"已设置称呼「{new_name}」"
    return {
        "ok": True,
        "slot": "slot_a",
        "overwritten": old_value,
        "message": msg,
    }


def _switch_active_slot(qq: str) -> str:
    """切换当前活跃称呼位，返回切换后的称呼值。"""
    data = _get_nickname_data(qq)
    active = data.get("active_slot", "")
    other = "slot_b" if active == "slot_a" else "slot_a"
    new_value = data.get(f"{other}_value", "")
    if not new_value:
        return ""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data["active_slot"] = other
    data["last_switch_time"] = now
    _save_nickname_data(qq, data)
    _invalidate_person_index()

    return new_value


def _auto_switch_all_nicknames():
    """定时检查所有称呼数据，距离上次切换超过 48 小时则自动切。"""
    from db import get_all_nickname_rows as _get_all
    now = datetime.now()
    now_iso = now.strftime("%Y-%m-%d %H:%M:%S")
    switched = 0
    for row in _get_all():
        qq = row["qq"]
        last = row.get("last_switch_time", "")
        if last:
            try:
                last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
                if (now - last_dt).total_seconds() < 48 * 3600:
                    continue
            except Exception:
                pass
        data = dict(row)
        active = data.get("active_slot", "")
        other = "slot_b" if active == "slot_a" else "slot_a"
        old_value = data.get(f"{active}_value", "") if active else ""
        new_value = data.get(f"{other}_value", "")
        if not new_value:
            continue
        data["active_slot"] = other
        data["last_switch_time"] = now_iso
        _save_nickname_data(qq, data)
        # Phase 3.2: 记录轮换事件，供 prompt 注入提醒
        if old_value and new_value:
            _recent_name_rotations[qq] = (old_value, new_value, time.time())
        switched += 1
    if switched:
        _invalidate_person_index()
        log(f"称呼轮换: {switched} 人已自动切换活跃称呼")


def _set_contact_relation(qq: str, relation: str):
    """写关系标签到 SQLite。"""
    from db import set_relation, set_first_contact_done
    set_relation(qq, relation)
    set_first_contact_done(qq)


def _get_active_nickname(qq: str) -> str:
    """快捷获取当前活跃称呼。"""
    data = _get_nickname_data(qq)
    active = data.get("active_slot", "")
    return data.get(f"{active}_value", "") if active else ""


# ============================================================

def _migrate_roles_json_to_data():
    """一次性迁移：将 roles.json 中的角色配置合并到对应 private 文件的 role 字段。
    迁移完成后重命名 roles.json 为 .migrated 备份。
    返回迁移的人数。"""
    if not ROLES_FILE.exists():
        return 0
    try:
        roles = json.loads(ROLES_FILE.read_text("utf-8"))
    except Exception:
        return 0
    if not roles:
        return 0
    count = 0
    for qq, role_data in roles.items():
        mem_key = f"private_{qq}"
        lock = _get_memory_lock(mem_key)
        with lock:
            mem = _load_memory(mem_key)
            mem.setdefault("role", {})
            if "性格" in role_data:
                mem["role"]["personality"] = role_data["性格"]
            if "称呼" in role_data:
                mem["role"]["display_name"] = role_data["称呼"]
                cc = role_data["称呼"]
                if cc:
                    # 直接操作 notes，不使用 _upsert_person_note（避免同一锁的重入死锁）
                    notes = mem.get("notes", [])
                    # 移除旧的称呼: 条目
                    notes = [n for n in notes if not n.get("note", "").strip().startswith("称呼:")]
                    notes.append({"time": datetime.now().isoformat(), "note": f"称呼:{cc}"})
                    mem["notes"] = notes
                    mem["name"] = cc
            if "规则" in role_data:
                mem["role"]["custom_rules"] = role_data["规则"]
            _save_memory(mem_key, mem)
            count += 1
    import shutil
    backup = ROLES_FILE.with_suffix(".json.migrated")
    shutil.move(str(ROLES_FILE), str(backup))
    log(f"roles.json 已迁移 {count} 人的配置到 data 文件，备份为 {backup}")
    return count


# ==================== Phase 1.4: 启动一致性检查 ====================

def _check_name_consistency():
    """扫描所有联系人，检查 nickname_slots / fixed_name / JSON name 之间的一致性。
    仅记录警告，不自动修复。
    """
    warnings = []
    from db import get_all_nickname_rows as _db_get_all, get_profile as _db_profile
    try:
        rows = _db_get_all()
    except Exception:
        return warnings  # DB 未就绪

    for row in rows:
        qq = row["qq"]
        slot_a = (row.get("slot_a_value") or "").strip()
        slot_b = (row.get("slot_b_value") or "").strip()
        active = row.get("active_slot", "")

        # 检查 active_slot 对应的槽位是否有值
        if active and active in ("slot_a", "slot_b"):
            active_val = slot_a if active == "slot_a" else slot_b
            if not active_val:
                warnings.append(f"⚠ {qq}: active_slot={active} 但对应槽位为空")

        # 检查与 fixed_name 的冲突
        try:
            prof = _db_profile(qq)
            if prof and prof.get("fixed_name"):
                fn = prof["fixed_name"]
                if slot_a and slot_a != fn and slot_b and slot_b != fn:

                # Check single-slot conflicts with fixed_name
                    if slot_a and slot_a != fn and not slot_b:
                        warnings.append(f"[WARN] {qq}: slot_a=\"{slot_a}\" != fixed_name=\"{fn}\"")
                    if slot_b and slot_b != fn and not slot_a:
                        warnings.append(f"[WARN] {qq}: slot_b=\"{slot_b}\" != fixed_name=\"{fn}\"")
                    warnings.append(f"[WARN] {qq}: nickname_slots=[\"{slot_a}\",\"{slot_b}\"] != fixed_name=\"{fn}\"")
        except Exception:
            pass

        # 检查 JSON name 字段是否还有残余值
        try:
            mem_file = DATA_DIR / f"private_{qq}.json"
            if mem_file.exists():
                mem = json.loads(mem_file.read_text("utf-8"))
                json_name = mem.get("name", "").strip()
                active_name = slot_a if active == "slot_a" else slot_b
                if json_name and json_name != active_name:
                    warnings.append(f"⚠ {qq}: JSON name='{json_name}' ≠ nickname_slots active='{active_name}'")
        except Exception:
            pass

    if warnings:
        log(f"名字一致性检查: {len(warnings)} 条警告")
    return warnings


# ==================== Phase 2.4: 智能上下文压缩 ====================

def _compress_context(conv: list[dict], max_chars: int = 3000) -> str:
    """智能上下文压缩：近 5 条完整 + 更早历史 AI 摘要 + 关键信息保留。

    - 短对话（<=12 条）不压缩，返回原文（保持现有行为）
    - 长对话（>12 条）：最近 5 条完整 + 更早历史调 AI 摘要
    """
    if len(conv) <= 12:
        # 短对话：维持现有行为
        return "\n".join(
            f"[{'我' if m.get('direction') == 'out' else '他'}]: {m.get('content', '')[:300]}"
            for m in conv
        )
    # 长对话：近 5 条完整 + 更早摘要
    recent = conv[-5:]
    recent_text = "\n".join(
        f"[{'我' if m.get('direction') == 'out' else '他'}]: {m.get('content', '')[:300]}"
        for m in recent
    )
    earlier = conv[:-5]
    # 取更早的最近 20 条做摘要（避免 prompt 过长）
    earlier_sample = earlier[-20:]
    lines = "\n".join(
        f"{'我' if m.get('direction') == 'out' else '他'}: {m.get('content', '')[:100]}"
        for m in earlier_sample
    )
    try:
        from tools import _call_deepseek
        summary = _call_deepseek(
            "你是对话摘要助手。用 50 字以内总结以下对话的关键信息（人名、计划、决定、情绪）：",
            lines, max_tokens=100)
        summary_text = summary.strip() if summary else ""
    except Exception:
        summary_text = ""
    if summary_text:
        return f"[历史摘要]: {summary_text}\n\n[最近对话]:\n{recent_text}"
    return recent_text


# ==================== Phase 2.3: 三级记忆系统 ====================

class MemoryManager:
    """三级记忆系统：L1 工作记忆（当前对话）、L2 情景记忆（每日摘要）、L3 语义记忆（人物档案）。"""

    @classmethod
    def nightly_summarize(cls) -> None:
        """每天凌晨为每个活跃联系人生成当日摘要 → 写入 data/daily_summary_{date}.json。"""
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            summaries = {}
            for mem_file in DATA_DIR.glob("private_*.json"):
                qq = mem_file.stem.split("_", 1)[1]
                try:
                    mem = json.loads(mem_file.read_text("utf-8"))
                except Exception:
                    continue
                conv = mem.get("conversation", [])
                if len(conv) < 4:
                    continue
                from tools import _call_deepseek
                lines = "\n".join(
                    f"{'TA' if m.get('direction') == 'in' else '我'}: {m.get('content', '')[:150]}"
                    for m in conv[-30:]
                )
                prompt = f"用 3-5 条简短要点总结今天的对话关键信息（人名、计划、情绪、决定）：\n{lines}"
                summary = _call_deepseek("你是对话摘要助手", prompt, max_tokens=200)
                if summary:
                    summaries[qq] = summary.strip()
            if summaries:
                summary_path = DATA_DIR / f"daily_summary_{today}.json"
                summary_path.write_text(
                    json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
                log(f"[MEMORY] nightly summarize: {len(summaries)} contacts → {summary_path.name}")
            # 清理 30 天前的摘要
            cls._cleanup_old_summaries(days=30)
        except Exception as e:
            log(f"[MEMORY] nightly_summarize error: {e}")

    @classmethod
    def recall(cls, query: str, k: int = 5) -> list[dict]:
        """语义搜索：从 L2+L3 检索相关信息。关键词匹配，无需 embedding API。"""
        results = []
        query_lower = query.lower()
        # L3: SQLite 人物档案
        try:
            from db import get_profiles_batch
            index = _get_person_index()
            all_names = index.get("all_names", [])
            matched_qqs = set()
            for name, qq, kind in all_names:
                if name and query_lower in name.lower():
                    matched_qqs.add(qq)
            if matched_qqs:
                profiles = get_profiles_batch(list(matched_qqs))
                for qq, prof in profiles.items():
                    results.append({
                        "type": "semantic", "qq": qq,
                        "name": prof.get("fixed_name", ""),
                        "tags": prof.get("tag_list_parsed", []),
                    })
        except Exception:
            pass
        # L2: 最近摘要
        try:
            for summary_file in sorted(DATA_DIR.glob("daily_summary_*.json"), reverse=True)[:7]:
                data = json.loads(summary_file.read_text("utf-8"))
                for qq, summary in data.items():
                    if query_lower in summary.lower():
                        results.append({
                            "type": "episodic", "qq": qq,
                            "summary": summary[:200],
                            "date": summary_file.stem.replace("daily_summary_", ""),
                        })
        except Exception:
            pass
        return results[:k]

    @classmethod
    def _cleanup_old_summaries(cls, days: int = 30) -> None:
        """清理超过 N 天的每日摘要文件"""
        try:
            cutoff = datetime.now().timestamp() - days * 86400
            for f in DATA_DIR.glob("daily_summary_*.json"):
                if f.stat().st_mtime < cutoff:
                    f.unlink()
        except Exception:
            pass
