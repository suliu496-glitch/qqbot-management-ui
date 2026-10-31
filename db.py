#!/usr/bin/env python3
"""QQ Bot SQLite 数据库层 — 全局人物档案、聊天日志、临时名称缓存。

线程安全：每线程独立 sqlite3.Connection（threading.local）。
WAL 模式：高并发读，写入不阻塞读取。
"""

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

# ── 路径常量 ──────────────────────────────────────────────
DB_PATH = Path(__file__).parent / "data" / "bot.db"

# ── 线程本地连接（每个线程独立连接，线程安全）──────────────
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn.execute("PRAGMA busy_timeout=5000")
        _local.conn.row_factory = sqlite3.Row
    return _local.conn


def close_db():
    if hasattr(_local, "conn") and _local.conn:
        try:
            _local.conn.close()
        except Exception:
            pass
        _local.conn = None


# ── 初始化 ────────────────────────────────────────────────

def init_db():
    """建表（幂等）。启动时调用。"""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS user_profile (
            qq              TEXT PRIMARY KEY,
            fixed_name      TEXT,
            is_friend       INTEGER DEFAULT 0,
            feature_summary TEXT,
            tag_list        TEXT,
            source_group    TEXT,
            last_update     TEXT,
            city            TEXT DEFAULT '',
            city_set_by     TEXT DEFAULT '',
            city_set_time   TEXT DEFAULT '',
            user_summary    TEXT DEFAULT '',
            real_name       TEXT DEFAULT '',
            key_facts       TEXT DEFAULT '',
            free_notes      TEXT DEFAULT '',
            persona         TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS global_chat_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            sender    TEXT NOT NULL,
            bot_qq    TEXT NOT NULL,
            group_id  TEXT NOT NULL DEFAULT '0',
            msg       TEXT NOT NULL,
            send_time TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_log_sender      ON global_chat_log(sender);
        CREATE INDEX IF NOT EXISTS idx_log_time        ON global_chat_log(send_time);
        CREATE INDEX IF NOT EXISTS idx_log_sender_time ON global_chat_log(sender, send_time);

        CREATE TABLE IF NOT EXISTS temp_name_cache (
            group_id    TEXT NOT NULL,
            user_qq     TEXT NOT NULL,
            temp_name   TEXT NOT NULL,
            update_time TEXT NOT NULL,
            PRIMARY KEY (group_id, user_qq)
        );

        CREATE INDEX IF NOT EXISTS idx_temp_name_user ON temp_name_cache(user_qq);

        CREATE TABLE IF NOT EXISTS nickname_slots (
            qq                  TEXT PRIMARY KEY,
            slot_a_value        TEXT DEFAULT '',
            slot_a_set_by       TEXT DEFAULT '',
            slot_a_set_time     TEXT DEFAULT '',
            slot_a_locked       INTEGER DEFAULT 0,
            slot_b_value        TEXT DEFAULT '',
            slot_b_set_by       TEXT DEFAULT '',
            slot_b_set_time     TEXT DEFAULT '',
            slot_b_locked       INTEGER DEFAULT 0,
            active_slot         TEXT DEFAULT '',
            relation            TEXT DEFAULT '',
            first_contact_done  INTEGER DEFAULT 0,
            last_switch_time    TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id              TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            task_type       TEXT NOT NULL,
            trigger_time    TEXT DEFAULT '',
            interval_hours  INTEGER DEFAULT 0,
            target_type     TEXT NOT NULL,
            target_id       TEXT NOT NULL,
            content         TEXT NOT NULL,
            created_by      TEXT NOT NULL,
            enabled         INTEGER DEFAULT 1,
            last_run        TEXT DEFAULT '',
            created_at      TEXT NOT NULL,
            action_type     TEXT DEFAULT 'static'
        );

        CREATE TABLE IF NOT EXISTS _migration (
            version INTEGER PRIMARY KEY,
            done_at TEXT
        );

        CREATE TABLE IF NOT EXISTS schedule (
            id          TEXT PRIMARY KEY,
            qq          TEXT NOT NULL,
            title       TEXT NOT NULL,
            when_time   TEXT NOT NULL,
            description TEXT DEFAULT '',
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS todo (
            id          TEXT PRIMARY KEY,
            qq          TEXT NOT NULL,
            title       TEXT NOT NULL,
            done        INTEGER DEFAULT 0,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS auto_reply_rules (
            id          TEXT PRIMARY KEY,
            qq          TEXT NOT NULL,
            trigger     TEXT NOT NULL,
            response    TEXT NOT NULL,
            enabled     INTEGER DEFAULT 1,
            created_at  TEXT NOT NULL
        );
    """)
    conn.commit()

    # 迁移：为旧数据库补上 action_type 列
    try:
        conn.execute("ALTER TABLE scheduled_tasks ADD COLUMN action_type TEXT DEFAULT 'static'")
        conn.commit()
    except Exception:
        pass

    # 迁移：为旧数据库补上 city 列
    for col, col_def in [
        ("city", "TEXT DEFAULT ''"),
        ("city_set_by", "TEXT DEFAULT ''"),
        ("city_set_time", "TEXT DEFAULT ''"),
    ]:
        try:
            conn.execute(f"ALTER TABLE user_profile ADD COLUMN {col} {col_def}")
            conn.commit()
        except Exception:
            pass

    # 迁移：为旧数据库补上 user_summary 列
    try:
        conn.execute("ALTER TABLE user_profile ADD COLUMN user_summary TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass

    # 迁移：为旧数据库补上 real_name, key_facts, free_notes 列
    for col in ["real_name", "key_facts", "free_notes"]:
        try:
            conn.execute(f"ALTER TABLE user_profile ADD COLUMN {col} TEXT DEFAULT ''")
            conn.commit()
        except Exception:
            pass

    # 迁移：persona 列（users.py 每用户独立人设）
    try:
        conn.execute("ALTER TABLE user_profile ADD COLUMN persona TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass

    # 迁移：scheduled_tasks first_trigger 列（首次触发时间，避免新建即触发）
    try:
        conn.execute("ALTER TABLE scheduled_tasks ADD COLUMN first_trigger TEXT DEFAULT ''")
        conn.commit()
    except Exception:
        pass

    # 迁移：修复 task_type='cron' 的遗留任务，根据名称补 trigger_time
    try:
        cron_tasks = conn.execute(
            "SELECT id, name FROM scheduled_tasks WHERE task_type='cron' AND (trigger_time='' OR trigger_time IS NULL)"
        ).fetchall()
        for t in cron_tasks:
            name = t["name"]
            if "早安" in name:
                conn.execute("UPDATE scheduled_tasks SET trigger_time='08:00' WHERE id=?", (t["id"],))
            elif "午" in name and "天气" in name:
                conn.execute("UPDATE scheduled_tasks SET trigger_time='12:00' WHERE id=?", (t["id"],))
            elif "晚安" in name or "晚" in name:
                conn.execute("UPDATE scheduled_tasks SET trigger_time='22:00' WHERE id=?", (t["id"],))
        conn.commit()
    except Exception:
        pass

    # 迁移：确保所有已启用任务 first_trigger 指向未来，防止启动即触发
    try:
        from datetime import datetime as _dt, timedelta as _td
        _now = _dt.now()
        _now_ts = _now.timestamp()
        # 1) daily/cron：补 first_trigger 或把过期 first_trigger 推进到下次
        for row in conn.execute(
            "SELECT id, task_type, trigger_time, first_trigger FROM scheduled_tasks "
            "WHERE task_type IN ('cron','daily') AND enabled=1"
        ).fetchall():
            try:
                _h, _m = map(int, row["trigger_time"].split(":"))
                _ft = _now.replace(hour=_h, minute=_m, second=0, microsecond=0)
                # 推进直到 first_trigger >= now（跨天可能性）
                while _ft.timestamp() <= _now_ts:
                    _ft += _td(days=1)
                conn.execute("UPDATE scheduled_tasks SET first_trigger=? WHERE id=?",
                             (_ft.isoformat(), row["id"]))
            except Exception:
                pass
        # 2) once：过期任务直接关闭（已错过）
        conn.execute(
            "UPDATE scheduled_tasks SET enabled=0 WHERE task_type='once' AND enabled=1 "
            "AND first_trigger!='' AND first_trigger < ?",
            (_now.isoformat(),))
        # 3) interval：补 first_trigger
        for row in conn.execute(
            "SELECT id, interval_hours FROM scheduled_tasks "
            "WHERE task_type='interval' AND enabled=1 "
            "AND (first_trigger='' OR first_trigger IS NULL)"
        ).fetchall():
            try:
                _ih = float(row["interval_hours"])
                if _ih > 0:
                    _ft = _now + _td(hours=_ih)
                    conn.execute("UPDATE scheduled_tasks SET first_trigger=? WHERE id=?",
                                 (_ft.isoformat(), row["id"]))
            except Exception:
                pass
        conn.commit()
    except Exception:
        pass

    # person_tags 表：多对多关系标签
    conn.execute("""
        CREATE TABLE IF NOT EXISTS person_tags (
            qq   TEXT NOT NULL,
            tag  TEXT NOT NULL,
            PRIMARY KEY (qq, tag)
        )
    """)
    conn.commit()

    # audit_log 表：审计日志（效果四）
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            action     TEXT NOT NULL,
            detail     TEXT DEFAULT '',
            operator   TEXT NOT NULL,
            target     TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()


# ── 名称解析（核心优先级逻辑）─────────────────────────────

def get_resolved_name(qq: str, group_id: str = "") -> str:
    """获取用户显示名称。单一权威解析入口 — 5 级回退链：
    1. nickname_slots 活跃槽位值（用户设置的权威称呼）
    2. user_profile.fixed_name（管理员设置的固定备注）
    3. temp_name_cache（群临时昵称，仅在传入 group_id 时查询）
    4. 返回 "" — 调用方继续回退到 NapCat 好友缓存 / QQ 末 4 位
    """
    conn = _get_conn()

    # Level 1: nickname_slots 活跃槽位
    row = conn.execute(
        "SELECT active_slot, slot_a_value, slot_b_value FROM nickname_slots WHERE qq = ?",
        (qq,),
    ).fetchone()
    if row:
        active = row["active_slot"]
        if active == "slot_a" and row["slot_a_value"]:
            return row["slot_a_value"]
        if active == "slot_b" and row["slot_b_value"]:
            return row["slot_b_value"]

    # Level 2: user_profile.fixed_name（管理员固定备注）
    prof = conn.execute(
        "SELECT fixed_name FROM user_profile WHERE qq = ?", (qq,)
    ).fetchone()
    if prof and prof["fixed_name"]:
        return prof["fixed_name"]

    # Level 3: temp_name_cache（群临时昵称，需 group_id）
    if group_id:
        tn = conn.execute(
            "SELECT temp_name FROM temp_name_cache WHERE group_id = ? AND user_qq = ?",
            (group_id, qq),
        ).fetchone()
        if tn and tn["temp_name"]:
            return tn["temp_name"]

    # Level 4: 空 — 调用方（_resolve_speaker_name）回退到 _friend_cache / QQ 末 4 位
    return ""


# ── 管理员操作 ────────────────────────────────────────────

def set_fixed_name(qq: str, name: str):
    """管理员设置全局固定备注。
    1. 写入/更新 user_profile.fixed_name
    2. 清空该用户全部群临时昵称缓存
    """
    now = datetime.now().isoformat()
    conn = _get_conn()
    conn.execute(
        """INSERT INTO user_profile (qq, fixed_name, last_update)
           VALUES (?, ?, ?)
           ON CONFLICT(qq) DO UPDATE SET
               fixed_name = excluded.fixed_name,
               last_update = excluded.last_update""",
        (qq, name, now),
    )
    conn.execute("DELETE FROM temp_name_cache WHERE user_qq = ?", (qq,))
    conn.commit()


def upsert_profile(qq: str, *, is_friend: int | None = None,
                   feature_summary: str | None = None,
                   tags: list[str] | None = None,
                   source_group: str | None = None):
    """写入或更新人物档案。仅更新传入的非 None 字段。
    - feature_summary：直接覆盖（不追加）
    - tags：合并去重追加
    - source_group：追加（逗号分隔）
    """
    conn = _get_conn()
    now = datetime.now().isoformat()
    existing = conn.execute("SELECT * FROM user_profile WHERE qq = ?", (qq,)).fetchone()

    if existing:
        updates = {"last_update": now}
        if is_friend is not None:
            updates["is_friend"] = is_friend
        if feature_summary is not None:
            updates["feature_summary"] = feature_summary
        if tags is not None:
            existing_tags_raw = existing["tag_list"] or ""
            existing_tags = []
            if existing_tags_raw:
                try:
                    existing_tags = json.loads(existing_tags_raw)
                except Exception:
                    existing_tags = []
            existing_set = set(existing_tags)
            combined = existing_tags + [t for t in tags if t not in existing_set]
            updates["tag_list"] = json.dumps(combined, ensure_ascii=False)
        if source_group is not None:
            existing_sg = existing["source_group"] or ""
            sg_set = set(s for s in existing_sg.split(",") if s)
            sg_set.add(source_group)
            updates["source_group"] = ",".join(sorted(sg_set))

        set_clause = ", ".join(f"{k} = :{k}" for k in updates)
        conn.execute(
            f"UPDATE user_profile SET {set_clause} WHERE qq = :qq",
            {**updates, "qq": qq},
        )
    else:
        conn.execute(
            """INSERT INTO user_profile (qq, fixed_name, is_friend, feature_summary,
                                         tag_list, source_group, last_update)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                qq,
                None,
                is_friend if is_friend is not None else 0,
                feature_summary,
                json.dumps(tags, ensure_ascii=False) if tags else None,
                source_group,
                now,
            ),
        )
    conn.commit()


# ── 临时名称 ──────────────────────────────────────────────

def upsert_temp_name(group_id: str, user_qq: str, temp_name: str):
    """更新群临时昵称缓存。如果 user_profile 已有 fixed_name 则跳过。"""
    if not group_id or group_id == "0":
        return
    conn = _get_conn()
    row = conn.execute(
        "SELECT fixed_name FROM user_profile WHERE qq = ?", (user_qq,)
    ).fetchone()
    if row and row["fixed_name"]:
        return  # 已有全局固定备注，不写入临时名称
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO temp_name_cache (group_id, user_qq, temp_name, update_time)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(group_id, user_qq) DO UPDATE SET
               temp_name = excluded.temp_name,
               update_time = excluded.update_time""",
        (group_id, user_qq, temp_name, now),
    )
    conn.commit()


# ── 查询 ──────────────────────────────────────────────────

def get_profile(qq: str) -> dict | None:
    """获取完整人物档案。返回 dict 或 None。tag_list 从 JSON 解析。"""
    row = _get_conn().execute(
        "SELECT * FROM user_profile WHERE qq = ?", (qq,)
    ).fetchone()
    if not row:
        return None
    result = dict(row)
    if result.get("tag_list"):
        try:
            result["tag_list_parsed"] = json.loads(result["tag_list"])
        except Exception:
            result["tag_list_parsed"] = []
    else:
        result["tag_list_parsed"] = []
    return result


def get_profiles_batch(qq_list: list[str]) -> dict[str, dict]:
    """批量查询档案。返回 {qq: profile_dict, ...}。"""
    if not qq_list:
        return {}
    conn = _get_conn()
    placeholders = ",".join("?" for _ in qq_list)
    rows = conn.execute(
        f"SELECT * FROM user_profile WHERE qq IN ({placeholders})",
        qq_list,
    ).fetchall()
    result = {}
    for row in rows:
        d = dict(row)
        if d.get("tag_list"):
            try:
                d["tag_list_parsed"] = json.loads(d["tag_list"])
            except Exception:
                d["tag_list_parsed"] = []
        else:
            d["tag_list_parsed"] = []
        result[d["qq"]] = d
    return result


# ── 聊天日志 ──────────────────────────────────────────────

def insert_chat_log(sender: str, bot_qq: str, group_id: str, msg: str):
    """写入全局聊天日志。"""
    _get_conn().execute(
        """INSERT INTO global_chat_log (sender, bot_qq, group_id, msg, send_time)
           VALUES (?, ?, ?, ?, ?)""",
        (sender, bot_qq, group_id or "0", msg, datetime.now().isoformat()),
    )
    _get_conn().commit()


def query_recent_chats(days: int = 7, limit: int = 100) -> list[dict]:
    """查询最近 N 天全部非机器人发送的聊天记录（跨私聊/群聊）。"""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    rows = _get_conn().execute(
        """SELECT sender, group_id, msg, send_time
           FROM global_chat_log
           WHERE sender != ? AND send_time >= ?
           ORDER BY send_time DESC
           LIMIT ?""",
        ("0", cutoff, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def query_chat_history_by_sender(sender_qq: str, days: int = 7,
                                 limit: int = 100) -> list[dict]:
    """查询指定发送者的聊天历史。"""
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    rows = _get_conn().execute(
        """SELECT sender, group_id, msg, send_time
           FROM global_chat_log
           WHERE sender = ? AND send_time >= ?
           ORDER BY send_time DESC
           LIMIT ?""",
        (sender_qq, cutoff, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def search_conversations(keyword: str, limit: int = 50) -> list[dict]:
    """全文搜索聊天记录（LIKE 模糊匹配）。"""
    rows = _get_conn().execute(
        """SELECT sender, group_id, msg, send_time
           FROM global_chat_log
           WHERE msg LIKE ?
           ORDER BY send_time DESC
           LIMIT ?""",
        (f"%{keyword}%", limit),
    ).fetchall()
    return [dict(r) for r in rows]


def count_messages_by_sender(sender_qq: str) -> tuple[int, int, str, str]:
    """返回 (收消息数, 发消息数, 首次联系时间, 末次联系时间)。"""
    conn = _get_conn()
    total_in = conn.execute(
        "SELECT COUNT(*) FROM global_chat_log WHERE sender = ?",
        (sender_qq,),
    ).fetchone()[0]

    total_out = conn.execute(
        "SELECT COUNT(*) FROM global_chat_log WHERE sender = ? AND group_id = '0'",
        ("0",),
    ).fetchone()[0]
    # 更精确：统计 bot 对该人发送的消息数（bot_qq = sender_qq 的某人，且 sender = bot）
    # 简化处理：从 JSON 统计，这里只提供 DB 能给的

    first = conn.execute(
        "SELECT send_time FROM global_chat_log WHERE sender = ? ORDER BY send_time ASC LIMIT 1",
        (sender_qq,),
    ).fetchone()
    first_time = first[0] if first else ""

    last = conn.execute(
        "SELECT send_time FROM global_chat_log WHERE sender = ? ORDER BY send_time DESC LIMIT 1",
        (sender_qq,),
    ).fetchone()
    last_time = last[0] if last else ""

    return (total_in, 0, first_time, last_time)


# ── 迁移：JSON → SQLite ───────────────────────────────────

def _is_migrated_v2() -> bool:
    """检查是否已完成 v2 迁移（JSON name → nickname_slots）。"""
    try:
        row = _get_conn().execute(
            "SELECT version FROM _migration WHERE version = 2"
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _is_migrated() -> bool:
    """检查是否已完成 v1 迁移。"""
    try:
        row = _get_conn().execute(
            "SELECT version FROM _migration WHERE version = 1"
        ).fetchone()
        return row is not None
    except Exception:
        return False


def migrate_json_to_db(data_dir: Path):
    """将现有 JSON 文件数据迁移到 SQLite。幂等（已迁移则跳过）。"""
    if _is_migrated():
        return

    from messaging import log as _log
    _log("开始数据迁移 JSON → SQLite ...")

    conn = _get_conn()
    profiles_created = 0
    chat_logs_inserted = 0
    seen_logs: set[tuple[str, str, str, str]] = set()

    # 先读一遍 global_chat_log 已有的消息做去重（如果部分导入过）
    try:
        for row in conn.execute("SELECT sender, bot_qq, group_id, send_time FROM global_chat_log"):
            seen_logs.add((row["sender"], row["bot_qq"], row["group_id"], row["send_time"]))
    except Exception:
        pass

    # 第一遍：处理 private_{QQ}.json
    for f in sorted(data_dir.glob("private_*.json")):
        qq = f.stem.split("_", 1)[1]
        try:
            mem = json.loads(f.read_text("utf-8"))
        except Exception:
            continue

        # 提取 fixed_name：优先级 称呼: > 名字: > mem["name"]
        fixed_name = None
        for n in mem.get("notes", []):
            note = n.get("note", "")
            if note.startswith("称呼:") and len(note) > 3:
                fixed_name = note[3:].strip()
                break
        if not fixed_name:
            for n in mem.get("notes", []):
                note = n.get("note", "")
                if note.startswith("名字:") and len(note) > 3:
                    fixed_name = note[3:].strip()
                    break
        if not fixed_name:
            fixed_name = mem.get("name", "").strip() or None

        # 提取 personality
        role = mem.get("role", {})
        feature_summary = role.get("personality", "").strip() or None
        if not feature_summary:
            feature_summary = role.get("性格", "").strip() or None

        # 提取关系标签
        tags = []
        for n in mem.get("notes", []):
            note = n.get("note", "")
            if note.startswith("关系:"):
                tag = note[3:].strip()
                if tag.endswith("(自动)") or tag.endswith("(AI分析)"):
                    tag = tag.split("(")[0].strip()
                if tag and tag not in tags:
                    tags.append(tag)

        # 写入 user_profile
        cur = conn.execute(
            """INSERT OR IGNORE INTO user_profile
               (qq, fixed_name, is_friend, feature_summary, tag_list, source_group, last_update)
               VALUES (?, ?, 1, ?, ?, NULL, ?)""",
            (
                qq,
                fixed_name,
                feature_summary,
                json.dumps(tags, ensure_ascii=False) if tags else None,
                datetime.now().isoformat(),
            ),
        )
        if cur.rowcount > 0:
            profiles_created += 1

        # 迁移 notes[] → 结构化列
        for n in mem.get("notes", []):
            note = n.get("note", "")
            if note.startswith("名字:") and len(note) > 3:
                val = note[3:].strip()
                if val:
                    conn.execute(
                        "UPDATE user_profile SET real_name=? WHERE qq=?",
                        (val, qq),
                    )
            elif note.startswith("关系:"):
                tag = note[3:].strip()
                if tag.endswith("(自动)") or tag.endswith("(AI分析)"):
                    tag = tag.rsplit("(")[0].strip()
                if tag:
                    conn.execute("INSERT OR IGNORE INTO person_tags (qq, tag) VALUES (?, ?)", (qq, tag))
            elif note.startswith("关键信息:"):
                fact = note[5:].strip()
                if fact.endswith("(AI分析)"):
                    fact = fact.rsplit("(")[0].strip()
                if fact:
                    conn.execute(
                        "INSERT OR IGNORE INTO person_tags (qq, tag) VALUES (?, ?)",
                        (qq, fact),
                    )

        # 新联系人标记（导入的不算新联系人）
        conn.execute(
            "INSERT INTO nickname_slots (qq, first_contact_done) VALUES (?, 1) ON CONFLICT(qq) DO UPDATE SET first_contact_done = 1",
            (qq,),
        )

        # 导入 conversation 到 global_chat_log
        for m in mem.get("conversation", []):
            t = m.get("time", "")
            content = m.get("content", "")
            direction = m.get("direction", "in")
            sender = qq if direction == "in" else "0"  # "0" 代表 bot
            bot_qq = "0" if direction == "in" else qq  # 简化
            log_key = (sender, "0", "0", t)
            if log_key not in seen_logs:
                conn.execute(
                    """INSERT INTO global_chat_log (sender, bot_qq, group_id, msg, send_time)
                       VALUES (?, ?, '0', ?, ?)""",
                    (sender, "0", content, _norm_time(t)),
                )
                seen_logs.add(log_key)
                chat_logs_inserted += 1

    # 第二遍：处理 group_{GID}_person_{QQ}.json
    for f in sorted(data_dir.glob("group_*_person_*.json")):
        parts = f.stem.split("_person_")
        if len(parts) != 2:
            continue
        gid_group = parts[0].replace("group_", "")
        qq = parts[1]
        try:
            mem = json.loads(f.read_text("utf-8"))
        except Exception:
            continue

        # 如果该用户尚无档案 → 创建空白档案
        row = conn.execute("SELECT qq FROM user_profile WHERE qq = ?", (qq,)).fetchone()
        if not row:
            cur = conn.execute(
                """INSERT OR IGNORE INTO user_profile
                   (qq, fixed_name, is_friend, feature_summary, tag_list, source_group, last_update)
                   VALUES (?, NULL, 0, NULL, NULL, ?, ?)""",
                (qq, gid_group, datetime.now().isoformat()),
            )
            if cur.rowcount > 0:
                profiles_created += 1
        else:
            # 追加 source_group
            conn.execute(
                "SELECT source_group FROM user_profile WHERE qq = ?", (qq,)
            )
            existing_sg = conn.execute(
                "SELECT source_group FROM user_profile WHERE qq = ?", (qq,)
            ).fetchone()
            sg_raw = existing_sg["source_group"] or "" if existing_sg else ""
            sg_set = set(s for s in sg_raw.split(",") if s)
            if gid_group not in sg_set:
                sg_set.add(gid_group)
                new_sg = ",".join(sorted(sg_set))
                conn.execute(
                    "UPDATE user_profile SET source_group = ? WHERE qq = ?",
                    (new_sg, qq),
                )

        # 如果没有 fixed_name，写入 temp_name_cache
        row2 = conn.execute(
            "SELECT fixed_name FROM user_profile WHERE qq = ?", (qq,)
        ).fetchone()
        if not (row2 and row2["fixed_name"]):
            name = mem.get("name", "").strip()
            if name:
                conn.execute(
                    """INSERT OR IGNORE INTO temp_name_cache
                       (group_id, user_qq, temp_name, update_time)
                       VALUES (?, ?, ?, ?)""",
                    (gid_group, qq, name, datetime.now().isoformat()),
                )

        # 导入 conversation 到 global_chat_log
        for m in mem.get("conversation", []):
            t = m.get("time", "")
            direction = m.get("direction", "in")
            sender = qq if direction == "in" else "0"
            log_key = (sender, "0", gid_group, t)
            if log_key not in seen_logs:
                conn.execute(
                    """INSERT INTO global_chat_log (sender, bot_qq, group_id, msg, send_time)
                       VALUES (?, ?, ?, ?, ?)""",
                    (sender, "0", gid_group, m.get("content", ""), _norm_time(t)),
                )
                seen_logs.add(log_key)
                chat_logs_inserted += 1

    # 标记迁移完成
    conn.execute(
        "INSERT OR IGNORE INTO _migration (version, done_at) VALUES (1, ?)",
        (datetime.now().isoformat(),),
    )
    conn.commit()

    if profiles_created or chat_logs_inserted:
        _log(f"数据迁移完成: {profiles_created} 条用户档案, {chat_logs_inserted} 条聊天记录")
    else:
        _log("数据迁移完成: 无需导入（数据库已是最新）")


def migrate_v2_consolidate_names(data_dir: Path):
    """v2 迁移: 将 JSON name 字段和 notes 中的称呼迁移到 nickname_slots。
    清理 JSON name 字段，建立 SQLite 为唯一权威来源。幂等。"""
    if _is_migrated_v2():
        return

    from messaging import log as _log
    import json as _json
    from datetime import datetime as _datetime

    _log("开始 v2 数据迁移: 名称统一到 nickname_slots ...")

    conn = _get_conn()
    migrated_count = 0

    for f in sorted(data_dir.glob("private_*.json")):
        qq = f.stem.split("_", 1)[1]
        try:
            mem = _json.loads(f.read_text("utf-8"))
        except Exception:
            continue

        # 检查是否已有 nickname_slots 条目（取所有列）
        existing = conn.execute(
            "SELECT * FROM nickname_slots WHERE qq = ?", (qq,)
        ).fetchone()

        json_name = (mem.get("name") or "").strip()
        notes_names = []
        for n in mem.get("notes", []):
            note = n.get("note", "")
            if note.startswith("称呼:"):
                raw_name = note[3:].strip()
                # 去掉分号后面的关系标签（如 "联系人乙；表妹" → "联系人乙"）
                for sep in ("；", ";"):
                    if sep in raw_name:
                        raw_name = raw_name.split(sep)[0].strip()
                if raw_name:
                    notes_names.append(raw_name)

        # 迁移逻辑: 如果 slots 为空但 JSON 有数据，创建槽位
        if not existing:
            candidate = json_name or (notes_names[0] if notes_names else "")
            if candidate:
                now = _datetime.now().isoformat()
                conn.execute(
                    """INSERT INTO nickname_slots
                       (qq, slot_a_value, slot_a_set_by, slot_a_set_time, slot_a_locked,
                        slot_b_value, slot_b_set_by, slot_b_set_time, slot_b_locked,
                        active_slot, last_switch_time)
                       VALUES (?, ?, 'migration', ?, 0, '', '', '', 0, 'slot_a', ?)""",
                    (qq, candidate, now, now),
                )
                migrated_count += 1
        else:
            # slots 已存在，但如果有 JSON notes 中的称呼未在 slots 中，补充
            slot_a = (existing["slot_a_value"] or "").strip()
            slot_b = (existing["slot_b_value"] or "").strip()
            for name in notes_names:
                if name and name not in (slot_a, slot_b):
                    # 放入空闲槽位
                    if not slot_a:
                        conn.execute(
                            "UPDATE nickname_slots SET slot_a_value=?, slot_a_set_by='migration', slot_a_set_time=? WHERE qq=?",
                            (name, _datetime.now().isoformat(), qq),
                        )
                        slot_a = name
                        migrated_count += 1
                    elif not slot_b:
                        conn.execute(
                            "UPDATE nickname_slots SET slot_b_value=?, slot_b_set_by='migration', slot_b_set_time=? WHERE qq=?",
                            (name, _datetime.now().isoformat(), qq),
                        )
                        slot_b = name
                        migrated_count += 1

        # 清理 JSON name 字段（数据已迁移到 SQLite）
        if json_name:
            try:
                mem["name"] = ""
                _json.dump(mem, open(str(f), "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            except Exception:
                pass

    conn.execute(
        "INSERT OR IGNORE INTO _migration (version, done_at) VALUES (2, ?)",
        (_datetime.now().isoformat(),),
    )
    conn.commit()

    if migrated_count > 0:
        _log(f"v2 名称迁移完成: {migrated_count} 条记录")
    else:
        _log("v2 名称迁移完成: 无需导入")


def _norm_time(t: str) -> str:
    """将 JSON 中的时间格式 'MM-DD HH:MM:SS' 标准化为 ISO 格式。
    如果是 ISO 格式则原样返回。"""
    t = t.strip()
    if "T" in t:
        return t
    try:
        now = datetime.now()
        dt = datetime.strptime(f"{now.year}-{t}", "%Y-%m-%d %H:%M:%S")
        return dt.isoformat()
    except Exception:
        return t


# ── 称呼插槽 ──────────────────────────────────────────────

def get_nickname_slots(qq: str) -> dict:
    """获取某人的完整称呼插槽数据，若无记录返回空默认结构。"""
    row = _get_conn().execute(
        "SELECT * FROM nickname_slots WHERE qq = ?", (qq,)
    ).fetchone()
    if not row:
        return {
            "qq": qq,
            "slot_a_value": "",  "slot_a_set_by": "",  "slot_a_set_time": "",  "slot_a_locked": 0,
            "slot_b_value": "",  "slot_b_set_by": "",  "slot_b_set_time": "",  "slot_b_locked": 0,
            "active_slot": "",   "relation": "",        "first_contact_done": 0, "last_switch_time": "",
        }
    return dict(row)


def _default_slot_data(qq: str) -> dict:
    return {
        "qq": qq,
        "slot_a_value": "",  "slot_a_set_by": "",  "slot_a_set_time": "",  "slot_a_locked": 0,
        "slot_b_value": "",  "slot_b_set_by": "",  "slot_b_set_time": "",  "slot_b_locked": 0,
        "active_slot": "",   "relation": "",        "first_contact_done": 0, "last_switch_time": "",
    }


def save_nickname_slots(qq: str, data: dict):
    """完整覆盖保存称呼插槽数据。"""
    conn = _get_conn()
    conn.execute(
        """INSERT INTO nickname_slots
           (qq, slot_a_value, slot_a_set_by, slot_a_set_time, slot_a_locked,
            slot_b_value, slot_b_set_by, slot_b_set_time, slot_b_locked,
            active_slot, relation, first_contact_done, last_switch_time)
           VALUES (?,?,?,?,?, ?,?,?,?, ?,?,?,?)
           ON CONFLICT(qq) DO UPDATE SET
               slot_a_value=excluded.slot_a_value, slot_a_set_by=excluded.slot_a_set_by,
               slot_a_set_time=excluded.slot_a_set_time, slot_a_locked=excluded.slot_a_locked,
               slot_b_value=excluded.slot_b_value, slot_b_set_by=excluded.slot_b_set_by,
               slot_b_set_time=excluded.slot_b_set_time, slot_b_locked=excluded.slot_b_locked,
               active_slot=excluded.active_slot, relation=excluded.relation,
               first_contact_done=excluded.first_contact_done,
               last_switch_time=excluded.last_switch_time""",
        (qq,
         data["slot_a_value"], data["slot_a_set_by"], data["slot_a_set_time"], data["slot_a_locked"],
         data["slot_b_value"], data["slot_b_set_by"], data["slot_b_set_time"], data["slot_b_locked"],
         data["active_slot"], data["relation"], data["first_contact_done"], data["last_switch_time"]),
    )
    conn.commit()


def set_active_slot(qq: str, slot: str, now_iso: str = ""):
    """切换活跃称呼位并更新切换时间。"""
    conn = _get_conn()
    conn.execute(
        """INSERT INTO nickname_slots (qq, active_slot, last_switch_time)
           VALUES (?, ?, ?)
           ON CONFLICT(qq) DO UPDATE SET
               active_slot = excluded.active_slot,
               last_switch_time = excluded.last_switch_time""",
        (qq, slot, now_iso or datetime.now().isoformat()),
    )
    conn.commit()


def set_first_contact_done(qq: str):
    """标记首次对话已完成。"""
    conn = _get_conn()
    conn.execute(
        """INSERT INTO nickname_slots (qq, first_contact_done)
           VALUES (?, 1)
           ON CONFLICT(qq) DO UPDATE SET first_contact_done = 1""",
        (qq,),
    )
    conn.commit()


def set_relation(qq: str, relation: str):
    """写入关系标签。"""
    conn = _get_conn()
    conn.execute(
        """INSERT INTO nickname_slots (qq, relation)
           VALUES (?, ?)
           ON CONFLICT(qq) DO UPDATE SET relation = excluded.relation""",
        (qq, relation),
    )
    conn.commit()


def get_all_nickname_rows() -> list[dict]:
    """返回所有有称呼数据的人（用于定时轮换）。"""
    rows = _get_conn().execute(
        "SELECT * FROM nickname_slots WHERE active_slot != ''"
    ).fetchall()
    return [dict(r) for r in rows]


def set_person_city(qq: str, city: str, set_by: str = ""):
    """设置某人的所在城市"""
    conn = _get_conn()
    now = datetime.now().isoformat()
    existing = conn.execute("SELECT qq FROM user_profile WHERE qq = ?", (qq,)).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO user_profile (qq, city, city_set_by, city_set_time, last_update) VALUES (?, ?, ?, ?, ?)",
            (qq, city, set_by, now, now),
        )
    else:
        conn.execute(
            "UPDATE user_profile SET city=?, city_set_by=?, city_set_time=?, last_update=? WHERE qq=?",
            (city, set_by, now, now, qq),
        )
    conn.commit()


def get_person_city(qq: str) -> str | None:
    """获取某人的城市，未设置返回 None"""
    row = _get_conn().execute("SELECT city FROM user_profile WHERE qq = ?", (qq,)).fetchone()
    if row and row["city"]:
        return row["city"]
    return None


def get_all_cities() -> list[dict]:
    """获取所有设置了城市的人"""
    rows = _get_conn().execute(
        "SELECT qq, fixed_name, city FROM user_profile WHERE city IS NOT NULL AND city != ''"
    ).fetchall()
    return [{"qq": r["qq"], "fixed_name": r["fixed_name"] or "", "city": r["city"]} for r in rows]


def get_all_contacts(friend_cache: dict | None = None) -> list[dict]:
    """获取所有联系人（不限城市）。用于广播/个性化广播定时任务。
    传入 friend_cache 时按真实好友列表过滤，排除残留无效账号。
    """
    rows = _get_conn().execute(
        "SELECT qq, fixed_name, city FROM user_profile"
    ).fetchall()
    contacts = [{"qq": r["qq"], "fixed_name": r["fixed_name"] or "", "city": r["city"] or ""} for r in rows]
    if friend_cache is not None:
        contacts = [c for c in contacts if c["qq"] in friend_cache]
    return contacts


def upsert_user_summary(qq: str, summary: str):
    """写入或更新用户画像摘要。覆盖已有摘要。"""
    conn = _get_conn()
    now = datetime.now().isoformat()
    existing = conn.execute("SELECT qq FROM user_profile WHERE qq = ?", (qq,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE user_profile SET user_summary=?, last_update=? WHERE qq=?",
            (summary, now, qq),
        )
    else:
        conn.execute(
            "INSERT INTO user_profile (qq, user_summary, last_update) VALUES (?, ?, ?)",
            (qq, summary, now),
        )
    conn.commit()


def get_user_summary(qq: str) -> str:
    """获取用户画像摘要，未设置返回空字符串"""
    row = _get_conn().execute("SELECT user_summary FROM user_profile WHERE qq = ?", (qq,)).fetchone()
    return (row["user_summary"] or "") if row else ""


# ── Person Tags（多对多关系标签）───────────────────────────

def set_person_tags(qq: str, tags: list[str]):
    """批量设置某人的关系标签（全量替换）。"""
    conn = _get_conn()
    conn.execute("DELETE FROM person_tags WHERE qq = ?", (qq,))
    for tag in tags:
        tag = tag.strip()
        if tag and not tag.endswith("(AI分析)") and not tag.endswith("(自动)"):
            conn.execute("INSERT OR IGNORE INTO person_tags (qq, tag) VALUES (?, ?)", (qq, tag))
    conn.commit()


def add_person_tag(qq: str, tag: str):
    """追加一个关系标签（去重）。"""
    tag = tag.strip().replace("(AI分析)", "").replace("(自动)", "").replace("(推测)", "")
    if tag:
        conn = _get_conn()
        conn.execute("INSERT OR IGNORE INTO person_tags (qq, tag) VALUES (?, ?)", (qq, tag))
        conn.commit()


def remove_person_tag(qq: str, tag: str):
    """删除某人的一个关系标签。"""
    conn = _get_conn()
    conn.execute("DELETE FROM person_tags WHERE qq = ? AND tag = ?", (qq, tag.strip()))
    conn.commit()


def get_person_tags(qq: str) -> list[str]:
    """获取某人的所有关系标签。"""
    rows = _get_conn().execute("SELECT tag FROM person_tags WHERE qq = ?", (qq,)).fetchall()
    return [r["tag"] for r in rows]


def get_all_tags() -> dict[str, list[str]]:
    """获取所有关系标签的倒排索引。返回 {tag: [qq, ...]}。"""
    rows = _get_conn().execute("SELECT qq, tag FROM person_tags ORDER BY tag").fetchall()
    index: dict[str, list[str]] = {}
    for r in rows:
        index.setdefault(r["tag"], []).append(r["qq"])
    return index


def build_relation_index() -> dict[str, list[tuple[str, str]]]:
    """构建关系→联系人索引。返回 {tag: [(name, qq), ...]}。"""
    from db import get_resolved_name
    rows = _get_conn().execute("""
        SELECT pt.tag, pt.qq, up.fixed_name
        FROM person_tags pt
        LEFT JOIN user_profile up ON pt.qq = up.qq
        ORDER BY pt.tag
    """).fetchall()
    index: dict[str, list[tuple[str, str]]] = {}
    for r in rows:
        name = get_resolved_name(r["qq"]) or r["fixed_name"] or ""
        if not name:
            continue
        index.setdefault(r["tag"], []).append((name, r["qq"]))
    return index


# ── 结构化备注（替代 JSON notes[]）────────────────────────

def upsert_person_real_name(qq: str, real_name: str):
    """设置真实姓名。"""
    conn = _get_conn()
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO user_profile (qq, real_name, last_update)
           VALUES (?, ?, ?)
           ON CONFLICT(qq) DO UPDATE SET real_name=excluded.real_name, last_update=excluded.last_update""",
        (qq, real_name, now),
    )
    conn.commit()


def get_person_real_name(qq: str) -> str:
    row = _get_conn().execute("SELECT real_name FROM user_profile WHERE qq = ?", (qq,)).fetchone()
    return (row["real_name"] or "") if row else ""


def add_key_fact(qq: str, fact: str):
    """追加一条关键信息。"""
    conn = _get_conn()
    row = conn.execute("SELECT key_facts FROM user_profile WHERE qq = ?", (qq,)).fetchone()
    facts = []
    if row and row["key_facts"]:
        try:
            facts = json.loads(row["key_facts"])
        except Exception:
            facts = []
    if fact not in facts:
        facts.append(fact)
    conn.execute(
        """INSERT INTO user_profile (qq, key_facts, last_update)
           VALUES (?, ?, ?)
           ON CONFLICT(qq) DO UPDATE SET key_facts=excluded.key_facts, last_update=excluded.last_update""",
        (qq, json.dumps(facts, ensure_ascii=False), datetime.now().isoformat()),
    )
    conn.commit()


def get_key_facts(qq: str) -> list[str]:
    row = _get_conn().execute("SELECT key_facts FROM user_profile WHERE qq = ?", (qq,)).fetchone()
    if row and row["key_facts"]:
        try:
            return json.loads(row["key_facts"])
        except Exception:
            pass
    return []


def set_free_notes(qq: str, notes_text: str):
    """设置自由格式备注。"""
    conn = _get_conn()
    conn.execute(
        """INSERT INTO user_profile (qq, free_notes, last_update)
           VALUES (?, ?, ?)
           ON CONFLICT(qq) DO UPDATE SET free_notes=excluded.free_notes, last_update=excluded.last_update""",
        (qq, notes_text, datetime.now().isoformat()),
    )
    conn.commit()


def get_free_notes(qq: str) -> str:
    row = _get_conn().execute("SELECT free_notes FROM user_profile WHERE qq = ?", (qq,)).fetchone()
    return (row["free_notes"] or "") if row else ""


# ── 定时任务 ────────────────────────────────────────────────

def add_scheduled_task(
    name: str, task_type: str, trigger_time: str, interval_hours: int,
    target_type: str, target_id: str, content: str, created_by: str,
    action_type: str = "ai_generate",
) -> str:
    """添加一条定时任务，返回 task_id。首次触发时间（first_trigger）自动计算：
    - daily: 今天的 trigger_time（若未过）或明天（若已过）
    - interval: 当前时间 + interval_hours
    - once: trigger_time 本身
    这样新建任务不会因 trigger_time 已过而立刻触发。
    """
    import uuid
    now_dt = datetime.now()
    now = now_dt.isoformat()
    task_id = f"task_{uuid.uuid4().hex[:8]}"
    # 计算正确的首次触发时间戳
    first_trigger = trigger_time  # once / static 直接用 trigger_time
    if task_type == "daily":
        # 今天的触发时刻
        try:
            h, m = map(int, trigger_time.split(":"))
            today_trigger = now_dt.replace(hour=h, minute=m, second=0, microsecond=0)
            if today_trigger <= now_dt:
                # 今天已过 → 首次触发为明天
                from datetime import timedelta
                today_trigger += timedelta(days=1)
            first_trigger = today_trigger.isoformat()
        except Exception:
            first_trigger = now
    elif task_type == "interval":
        from datetime import timedelta
        first_trigger = (now_dt + timedelta(hours=interval_hours)).isoformat()
    conn = _get_conn()
    conn.execute(
        """INSERT INTO scheduled_tasks
           (id, name, task_type, trigger_time, interval_hours,
            target_type, target_id, content, created_by, enabled, last_run, created_at, action_type, first_trigger)
           VALUES (?,?,?,?,?, ?,?,?,?, 1, '', ?, ?, ?)""",
        (task_id, name, task_type, trigger_time, interval_hours,
         target_type, target_id, content, created_by, now, action_type, first_trigger),
    )
    conn.commit()
    return task_id


def get_scheduled_tasks(enabled_only: bool = False) -> list[dict]:
    """获取所有定时任务列表。"""
    sql = "SELECT * FROM scheduled_tasks"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY created_at DESC"
    rows = _get_conn().execute(sql).fetchall()
    return [dict(r) for r in rows]


def get_scheduled_task(task_id: str) -> dict | None:
    """获取单个定时任务。"""
    row = _get_conn().execute(
        "SELECT * FROM scheduled_tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return dict(row) if row else None


def delete_scheduled_task(task_id: str) -> bool:
    """删除定时任务，返回是否成功。"""
    conn = _get_conn()
    cur = conn.execute("DELETE FROM scheduled_tasks WHERE id = ?", (task_id,))
    conn.commit()
    return cur.rowcount > 0


def toggle_scheduled_task(task_id: str, enabled: bool) -> bool:
    """启用/停用定时任务，返回是否成功。"""
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE scheduled_tasks SET enabled = ? WHERE id = ?",
        (1 if enabled else 0, task_id),
    )
    conn.commit()
    return cur.rowcount > 0


def update_task_last_run(task_id: str, last_run: str) -> bool:
    """更新任务上次执行时间。"""
    conn = _get_conn()
    cur = conn.execute(
        "UPDATE scheduled_tasks SET last_run = ? WHERE id = ?",
        (last_run, task_id),
    )
    conn.commit()
    return cur.rowcount > 0


def get_due_tasks(now_ts: float, today: str) -> list[dict]:
    """获取到期待执行的定时任务。
    统一逻辑：所有 task_type，now_ts >= first_trigger 即为到期。
    不再用 last_run 做调度判断、不再做 now_hm 字符串比较。
    first_trigger 缺失的跳过（init_db 迁移负责补齐）。
    """
    all_tasks = get_scheduled_tasks(enabled_only=True)
    due = []
    for t in all_tasks:
        # 新建任务冷却 60 秒，防止添加后立即触发
        created_at = t.get("created_at", "")
        if created_at:
            try:
                if (now_ts - _parse_time_to_ts(created_at)) < 60:
                    continue
            except Exception:
                pass
        first_trigger = t.get("first_trigger", "")
        if not first_trigger:
            continue  # 无 first_trigger → 等待 init_db 迁移修复
        ft_ts = _parse_time_to_ts(first_trigger)
        if ft_ts and now_ts >= ft_ts:
            due.append(t)
    return due


def _parse_time_to_ts(time_str: str) -> float | None:
    """将 ISO 时间字符串解析为时间戳。"""
    if not time_str:
        return None
    try:
        from datetime import datetime as dt
        return dt.fromisoformat(time_str).timestamp()
    except Exception:
        return None


# ── Phase 6: 数据完整性验证 ────────────────────────────────

def verify_data_integrity() -> list[str]:
    """启动时运行一致性检查，返回警告列表。"""
    warnings = []
    conn = _get_conn()

    # 1. nickname_slots 有 active_slot 但对应槽位为空
    rows = conn.execute(
        "SELECT qq, active_slot, slot_a_value, slot_b_value FROM nickname_slots"
    ).fetchall()
    for row in rows:
        active = row["active_slot"]
        if active == "slot_a" and not (row["slot_a_value"] or "").strip():
            warnings.append(f"⚠ nickname_slots: {row['qq']} active=slot_a 但 slot_a 为空")
        if active == "slot_b" and not (row["slot_b_value"] or "").strip():
            warnings.append(f"⚠ nickname_slots: {row['qq']} active=slot_b 但 slot_b 为空")

    # 2. person_tags 孤儿标签（QQ 不在任何表中）
    tags = conn.execute("SELECT DISTINCT qq FROM person_tags").fetchall()
    for t in tags:
        qq = t["qq"]
        exists = conn.execute(
            "SELECT 1 FROM nickname_slots WHERE qq=? UNION ALL SELECT 1 FROM user_profile WHERE qq=? LIMIT 1",
            (qq, qq),
        ).fetchone()
        if not exists:
            warnings.append(f"⚠ person_tags: {qq} 有标签但不在 nickname_slots/user_profile 中")

    # 3. scheduled_tasks 启用的任务有无效 target
    tasks = conn.execute(
        "SELECT id, name, target_type, target_id FROM scheduled_tasks WHERE enabled=1"
    ).fetchall()
    for t in tasks:
        if t["target_type"] not in ("private", "group"):
            warnings.append(f"⚠ scheduled_tasks: {t['id']} '{t['name']}' target_type={t['target_type']} 无效")

    # 4. temp_name_cache 条目无对应 nickname_slots 或 user_profile 记录
    tn_rows = conn.execute("SELECT DISTINCT user_qq FROM temp_name_cache").fetchall()
    for tn in tn_rows:
        qq = tn["user_qq"]
        exists = conn.execute(
            "SELECT 1 FROM nickname_slots WHERE qq=? UNION ALL SELECT 1 FROM user_profile WHERE qq=? LIMIT 1",
            (qq, qq),
        ).fetchone()
        if not exists:
            warnings.append(f"⚠ temp_name_cache: {qq} 有缓存但不在 nickname_slots/user_profile 中")

    # 5. user_profile 孤立条目（不在好友缓存 + 无有效数据）
    try:
        from config import _friend_cache
        friend_qqs = set(_friend_cache.keys())
    except Exception:
        friend_qqs = None
    if friend_qqs:
        for row in conn.execute("SELECT qq, fixed_name, city, feature_summary FROM user_profile").fetchall():
            qq = row["qq"]
            if qq in friend_qqs:
                continue
            # 有有效数据 → 保留（可能是你认识的人但 NapCat 缓存未加载）
            if (row["fixed_name"] or "").strip() or (row["city"] or "").strip() or (row["feature_summary"] or "").strip():
                continue
            warnings.append(f"⚠ user_profile: {qq} 无好友关系且无有效数据（建议清理）")

    return warnings


# ── Phase 7: 孤立 profile 清理 ──────────────────────────────

def cleanup_orphan_profiles(friend_qqs: set[str]) -> int:
    """删除不在好友列表中且无有效数据/无聊天记录的 user_profile + nickname_slots + person_tags。

    安全保底：friend_qqs 为空（WS 未连接）时不执行任何删除，避免误删。
    有效数据定义：fixed_name / city / feature_summary / real_name / key_facts 任一非空 → 保留。
    有聊天记录（global_chat_log）也 → 保留。
    """
    # 保底：好友列表为空 → 不清理（避免独立运行时误删）
    if not friend_qqs:
        return 0

    conn = _get_conn()
    try:
        # 单次查询找出孤立、无数据、无聊天记录的 QQ
        # 条件：不在 friend_qqs + 无有效字段 + global_chat_log 中无 sender 记录
        candidates = [
            row["qq"]
            for row in conn.execute(
                """SELECT p.qq FROM user_profile p
                   LEFT JOIN (SELECT DISTINCT sender FROM global_chat_log) g ON p.qq = g.sender
                   WHERE p.qq NOT IN ({friends})
                     AND g.sender IS NULL
                     AND (p.fixed_name IS NULL OR p.fixed_name = '')
                     AND (p.city IS NULL OR p.city = '')
                     AND (p.feature_summary IS NULL OR p.feature_summary = '')
                     AND (p.real_name IS NULL OR p.real_name = '')
                     AND (p.key_facts IS NULL OR p.key_facts = '')
                """.format(friends=",".join("?" * len(friend_qqs))),
                list(friend_qqs),
            ).fetchall()
        ]

        if not candidates:
            return 0

        placeholders = ",".join("?" * len(candidates))
        conn.execute(f"DELETE FROM nickname_slots WHERE qq IN ({placeholders})", candidates)
        conn.execute(f"DELETE FROM person_tags WHERE qq IN ({placeholders})", candidates)
        conn.execute(f"DELETE FROM temp_name_cache WHERE user_qq IN ({placeholders})", candidates)
        conn.execute(f"DELETE FROM user_profile WHERE qq IN ({placeholders})", candidates)
        conn.commit()
        return len(candidates)
    except Exception:
        return 0


# ── Phase 3.3: schedule / todo / auto_reply_rules CRUD ──

def add_schedule(qq: str, title: str, when_time: str, description: str = "") -> str:
    import uuid
    sid = f"sch_{uuid.uuid4().hex[:8]}"
    _get_conn().execute(
        "INSERT INTO schedule (id, qq, title, when_time, description, created_at) VALUES (?,?,?,?,?,?)",
        (sid, qq, title, when_time, description, datetime.now().isoformat()))
    _get_conn().commit()
    return sid

def get_schedules(qq: str) -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM schedule WHERE qq=? ORDER BY when_time ASC", (qq,)).fetchall()
    return [dict(r) for r in rows]

def update_schedule(qq: str, sid: str, **kwargs) -> bool:
    # 简化：不支持
    return False

def delete_schedule(qq: str, sid: str) -> bool:
    cur = _get_conn().execute("DELETE FROM schedule WHERE id=? AND qq=?", (sid, qq))
    _get_conn().commit()
    return cur.rowcount > 0

def add_todo(qq: str, title: str) -> str:
    import uuid
    tid = f"todo_{uuid.uuid4().hex[:8]}"
    _get_conn().execute(
        "INSERT INTO todo (id, qq, title, created_at) VALUES (?,?,?,?)",
        (tid, qq, title, datetime.now().isoformat()))
    _get_conn().commit()
    return tid

def get_todos(qq: str) -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM todo WHERE qq=? ORDER BY created_at ASC", (qq,)).fetchall()
    return [dict(r) for r in rows]

def complete_todo(qq: str, tid: str) -> bool:
    cur = _get_conn().execute("UPDATE todo SET done=1 WHERE id=? AND qq=?", (tid, qq))
    _get_conn().commit()
    return cur.rowcount > 0

def delete_todo(qq: str, tid: str) -> bool:
    cur = _get_conn().execute("DELETE FROM todo WHERE id=? AND qq=?", (tid, qq))
    _get_conn().commit()
    return cur.rowcount > 0

def add_auto_reply_rule(qq: str, trigger: str, response: str) -> str:
    import uuid
    rid = f"ar_{uuid.uuid4().hex[:8]}"
    _get_conn().execute(
        "INSERT INTO auto_reply_rules (id, qq, trigger, response, created_at) VALUES (?,?,?,?,?)",
        (rid, qq, trigger, response, datetime.now().isoformat()))
    _get_conn().commit()
    return rid

def get_auto_reply_rules(qq: str) -> list[dict]:
    rows = _get_conn().execute(
        "SELECT * FROM auto_reply_rules WHERE qq=? AND enabled=1 ORDER BY created_at ASC", (qq,)).fetchall()
    return [dict(r) for r in rows]


# ── 审计日志（效果四）──────────────────────────────────────

def log_audit(action: str, operator: str, detail: str = "", target: str = ""):
    """记录敏感操作到审计日志。operator=操作者QQ，target=被操作对象。"""
    try:
        _get_conn().execute(
            "INSERT INTO audit_log (action, detail, operator, target, created_at) VALUES (?,?,?,?,?)",
            (action, detail, operator, target, datetime.now().isoformat()))
        _get_conn().commit()
    except Exception:
        pass  # 审计失败不影响主流程


def get_audit_log(limit: int = 50) -> list[dict]:
    """查询最近 N 条审计记录。"""
    rows = _get_conn().execute(
        "SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
