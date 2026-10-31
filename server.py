#!/usr/bin/env python3
"""
QQ MCP Server v3 — 思考后回复 + 每人独立记忆
去掉后台WS线程（避免事件循环冲突），改用 API 轮询新消息
"""

import json, os, re, sys, time
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen

# 确保 Windows 终端 UTF-8 输出
if sys.stdout.encoding != "utf-8":
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
if sys.stderr.encoding != "utf-8":
    try: sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

from mcp.server.fastmcp import FastMCP

NAP_CAT_HTTP = os.environ.get("NAP_CAT_HTTP", "http://127.0.0.1:3000")
MY_QQ = os.environ.get("MY_QQ", "0")
MEMORY_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent / "data"))

MEMORY_DIR.mkdir(parents=True, exist_ok=True)
mcp = FastMCP("qq-mcp")

_person_memories: dict[str, dict] = {}
_last_check_time = 0  # 上次检查新消息的时间戳

def _api(action: str, params: dict | None = None) -> dict:
    data = json.dumps(params or {}).encode("utf-8")
    req = Request(f"{NAP_CAT_HTTP}/{action}", data=data,
                  headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"status": "error", "message": str(e)}

def _person_key(msg_type: str, target_id: str, sender_id: str) -> str:
    if msg_type == "group":
        return f"group_{target_id}_person_{sender_id}"
    return f"private_{sender_id}"

def _load_memory(key: str) -> dict:
    if key in _person_memories:
        return _person_memories[key]
    f = MEMORY_DIR / f"{key}.json"
    if f.exists():
        try:
            _person_memories[key] = json.loads(f.read_text("utf-8"))
            return _person_memories[key]
        except Exception:
            pass
    mem = {"key": key, "name": "", "notes": [], "conversation": [], "message_cache": {}, "first_seen": datetime.now().isoformat()}
    _person_memories[key] = mem
    return mem

def _save_memory(key: str, mem: dict):
    _person_memories[key] = mem
    f = MEMORY_DIR / f"{key}.json"
    f.write_text(json.dumps(mem, ensure_ascii=False, indent=2), "utf-8")

def _record(msg_type: str, target_id: str, sender_id: str, sender_nick: str, content: str, is_self: bool):
    key = _person_key(msg_type, target_id, sender_id)
    mem = _load_memory(key)
    if sender_nick and not mem.get("name"):
        mem["name"] = sender_nick
    mem["conversation"].append({
        "time": datetime.now().strftime("%m-%d %H:%M:%S"),
        "direction": "out" if is_self else "in",
        "sender_id": sender_id,
        "content": content,
    })
    if len(mem["conversation"]) > 500:
        mem["conversation"] = mem["conversation"][-500:]
    _save_memory(key, mem)

def _sent(msg_type: str, target_id: str, text: str) -> str:
    sentences = re.split(r"(?<=[。！？\n])", text)
    chunks, cur = [], ""
    for s in sentences:
        if len(cur + s) > 300:
            if cur: chunks.append(cur)
            cur = s
        else:
            cur += s
    if cur: chunks.append(cur)
    if not chunks: chunks = [text]
    ids = []
    for chunk in chunks:
        if msg_type == "group":
            r = _api("send_group_msg", {"group_id": int(target_id), "message": chunk})
        else:
            r = _api("send_private_msg", {"user_id": int(target_id), "message": chunk})
        mid = r.get("data")
        if mid and isinstance(mid, dict):
            ids.append(str(mid.get("message_id", "?")))
    return ",".join(ids)


# ============ MCP 工具 ============
@mcp.tool()
async def check_inbox() -> str:
    """检查QQ新消息。拉取所有好友和群的最新消息，和上次对比找出新消息。"""
    global _last_check_time
    now = int(time.time())
    since = _last_check_time
    _last_check_time = now

    new_msgs = []

    # 遍历所有好友
    friends = _api("get_friend_list").get("data", [])
    for f in friends:
        uid = str(f["user_id"])
        if uid == MY_QQ:
            continue
        try:
            msgs = _api("get_friend_msg_history", {"user_id": int(uid), "count": 10}).get("data", {}).get("messages", [])
            for m in msgs:
                t = m.get("time", 0)
                sid = str(m.get("sender", {}).get("user_id", m.get("user_id", "")))
                if t > since and sid != MY_QQ:
                    sn = m.get("sender", {}).get("nickname", "?")
                    content = str(m.get("raw_message", m.get("message", "")))
                    key = _person_key("private", uid, sid)
                    # 检查是否已记录
                    mem = _load_memory(key)
                    cache = mem.setdefault("message_cache", {})
                    msg_id = str(m.get("message_id", ""))
                    if msg_id not in cache:
                        cache[msg_id] = True
                        _record("private", uid, sid, sn, content, False)
                        new_msgs.append({
                            "type": "private", "target_id": uid, "sender_id": sid,
                            "sender_nick": sn, "content": content,
                            "time": datetime.fromtimestamp(t).strftime("%m-%d %H:%M:%S"),
                            "person_key": key,
                        })
                        # 清理旧缓存
                        if len(cache) > 200:
                            keys = list(cache.keys())
                            for old in keys[:-100]:
                                del cache[old]
                    _save_memory(key, mem)
        except Exception:
            pass

    # 遍历群（如果有群的话）
    groups = _api("get_group_list").get("data", [])
    for g in groups:
        gid = str(g["group_id"])
        try:
            msgs = _api("get_group_msg_history", {"group_id": int(gid), "count": 10}).get("data", {}).get("messages", [])
            for m in msgs:
                t = m.get("time", 0)
                sid = str(m.get("sender", {}).get("user_id", m.get("user_id", "")))
                if t > since and sid != MY_QQ:
                    sn = m.get("sender", {}).get("nickname", "?")
                    content = str(m.get("raw_message", m.get("message", "")))
                    key = _person_key("group", gid, sid)
                    mem = _load_memory(key)
                    cache = mem.setdefault("message_cache", {})
                    msg_id = str(m.get("message_id", ""))
                    if msg_id not in cache:
                        cache[msg_id] = True
                        is_at = f"[CQ:at,qq={MY_QQ}]" in content
                        _record("group", gid, sid, sn, content, False)
                        new_msgs.append({
                            "type": "group", "target_id": gid, "sender_id": sid,
                            "sender_nick": sn, "content": content, "is_at_me": is_at,
                            "time": datetime.fromtimestamp(t).strftime("%m-%d %H:%M:%S"),
                            "group_name": g.get("group_name", ""),
                            "person_key": key,
                        })
                    if len(cache) > 200:
                        keys = list(cache.keys())
                        for old in keys[:-100]:
                            del cache[old]
                    _save_memory(key, mem)
        except Exception:
            pass

    return json.dumps({"new_messages": len(new_msgs), "since": datetime.fromtimestamp(since).strftime("%m-%d %H:%M:%S") if since else "first_check", "messages": new_msgs}, ensure_ascii=False, indent=2)


@mcp.tool()
async def get_person_memory(person_key: str) -> str:
    """获取某人的完整记忆：姓名、备注、对话历史。person_key 格式: private_QQ号 或 group_群号_person_QQ号"""
    mem = _load_memory(person_key)
    return json.dumps({
        "key": mem.get("key",""), "name": mem.get("name",""),
        "notes": mem.get("notes",[]),
        "conversation": mem.get("conversation",[])[-30:],
        "first_seen": mem.get("first_seen",""),
    }, ensure_ascii=False, indent=2)


@mcp.tool()
async def reply_message(message_type: str, target_id: str, text: str, sender_id: str = "", sender_nick: str = "") -> str:
    """回复QQ消息。回复前请先查看对方记忆(用get_person_memory)，确保回复得体。

    Args:
        message_type: "group" 或 "private"
        target_id: 群号或对方QQ号
        text: 回复内容（已经思考好的）
        sender_id: 对方QQ号（可选，用于记忆关联）
        sender_nick: 对方昵称（可选）
    """
    if not text.strip():
        return json.dumps({"ok": False, "error": "empty"})
    if sender_id:
        key = _person_key(message_type, target_id, sender_id)
        _record(message_type, target_id, sender_id, sender_nick, text, True)
    ids = _sent(message_type, target_id, text)
    return json.dumps({"ok": True, "message_ids": ids}, ensure_ascii=False)


@mcp.tool()
async def save_person_note(person_key: str, note: str) -> str:
    """给某人添加备注/记忆。如: 他的名字叫XX、他是XX班的

    Args:
        person_key: 人物标识 (private_QQ号 或 group_群号_person_QQ号)
        note: 备注内容
    """
    mem = _load_memory(person_key)
    mem["notes"].append({
        "time": datetime.now().strftime("%m-%d %H:%M:%S"),
        "note": note,
    })
    _save_memory(person_key, mem)
    return json.dumps({"ok": True}, ensure_ascii=False)


@mcp.tool()
async def list_contacts() -> str:
    """列出所有联系过的人及其记忆摘要"""
    summaries = []
    for f in sorted(MEMORY_DIR.glob("*.json")):
        try:
            mem = json.loads(f.read_text("utf-8"))
            conv = mem.get("conversation", [])
            last = conv[-1]["content"][:40] if conv else ""
            summaries.append({
                "key": mem["key"], "name": mem.get("name",""),
                "messages": len(conv), "notes": len(mem.get("notes",[])),
                "last_msg": last,
                "first_seen": mem.get("first_seen","")[:10],
            })
        except Exception:
            pass
    return json.dumps(summaries, ensure_ascii=False, indent=2)


def main():
    import sys as _sys
    print(f"[qq-mcp] Starting... Python {_sys.version.split()[0]}", file=_sys.stderr, flush=True)
    print(f"[qq-mcp] NAP_CAT_HTTP={NAP_CAT_HTTP} MY_QQ={MY_QQ}", file=_sys.stderr, flush=True)
    print(f"[qq-mcp] Tools: check_inbox, get_person_memory, reply_message, save_person_note, list_contacts", file=_sys.stderr, flush=True)
    try:
        mcp.run(transport="stdio")
    except Exception as e:
        print(f"[qq-mcp] FATAL: {e}", file=_sys.stderr, flush=True)
        raise

if __name__ == "__main__":
    main()
