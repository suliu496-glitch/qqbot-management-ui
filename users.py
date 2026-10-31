#!/usr/bin/env python3
"""
users.py — 每用户独立人设 + 静默管理

- 人设（persona）：每个联系人可拥有独立的人设设定，仅对该用户生效。
  存储在 SQLite user_profile.persona 字段。
- 静默（mute）：每个用户可设置静默（如"12小时内不要找我"），静默期间：
  定时任务/群发/管理员指定消息不对该用户执行，并告知谁静默及解除时间。
"""
import time
import threading
from typing import Optional

_local_lock = threading.Lock()

# ── 静默内存缓存（重启后建议由 SQLite 持久化，此处先用内存+启动时加载）──
_mute_cache: dict[str, float] = {}  # {qq: expiry_timestamp}

# ── 每用户人设 ──────────────────────────────────────────────────────

def set_user_persona(qq: str, persona_text: str) -> None:
    """设置某用户的人设（覆盖式），仅对该用户生效。"""
    from db import _get_conn
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    conn = _get_conn()
    conn.execute(
        """INSERT INTO user_profile (qq, persona, last_update)
           VALUES (?, ?, ?)
           ON CONFLICT(qq) DO UPDATE SET persona = excluded.persona, last_update = excluded.last_update""",
        (qq, persona_text, now))
    conn.commit()


def get_user_persona(qq: str) -> Optional[str]:
    """获取某用户独立人设，未设置返回 None。"""
    from db import _get_conn
    conn = _get_conn()
    row = conn.execute("SELECT persona FROM user_profile WHERE qq = ?", (qq,)).fetchone()
    return row["persona"] if row and row["persona"] else None


def get_persona_prompt_section(qq: str, default_persona: str) -> str:
    """返回该用户专属人设的 prompt 段落。"""
    persona = get_user_persona(qq)
    if persona:
        return f"【此人专属人设（仅对 QQ:{qq} 生效）】\n{persona}\n"
    return f"【默认人设】\n{default_persona}\n"

# ── 静默功能 ──────────────────────────────────────────────────────────

def set_mute(qq: str, duration_hours: float) -> str:
    """设置静默（从现在起 N 小时），返回解除时间描述。"""
    expiry = time.time() + duration_hours * 3600
    with _local_lock:
        _mute_cache[qq] = expiry
    until = time.strftime("%Y-%m-%d %H:%M", time.localtime(expiry))
    return until


def cancel_mute(qq: str) -> None:
    """取消静默。"""
    with _local_lock:
        _mute_cache.pop(qq, None)


def is_muted(qq: str) -> bool:
    """检查该用户是否处于静默状态。"""
    with _local_lock:
        expiry = _mute_cache.get(qq, 0)
        if expiry and time.time() < expiry:
            return True
        if expiry and time.time() >= expiry:
            _mute_cache.pop(qq, None)
        return False


def get_mute_status(qq: str) -> Optional[str]:
    """获取静默状态描述，未静默返回 None。"""
    if not is_muted(qq):
        return None
    with _local_lock:
        expiry = _mute_cache.get(qq, 0)
    remaining_h = (expiry - time.time()) / 3600
    until = time.strftime("%H:%M", time.localtime(expiry))
    return f"静默中（约 {remaining_h:.1f} 小时后解除，{until}）"


def get_all_muted() -> list[tuple[str, str]]:
    """获取所有静默用户列表 [(qq, status_desc), ...]。"""
    result = []
    with _local_lock:
        now = time.time()
        for qq, expiry in list(_mute_cache.items()):
            if now < expiry:
                remaining_h = (expiry - now) / 3600
                result.append((qq, f"静默中（约 {remaining_h:.1f} 小时后解除）"))
            else:
                _mute_cache.pop(qq, None)
    return result