#!/usr/bin/env python3
"""QQ Bot AI 工具与路由层 — TOOLS 定义、_execute_tool、AI 调用、搜索、总结"""
import json, os, re, time, random
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from retry import with_retry
from agent import Agent

from messaging import log, _send_text, _send_image, _send_file, _api
from config import (
    DEEPSEEK_BASE_URL, DEEPSEEK_API_KEY, DEEPSEEK_MODEL,
    ZHIPU_BASE_URL, ZHIPU_API_KEY,
    DATA_DIR, SELF_QQ, SELF_NICK,
    _friend_cache, _group_cache,
    _resolve_targets, _resolve_speaker_name,
    _load_memory, _save_memory, _get_person_profile, _get_person_index, _build_relation_index,
    _extract_relation, _sync_person_bidirectional, _validate_note, _invalidate_person_index,
    _upsert_person_note, _get_memory_lock, _apply_relation_to_role,
    _refresh_contact_cache, _get_contact_summary,
    _get_master_ids, _owner_add, _owner_remove,
    _is_chat_muted, _set_chat_muted,
    _set_nickname, _switch_active_slot, _get_nickname_data,
)
from db import (
    add_scheduled_task, get_scheduled_tasks, get_scheduled_task,
    delete_scheduled_task, toggle_scheduled_task, update_task_last_run,
    set_person_city, get_person_city, get_all_cities,
    add_person_tag, get_person_tags, upsert_person_real_name,
    get_person_real_name, add_key_fact, get_key_facts,
    set_free_notes, get_free_notes,
)
from games import _get_game_id

# Lazy import for game funcs to avoid circular dependency
def _start_game(game_id, game_type, sender_id, chat_id, msg_type):
    from games import _start_game as fn; return fn(game_id, game_type, sender_id, chat_id, msg_type)
def _handle_game_action(game_id, sender_id, action):
    from games import _handle_game_action as fn; return fn(game_id, sender_id, action)
def _end_game(game_id):
    from games import _end_game as fn; return fn(game_id)
def _game_status_text(game_id):
    from games import _game_status_text as fn; return fn(game_id)

# ==================== 通知辅助 ====================

def _notify_user(qq: str, message: str, operator: str = "系统") -> None:
    """向指定 QQ 发送详细通知（含操作人 + 具体变更内容）。"""
    try:
        if not qq or qq == "0":
            return
        from config import _get_master_ids
        masters = _get_master_ids()
        op_label = "管理员" if operator in masters else "你"
        formatted = (
            f"📢 【基本信息更新通知】\n"
            f"{message}\n"
            f"操作人：{op_label}\n"
            f"如有疑问请随时问我哦~"
        )
        _send_text("private", qq, formatted)
    except Exception as e:
        from messaging import log
        log(f"[NOTIFY] 通知 {qq} 失败: {e}")


# ==================== AI 调用 ====================

@with_retry(max_retries=1, base_delay=2.0, backoff=2.0,
            retryable_exceptions=(ConnectionError, TimeoutError))
def _call_deepseek(system: str, user: str, max_tokens: int = 800) -> str | None:
    try:
        import httpx
        resp = httpx.post(
            f"{DEEPSEEK_BASE_URL}/v1/chat/completions",
            json={
                "model": DEEPSEEK_MODEL,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        result = resp.json()
        for choice in result.get("choices", []):
            msg = choice.get("message", {})
            if msg.get("content"):
                return msg["content"]
        return result.get("choices", [{}])[0].get("message", {}).get("content")
    except Exception as e:
        log(f"AI call failed: {e}")
    return None


# ==================== 消息润色 & 人物分析 ====================

def _polish_message(original: str, recipients: list[str],
                    sender_nick: str = "") -> str:
    """用 AI 润色消息。保持原意，优化表达。出错时返回原文。"""
    if not original.strip():
        return original
    try:
        rcpt_str = "、".join(recipients) if recipients else "未知"
        system = (
            "你是消息润色助手。帮主人把消息润色得更得体、清晰。"
            "规则：1.保持原意，不改事实 2.修正错别字、优化语气 "
            "3.根据收信人身份调整称呼和礼貌程度 "
            "4.输出纯润色后的消息，不要加任何解释 5.如果原文已经很好了，原样返回。"
            "6.如果是群发（收信人≥2），每个人是单独收到的，绝对不要在消息开头列出收信人名字、不要加称呼列表。"
        )
        user = (
            f"主人「{sender_nick}」要对以下收信人说：\n"
            f"收信人：{rcpt_str}\n"
            f"原文：{original}\n\n"
            f"请润色后输出："
        )
        polished = _call_deepseek(system, user, max_tokens=600)
        return polished.strip() if polished else original
    except Exception as e:
        log(f"润色失败: {e}")
        return original


def _generate_person_report(their_msgs: list, my_msgs: list, name: str, notes: list,
                              relation: str = "") -> str:
    """用 AI 分析一个人的聊天记录，生成性格报告"""
    try:
        their_sample = "\n".join([f"- {m[:200]}" for m in their_msgs[-30:]])
        my_sample = "\n".join([f"- {m[:200]}" for m in my_msgs[-10:]])
        notes_excl_relation = [n for n in notes if not n.startswith("关系:")]
        notes_summary = "; ".join(notes_excl_relation) if notes_excl_relation else "无"

        rel_line = f"此人与主人的关系：{relation}\n" if relation else ""
        system = "你是人物分析助手。根据聊天记录分析一个人的性格、情绪状态、与主人的关系。用简洁的中文报告，200字以内。如果有关系统标签，务必在报告中明确提及。"
        user = f"""人物：{name}
{rel_line}备注信息：{notes_summary}

TA的最近发言（抽样）：
{their_sample}

你对TA的回复（抽样）：
{my_sample}

请分析TA的性格特点、目前的情绪状态、与主人的互动模式。用自然对话的语气报告，像在跟主人汇报。"""
        return _call_deepseek(system, user, max_tokens=400) or f"无法生成 {name} 的报告"
    except Exception as e:
        return f"分析失败: {e}"


# ==================== AI 动态信息提取 ====================

import threading

_pending_analyses: set[str] = set()
_analysis_lock = threading.Lock()


def _schedule_person_analysis(qq: str):
    """安排某人进行 AI 后台分析（daemon 线程执行）。
    防重入：同一 QQ 号同时只跑一个分析。失败静默降级。"""
    with _analysis_lock:
        if qq in _pending_analyses:
            return
        _pending_analyses.add(qq)

    def _run():
        try:
            extraction = _ai_extract_person_info(qq)
            if extraction:
                _persist_ai_extraction(qq, extraction)
        except Exception:
            pass
        finally:
            with _analysis_lock:
                _pending_analyses.discard(qq)

    t = threading.Thread(target=_run, daemon=True)
    t.start()


def _ai_extract_person_info(qq: str) -> dict | None:
    """用 AI 从最近对话中提取人物信息。
    返回结构化 dict 或 None（失败时）。"""
    mem_key = f"private_{qq}"
    mem = _load_memory(mem_key)
    conv = mem.get("conversation", [])
    if len(conv) < 10:
        return None

    recent = conv[-50:]
    their_msgs = [m["content"] for m in recent if m.get("direction") == "in"][-30:]
    my_msgs = [m["content"] for m in recent if m.get("direction") == "out"][-10:]

    their_sample = "\n".join([f"- {m[:200]}" for m in their_msgs])
    my_sample = "\n".join([f"- {m[:200]}" for m in my_msgs])

    existing_notes = [n.get("note", "") for n in mem.get("notes", [])]
    notes_summary = "; ".join(existing_notes) if existing_notes else "无"
    current_name = mem.get("name", "未知")

    system = """你是人物信息提取助手。分析对话记录，提取关于对方的关键信息。
只提取高置信度(>=80%)的信息。宁可漏报，不可误报。

输出JSON格式:
{
  "称呼": {"value": "对方喜欢的称呼或null", "confidence": 0.0},
  "名字": {"value": "对方的真实姓名或null", "confidence": 0.0},
  "关系": [{"value": "与主人的关系", "confidence": 0.0}],
  "关键信息": [{"value": "值得记录的长期事实", "confidence": 0.0}],
  "性格特征": "一段简洁的性格描述，或空字符串"
}

重要规则:
1. 称呼和名字必须是对方明确提出的，不要猜测
2. 关系基于对方说的话推断，但必须明确
3. 关键信息需要是有价值的长期事实（如职业、所在城市、兴趣爱好）
4. 性格特征需基于多次对话观察，总结对方的语气和互动风格
5. 如果信息不确定，confidence给0.5以下，系统会自动过滤"""

    user = f"""当前记录:
- 已知称呼: {current_name}
- 已知备注: {notes_summary}

对方最近发言:
{their_sample}

机器人的回复:
{my_sample}

请提取关于此人的信息(JSON):"""

    try:
        import httpx
        resp = httpx.post(
            f"{DEEPSEEK_BASE_URL}/v1/chat/completions",
            json={
                "model": DEEPSEEK_MODEL,
                "max_tokens": 500,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "response_format": {"type": "json_object"},
            },
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        result = resp.json()
        content = result["choices"][0]["message"]["content"]
        return json.loads(content)
    except Exception:
        return None


def _persist_ai_extraction(qq: str, extraction: dict):
    """将 AI 提取的结果持久化到 data 文件和 SQLite。
    高置信度阈值 0.8，AI 来源不覆盖手动数据。"""
    CONFIDENCE_THRESHOLD = 0.8
    mem_key = f"private_{qq}"
    lock = _get_memory_lock(mem_key)

    with lock:
        mem = _load_memory(mem_key)
        mem.setdefault("role", {})
        changed = False

        # 名字 — SQLite upsert + JSON fallback
        name_ext = extraction.get("名字", {})
        if name_ext.get("value") and name_ext.get("confidence", 0) >= CONFIDENCE_THRESHOLD:
            val = name_ext["value"].strip()
            if len(val) <= 8 and len(val) >= 1:
                has_manual = any(
                    n.get("note", "").startswith("名字:") and not n.get("note", "").endswith("(AI分析)")
                    for n in mem.get("notes", [])
                )
                if not has_manual:
                    _upsert_person_note(qq, f"名字:{val}", source="ai_analysis")
                    try:
                        from db import upsert_person_real_name
                        upsert_person_real_name(qq, val)
                    except Exception:
                        pass
                    changed = True

        # 关系 — SQLite add_person_tag + JSON fallback
        relations = extraction.get("关系", [])
        if isinstance(relations, list):
            for rel in relations:
                if rel.get("value") and rel.get("confidence", 0) >= CONFIDENCE_THRESHOLD:
                    val = rel["value"].strip()
                    _upsert_person_note(qq, f"关系:{val}(AI分析)", source="ai_analysis")
                    try:
                        from db import add_person_tag
                        add_person_tag(qq, val)
                    except Exception:
                        pass
                    changed = True

        # 关键信息 — SQLite add_key_fact + JSON fallback
        facts = extraction.get("关键信息", [])
        if isinstance(facts, list):
            for fact in facts:
                if fact.get("value") and fact.get("confidence", 0) >= CONFIDENCE_THRESHOLD:
                    val = fact["value"].strip()
                    if len(val) <= 200:
                        _upsert_person_note(qq, f"关键信息:{val}(AI分析)", source="ai_analysis")
                        try:
                            from db import add_key_fact
                            add_key_fact(qq, val)
                        except Exception:
                            pass
                        changed = True

        # 性格特征 — 覆盖更新（JSON role.personality）
        personality = extraction.get("性格特征", "")
        if personality and len(personality) > 5:
            mem = _load_memory(mem_key)  # re-read after note changes
            mem.setdefault("role", {})
            old_personality = mem["role"].get("personality", "")
            if personality != old_personality:
                mem["role"]["personality"] = personality
                changed = True

        # 更新元数据
        mem = _load_memory(mem_key)
        mem.setdefault("role", {})
        mem["role"]["last_analysis"] = datetime.now().isoformat()
        mem["role"]["analysis_pending_count"] = 0

        if changed:
            _save_memory(mem_key, mem)
            _invalidate_person_index()
            log(f"AI分析已更新 {qq} 的人物信息")

        # 构建 user_summary 并写入 SQLite
        from db import upsert_user_summary, get_resolved_name
        parts = []
        resolved = get_resolved_name(qq) or ""
        if resolved:
            parts.append(f"称呼: {resolved}")
        for note in mem.get("notes", []):
            n = note.get("note", "")
            if n.startswith("名字:") and not n.endswith("(AI分析)"):
                parts.append(f"姓名: {n.replace('名字:', '').strip()}")
            elif n.startswith("关系:") and not n.endswith("(AI分析)"):
                parts.append(n.replace("关系:", "").replace("(AI分析)", "").strip())
        personality = mem.get("role", {}).get("personality", "")
        if personality:
            parts.append(f"性格: {personality}")
        # 收集关键信息
        key_facts = []
        for note in mem.get("notes", []):
            n = note.get("note", "")
            if n.startswith("关键信息:") and n.endswith("(AI分析)"):
                key_facts.append(n.replace("关键信息:", "").replace("(AI分析)", "").strip())
        if key_facts:
            parts.append(f"关键信息: {'; '.join(key_facts)}")
        if parts:
            summary = "。".join(parts) + "。"
            upsert_user_summary(qq, summary)


# ==================== P8.3: 工具定义 ====================

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "check_inbox",
            "description": "查看最近24小时内有哪些人给主人发了消息。返回发件人列表和内容摘要。口语示例：'谁找我了'、'看看收件箱'、'有新消息吗'、'有人给我发消息了吗'。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "给指定的一个人或一群人发消息。target可以是名字、QQ号、关系标签（如'同事'），或逗号分隔的多个目标。消息会自动润色。口语示例：'给小明发个消息说你好'、'告诉张三明天开会'、'帮我跟李四说生日快乐'。可先调get_person_info了解对方或先调get_person_conversation查看上下文。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "收信人描述：名字、QQ号、关系标签，或逗号分隔的列表"},
                    "message": {"type": "string", "description": "要发送的消息内容"},
                },
                "required": ["target", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "broadcast_message",
            "description": "向所有QQ好友群发消息。每个人单独收到，消息会自动润色。可先调用其他工具获取信息再群发（如先查天气再推送给所有人）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "要群发的消息内容"},
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_contacts",
            "description": "列出机器人账号里的好友列表或群列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["friends", "groups", "all"],
                        "description": "要列出的类型：friends=好友, groups=群, all=全部",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "联网搜索实时信息。可以搜新闻、百科、天气、股票、事实性问题等。所有人都可以调用。返回搜索结果的文字摘要，通常2-5句话。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词，如'今天北京天气'、'2026世界杯最新消息'、'某某公司股价'"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市的天气。返回结构化数据（温度、湿度、预报）。常与 broadcast_message 配合：查完天气后群发给所有人。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名称，例如'北京'"},
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "mute_chat",
            "description": "让机器人在当前会话中静默，不再自动回复。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unmute_chat",
            "description": "恢复当前会话中机器人的自动回复。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_person_info",
            "description": "查看某个人的详细档案，包括姓名、备注、角色、关系标签等。口语示例：'小明是谁'、'查一下张三的资料'、'李四是什么关系'、'看看小王的备注'。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "目标描述：名字或QQ号"},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "refresh_contacts",
            "description": "刷新好友和群列表缓存（新加好友后使用）。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_person_relation",
            "description": "设置或更新某人的关系标签。target可以是名字或QQ号。relation如：同事、朋友、家人、同学、客户等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "目标人物：名字或QQ号"},
                    "relation": {"type": "string", "description": "关系描述，如'同事'、'朋友'、'家人'"},
                },
                "required": ["target", "relation"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_person_note",
            "description": "为某人添加通用备注信息（如性格特点、关键信息等）。注意：设置日常称呼请用 set_nickname 工具，不要用这个。target可以是名字或QQ号。note建议格式：关键信息:XX 或 性格:XX。口语示例：'给张三备注为同事'、'给李四加个备注性格开朗'。收到对方要求加备注时，先调工具再回复。绝对不要口头答应而不调工具——那叫口嗨，是严重的错误。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "目标人物：名字或QQ号"},
                    "note": {"type": "string", "description": "备注内容，建议格式：关键信息:XX 或 性格:XX"},
                },
                "required": ["target", "note"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_nickname",
            "description": "★ 设置日常称呼的首选工具。当对方说「叫我XX」「以后叫我XX」「我是XX」时，必须用这个工具。每人最多2个称呼位，新称呼自动覆盖时间更旧的那个。称呼会被自动校验，脏话或纯数字会被拒绝。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "目标人物：名字或QQ号"},
                    "nickname": {"type": "string", "description": "新称呼，1-10个字符"},
                },
                "required": ["target", "nickname"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "switch_nickname",
            "description": "手动切换某人当前使用的称呼位（在TA的两个称呼之间切换）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "目标人物：名字或QQ号"},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_nickname_status",
            "description": "查看某人的称呼设置状态（两个称呼分别是什么、谁设置的、哪个当前活跃）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "目标人物：名字或QQ号"},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_person_conversation",
            "description": "查看与某人的最近对话记录摘要。用于了解某人最新动态或对话上下文。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "目标人物：名字或QQ号"},
                    "count": {"type": "number", "description": "返回最近N条消息，默认10"},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "group_broadcast",
            "description": "向指定的QQ群发送消息。target_group可以是群名或群号。消息会自动润色。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_group": {"type": "string", "description": "目标群：群名或群号"},
                    "message": {"type": "string", "description": "要发送的消息内容"},
                },
                "required": ["target_group", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_personality",
            "description": "用AI分析某人的性格特点。需要至少有一些对话记录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "目标人物：名字或QQ号"},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_group_members",
            "description": "列出指定QQ群的成员列表。返回昵称、QQ号、群名片等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_id": {"type": "string", "description": "目标群：群号或群名"},
                },
                "required": ["group_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_group_activity",
            "description": "查看指定群聊最近24小时的活动情况：谁在活跃、说了什么。",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_id": {"type": "string", "description": "目标群：群号或群名"},
                    "hours": {"type": "number", "description": "查询最近多少小时，默认24"},
                },
                "required": ["group_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_conversations",
            "description": "在所有对话中搜索匹配关键词的消息。返回发件人、内容和时间。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "搜索关键词"},
                    "limit": {"type": "number", "description": "最多返回条数，默认10"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_person_stats",
            "description": "获取与某人的对话统计信息：总条数、首次联系时间、活跃频道等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "目标人物：名字或QQ号"},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_all_persons",
            "description": "列出所有已知联系人的概要信息：姓名、关系、备注数等。用于批量了解联系人概况。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_all_notes",
            "description": "列出所有联系人的所有备注信息（JSON备注 + SQLite结构化数据）。按人归类。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_admin",
            "description": "添加管理员。target为名字或QQ号。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "目标人物：名字或QQ号"},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remove_admin",
            "description": "移除管理员权限。target为名字或QQ号。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "目标人物：名字或QQ号"},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_admins",
            "description": "列出所有管理员。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "start_game",
            "description": "开始一个游戏。可选：猜数字(guess_number)、数字炸弹(number_bomb)、谁是卧底(undercover)、脑筋急转弯(riddle)、你画我猜(word_guess)、真心话大冒险(truth_dare)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "game": {"type": "string", "description": "游戏名称：guess_number/number_bomb/undercover/riddle/word_guess/truth_dare"},
                },
                "required": ["game"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "game_action",
            "description": "进行游戏操作，如猜数字。action为操作内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "游戏操作内容"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "end_game",
            "description": "结束当前游戏。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "game_status",
            "description": "查看当前游戏状态。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "summarize_recent",
            "description": "总结最近的收件箱消息或指定某人的近期对话。不填target默认总结24h收件箱。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "可选：目标人物名字或QQ号。不填则总结收件箱"},
                    "hours": {"type": "number", "description": "总结最近多少小时，默认24"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_scheduled_task",
            "description": "添加一条定时消息。所有用户可用。非管理员只能创建发送给自己的定时消息（target_type=private，target_id为自己QQ号）。管理员可以创建发送到任何地方的消息。口语示例：'三点提醒我去上课'、'明天8点叫群里的张三起床'、'2.42分时给小明推送今天天气'、'每2小时提醒我喝水'、'每天早上8点发早安'。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_type": {
                        "type": "string",
                        "enum": ["once", "daily", "interval"],
                        "description": "任务类型：once=一次性, daily=每天固定时间, interval=每隔N小时",
                    },
                    "name": {"type": "string", "description": "任务名称，方便管理"},
                    "trigger_time": {
                        "type": "string",
                        "description": "触发时间。daily填HH:MM（如08:00），once填YYYY-MM-DD HH:MM（如2026-07-05 21:00），interval不填",
                    },
                    "interval_hours": {
                        "type": "number",
                        "description": "间隔小时数，仅interval类型使用。例如2表示每2小时执行一次",
                    },
                    "content": {"type": "string", "description": "要发送的消息内容。如果是ai_generate或weather类型，这里是给AI的prompt（如城市名或生成指令）"},
                    "target_type": {
                        "type": "string",
                        "enum": ["private", "group"],
                        "description": "发送目标类型：private=私聊, group=群。非管理员只能选private",
                    },
                    "target_id": {
                        "type": "string",
                        "description": "目标ID。私聊填QQ号，群聊填群号。非管理员只能填自己的QQ号",
                    },
                    "action_type": {
                        "type": "string",
                        "enum": ["ai_generate"],
                        "description": "执行动作类型。所有任务均通过 AI 生成消息内容后发送。默认 ai_generate。",
                    },
                },
                "required": ["task_type", "name", "content", "target_type", "target_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_my_tasks",
            "description": "查看定时任务列表。非管理员只能看到自己创建的任务。管理员可以看到所有任务。",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["all", "enabled", "disabled"],
                        "description": "筛选：all=全部, enabled=启用, disabled=停用",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_scheduled_task",
            "description": "删除定时任务。非管理员只能删除自己创建的任务。管理员可以删除任何任务。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "任务ID"},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "toggle_scheduled_task",
            "description": "启用或停用定时任务。非管理员只能操作自己创建的任务。管理员可以操作任何任务。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "任务ID"},
                    "enable": {"type": "boolean", "description": "true=启用, false=停用"},
                },
                "required": ["task_id", "enable"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_person_address",
            "description": "设置或修改某人的所在城市/地址。用于后续推送本地化天气提醒。管理员可以给任何人设置，非管理员只能设置自己的地址。口语示例：'把张三的地址设为北京'、'我在上海'、'李四在上海上班'。",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "目标：QQ号或名字"},
                    "city": {"type": "string", "description": "城市名称，如'北京'、'上海'、'广州'"},
                },
                "required": ["target", "city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "batch_send_weather_alerts",
            "description": "给指定的一批人（或所有人）发送本地化天气提醒。系统会自动根据每个人存储的地址查询当地天气，发送对应的内容。未设置地址的人会跳过。管理员专属。口语示例：'给大家发天气提醒'、'提醒所有人注意天气'、'给好友们发天气，注意防暑'。",
            "parameters": {
                "type": "object",
                "properties": {
                    "targets": {"type": "string", "description": "目标列表。填'all'表示所有人/全部好友；也可以填逗号分隔的QQ号或名字，如'张三,李四'"},
                    "custom_message": {"type": "string", "description": "可选，在天气信息后面附加的自定义文字，如'注意防暑'、'出门带伞'"},
                },
                "required": ["targets"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": "根据文字描述生成一张图片并通过QQ发送。可以画各种风格（写实、卡通、插画、水彩等）。所有人可以调用。使用示例：'帮我画一只猫'、'生成一张樱花树下的少女插画'、'画一张赛博朋克城市'",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "图片描述，越详细越好。支持中文和英文。"},
                    "style": {"type": "string", "description": "可选，风格提示。如'写实'、'卡通'、'插画'、'水彩'、'素描'、'油画'"},
                    "target": {"type": "string", "description": "可选，发给谁。不填则发给当前对话的人。"},
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_file",
            "description": "将文本内容生成为文件并通过QQ发送。适用于导出聊天记录、生成报告、保存笔记等场景。所有人可以调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "要发送的文件内容（文本）"},
                    "file_name": {"type": "string", "description": "文件名，如'report.txt'、'notes.md'"},
                    "target": {"type": "string", "description": "可选，发给谁。不填则发给当前对话的人。"},
                },
                "required": ["content", "file_name"],
            },
        },
    },
]

# ==================== 收件箱 ====================

def _check_inbox_for_master() -> str:
    """扫描所有会话文件，找出最近 24h 内（非主人）发来的消息。"""
    cutoff_ts = time.time() - 86400
    now = datetime.now()
    masters = _get_master_ids()
    results = []  # [(name, qq, source_label, count, snippet, last_time_str, relation)]

    for f in sorted(DATA_DIR.glob("*.json")):
        try:
            mem = json.loads(f.read_text("utf-8"))
            key = mem.get("key", "")
            if key.startswith("private_"):
                sender_qq = key.split("_", 1)[1]
                source = "私聊"
            elif "_person_" in key:
                sender_qq = key.split("_person_")[1]
                group_id = key.split("_person_")[0].replace("group_", "")
                source = f"群聊({group_id})"
            else:
                continue

            if sender_qq in masters:
                continue

            conv = mem.get("conversation", [])
            recent_in = []
            for m in conv:
                if m.get("direction") != "in":
                    continue
                try:
                    t = m.get("time", "")
                    msg_dt = datetime.strptime(
                        str(now.year) + "-" + t, "%Y-%m-%d %H:%M:%S"
                    )
                    if (now - msg_dt).total_seconds() <= 86400:
                        recent_in.append(m)
                except Exception:
                    continue

            if not recent_in:
                continue

            # 名字优先级：称呼(nickname_slots) > 备注(fixed_name) > 好友昵称 > QQ末4位
            name = _resolve_speaker_name(sender_qq) or mem.get("name", "") or sender_qq
            relation = _extract_relation(mem)
            last_time = recent_in[-1].get("time", "?")
            snippet = recent_in[-1].get("content", "")[:80]
            results.append((name, sender_qq, source, len(recent_in), snippet, last_time, relation))
        except Exception:
            continue

    if not results:
        return "📭 最近24小时内没有来自非管理员的消息。"

    results.sort(key=lambda x: x[3], reverse=True)
    lines = [f"📬 最近24小时内 {len(results)} 位联系人发来消息："]
    for name, qq, source, count, snippet, last_time, relation in results[:5]:
        rel_tag = f"[{relation}] " if relation else ""
        # 称呼优先：有称呼时只显示称呼，没有称呼才显示QQ号
        if name == qq or name == qq[-4:]:
            display = qq
        else:
            display = name
        # 摘要截断：去换行，截60字
        clean_snippet = snippet.replace(chr(10), " ").strip()[:60]
        lines.append(
            f"  - {rel_tag}{display}：{count}条，最后[{last_time}]「{clean_snippet}」"
        )
    # 提示未发消息的联系人（从所有联系人中排除已列出的）
    try:
        from db import get_all_contacts
        from config import _friend_cache
        all_contacts = get_all_contacts(friend_cache=_friend_cache if _friend_cache else None)
        if all_contacts:
            mentioned_ids = {r[1] for r in results}
            silent = [c for c in all_contacts if c["qq"] not in mentioned_ids]
            if silent:
                silent_names = []
                for s in silent[:10]:
                    sn = _resolve_speaker_name(s["qq"]) or s.get("fixed_name") or s["qq"]
                    silent_names.append(sn if sn != s["qq"] else s["qq"])
                lines.append(f"  ⚡ 未发消息：{'、'.join(silent_names)}等{len(silent)}人")
    except Exception:
        pass
    if len(results) > 5:
        lines.append(f"  ...共{len(results)}人（仅显示前5）")
    return "\n".join(lines)


# ==================== 消息总结 ====================

def _summarize_inbox(master_id: str, hours: int = 24) -> str:
    """总结最近谁给管理员发了消息。扫描所有 private 文件。"""
    now = datetime.now()
    cutoff = now - timedelta(hours=hours)
    cutoff_str = cutoff.strftime("%m-%d %H:%M")

    results = []
    for f in sorted(DATA_DIR.glob("private_*.json")):
        qq = f.stem.split("_", 1)[1]
        if qq == master_id or qq == SELF_QQ:
            continue
        try:
            mem = json.loads(f.read_text("utf-8"))
            # 名字优先级：称呼 > 备注 > 好友昵称 > QQ末4位
            name = _resolve_speaker_name(qq) or mem.get("name", "") or qq
            incoming = [m for m in mem.get("conversation", [])
                       if m.get("direction") == "in" and m.get("time", "") >= cutoff_str[:5]]
            if incoming:
                results.append({
                    "qq": qq, "name": name,
                    "count": len(incoming),
                    "last_msg": incoming[-1]["content"][:100],
                    "last_time": incoming[-1]["time"],
                })
        except Exception:
            pass

    if not results:
        return f"最近{hours}小时内没有人给管理员发消息。"

    results.sort(key=lambda x: x["count"], reverse=True)
    total_msgs = sum(r["count"] for r in results)
    lines = [f"最近{hours}小时内共{len(results)}人发来{total_msgs}条消息。"]
    for r in results[:5]:
        # 称呼优先：有称呼只显示称呼，没有则显示QQ
        display = r['name'] if r['name'] != r['qq'] and r['name'] != r['qq'][-4:] else r['qq']
        clean = r['last_msg'].replace(chr(10), " ").strip()[:60]
        lines.append(f"  - {display}：{r['count']}条，最后[{r['last_time']}]「{clean}」")
    return "\n".join(lines)


def _summarize_person_dialog(target_id: str, hours: int = 24) -> str:
    """总结与某人的近期对话，调用 AI 生成摘要。"""
    from datetime import timedelta
    name = _resolve_speaker_name(target_id)

    all_msgs = []
    mem = {}
    for pattern in [f"*_person_{target_id}.json", f"private_{target_id}.json"]:
        for f in DATA_DIR.glob(pattern):
            try:
                gm = json.loads(f.read_text("utf-8"))
                all_msgs.extend(gm.get("conversation", []))
                if not mem:
                    mem = gm
            except Exception:
                continue

    if not all_msgs:
        return f"没有 {name} 的对话记录。"

    seen = set()
    unique = []
    for m in sorted(all_msgs, key=lambda x: x.get("time", "")):
        key = (m.get("time", "")[:10], m.get("content", "")[:50])
        if key not in seen:
            seen.add(key)
            unique.append(m)

    now = datetime.now()
    cutoff = now - timedelta(hours=hours)
    cutoff_str = cutoff.strftime("%m-%d %H:%M")
    recent = [m for m in unique if m.get("time", "") >= cutoff_str[:5]][-50:]

    if not recent:
        return f"最近{hours}小时内没有 {name} 的对话记录。"

    their_msgs = [m["content"] for m in recent if m.get("direction") == "in"]
    my_msgs = [m["content"] for m in recent if m.get("direction") == "out"]

    their_sample = "\n".join([f"- {m[:200]}" for m in their_msgs[-30:]])
    my_sample = "\n".join([f"- {m[:200]}" for m in my_msgs[-10:]])

    system = "你是对话总结助手。总结管理员与某人的近期对话，提取重要信息。200字以内，用自然语气。"
    user = f"""请总结管理员与 {name} 的近期对话（最近{hours}小时，共{len(recent)}条消息）。
对方发言：
{their_sample}

机器人的回复：
{my_sample}

总结要点：1.对方找管理员做什么 2.有没有重要事项需要处理 3.对方情绪状态 4.是否需要管理员回复"""

    return _call_deepseek(system, user, max_tokens=400) or f"无法生成 {name} 的总结"


# ==================== 搜索 ====================
SEARCH_REALTIME_PATTERNS = re.compile(
    r"(?:天气|气温|下雨|多少度|几度|明天天气|后天天气|今天天气)"
    r"|(?:新闻|股价|股票|汇率|比分|比赛|比特币|最新|最近发生)"
    r"|(?:帮我查|能不能查|查一下|搜一下|帮我搜|搜索)"
    r"|(?:多少钱|价格|什么时候|几月几日|日期|时间|几点)"
    r"|(?:在哪|哪个|哪些|什么电影|新游戏|上线了|发布了)"
)

WEATHER_URL = os.environ.get("WEATHER_URL", "https://wttr.in")


def _extract_search_query(content: str, context_str: str) -> str:
    """用 DeepSeek 从消息中提取最佳搜索关键词（10字以内）"""
    try:
        import httpx
        ctx = context_str[-300:] if len(context_str) > 300 else context_str
        resp = httpx.post(
            f"{DEEPSEEK_BASE_URL}/v1/chat/completions",
            json={
                "model": DEEPSEEK_MODEL,
                "max_tokens": 20,
                "messages": [
                    {"role": "system", "content": "从用户消息中提取搜索关键词。只输出10字以内的关键词。"},
                    {"role": "user", "content": f"对话上下文：{ctx}\n用户消息：{content}\n搜索关键词："},
                ],
            },
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            },
            timeout=8.0,
        )
        resp.raise_for_status()
        kw = resp.json()["choices"][0]["message"]["content"].strip()
        kw = kw.strip("。！？.!? \"'\"'「」『』\n")
        return kw if kw and len(kw) >= 2 else content
    except Exception:
        return content


def _get_weather(city: str) -> str | None:
    """wttr.in 精确天气查询"""
    try:
        import httpx
        safe_city = city.strip().replace("市", "").replace("天气", "").strip()
        if not safe_city:
            return None
        resp = httpx.get(
            f"{WEATHER_URL}/{safe_city}?format=j1&lang=zh",
            headers={"User-Agent": "curl/8.0"},
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        cur = data["current_condition"][0]
        weather = (
            f"{safe_city}实时天气：{cur['weatherDesc'][0]['value']}\n"
            f"温度 {cur['temp_C']}°C（体感 {cur['FeelsLikeC']}°C）\n"
            f"湿度 {cur['humidity']}%，风向 {cur['winddir16Point']}，风速 {cur['windspeedKmph']} km/h"
        )
        for i, day in enumerate(data["weather"][:2]):
            label = "明天" if i == 0 else "后天"
            weather += (
                f"\n{label}（{day['date']}）："
                f"最高 {day['maxtempC']}°C / 最低 {day['mintempC']}°C，"
                f"{day['hourly'][4]['weatherDesc'][0]['value']}"
            )
        return weather
    except Exception as e:
        log(f"天气查询失败: {e}")
        return None


def _batch_send_weather(targets_spec: str, custom_message: str = "") -> str:
    """给一批人发送本地化天气提醒。共享于工具 handler 和调度器。
    发送给所有好友（不仅有城市的），未设置城市的跳过并在结果中列出（提醒设置城市）。
    返回格式："✅ 已发送 X 人：...\n⚠️ Y 人未设置城市：..."
    """
    from config import _friend_cache
    from db import get_all_contacts
    # 按真实好友列表过滤，排除残留无效账号
    all_contacts = get_all_contacts(friend_cache=_friend_cache if _friend_cache else None)
    if not all_contacts:
        return "好友列表为空，无法发送天气提醒。"

    if targets_spec in ("all", "所有人", "全部好友", "全部", ""):
        targets = all_contacts
    else:
        names = [n.strip() for n in targets_spec.replace("，", ",").split(",") if n.strip()]
        qq_to_info = {info["qq"]: info for info in all_contacts}
        name_to_info = {}
        for info in all_contacts:
            if info["fixed_name"]:
                name_to_info[info["fixed_name"]] = info
        targets = []
        for name in names:
            if name in qq_to_info:
                targets.append(qq_to_info[name])
            elif name in name_to_info:
                targets.append(name_to_info[name])

    success = []
    failed = []
    now = datetime.now()
    hour = now.hour
    if hour < 6:
        time_greeting = "夜深了"
    elif hour < 9:
        time_greeting = "早上好"
    elif hour < 12:
        time_greeting = "上午好"
    elif hour < 14:
        time_greeting = "中午好"
    elif hour < 18:
        time_greeting = "下午好"
    else:
        time_greeting = "晚上好"

    # 城市级天气缓存：同一城市只查一次 wttr.in
    weather_cache = {}

    def _fetch_weather_cached(city: str) -> str | None:
        if city not in weather_cache:
            weather_cache[city] = _get_weather(city)
        return weather_cache[city]

    # 每个人的处理逻辑
    def _send_one(info: dict) -> tuple[str, str] | None:
        """返回 (label, status) 或 None 如果跳过。label 如 "用户(城市)"，status 为 "ok" 或 "fail"。
        未设置城市的人 → 不发送天气，记入 no_city 列表提醒用户设置。
        """
        city = info.get("city", "").strip()
        qq = info["qq"]
        from db import get_resolved_name, get_user_summary
        resolved = get_resolved_name(qq) or info["fixed_name"] or qq

        # 未设置城市 → 跳过并记录，不发送天气
        if not city:
            return (f"{resolved}(未设置城市)", "no_city")

        label = f"{resolved}({city})"
        weather = _fetch_weather_cached(city)
        if weather is None:
            return (label, "fail")

        greeting = f"{resolved}{time_greeting}~" if resolved else f"{time_greeting}~"
        msg = f"{greeting} {city}天气：{weather}"

        summary = get_user_summary(qq)
        if summary and "同事" in summary:
            msg += "\n上班路上注意天气变化哦~"
        elif summary and ("家人" in summary or "朋友" in summary):
            msg += "\n注意天气，照顾好自己哦~"

        if custom_message:
            msg += f"\n{custom_message}"
        if _send_text("private", qq, msg):
            return (label, "ok")
        else:
            return (label, "fail")

    # 线程池执行，单个人 35s 超时（含 API 请求 + QQ 发送）
    no_city: list[str] = []  # 未设置城市的人
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_send_one, info): info for info in targets}
        try:
            for future in as_completed(futures):
                info = futures[future]
                try:
                    result = future.result(timeout=35)
                    if result:
                        label, status = result
                        if status == "ok":
                            success.append(label)
                        elif status == "no_city":
                            no_city.append(label)
                        else:
                            failed.append(label)
                except Exception as e:
                    city = info.get("city", "")
                    qq = info.get("qq", "")
                    label = f"{info.get('fixed_name', qq)}({city})"
                    log(f"批量天气超时: {label} - {e}")
                    failed.append(label)
        except TimeoutError:
            # as_completed 整体超时 — 处理尚未完成的 future
            remaining = [info for f, info in futures.items() if not f.done()]
            for info in remaining:
                qq = info.get("qq", "")
                city_name = info.get("city", "?")
                label = f"{info.get('fixed_name', qq)}({city_name})"
                log(f"批量天气超时(整体): {label}")
                failed.append(label)

    result_parts = []
    if success:
        result_parts.append(f"✅ 已发送 {len(success)} 人：{', '.join(success)}")
    if failed:
        result_parts.append(f"❌ 失败 {len(failed)} 人：{', '.join(failed)}")
    if no_city:
        result_parts.append(f"⚠️ {len(no_city)} 人未设置城市，未发送天气：{', '.join(no_city)}")
    return "\n".join(result_parts) if result_parts else "没有可发送的对象。"


def _should_search_ai(content: str, context_str: str) -> bool:
    if SEARCH_REALTIME_PATTERNS.search(content):
        return True
    if "?" in content or "？" in content:
        return len(content) > 15
    return False


def _generate_image(prompt: str, style: str = "") -> str | None:
    """通过智谱 CogView-3-Flash 生成图片，返回图片 URL。"""
    try:
        import httpx
        full_prompt = f"{prompt}。{style}" if style else prompt
        resp = httpx.post(
            f"{ZHIPU_BASE_URL}/images/generations",
            json={
                "model": "cogview-3-flash",
                "prompt": full_prompt,
            },
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {ZHIPU_API_KEY}",
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["data"][0]["url"]
    except Exception as e:
        log(f"图片生成失败: {e}")
        return None


def _web_search(query: str) -> str | None:
    """智谱 GLM web_search"""
    try:
        import httpx
        resp = httpx.post(
            f"{ZHIPU_BASE_URL}/chat/completions",
            json={
                "model": "glm-4-flash",
                "max_tokens": 500,
                "messages": [{"role": "user", "content": query}],
                "tools": [{"type": "web_search", "web_search": {"search_query": query}}],
            },
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {ZHIPU_API_KEY}",
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return content.strip() if content else None
    except Exception as e:
        log(f"智谱搜索失败: {e}")
        return None


# ==================== P8.2: 工具执行器（dict dispatch） ====================

def _tool_check_inbox(args: dict, ctx: dict) -> str:
    return _check_inbox_for_master()


def _tool_send_message(args: dict, ctx: dict) -> str:
    target_str = args.get("target", "")
    raw_msg = args.get("message", "")
    confirmed = args.get("confirmed", False)
    if not target_str or not raw_msg:
        return "错误：缺少 target 或 message 参数。"
    # 角色词目标：「管理员/主人」不是某个人的名字——自动把话转达给所有管理员（私聊，带留言人署名）
    if target_str.strip() in _ADMIN_ALIAS_TARGETS:
        sender_id = str(ctx.get("sender_id", "") or "")
        disp = _resolve_speaker_name(sender_id) or ""
        if not disp or disp == sender_id[-4:]:
            disp = ctx.get("sender_nick", "") or sender_id  # 称呼兜底链：解析称呼 → QQ昵称 → QQ号
        final_alias_msg = f"📩 来自 {disp}（QQ:{sender_id}）的留言：\n{_polish_message(raw_msg, ['管理员'], disp)}"
        sent_cnt = 0
        for m_qq in sorted(_get_master_ids()):
            if str(m_qq) == str(ctx.get("sender_id", "")):
                continue  # 说话人自己是管理员时不重复转达给自己
            if _send_text("private", str(m_qq), final_alias_msg):
                sent_cnt += 1
        if sent_cnt == 0:
            return "没有需要转达的管理员哦～如果你想找的管理员就是你自己，直接说事就行啦。"
        return f"✅ 已把你的话转达给 {sent_cnt} 位管理员。"
    # 模糊匹配检查：非精确匹配时先向用户确认（confirmed=False → 返回确认提示）
    if not confirmed:
        from config import _peek_match_confidence
        qq, name, conf = _peek_match_confidence(target_str)
        if qq and conf < 0.95:
            return (f"🔍 模糊匹配「{target_str}」→「{name}」(QQ:{qq})。"
                    f"置信度 {conf:.0%}，请确认是否正确？确认后回复「确认」再发。")
        if not qq:
            # 完全找不到 → 尝试标准解析
            targets, unresolved = _resolve_targets(target_str)
            if not targets:
                hint = f"找不到目标「{target_str}」。"
                if unresolved:
                    hint += f" 未识别：{', '.join(unresolved)}"
                return hint
    targets, unresolved = _resolve_targets(target_str)
    if not targets:
        hint = f"找不到目标「{target_str}」。"
        if unresolved:
            hint += f" 未识别：{', '.join(unresolved)}"
        return hint
    recipient_names = [t[2] for t in targets]
    sender_name = ctx.get("sender_nick", "主人")
    final_msg = _polish_message(raw_msg, recipient_names, sender_name)
    if not final_msg:
        return "消息内容为空，无法发送。"
    ok, fail = [], []
    for t_type, t_id, t_name in targets:
        if _send_text(t_type, t_id, final_msg):
            ok.append(t_name)
        else:
            fail.append(t_name)
    parts = [f"✅ 已发送给 {len(ok)}/{len(targets)} 人"]
    if fail:
        parts.append(f"❌ 失败：{'、'.join(fail)}")
    if unresolved:
        parts.append(f"⚠️ 未识别：{'、'.join(unresolved)}")
    return "\n".join(parts)


def _tool_broadcast_message(args: dict, ctx: dict) -> str:
    raw_msg = args.get("message", "")
    if not raw_msg:
        return "错误：缺少 message 参数。"
    targets, _ = _resolve_targets("所有人")
    if not targets:
        return "好友列表为空，缓存可能尚未加载。请稍后重试。"
    recipient_names = [t[2] for t in targets]
    sender_name = ctx.get("sender_nick", "主人")
    final_msg = _polish_message(raw_msg, recipient_names, sender_name)
    ok, fail = [], []
    for t_type, t_id, t_name in targets:
        if _send_text(t_type, t_id, final_msg):
            ok.append(t_name)
        else:
            fail.append(t_name)
    parts = [f"✅ 已群发给 {len(ok)}/{len(targets)} 人"]
    if fail:
        parts.append(f"❌ 失败：{'、'.join(fail)}")
    return "\n".join(parts)


def _tool_list_contacts(args: dict, ctx: dict) -> str:
    list_type = args.get("type", "all")
    summary = _get_contact_summary(detail=True)
    if not summary:
        return "联系人缓存尚未加载，请稍后重试。"
    if list_type == "friends":
        if _friend_cache:
            lines = "\n".join([f"  - {n}({q})" for q, n in sorted(_friend_cache.items(), key=lambda x: x[1])])
            return f"共 {len(_friend_cache)} 个好友：\n{lines}"
        return "暂无好友数据。"
    elif list_type == "groups":
        if _group_cache:
            lines = "\n".join([f"  - {n}({g})" for g, n in sorted(_group_cache.items(), key=lambda x: x[1])])
            return f"共 {len(_group_cache)} 个群：\n{lines}"
        return "暂无群数据。"
    return summary


def _tool_generate_image(args: dict, ctx: dict) -> str:
    prompt = args.get("prompt", "")
    if not prompt:
        return "错误：缺少 prompt 参数。"
    style = args.get("style", "")
    log(f"图片生成请求: {prompt[:80]} 风格={style or '默认'}")
    image_url = _generate_image(prompt, style)
    if not image_url:
        return "❌ 图片生成失败，请稍后重试。"
    target_str = args.get("target", "")
    msg_type = ctx.get("msg_type", "private")
    if target_str:
        targets, _ = _resolve_targets(target_str)
        if not targets:
            return f"❌ 找不到目标「{target_str}」。"
        label, t_id = targets[0]
        ok = _send_image("private", t_id, image_url)
    else:
        t_id = ctx.get("target_id", ctx["sender_id"])
        ok = _send_image(msg_type, t_id, image_url)
    if ok:
        return f"✅ 图片已生成并发送！提示词：{prompt[:100]}"
    else:
        return f"✅ 图片已生成：{image_url}，但发送失败，请检查网络。"


def _tool_send_file(args: dict, ctx: dict) -> str:
    """发送文件。根据 file_name 后缀自动选择格式：.docx→Word / .md→Markdown / 其他→纯文本。"""
    return "此发行版暂未启用文件工具。"
    file_content = args.get("content", "")
    file_name = args.get("file_name", "output.txt")
    if not file_content:
        return "错误：缺少 content 参数。"
    import tempfile
    tmp_dir = tempfile.gettempdir()
    file_path = os.path.join(tmp_dir, file_name)
    try:
        if file_name.lower().endswith(".docx"):
            # 生成真正的 Word 文档（效果八）
            from docx import Document
            doc = Document()
            # 简单排版：空行分段，以 # 开头的行作为标题
            for line in file_content.split("\n"):
                line = line.strip()
                if line.startswith("# "):
                    doc.add_heading(line[2:], level=1)
                elif line.startswith("## "):
                    doc.add_heading(line[3:], level=2)
                elif line.startswith("- "):
                    doc.add_paragraph(line[2:], style="List Bullet")
                elif line:
                    doc.add_paragraph(line)
                else:
                    doc.add_paragraph("")
            doc.save(file_path)
        else:
            # .md / .txt → 直接写文本
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(file_content)
    except Exception as e:
        return f"❌ 写入文件失败: {e}"
    target_str = args.get("target", "")
    msg_type = ctx.get("msg_type", "private")
    if target_str:
        targets, _ = _resolve_targets(target_str)
        if not targets:
            return f"❌ 找不到目标「{target_str}」。"
        label, t_id = targets[0]
        ok = _send_file("private", t_id, file_path, file_name)
    else:
        t_id = ctx.get("target_id", ctx["sender_id"])
        ok = _send_file(msg_type, t_id, file_path, file_name)
    if ok:
        return f"✅ 文件「{file_name}」已发送。"
    else:
        return f"❌ 文件「{file_name}」发送失败，请检查网络。"


def _tool_web_search(args: dict, ctx: dict) -> str:
    query = args.get("query", "")
    if not query:
        return "错误：缺少 query 参数。"
    result = _web_search(query)
    return result if result else f"❌ 搜索「{query}」失败，请稍后重试。"


def _tool_get_weather(args: dict, ctx: dict) -> str:
    city = args.get("city", "")
    if not city:
        return "错误：缺少 city 参数。"
    weather = _get_weather(city)
    return weather if weather else f"❌ 查询「{city}」天气失败，请稍后重试。"


def _tool_mute_chat(args: dict, ctx: dict) -> str:
    chat_id = ctx.get("chat_id", "")
    _set_chat_muted(chat_id, True)
    return "✅ 已静默当前会话。"


def _tool_unmute_chat(args: dict, ctx: dict) -> str:
    chat_id = ctx.get("chat_id", "")
    _set_chat_muted(chat_id, False)
    return "✅ 已恢复当前会话。"


def _tool_get_person_info(args: dict, ctx: dict) -> str:
    target_str = args.get("target", "")
    if not target_str:
        return "错误：缺少 target 参数。"
    targets, unresolved = _resolve_targets(target_str)
    if not targets:
        return f"找不到「{target_str}」。"
    _, person_id, person_name = targets[0]
    profile = _get_person_profile(person_id)
    lines = [f"📋 {person_name}（{person_id}）"]
    if profile.get("name"):
        lines.append(f"  称呼：{profile['name']}")
    notes = profile.get("notes", [])
    if notes:
        lines.append("  备注：")
        for n in notes:
            lines.append(f"    - {n.get('note', '')}")
    rel = _extract_relation(profile)
    if rel:
        lines.append(f"  关系：{rel}")
    if profile.get("role"):
        lines.append(f"  人设：{profile['role']}")
    # 补充 SQLite 档案信息
    try:
        from db import get_profile as _db_get_profile
        db_prof = _db_get_profile(person_id)
        if db_prof:
            if db_prof.get("fixed_name"):
                lines.append(f"  固定备注：{db_prof['fixed_name']}")
            if db_prof.get("feature_summary"):
                lines.append(f"  性格总结：{db_prof['feature_summary']}")
            if db_prof.get("tag_list_parsed"):
                lines.append(f"  标签：{', '.join(db_prof['tag_list_parsed'])}")
            if db_prof.get("source_group"):
                lines.append(f"  来源群组：{db_prof['source_group']}")
            lines.append(f"  好友状态：{'是' if db_prof.get('is_friend') else '否'}")
            if db_prof.get("city"):
                lines.append(f"  所在城市：{db_prof['city']}")
            if db_prof.get("user_summary"):
                lines.append(f"  人物画像：{db_prof['user_summary']}")
            if db_prof.get("real_name"):
                lines.append(f"  真实姓名：{db_prof['real_name']}")
            if db_prof.get("free_notes"):
                lines.append(f"  自由备注：{db_prof['free_notes']}")
            # 关键信息 (key_facts stored as JSON)
            key_facts = []
            try:
                from db import get_key_facts
                key_facts = get_key_facts(person_id)
            except Exception:
                pass
            if key_facts:
                lines.append(f"  关键信息：{'；'.join(key_facts)}")
            # 关系标签 (person_tags)
            try:
                from db import get_person_tags
                tags = get_person_tags(person_id)
                if tags:
                    lines.append(f"  关系标签：{', '.join(tags)}")
            except Exception:
                pass
    except Exception:
        pass
    return "\n".join(lines)


def _tool_refresh_contacts(args: dict, ctx: dict) -> str:
    _refresh_contact_cache()
    _invalidate_person_index()
    return f"✅ 联系人缓存已刷新：{len(_friend_cache)} 好友, {len(_group_cache)} 群"


def _tool_set_person_relation(args: dict, ctx: dict) -> str:
    target_str = args.get("target", "")
    relation = args.get("relation", "").strip()
    if not target_str or not relation:
        return "错误：需要 target 和 relation 参数。"
    valid, reason = _validate_note(f"关系:{relation}")
    if not valid:
        return f"❌ {reason}"
    confirmed = args.get("confirmed", False)
    # 模糊匹配检查（非精确匹配需确认）
    if not confirmed:
        from config import _peek_match_confidence
        matched_qq, matched_name, conf = _peek_match_confidence(target_str)
        if matched_qq and conf < 0.95:
            return (f"🔍 模糊匹配「{target_str}」→「{matched_name}」(QQ:{matched_qq})。"
                    f"请确认是否正确？确认后回复「确认」再改。")
    targets, unresolved = _resolve_targets(target_str)
    if not targets:
        hint = f"找不到「{target_str}」。"
        if unresolved:
            hint += f" 未识别：{', '.join(unresolved)}"
        return hint
    _, qq, name = targets[0]
    sender = ctx.get("sender_id", "")
    # 获取旧关系用于通知
    from db import get_person_tags
    old_tags = get_person_tags(qq)
    old_label = "、".join(old_tags) if old_tags else "无"
    # 单一值覆盖：清除旧标签，写入新关系
    _apply_relation_to_role(qq, relation)
    try:
        from db import set_person_tags
        set_person_tags(qq, [relation])
    except Exception:
        pass
    # 通知双方（详细：旧值→新值 + 操作人）
    detail = f"关系：{old_label} → 「{relation}」"
    _notify_user(qq, detail, operator=sender)
    # 非管理员操作 → 通知管理员
    from config import _get_master_ids
    if sender not in _get_master_ids():
        for mid in _get_master_ids():
            _notify_user(mid, f"用户 {name}({qq}) 的关系已更新：{old_label} → 「{relation}」", operator=sender)
    return f"✅ 已将 {name}({qq}) 的关系设为「{relation}」。"


def _tool_set_person_note(args: dict, ctx: dict) -> str:
    """设置备注/关键信息（单一值覆盖，后来者替换前者）+ 模糊匹配确认。"""
    sender = ctx.get("sender_id", "")
    target_str = args.get("target", "")
    note_text = args.get("note", "").strip()
    if not target_str or not note_text:
        return "错误：需要 target 和 note 参数。"
    valid, reason = _validate_note(note_text)
    if not valid:
        return f"❌ {reason}"
    confirmed = args.get("confirmed", False)
    if not confirmed:
        from config import _peek_match_confidence
        matched_qq, matched_name, conf = _peek_match_confidence(target_str)
        if matched_qq and conf < 0.95:
            return (f"🔍 模糊匹配「{target_str}」→「{matched_name}」(QQ:{matched_qq})。"
                    f"请确认是否正确？确认后回复「确认」再改。")
    targets, unresolved = _resolve_targets(target_str)
    if not targets:
        hint = f"找不到「{target_str}」。"
        if unresolved:
            hint += f" 未识别：{', '.join(unresolved)}"
        return hint
    _, qq, name = targets[0]
    # 单一值覆盖：直接用 set_free_notes 覆盖全部备注
    try:
        from db import set_free_notes
        set_free_notes(qq, note_text)
    except Exception:
        pass
    # 通知对方
    _notify_user(qq, f"你的备注信息已被更新为「{note_text}」", operator=sender)
    return f"✅ 已将 {name}({qq}) 的备注设为「{note_text}」。"



def _tool_get_person_conversation(args: dict, ctx: dict) -> str:
    target_str = args.get("target", "")
    count = int(args.get("count", 10))
    if not target_str:
        return "错误：缺少 target 参数。"
    targets, unresolved = _resolve_targets(target_str)
    if not targets:
        hint = f"找不到「{target_str}」。"
        if unresolved:
            hint += f" {', '.join(unresolved)}"
        return hint
    _, qq, name = targets[0]
    pk = f"private_{qq}"
    pm = _load_memory(pk)
    conv = pm.get("conversation", [])
    if not conv:
        return f"📋 {name} 还没有对话记录。"
    recent = conv[-count:]
    lines = [f"📋 {name} 最近 {len(recent)} 条对话："]
    for m in recent:
        tag = "TA" if m["direction"] == "in" else "机器人"
        lines.append(f"  [{m['time']}] {tag}: {m['content'][:200]}")
    return "\n".join(lines)


def _tool_group_broadcast(args: dict, ctx: dict) -> str:
    target_group = args.get("target_group", "")
    raw_msg = args.get("message", "")
    if not target_group or not raw_msg:
        return "错误：需要 target_group 和 message 参数。"
    found_gid = None
    found_gname = None
    if target_group in _group_cache:
        found_gid = target_group
        found_gname = _group_cache[target_group]
    else:
        for gid, gname in _group_cache.items():
            if target_group in gname:
                found_gid = gid
                found_gname = gname
                break
    if not found_gid:
        return f"找不到群「{target_group}」。"
    sender_name = ctx.get("sender_nick", "主人")
    final_msg = _polish_message(raw_msg, [found_gname], sender_name)
    if not final_msg:
        return "消息内容为空，无法发送。"
    if _send_text("group", found_gid, final_msg):
        return f"✅ 已发送到群「{found_gname}」。"
    return f"❌ 发送到「{found_gname}」失败。"


def _tool_analyze_personality(args: dict, ctx: dict) -> str:
    target_str = args.get("target", "")
    if not target_str:
        return "错误：缺少 target 参数。"
    targets, unresolved = _resolve_targets(target_str)
    if not targets:
        hint = f"找不到「{target_str}」。"
        if unresolved:
            hint += f" {', '.join(unresolved)}"
        return hint
    _, qq, person_name = targets[0]
    profile = _get_person_profile(qq)
    conv = profile.get("conversation", [])
    if not conv:
        return f"还没有和 {person_name} 的对话记录，无法分析。"
    their_msgs = [m["content"] for m in conv if m.get("direction") == "in"][-50:]
    my_msgs = [m["content"] for m in conv if m.get("direction") == "out"][-50:]
    notes = [n.get("note", "") for n in profile.get("notes", [])]
    relation = _extract_relation(profile)
    report = _generate_person_report(their_msgs, my_msgs, person_name, notes, relation)
    # 分析完成 → 同时触发 AI 提取持久化（异步，不阻塞返回）
    _schedule_person_analysis(qq)
    return report


def _tool_list_group_members(args: dict, ctx: dict) -> str:
    group_query = args.get("group_id", "")
    if not group_query:
        return "错误：缺少 group_id 参数。"
    found_gid, found_gname = None, None
    if group_query in _group_cache:
        found_gid = group_query
        found_gname = _group_cache[group_query]
    else:
        for gid, gname in _group_cache.items():
            if group_query in gname:
                found_gid = gid
                found_gname = gname
                break
    if not found_gid:
        return f"找不到群「{group_query}」。请先刷新联系人列表。"
    try:
        resp = _api("get_group_member_list", {"group_id": int(found_gid)})
        if resp.get("status") == "ok":
            members = resp.get("data", [])
            lines = [f"群「{found_gname}」（{found_gid}）共 {len(members)} 人："]
            for m in members:
                mqq = str(m.get("user_id", ""))
                mnick = m.get("nickname", "")
                mcard = m.get("card", "")
                display = mcard or mnick or mqq
                try:
                    mm = _load_memory(f"private_{mqq}")
                    mname = mm.get("name", "")
                    if mname:
                        display = f"{mname}({display})"
                except Exception:
                    pass
                role_tag = " [管理]" if m.get("role") in ("admin", "owner") else ""
                lines.append(f"  - {display}({mqq}){role_tag}")
            return "\n".join(lines)
        else:
            return f"获取群成员失败：{resp.get('message', '未知错误')}"
    except Exception as e:
        return f"获取群成员出错：{e}"


def _tool_get_group_activity(args: dict, ctx: dict) -> str:
    group_query = args.get("group_id", "")
    hours = int(args.get("hours", 24))
    if not group_query:
        return "错误：缺少 group_id 参数。"
    found_gid, found_gname = None, None
    if group_query in _group_cache:
        found_gid = group_query
        found_gname = _group_cache[group_query]
    else:
        for gid, gname in _group_cache.items():
            if group_query in gname:
                found_gid = gid
                found_gname = gname
                break
    if not found_gid:
        return f"找不到群「{group_query}」。"
    cutoff = hours * 3600
    now_dt = datetime.now()
    active = {}
    total = 0
    for gf in DATA_DIR.glob(f"group_{found_gid}_person_*.json"):
        try:
            gm = json.loads(gf.read_text("utf-8"))
            sq = gf.stem.split("_person_")[1]
            gname = gm.get("name", "") or sq
            cnt = 0
            lt, lm = "", ""
            for m in gm.get("conversation", []):
                if m.get("direction") != "in":
                    continue
                try:
                    dt_str = f"{now_dt.year}-{m.get('time', '')}"
                    md = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                    if (now_dt - md).total_seconds() <= cutoff:
                        cnt += 1
                        total += 1
                        lt = m["time"]
                        lm = m.get("content", "")[:60]
                except Exception:
                    continue
            if cnt > 0:
                active[sq] = {"name": gname, "count": cnt, "last_msg": lm, "last_time": lt}
        except Exception:
            pass
    if not active:
        return f"群「{found_gname}」最近 {hours} 小时内没有记录到活动。"
    sorted_a = sorted(active.items(), key=lambda x: -x[1]["count"])
    lines = [
        f"📊 群「{found_gname}」最近 {hours} 小时活动：",
        f"活跃成员 {len(sorted_a)} 人，共 {total} 条消息",
    ]
    for qq, info in sorted_a[:15]:
        lines.append(
            f"  - {info['name']}({qq})：{info['count']}条，"
            f"最新「{info['last_msg']}」（{info['last_time']}）"
        )
    return "\n".join(lines)


def _read_file_content(file_url: str, file_name: str) -> str:
    """读取文件内容（HTTP URL / 本地路径 / .docx 文档），返回文本。"""
    import httpx
    if not file_url or not file_url.strip():
        raise ValueError(f"文件地址为空（{file_name}）")
    try:
        if file_url.startswith(("http://", "https://")):
            resp = httpx.get(file_url, timeout=15.0)
            resp.raise_for_status()
            # 远程 .docx → 用 python-docx 提取
            if file_name.lower().endswith(".docx"):
                from docx import Document
                from io import BytesIO
                doc = Document(BytesIO(resp.content))
                return "\n".join(p.text for p in doc.paragraphs)[:5000]
            return resp.text[:5000]
        # 本地路径
        from pathlib import Path
        p = Path(file_url)
        if p.is_file():
            # .docx → 提取段落文本（效果九）
            if file_name.lower().endswith(".docx"):
                from docx import Document
                doc = Document(str(p))
                return "\n".join(p.text for p in doc.paragraphs)[:5000]
            return p.read_text("utf-8", errors="replace")[:5000]
        if p.exists():
            raise ValueError(f"「{file_name}」是一个目录，无法读取")
        return f"（文件不可达: {file_name}）"
    except Exception as e:
        return f"（读取失败: {e}）"


def _collect_recent_summary(hours: int = 8) -> str:
    """汇总最近 N 小时内的消息。显示称呼而非QQ号，统计人数+条数+首条摘要。"""
    from datetime import timedelta
    from db import query_recent_chats
    from config import _resolve_speaker_name
    now = datetime.now()
    cutoff = (now - timedelta(hours=hours)).isoformat()
    chats = [c for c in query_recent_chats(days=1, limit=200) if c.get("send_time", "") >= cutoff]
    if not chats:
        return f"最近 {hours} 小时无新消息。"
    from collections import defaultdict
    by_sender: dict[str, list[str]] = defaultdict(list)
    for c in chats:
        by_sender[c["sender"]].append(c["msg"][:80])
    sorted_senders = sorted(by_sender.items(), key=lambda x: -len(x[1]))
    total = len(chats)
    lines = [f"最近 {hours} 小时 {len(by_sender)} 人共发 {total} 条消息："]
    for sender, msgs in sorted_senders[:15]:
        nick = _resolve_speaker_name(sender) or sender[-4:]
        first_line = (msgs[0].replace(chr(10), " ")[:40]) if msgs else ""
        lines.append(f"  · {nick}：{len(msgs)}条「{first_line}」")
def _collect_recent_summary(hours: int = 8) -> str:
    """汇总最近 N 小时内的消息。显示称呼而非QQ号，统计人数+条数+首条摘要。"""
    from datetime import timedelta
    from db import query_recent_chats
    from config import _resolve_speaker_name
    now = datetime.now()
    cutoff = (now - timedelta(hours=hours)).isoformat()
    chats = [c for c in query_recent_chats(days=1, limit=200) if c.get("send_time", "") >= cutoff]
    if not chats:
        return f"最近 {hours} 小时无新消息。"
    from collections import defaultdict
    by_sender: dict[str, list[str]] = defaultdict(list)
    for c in chats:
        by_sender[c["sender"]].append(c["msg"][:80])
    sorted_senders = sorted(by_sender.items(), key=lambda x: -len(x[1]))
    total = len(chats)
    lines = [f"最近 {hours} 小时 {len(by_sender)} 人共发 {total} 条消息："]
    for sender, msgs in sorted_senders[:15]:
        nick = _resolve_speaker_name(sender) or sender[-4:]
        first_line = (msgs[0].replace(chr(10), " ")[:40]) if msgs else ""
        lines.append(f"  · {nick}：{len(msgs)}条「{first_line}」")
    if len(sorted_senders) > 15:
        lines.append(f"  ... 还有 {len(sorted_senders) - 15} 人未显示")
    return "\n".join(lines)



def _cleanup_inconsistent_data() -> str:
    """清理数据不一致（昵称/备注冲突等），返回清理摘要。"""
    from config import _cleanup_person_notes
    from db import init_db
    init_db()
    cleaned = 0
    from config import DATA_DIR
    for f in sorted(DATA_DIR.glob("private_*.json")):
        qq = f.stem.split("_", 1)[1]
        try:
            n = _cleanup_person_notes(qq)
            cleaned += n
        except Exception:
            pass
    return f"清理完成：移除 {cleaned} 条不一致备注。"


def _tool_search_conversations(args: dict, ctx: dict) -> str:
    keyword = args.get("keyword", "")
    limit = int(args.get("limit", 10))
    if not keyword:
        return "错误：缺少 keyword 参数。"
    results = []

    # 优先从 SQLite 全局日志搜索（不区分私聊/群聊）
    try:
        from db import search_conversations as _db_search
        db_results = _db_search(keyword, limit)
        if db_results:
            for r in db_results:
                ch = f"群聊({r['group_id']})" if r["group_id"] != "0" else "私聊"
                results.append({
                    "who": r["sender"],
                    "channel": ch,
                    "time": r["send_time"][:16] if r["send_time"] else "",
                    "direction": "TA",
                    "content": r["msg"][:200],
                })
    except Exception:
        pass

    # 如果 DB 无结果，回退到 JSON 扫描
    if not results:
        for f in DATA_DIR.glob("*.json"):
            try:
                mem = json.loads(f.read_text("utf-8"))
                key = mem.get("key", "")
                pname = mem.get("name", "")
                if key.startswith("private_"):
                    who = pname or key.split("_", 1)[1]
                    ch = "私聊"
                elif "_person_" in key:
                    gid = key.split("_person_")[0].replace("group_", "")
                    who = pname or key.split("_person_")[1]
                    ch = f"群聊({gid})"
                else:
                    continue
                for m in mem.get("conversation", []):
                    if keyword in m.get("content", ""):
                        tag = "TA" if m["direction"] == "in" else "机器人"
                        results.append({
                            "who": who, "channel": ch,
                            "time": m.get("time", ""),
                            "direction": tag,
                            "content": m.get("content", "")[:200],
                        })
            except Exception:
                continue

    if not results:
        return f"未找到包含「{keyword}」的对话记录。"
    results.sort(key=lambda x: x["time"], reverse=True)
    offset = int(args.get("offset", 0))
    page = results[offset:offset + limit]
    lines = [f"🔍 搜索「{keyword}」找到 {len(results)} 条记录："]
    for r in page:
        lines.append(f"  [{r['time']}] [{r['channel']}] {r['who']}({r['direction']}): {r['content']}")
    if len(results) > offset + limit:
        lines.append(f"  ... 还有 {len(results) - offset - limit} 条，offset={offset + limit} 查看更多")
    return "\n".join(lines)


def _tool_get_person_stats(args: dict, ctx: dict) -> str:
    target_str = args.get("target", "")
    if not target_str:
        return "错误：缺少 target 参数。"
    targets, unresolved = _resolve_targets(target_str)
    if not targets:
        hint = f"找不到「{target_str}」。"
        if unresolved:
            hint += f" {', '.join(unresolved)}"
        return hint
    _, qq, person_name = targets[0]
    total_in, total_out = 0, 0
    first_time, last_time = None, None
    channels = set()
    for f in DATA_DIR.glob(f"*{qq}*.json"):
        try:
            mem = json.loads(f.read_text("utf-8"))
            key = mem.get("key", "")
            if key.startswith("private_"):
                channels.add("私聊")
            elif "_person_" in key:
                channels.add(f"群({key.split('_person_')[0].replace('group_', '')})")
            for m in mem.get("conversation", []):
                t = m.get("time", "")
                if m.get("direction") == "in":
                    total_in += 1
                else:
                    total_out += 1
                if first_time is None or t < first_time:
                    first_time = t
                if last_time is None or t > last_time:
                    last_time = t
        except Exception:
            continue
    profile = _get_person_profile(qq)
    relations = _extract_relation(profile)
    notes_count = len(profile.get("notes", []))

    # 补充 SQLite 关系标签
    sqlite_tags = []
    try:
        from db import get_person_tags as _db_get_tags
        sqlite_tags = _db_get_tags(qq)
    except Exception:
        pass

    # 补充 SQLite 关键信息数量
    sqlite_facts_count = 0
    try:
        from db import get_key_facts as _db_get_facts
        sqlite_facts_count = len(_db_get_facts(qq))
    except Exception:
        pass

    total = total_in + total_out

    # 关系标签优先使用 SQLite 数据，回退到 JSON
    relation_display = ', '.join(sqlite_tags) if sqlite_tags else (relations or '未设定')

    # 补充 SQLite 统计（全局消息数）
    db_stats_line = ""
    try:
        from db import count_messages_by_sender as _db_count
        db_total_in, _db_out, db_first, db_last = _db_count(qq)
        if db_total_in > 0:
            db_stats_line = f"\n  SQLite全局消息：{db_total_in} 条（自迁移后）"
            if db_first:
                db_stats_line += f"\n  全局首次：{db_first[:16]}"
            if db_last:
                db_stats_line += f"\n  全局最近：{db_last[:16]}"
    except Exception:
        pass

    if total == 0:
        return f"📊 {person_name}({qq}) 暂无对话记录。"
    notes_str = f"  备注数量：{notes_count} 条"
    if sqlite_facts_count > 0:
        notes_str += f"（SQLite关键信息 {sqlite_facts_count} 条）"
    return (
        f"📊 {person_name}（{qq}）统计：\n"
        f"  总对话：{total} 条（接收 {total_in}，发送 {total_out}）\n"
        f"  首次联系：{first_time}\n"
        f"  最近联系：{last_time}\n"
        f"  活跃频道：{'、'.join(sorted(channels)) if channels else '私聊'}\n"
        f"  关系标签：{relation_display}\n"
        + notes_str
        + db_stats_line
    )


def _tool_list_all_persons(args: dict, ctx: dict) -> str:
    results = []
    for f in sorted(DATA_DIR.glob("private_*.json")):
        try:
            mem = json.loads(f.read_text("utf-8"))
            qq = f.stem.split("_", 1)[1]
            person_name = mem.get("name", "") or qq
            notes = [n.get("note", "") for n in mem.get("notes", [])]
            notes_count = len(notes)
            relations = _extract_relation(mem)
            notes_preview = "、".join(notes[:3]) if notes else "无"
            if len(notes) > 3:
                notes_preview += f"...等{len(notes)}条"

            # 补充 SQLite 数据：标签和真实姓名
            sqlite_tags = []
            sqlite_real_name = ""
            try:
                from db import get_person_tags, get_person_real_name, get_profile
                sqlite_tags = get_person_tags(qq)
                sqlite_real_name = get_person_real_name(qq)
                db_prof = get_profile(qq)
                if db_prof and db_prof.get("user_summary") and not notes_preview or notes_preview == "无":
                    notes_preview = db_prof["user_summary"][:60]
                if db_prof and db_prof.get("fixed_name"):
                    person_name = db_prof["fixed_name"]
            except Exception:
                pass

            relation_display = ', '.join(sqlite_tags) if sqlite_tags else (relations or "-")

            results.append({
                "qq": qq, "name": person_name,
                "relation": relation_display,
                "notes_count": notes_count,
                "notes_preview": notes_preview,
            })
        except Exception:
            pass
    if not results:
        return "还没有任何联系人数据。"
    limit = int(args.get("limit", 20))
    offset = int(args.get("offset", 0))
    page = results[offset:offset + limit]
    lines = [f"📋 已知联系人（共 {len(results)} 人）："]
    for r in page:
        rel = f" [{r['relation']}]" if r['relation'] != '-' else ""
        lines.append(
            f"  - {r['name']}({r['qq']}){rel}"
            f" · {r['notes_count']}条备注：{r['notes_preview']}"
        )
    return "\n".join(lines)


def _tool_list_all_notes(args: dict, ctx: dict) -> str:
    results = []
    for f in sorted(DATA_DIR.glob("private_*.json")):
        try:
            mem = json.loads(f.read_text("utf-8"))
            qq = f.stem.split("_", 1)[1]
            person_name = mem.get("name", "") or qq
            notes = [n.get("note", "") for n in mem.get("notes", [])]
            if notes:
                for note in notes:
                    results.append(f"  - {person_name}({qq})：{note}")
        except Exception:
            pass

    # 补充 SQLite 结构化备注
    sqlite_notes = []
    for f in sorted(DATA_DIR.glob("private_*.json")):
        try:
            mem = json.loads(f.read_text("utf-8"))
            qq = f.stem.split("_", 1)[1]
            person_name = mem.get("name", "") or qq
            from db import get_person_tags, get_person_real_name, get_key_facts, get_free_notes, get_person_city

            city = get_person_city(qq)
            if city:
                sqlite_notes.append(f"  - {person_name}({qq})：城市:{city} [SQLite]")

            real_name = get_person_real_name(qq)
            if real_name:
                sqlite_notes.append(f"  - {person_name}({qq})：真实姓名:{real_name} [SQLite]")

            tags = get_person_tags(qq)
            for tag in tags:
                sqlite_notes.append(f"  - {person_name}({qq})：关系:{tag} [SQLite]")

            facts = get_key_facts(qq)
            for fact in facts:
                sqlite_notes.append(f"  - {person_name}({qq})：关键信息:{fact} [SQLite]")

            free = get_free_notes(qq)
            if free:
                sqlite_notes.append(f"  - {person_name}({qq})：自由备注:{free} [SQLite]")
        except Exception:
            pass

    if not results and not sqlite_notes:
        return "还没有任何备注信息。"
    out = "📋 所有备注信息：\n" + "\n".join(results)
    if sqlite_notes:
        out += "\n\n📋 SQLite结构化数据：\n" + "\n".join(sqlite_notes)
    return out


def _tool_add_admin(args: dict, ctx: dict) -> str:
    targets, unresolved = _resolve_targets(args.get("target", ""))
    if not targets:
        return f"找不到目标「{args.get('target', '')}」。请确认名字或QQ号是否正确。"
    _, qq, name = targets[0]
    ok = _owner_add(qq)
    if ok:
        _sync_person_bidirectional(qq)
    return f"✅ 已将 {name}({qq}) 设为管理员" if ok else f"{name}({qq}) 已经是管理员"


def _tool_remove_admin(args: dict, ctx: dict) -> str:
    targets, unresolved = _resolve_targets(args.get("target", ""))
    if not targets:
        return f"找不到目标「{args.get('target', '')}」。请确认名字或QQ号是否正确。"
    _, qq, name = targets[0]
    ok = _owner_remove(qq)
    if ok:
        _sync_person_bidirectional(qq)
    return f"✅ 已移除 {name}({qq}) 的管理员权限" if ok else f"{name}({qq}) 不是管理员"


def _tool_list_admins(args: dict, ctx: dict) -> str:
    masters = _get_master_ids()
    names = []
    for m_id in sorted(masters):
        name = _resolve_speaker_name(m_id)
        names.append(f"{name}({m_id})" if name != m_id[-4:] else m_id)
    return f"当前管理员（{len(masters)}人）：{', '.join(names) if masters else '无'}"


def _tool_start_game(args: dict, ctx: dict) -> str:
    game_type = args.get("game", "").strip()
    if not game_type:
        return "请指定游戏名称。可选：猜数字(guess_number)、数字炸弹(number_bomb)、谁是卧底(undercover)、脑筋急转弯(riddle)、你画我猜(word_guess)、真心话大冒险(truth_dare)。"
    game_id = _get_game_id(ctx["msg_type"], ctx.get("group_id", ""), ctx["chat_id"])
    return _start_game(game_id, game_type, ctx["sender_id"], ctx["chat_id"], ctx["msg_type"])


def _tool_game_action(args: dict, ctx: dict) -> str:
    game_id = _get_game_id(ctx["msg_type"], ctx.get("group_id", ""), ctx["chat_id"])
    return _handle_game_action(game_id, ctx["sender_id"], args.get("action", ""))


def _tool_end_game(args: dict, ctx: dict) -> str:
    game_id = _get_game_id(ctx["msg_type"], ctx.get("group_id", ""), ctx["chat_id"])
    return _end_game(game_id)


def _tool_game_status(args: dict, ctx: dict) -> str:
    game_id = _get_game_id(ctx["msg_type"], ctx.get("group_id", ""), ctx["chat_id"])
    return _game_status_text(game_id)


def _tool_summarize_recent(args: dict, ctx: dict) -> str:
    target = args.get("target", "").strip()
    hours = int(args.get("hours", 24))
    sender_id = ctx.get("sender_id", "")
    if not target:
        return _summarize_inbox(sender_id, hours)
    else:
        targets, _ = _resolve_targets(target)
        if not targets:
            return f"找不到目标「{target}」。请确认名字或QQ号。"
        _, qq, name = targets[0]
        return _summarize_person_dialog(qq, hours)


def _tool_set_nickname(args: dict, ctx: dict) -> str:
    target_str = args.get("target", "")
    targets, unresolved = _resolve_targets(target_str)
    if not targets:
        # 显式 target 解析失败 → 返回错误（不 fallback 到发送者）
        if target_str.strip():
            return f"找不到目标「{target_str}」。请确认名字或提供QQ号。"
        # target 为空 → 默认自己
        sender = ctx.get("sender_id", "")
        if sender:
            targets = [("private", sender, ctx.get("sender_nick", sender))]
        else:
            return "找不到目标。"
    _, qq, name = targets[0]
    new_name = args.get("nickname", "").strip()
    if not new_name:
        return "称呼不能为空。"
    confirmed = args.get("confirmed", False)
    # 模糊匹配检查：非精确匹配时先向用户确认
    if not confirmed:
        from config import _peek_match_confidence
        matched_qq, matched_name, conf = _peek_match_confidence(args.get("target", ""))
        if matched_qq and conf < 0.95 and matched_qq == qq:
            return (f"🔍 模糊匹配「{args.get('target', '')}」→「{name}」(QQ:{qq})。"
                    f"请确认是否正确？确认后回复「确认」再改。")
    set_by = ctx.get("sender_id", "admin")
    result = _set_nickname(qq, new_name, set_by)
    if result["ok"]:
        _sync_person_bidirectional(qq)
        # 通知对方称呼已变更
        _notify_user(qq, f"你的称呼已被更新为「{new_name}」", operator=set_by)
        return f"✅ {name}({qq})：{result['message']}"
    else:
        return f"❌ {name}({qq})：{result['message']}"


def _tool_switch_nickname(args: dict, ctx: dict) -> str:
    target_str = args.get("target", "")
    targets, unresolved = _resolve_targets(target_str)
    if not targets:
        if target_str.strip():
            return f"找不到目标「{target_str}」。请确认名字或提供QQ号。"
        sender = ctx.get("sender_id", "")
        if sender:
            targets = [("private", sender, ctx.get("sender_nick", sender))]
        else:
            return "找不到目标。"
    _, qq, name = targets[0]
    new_name = _switch_active_slot(qq)
    if new_name:
        _sync_person_bidirectional(qq)
        return f"✅ {name}({qq}) 的称呼已切换为「{new_name}」"
    else:
        return f"❌ {name}({qq}) 只有一个称呼位有值，无法切换。"


def _tool_get_nickname_status(args: dict, ctx: dict) -> str:
    target_str = args.get("target", "")
    targets, unresolved = _resolve_targets(target_str)
    if not targets:
        if target_str.strip():
            return f"找不到目标「{target_str}」。请确认名字或提供QQ号。"
        sender = ctx.get("sender_id", "")
        if sender:
            targets = [("private", sender, ctx.get("sender_nick", sender))]
        else:
            return "找不到目标。"
    _, qq, name = targets[0]
    data = _get_nickname_data(qq)
    a_val = data.get("slot_a_value", "") or "(空)"
    a_by  = data.get("slot_a_set_by", "")
    a_time = data.get("slot_a_set_time", "")
    a_locked = " 🔒" if data.get("slot_a_locked") else ""
    b_val = data.get("slot_b_value", "") or "(空)"
    b_by  = data.get("slot_b_set_by", "")
    b_time = data.get("slot_b_set_time", "")
    b_locked = " 🔒" if data.get("slot_b_locked") else ""
    active = data.get("active_slot", "")
    a_mark = " ★当前" if active == "slot_a" and a_val != "(空)" else ""
    b_mark = " ★当前" if active == "slot_b" and b_val != "(空)" else ""
    relation = data.get("relation", "") or "未设置"
    lines = [
        f"📋 {name}({qq}) 的称呼状态：",
        f"  slot_a: 「{a_val}」{a_mark}{a_locked}",
    ]
    if a_by:
        lines[-1] += f" (由{a_by}设置于{a_time})"
    lines.append(f"  slot_b: 「{b_val}」{b_mark}{b_locked}")
    if b_by:
        lines[-1] += f" (由{b_by}设置于{b_time})"
    lines.append(f"  关系：{relation}")
    lines.append(f"  首次对话完成：{'是' if data.get('first_contact_done') else '否'}")
    return "\n".join(lines)


def _tool_add_scheduled_task(args: dict, ctx: dict) -> str:
    task_type = args.get("task_type", "daily")
    name = args.get("name", "").strip()
    trigger_time = args.get("trigger_time", "").strip()
    interval_hours = float(args.get("interval_hours", 0))
    content = args.get("content", "").strip()
    target_type = args.get("target_type", "private")
    target_id = args.get("target_id", "").strip()
    action_type = args.get("action_type", "ai_generate")

    if not name or not content:
        return "任务名称和消息内容不能为空"

    if task_type == "interval" and interval_hours <= 0:
        return "interval 类型必须指定 interval_hours（大于0的小时数）"

    sender = ctx.get("sender_id", "")
    task_id = add_scheduled_task(
        name=name, task_type=task_type, trigger_time=trigger_time,
        interval_hours=interval_hours, target_type=target_type, target_id=target_id,
        content=content, created_by=sender, action_type=action_type,
    )

    trigger_desc = f"每{interval_hours}小时" if task_type == "interval" else trigger_time
    return (
        f"已添加定时任务「{name}」（{task_type}），"
        f"出发时间：{trigger_desc}，目标：{target_type}/{target_id}。任务ID：{task_id}"
    )


def _tool_list_my_tasks(args: dict, ctx: dict) -> str:
    sender = ctx.get("sender_id", "")
    is_master = sender in _get_master_ids()
    status = args.get("status", "all")

    all_tasks = get_scheduled_tasks()
    if not is_master:
        all_tasks = [t for t in all_tasks if t["created_by"] == sender]

    if status == "enabled":
        all_tasks = [t for t in all_tasks if t["enabled"]]
    elif status == "disabled":
        all_tasks = [t for t in all_tasks if not t["enabled"]]

    if not all_tasks:
        return "暂无定时任务"

    lines = ["定时任务列表："]
    for i, t in enumerate(all_tasks, 1):
        status_str = "启用" if t["enabled"] else "停用"
        if t["task_type"] == "interval":
            ih = float(t.get("interval_hours", 0))
            trigger = f"每{ih}h" if ih >= 1 else f"每{int(ih * 60)}分钟"
        else:
            ft = t.get("first_trigger", "") or t["trigger_time"]
            trigger = ft
        lines.append(
            f"{i}. [{t['id']}] {t['name']} - {t['task_type']} {trigger} "
            f"→ {t['target_type']}/{t['target_id']} [{status_str}]"
        )
    return "\n".join(lines)


def _tool_delete_scheduled_task(args: dict, ctx: dict) -> str:
    sender = ctx.get("sender_id", "")
    task_id = args.get("task_id", "").strip()

    if not task_id:
        return "请提供任务ID"

    task = get_scheduled_task(task_id)
    if not task:
        return f"找不到任务：{task_id}"

    # 主人可以删除任何任务，非主人只能删自己的
    if sender not in _get_master_ids() and task["created_by"] != sender:
        return "你只能删除自己创建的任务"

    delete_scheduled_task(task_id)
    return f"已删除任务「{task['name']}」（{task_id}）"


def _tool_toggle_scheduled_task(args: dict, ctx: dict) -> str:
    sender = ctx.get("sender_id", "")
    task_id = args.get("task_id", "").strip()
    enable = bool(args.get("enable", True))

    if not task_id:
        return "请提供任务ID"

    task = get_scheduled_task(task_id)
    if not task:
        return f"找不到任务：{task_id}"

    # 主人可以操作任何任务，非主人只能操作自己的
    if sender not in _get_master_ids() and task["created_by"] != sender:
        return "你只能操作自己创建的任务"

    toggle_scheduled_task(task_id, enable)
    action = "启用" if enable else "停用"
    return f"已{action}任务「{task['name']}」（{task_id}）"


def _tool_set_person_address(args: dict, ctx: dict) -> str:
    """设置所在城市（单一值覆盖 + 通知对方）+ 模糊匹配确认。"""
    sender = ctx.get("sender_id", "")
    target_str = args.get("target", "").strip()
    city = args.get("city", "").strip()
    if not target_str or not city:
        return "错误：缺少 target 或 city 参数。"
    confirmed = args.get("confirmed", False)
    if not confirmed:
        from config import _peek_match_confidence
        matched_qq, matched_name, conf = _peek_match_confidence(target_str)
        if matched_qq and conf < 0.95:
            return (f"🔍 模糊匹配「{target_str}」→「{matched_name}」(QQ:{matched_qq})。"
                    f"请确认是否正确？确认后回复「确认」再改。")

    targets, _ = _resolve_targets(target_str)
    if not targets:
        return f"找不到「{target_str}」，请确认名字或QQ号。"
    qq = targets[0][1]
    name = targets[0][2]
    set_person_city(qq, city, set_by=sender)
    # 通知对方
    _notify_user(qq, f"你的所在城市已被更新为「{city}」", operator=sender)
    return f"✅ 已记录：{name}（{qq}）的所在地设为「{city}」。以后推送天气提醒时会发{city}的天气给ta~"


def _tool_batch_send_weather_alerts(args: dict, ctx: dict) -> str:
    targets = args.get("targets", "all")
    custom_message = args.get("custom_message", "")
    return _batch_send_weather(targets, custom_message)


_TOOL_HANDLERS: dict[str, callable] = {
    "check_inbox": _tool_check_inbox,
    "send_message": _tool_send_message,
    "broadcast_message": _tool_broadcast_message,
    "list_contacts": _tool_list_contacts,
    "web_search": _tool_web_search,
    "generate_image": _tool_generate_image,
    "send_file": _tool_send_file,
    "get_weather": _tool_get_weather,
    "mute_chat": _tool_mute_chat,
    "unmute_chat": _tool_unmute_chat,
    "get_person_info": _tool_get_person_info,
    "refresh_contacts": _tool_refresh_contacts,
    "set_person_relation": _tool_set_person_relation,
    "set_person_note": _tool_set_person_note,
    "get_person_conversation": _tool_get_person_conversation,
    "group_broadcast": _tool_group_broadcast,
    "analyze_personality": _tool_analyze_personality,
    "list_group_members": _tool_list_group_members,
    "get_group_activity": _tool_get_group_activity,
    "search_conversations": _tool_search_conversations,
    "get_person_stats": _tool_get_person_stats,
    "list_all_persons": _tool_list_all_persons,
    "list_all_notes": _tool_list_all_notes,
    "add_admin": _tool_add_admin,
    "remove_admin": _tool_remove_admin,
    "list_admins": _tool_list_admins,
    "start_game": _tool_start_game,
    "game_action": _tool_game_action,
    "end_game": _tool_end_game,
    "game_status": _tool_game_status,
    "summarize_recent": _tool_summarize_recent,
    "set_nickname": _tool_set_nickname,
    "switch_nickname": _tool_switch_nickname,
    "get_nickname_status": _tool_get_nickname_status,
    "add_scheduled_task": _tool_add_scheduled_task,
    "list_my_tasks": _tool_list_my_tasks,
    "delete_scheduled_task": _tool_delete_scheduled_task,
    "toggle_scheduled_task": _tool_toggle_scheduled_task,
    "set_person_address": _tool_set_person_address,
    "batch_send_weather_alerts": _tool_batch_send_weather_alerts,
}


# ==================== Phase 3.1: 动态工具注册框架 ====================

_tool_registry: dict[str, dict] = {}

def register_tool(name: str, description: str, params: dict, handler: Callable) -> None:
    """动态注册工具：写入 registry + 同步更新 TOOLS / _TOOL_HANDLERS。"""
    spec = {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": params},
    }
    _tool_registry[name] = {"spec": spec, "handler": handler}
    idx = next((i for i, t in enumerate(TOOLS)
                if t.get("function", {}).get("name") == name), -1)
    if idx >= 0:
        TOOLS[idx] = spec
    else:
        TOOLS.append(spec)
    _TOOL_HANDLERS[name] = handler


# ==================== Phase 3.2: 8 个新 AI 工具 ====================

def _tool_manage_schedule(args: dict, ctx: dict) -> str:
    """日程管理：action=create/query/delete"""
    from db import add_schedule, get_schedules, delete_schedule
    action = args.get("action", "query")
    sender = ctx.get("sender_id", "")
    if action == "create":
        title = args.get("title", "").strip()
        when = args.get("when", "").strip()
        if not title or not when:
            return "缺少 title 或 when 参数"
        sid = add_schedule(sender, title, when, args.get("description", ""))
        return f"日程已创建 [{sid}] {title} @ {when}"
    elif action == "query":
        items = get_schedules(sender)
        if not items:
            return "暂无日程"
        lines = [f"{i+1}. [{it['id'][:8]}] {it['title']} @ {it['when']}" for i, it in enumerate(items)]
        return "你的日程：\n" + "\n".join(lines)
    elif action == "delete":
        sid = args.get("id", "")
        if delete_schedule(sender, sid):
            return f"已删除日程 {sid}"
        return "未找到该日程"
    return "不支持的操作，支持 create/query/delete"

def _tool_manage_tasks(args: dict, ctx: dict) -> str:
    """待办清单：action=add/list/done"""
    from db import add_todo, get_todos, complete_todo
    action = args.get("action", "list")
    sender = ctx.get("sender_id", "")
    if action == "add":
        title = args.get("title", "").strip()
        if not title:
            return "缺少 title"
        tid = add_todo(sender, title)
        return f"待办已添加 [{tid}] {title}"
    elif action == "list":
        items = get_todos(sender)
        if not items:
            return "暂无待办"
        lines = [f"{i+1}. [{'✓' if it.get('done') else '○'}] {it['title']}" for i, it in enumerate(items)]
        return "你的待办：\n" + "\n".join(lines)
    elif action == "done":
        tid = args.get("id", "")
        if complete_todo(sender, tid):
            return "已完成"
        return "未找到"
    return "不支持的操作，支持 add/list/done"

def _tool_get_news(args: dict, ctx: dict) -> str:
    """获取新闻摘要"""
    query = args.get("query", "今日热点")
    count = min(int(args.get("count", 5)), 10)
    result = _web_search(query)
    if result:
        return result[:1500]
    return f"无法获取 {query} 的新闻"

def _tool_file_operate(args: dict, ctx: dict) -> str:
    """文件操作（沙箱限制在 data/files/）"""
    return "此发行版暂未启用文件工具。"
    from config import DATA_DIR
    action = args.get("action", "list")
    files_dir = DATA_DIR / "files"
    if action == "list":
        files = list(files_dir.glob("*"))
        if not files:
            return "files/ 目录为空"
        return "文件列表：\n" + "\n".join(f"  - {f.name}" for f in files)
    elif action == "read":
        name = args.get("name", "")
        if not name or "/" in name or ".." in name:
            return "非法文件名"
        f = files_dir / name
        if not f.exists():
            return f"文件不存在: {name}"
        return f.read_text("utf-8")[:2000]
    return "不支持的操作，支持 list/read"

def _tool_data_query(args: dict, ctx: dict) -> str:
    """联系人统计查询"""
    from db import query_recent_chats
    days = int(args.get("days", 7))
    chats = query_recent_chats(days=days, limit=20)
    if not chats:
        return f"最近 {days} 天无聊天记录"
    return f"最近 {days} 天聊天记录（前 20 条）：\n" + "\n".join(
        f"  [{c['send_time'][:16]}] {c['sender']}: {c['msg'][:80]}" for c in chats
    )

def _tool_auto_reply(args: dict, ctx: dict) -> str:
    """设置自动回复规则：action=add/list"""
    from db import add_auto_reply_rule, get_auto_reply_rules
    action = args.get("action", "list")
    sender = ctx.get("sender_id", "")
    if action == "add":
        trigger = args.get("trigger", "").strip()
        response = args.get("response", "").strip()
        if not trigger or not response:
            return "缺少 trigger 或 response"
        rid = add_auto_reply_rule(sender, trigger, response)
        return f"规则已添加 [{rid}] 当「{trigger}」→「{response}」"
    elif action == "list":
        rules = get_auto_reply_rules(sender)
        if not rules:
            return "暂无自动回复规则"
        return "规则：\n" + "\n".join(f"  [{r['id'][:8]}] 「{r['trigger']}」→「{r['response']}」" for r in rules)
    return "不支持的操作，支持 add/list"

def _tool_translate(args: dict, ctx: dict) -> str:
    """翻译文本（使用 DeepSeek，不引入付费翻译 API）"""
    text = args.get("text", "")
    target = args.get("target", "中文")
    if not text:
        return "缺少 text 参数"
    result = _call_deepseek(
        f"你是翻译助手。将以下文本翻译为 {target}，只输出译文，不要解释。",
        text, max_tokens=500)
    return result or "翻译失败"

def _tool_remind_me(args: dict, ctx: dict) -> str:
    """单次定时提醒（复用 scheduled_tasks 的 once 类型）"""
    from db import add_scheduled_task
    from datetime import datetime
    sender = ctx.get("sender_id", "")
    message = args.get("message", "").strip()
    when = args.get("when", "").strip()
    if not message or not when:
        return "缺少 message 或 when（格式：YYYY-MM-DD HH:MM）"
    try:
        trigger_time = datetime.strptime(when, "%Y-%m-%d %H:%M").isoformat()
    except ValueError:
        return "时间格式错误，应为 YYYY-MM-DD HH:MM"
    task_id = add_scheduled_task(
        name="提醒", task_type="once", trigger_time=trigger_time,
        interval_hours=0, target_type="private", target_id=sender,
        content=f"提醒：{message}", created_by=sender)
    return f"已设置提醒 [{task_id}] {when} → {message}"


def _register_new_tools():
    """注册 8 个新工具到 TOOLS / _TOOL_HANDLERS"""
    register_tool(name="manage_schedule", description="日程管理：创建、查询、删除日程事件。action=create(需title+when)/query/delete(需id)。",
        params={"type": "object", "properties": {"action": {"type": "string", "enum": ["create", "query", "delete"]},
            "title": {"type": "string"}, "when": {"type": "string", "description": "时间，如 2026-07-15 14:00"},
            "description": {"type": "string"}, "id": {"type": "string"}}, "required": ["action"]},
        handler=_tool_manage_schedule)
    register_tool(name="manage_tasks", description="待办清单：添加、列出、完成待办。action=add(需title)/list/done(需id)。",
        params={"type": "object", "properties": {"action": {"type": "string", "enum": ["add", "list", "done"]},
            "title": {"type": "string"}, "id": {"type": "string"}}, "required": ["action"]},
        handler=_tool_manage_tasks)
    register_tool(name="get_news", description="获取新闻摘要。返回搜索结果的 AI 摘要。",
        params={"type": "object", "properties": {"query": {"type": "string", "description": "搜索关键词"},
            "count": {"type": "integer", "description": "条数，默认5，最大10"}}, "required": ["query"]},
        handler=_tool_get_news)
    register_tool(name="file_operate", description="文件操作（沙箱限制在 data/files/）。action=list(列出)/read(读取，需name)。",
        params={"type": "object", "properties": {"action": {"type": "string", "enum": ["list", "read"]},
            "name": {"type": "string"}}, "required": ["action"]},
        handler=_tool_file_operate)
    register_tool(name="data_query", description="查询最近聊天记录统计。days=天数（默认7）。",
        params={"type": "object", "properties": {"days": {"type": "integer", "description": "查询最近N天"}}},
        handler=_tool_data_query)
    register_tool(name="auto_reply", description="设置自动回复规则。action=add(需trigger+response)/list。",
        params={"type": "object", "properties": {"action": {"type": "string", "enum": ["add", "list"]},
            "trigger": {"type": "string"}, "response": {"type": "string"}}, "required": ["action"]},
        handler=_tool_auto_reply)
    register_tool(name="translate", description="翻译文本（使用 AI，无需付费API）。target=目标语言（默认中文）。",
        params={"type": "object", "properties": {"text": {"type": "string", "description": "要翻译的文本"},
            "target": {"type": "string", "description": "目标语言，如中文/英文"}}, "required": ["text"]},
        handler=_tool_translate)
    register_tool(name="remind_me", description="设置单次定时提醒。when=时间（格式 YYYY-MM-DD HH:MM），message=提醒内容。",
        params={"type": "object", "properties": {"when": {"type": "string", "description": "提醒时间，如 2026-07-15 14:00"},
            "message": {"type": "string", "description": "提醒内容"}}, "required": ["when", "message"]},
        handler=_tool_remind_me)

# ==================== 图片/文档处理工具 ====================

def _tool_analyze_image(args: dict, ctx: dict) -> str:
    """深入分析图片内容：OCR 提取文字、识别题目、解析图表。"""
    description = args.get("description", "")
    question = args.get("question", "")
    if not description and not question:
        return "缺少 description 或 question 参数"
    # 基于已有的图片描述做深度分析（调 DeepSeek）
    user_prompt = f"请分析以下图片内容：\n图片描述：{description}"
    if question:
        user_prompt += f"\n用户问题：{question}\n请结合图片描述回答用户的问题。"
    else:
        user_prompt += "\n请提取图片中的文字/公式/数据，并解释其含义。"
    result = _call_deepseek("你是图片分析助手。根据图片描述回答用户问题，提取关键信息。", user_prompt, max_tokens=800)
    return result or "分析失败"

def _tool_process_document(args: dict, ctx: dict) -> str:
    """处理文档：读取文件内容并摘要/提取/翻译。"""
    return "此发行版暂未启用文件工具。"
    from config import DATA_DIR
    action = args.get("action", "summary")
    name = args.get("name", "").strip()
    question = args.get("question", "")
    if not name:
        return "缺少 name 参数（文件名）"
    if action == "read":
        # 直接读取
        return _read_file_content(str(DATA_DIR / "files" / name), name)
    # summary / qa ：读取 + AI 处理
    content = _read_file_content(str(DATA_DIR / "files" / name), name)
    if content.startswith("（"):
        return content  # 读取失败
    if action == "summary":
        result = _call_deepseek("你是文档摘要助手。用简洁的中文总结以下文档的要点（200字内）：", content[:3000], max_tokens=400)
        return result or "摘要失败"
    # qa
    if not question:
        return "缺少 question 参数"
    result = _call_deepseek("你是文档问答助手。根据以下文档内容回答用户的问题：",
        f"文档内容：{content[:3000]}\n\n用户问题：{question}", max_tokens=600)
    return result or "回答失败"

def _register_media_tools():
    register_tool(name="analyze_image", description="深入分析图片内容（OCR提取文字、识别题目、解析图表）。"
        "description=图片描述（由视觉模型生成），question=用户的具体问题（可选）。",
        params={"type": "object", "properties": {
            "description": {"type": "string", "description": "图片描述，如'一张数学题截图'"},
            "question": {"type": "string", "description": "用户关于图片的问题，如'这道题怎么解'"},
        }, "required": ["description"]},
        handler=_tool_analyze_image)
    register_tool(name="process_document", description="处理文档文件：读取/摘要/问答。action=read(读取)/summary(摘要)/qa(问答，需question)。"
        "name=文件名（在 data/files/ 中）。",
        params={"type": "object", "properties": {
            "action": {"type": "string", "enum": ["read", "summary", "qa"]},
            "name": {"type": "string", "description": "文件名"},
            "question": {"type": "string", "description": "action=qa 时必填"},
        }, "required": ["action", "name"]},
        handler=_tool_process_document)

def _tool_my_profile(args: dict, ctx: dict) -> str:
    """非管理员个人主页：整合称呼、统计、提醒、关系等自查信息。"""
    sender = ctx.get("sender_id", "")
    # 称呼
    nickname_status = _tool_get_nickname_status({"target": sender}, ctx) if False else ""
    from config import _get_person_profile, _get_nickname_data
    profile = _get_person_profile(sender) or {}
    from db import get_resolved_name, get_person_tags, get_user_summary, get_person_city
    name = get_resolved_name(sender) or "未设置"
    tags = get_person_tags(sender)
    summary = get_user_summary(sender) or "暂无画像"
    city = get_person_city(sender) or "未设置"
    # 定时任务
    from db import get_scheduled_tasks
    my_tasks = [t for t in get_scheduled_tasks() if t.get("created_by") == sender]
    # 称呼
    nick_data = _get_nickname_data(sender)
    active_nick = ""
    if nick_data:
        slot = nick_data.get("active_slot", "")
        if slot:
            active_nick = nick_data.get(f"{slot}_value", "")
    lines = [f"📋 我的主页（{name}）："]
    if active_nick:
        lines.append(f"  称呼：{active_nick}")
    if tags:
        lines.append(f"  关系：{', '.join(tags)}")
    lines.append(f"  城市：{city}")
    lines.append(f"  定时提醒：{len(my_tasks)} 个")
    lines.append(f"  画像：{summary[:60]}")
    return "\n".join(lines)


def _tool_set_my_persona(args: dict, ctx: dict) -> str:
    """设置我自己的人设（仅对我自己生效，不干扰其他用户）。"""
    sender = ctx.get("sender_id", "")
    persona_text = args.get("persona", "").strip()
    if not persona_text:
        return "缺少 persona 参数（你的人设描述）"
    from users import set_user_persona
    set_user_persona(sender, persona_text)
    return f"✅ 已设置你的人设（仅对你生效）：{persona_text[:100]}"


def _tool_set_mute(args: dict, ctx: dict) -> str:
    """设置静默（N 小时内不主动找我）。duration=小时数。"""
    sender = ctx.get("sender_id", "")
    duration = float(args.get("duration", 12))
    if duration <= 0 or duration > 720:
        return "静默时长需在 0.1~720 小时（30天）之间"
    from users import set_mute
    until = set_mute(sender, duration)
    return f"🔇 已开启静默 {duration} 小时，预计 {until} 解除。期间定时任务/群发不会打扰你。"


def _tool_cancel_mute(args: dict, ctx: dict) -> str:
    """解除静默。target=目标QQ（不填则解除自己的静默）。"""
    from config import _get_master_ids
    sender = ctx.get("sender_id", "")
    is_master = sender in _get_master_ids()
    target_str = args.get("target", "").strip()
    if target_str:
        # 指定目标 → 只能管理员操作
        if not is_master:
            return "❌ 只有管理员才能解除他人的静默哦～"
        targets, unresolved = _resolve_targets(target_str)
        if not targets:
            return f"找不到「{target_str}」。"
        _, qq, name = targets[0]
    else:
        # 不指定 → 解除自己的静默
        qq = sender
        name = "你"
    from users import cancel_mute, is_muted
    if not is_muted(qq):
        return f"ℹ️ {name} 当前未处于静默状态。"
    cancel_mute(qq)
    # 通知管理员
    for _mid in _get_master_ids():
        if _mid != qq:
            _send_text("private", _mid, f"🔔 {name}({qq}) 的静默已解除。")
    return f"✅ 已解除 {name} 的静默，我现在恢复正常回复了~"


def _tool_mute_status(args: dict, ctx: dict) -> str:
    """查看当前静默状态。"""
    from users import get_mute_status, get_all_muted
    from config import _get_master_ids
    sender = ctx.get("sender_id", "")
    is_master = sender in _get_master_ids()
    if is_master:
        # 管理员看所有静默用户
        muted = get_all_muted()
        if not muted:
            return "当前没有用户处于静默状态。"
        lines = ["🔇 当前静默用户："]
        for qq, desc in muted:
            from config import _resolve_speaker_name
            name = _resolve_speaker_name(qq) or qq
            lines.append(f"  · {name}({qq})：{desc}")
        return "\n".join(lines)
    # 非管理员看自己
    status = get_mute_status(sender)
    return f"🔇 你的静默状态：{status}" if status else "🔔 你当前未处于静默状态。"


def _register_self_service_tools():
    register_tool(name="my_profile", description="查看我的个人主页：称呼、关系、城市、定时提醒、画像摘要。所有用户都可用。",
        params={"type": "object", "properties": {}},
        handler=_tool_my_profile)
    register_tool(name="set_my_persona", description="设置我自己的人设（仅对我生效，不干扰他人）。persona=人设描述文本。",
        params={"type": "object", "properties": {"persona": {"type": "string", "description": "你的人设描述"}}, "required": ["persona"]},
        handler=_tool_set_my_persona)
    register_tool(name="set_mute", description="设置静默（N 小时内不主动找我）。duration=小时数（默认12，最大720）。",
        params={"type": "object", "properties": {"duration": {"type": "number", "description": "静默小时数"}}, "required": ["duration"]},
        handler=_tool_set_mute)
    register_tool(name="mute_status", description="查看静默状态（管理员可查看所有静默用户）。",
        params={"type": "object", "properties": {}},
        handler=_tool_mute_status)
    register_tool(name="cancel_mute",
                  description="解除静默。target=目标QQ（不填则解除自己的静默，仅管理员可指定他人）。",
                  params={"type": "object",
                           "properties": {"target": {"type": "string",
                                                     "description": "目标QQ，不填=解除自己"}}},
                  handler=_tool_cancel_mute)

_register_media_tools()
_register_new_tools()
_register_self_service_tools()

# 不注册或暴露未完成安全隔离的文件工具。
_DISABLED_TOOLS = {"send_file", "process_document", "file_operate"}
TOOLS[:] = [spec for spec in TOOLS if spec.get("function", {}).get("name") not in _DISABLED_TOOLS]
for _disabled_tool in _DISABLED_TOOLS:
    _TOOL_HANDLERS.pop(_disabled_tool, None)
    _tool_registry.pop(_disabled_tool, None)

# 需要主人权限才能调用的工具（模块级常量：_execute_tool 执行拦截 + _ai_intent_route 工具表过滤共用）
_MASTER_ONLY_TOOLS = {
    "check_inbox", "broadcast_message", "list_contacts",
    "mute_chat", "unmute_chat",
    "refresh_contacts",
    "group_broadcast", "analyze_personality",
    "list_group_members", "get_group_activity",
    "list_all_persons", "list_all_notes",
    "add_admin", "remove_admin", "list_admins",
    "batch_send_weather_alerts",
    "data_query", "search_conversations",
}

# 「管理员/主人」等角色词目标：不是人名——send_message 据此转达给管理员，守卫据此放行
_ADMIN_ALIAS_TARGETS = {"管理员", "主人", "管理", "admin", "master"}


def _execute_tool(name: str, args: dict, ctx: dict) -> str:
    """执行 AI 请求的工具调用，返回结果字符串。集中权限检查。"""
    if name in _DISABLED_TOOLS:
        return "此发行版暂未启用文件工具。"
    # Phase 1.2: 幂等工具调用 ID，用于排查重复执行
    tool_call_id = f"{name}_{int(time.time())}_{random.randint(1000, 9999)}"
    sender = ctx.get("sender_id", "")
    is_master = sender in _get_master_ids()

    # 需要主人权限才能调用的工具（集合定义见模块级 _MASTER_ONLY_TOOLS）
    # send_message 已移出：非管理员可给管理员发消息（不能发给其他非管理员）
    # get_person_conversation / get_person_stats 已移出：非管理员可查自己的数据（自查模式）

    if name in _MASTER_ONLY_TOOLS and not is_master:
        return "❌ 权限不足：只有管理员才能执行此操作。你可以跟机器人聊天、玩游戏、或者给自己设置定时提醒哦～"

    # add_scheduled_task 非主人限制：只能给自己创建私聊任务
    if name == "add_scheduled_task" and not is_master:
        target_type = args.get("target_type", "private")
        target_id = str(args.get("target_id", ""))
        if target_type != "private" or target_id != sender:
            return "❌ 你只能给自己设置定时消息哦～用「X点提醒我XXX」这样的说法就行啦。"

    # set_nickname 非主人限制：只能设置自己的称呼
    if name == "set_nickname" and not is_master:
        target_str = args.get("target", "")
        if target_str:
            targets, _ = _resolve_targets(target_str)
            if not targets or targets[0][1] != sender:
                return "❌ 你只能设置自己的称呼哦～跟我说「叫我XX」就能改啦。"

    # set_person_address 非主人限制：只能设置自己的地址
    if name == "set_person_address" and not is_master:
        target_str = args.get("target", "")
        if target_str:
            targets, _ = _resolve_targets(target_str)
            if not targets or targets[0][1] != sender:
                return "❌ 你只能设置自己的地址哦～在聊天里说说你在哪个城市，我会帮你记录下来。"

    # set_person_note 非主人限制：只能给自己设置备注（称呼等）
    if name == "set_person_note" and not is_master:
        target_str = args.get("target", "")
        if target_str:
            targets, _ = _resolve_targets(target_str)
            if not targets or targets[0][1] != sender:
                return "❌ 你只能给自己设置备注哦～跟我说「叫我XX」就能改称呼啦。"

    # set_person_relation 非主人限制：只能给自己设关系标签
    if name == "set_person_relation" and not is_master:
        target_str = args.get("target", "")
        if target_str:
            targets, _ = _resolve_targets(target_str)
            if not targets or targets[0][1] != sender:
                return "❌ 你只能给自己设置关系标签哦～跟我说「我和主人是XX关系」就好啦。"

    # get_person_info 非主人限制：只能查自己
    if name == "get_person_info" and not is_master:
        target_str = args.get("target", "")
        targets, _ = _resolve_targets(target_str)
        if not targets or targets[0][1] != sender:
            return "❌ 你只能查看自己的信息哦～试试「我的信息」或「我的档案」吧。"

    # summarize_recent 非主人限制：只能总结自己
    if name == "summarize_recent" and not is_master:
        target_str = args.get("target", "")
        if target_str:
            targets, _ = _resolve_targets(target_str)
            if not targets or targets[0][1] != sender:
                return "❌ 你只能总结自己最近的对话哦～说「帮我总结一下最近的聊天」就可以啦。"

    # get_person_conversation 非主人限制：只能查看自己的对话（效果：非管理员查自己的聊天记录）
    if name == "get_person_conversation" and not is_master:
        target_str = args.get("target", "")
        if target_str:
            targets, _ = _resolve_targets(target_str)
            if not targets or targets[0][1] != sender:
                return "❌ 你只能查看自己的聊天记录哦～说「我的聊天记录」或「我跟机器人聊了啥」就可以啦。"
        else:
            # 没指定 target → 默认查自己
            args["target"] = sender

    # get_person_stats 非主人限制：只能查看自己的统计
    if name == "get_person_stats" and not is_master:
        target_str = args.get("target", "")
        if target_str:
            targets, _ = _resolve_targets(target_str)
            if not targets or targets[0][1] != sender:
                return "❌ 你只能查看自己的统计哦～说「我的统计」就可以啦。"
        else:
            args["target"] = sender

    # send_message 非主人限制：只能给管理员传话（目标解析不到也直接拒绝，不给"帮你找人"的空间）
    if name == "send_message" and not is_master:
        target_str = args.get("target", "")
        if target_str.strip() not in _ADMIN_ALIAS_TARGETS:
            targets, _ = _resolve_targets(target_str) if target_str else ([], [])
            # 所有收件人必须是管理员；解析不到目标同样拒绝（避免模型引导用户提供联系方式绕权限）
            if not targets:
                return "❌ 我只能帮普通用户给管理员传话哦～要说「帮我告诉管理员XXX」就行。"
            for t_type, t_id, t_name in targets:
                if t_id not in _get_master_ids():
                    return f"❌ 只能给管理员传话哦～不能发给 {t_name}({t_id})。要说「帮我告诉管理员XXX」就行。"

    # send_file 非主人限制：指定 target 时目标必须是管理员（与 send_message 同规则）
    if name == "send_file" and not is_master:
        sf_target = str(args.get("target", "") or "")
        if sf_target:
            sf_targets, _ = _resolve_targets(sf_target)
            if sf_targets and sf_targets[0][1] not in _get_master_ids():
                return "❌ 非管理员只能把文件发给管理员哦～不写收件人就是发给你自己。"

    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        return f"未知工具：{name}"
    # Phase 4.3: 熔断保护 — 连续失败 N 次后熔断，快速失败
    from safety import SafetyNet, CircuitBreakerOpenError
    from monitor import count_error
    try:
        result = SafetyNet.call_with_circuit(f"tool_{name}", handler, args, ctx)
    except CircuitBreakerOpenError:
        return f"❌ 工具「{name}」暂时不可用（连续失败过多，已熔断）。稍后再试。"
    except Exception as e:
        SafetyNet.record_failure(f"tool_{name}")
        count_error(f"tool_{name}")  # Phase 1.2: 错误计数
        log(f"工具执行失败 {name}: {e}")
        return f"❌ 执行「{name}」时出错：{e}"

    # P2-7: AI 自反思重试 — 工具返回"找不到目标"时，自动用模糊匹配纠正并重试一次
    result = _auto_retry_on_not_found(name, args, result, sender)

    if result is not None:
        ctx.setdefault("tool_call_history", []).append({
            "id": tool_call_id, "tool": name, "args": args, "result_preview": result[:200]
        })
        if name == "get_weather" and result:
            ctx.setdefault("cached_data", {})["weather"] = result
    return result

# ==================== P2-7: AI 自反思重试 ====================

_NOT_FOUND_PATTERNS = ("找不到", "未找到", "找不到目标")

def _auto_retry_on_not_found(name: str, args: dict, result: str, sender: str) -> str:
    """当工具返回'找不到目标'时，尝试用 _fuzzy_match_person 纠正 target 参数并重试一次。"""
    if not isinstance(result, str):
        return result
    if not any(pat in result for pat in _NOT_FOUND_PATTERNS):
        return result
    # 找到 args 中的 target 字段
    target_str = args.get("target", "")
    if not target_str or not isinstance(target_str, str):
        return result
    if len(target_str) > 50:
        return result
    try:
        from config import _fuzzy_match_person
        matches = _fuzzy_match_person(target_str)
        if matches and matches[0][3] >= 0.6:
            best = matches[0]  # (msg_type, qq, display_name, confidence)
            corrected = best[1]  # 用 QQ 号重试最精确
            new_args = dict(args)
            new_args["target"] = corrected
            handler = _TOOL_HANDLERS.get(name)
            if handler is None:
                return result
            from safety import SafetyNet, CircuitBreakerOpenError
            try:
                retry_result = SafetyNet.call_with_circuit(f"tool_{name}", handler, new_args, {
                    "sender_id": sender, "msg_type": args.get("_msg_type", "private"),
                    "tool_call_history": [], "cached_data": {},
                })
            except CircuitBreakerOpenError:
                return result
            except Exception:
                return result
            if retry_result and not (isinstance(retry_result, str) and
                    any(pat in retry_result for pat in _NOT_FOUND_PATTERNS)):
                log(f"[RETRY] {name} 模糊匹配纠正 {target_str}→{best[2]}({best[1]}) 重试成功")
                return retry_result
            return result
    except Exception:
        return result
    return result


# ==================== P8.1: function calling DeepSeek 调用 ====================

def _try_agent_plan(messages: list[dict], tools: list[dict],
                    max_tokens: int, ctx: dict) -> str | None:
    """尝试用 Agent 规划模式执行多步骤任务。返回 None 表示回退到简单循环。"""
    try:
        user_goal = ""
        for m in reversed(messages):
            if m.get("role") == "user" and isinstance(m.get("content"), str):
                user_goal = m["content"].strip()
                break
        if not user_goal or len(user_goal) < 8:
            return None
        available = [t["function"]["name"] for t in tools] if tools else []
        # 提取最近对话历史（最近 5 条 user+assistant），供 planner 理解上下文指代
        _history_lines = []
        for m in messages[-10:]:
            role = m.get("role", "")
            content = m.get("content", "")
            if isinstance(content, str) and content.strip():
                if role == "user":
                    _history_lines.append(f"用户：{content.strip()[:150]}")
                elif role == "assistant":
                    _history_lines.append(f"我：{content.strip()[:150]}")
        ctx_with_history = dict(ctx)
        ctx_with_history["_recent_history"] = "\n".join(_history_lines[:-1])  # 不含最新一条（已在 goal 中）
        import asyncio
        # 传入完整工具 spec，让 planner 知道每个工具的参数要求
        plan = asyncio.run(Agent.plan(user_goal, available, ctx_with_history, tool_specs=tools))
        if plan is None:
            return None
        result = asyncio.run(Agent.execute(plan))
        return result if result else None
    except Exception as e:
        from messaging import log
        log(f"[AGENT] plan/execute failed, falling back: {e}")
        return None


def _call_deepseek_with_tools(messages: list[dict], tools: list[dict],
                               max_tokens: int = 800, ctx: dict = None) -> str | None:
    """调用 DeepSeek，支持 function calling。Phase 2.2: 优先 Agent 规划，失败回退循环。"""
    if ctx is None:
        ctx = {}

    # --- Phase 2.2: 尝试 Agent 规划模式 ---
    agent_result = _try_agent_plan(messages, tools, max_tokens, ctx)
    if agent_result is not None:
        return agent_result

    # --- 回退：原有简单循环 ---
    tool_call_counts: dict[str, int] = {}
    for _round in range(10):
        try:
            import httpx
            resp = httpx.post(
                f"{DEEPSEEK_BASE_URL}/v1/chat/completions",
                json={
                    "model": DEEPSEEK_MODEL,
                    "max_tokens": max_tokens,
                    "messages": messages,
                    "tools": tools,
                },
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            body = resp.json()
            choice = body.get("choices", [{}])[0]
            msg = choice.get("message", {})

            if msg.get("content") and not msg.get("tool_calls"):
                return msg["content"]

            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": msg.get("content") or "",
                    "tool_calls": tool_calls,
                })
                for tc in tool_calls:
                    func_name = tc["function"]["name"]
                    tool_call_counts[func_name] = tool_call_counts.get(func_name, 0) + 1
                    if tool_call_counts[func_name] > 10:
                        log(f"TOOL LOOP: {func_name} called {tool_call_counts[func_name]} times, aborting")
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": (
                                f"操作已中断：{func_name} 被连续调用 {tool_call_counts[func_name]} 次，已达安全保护上限。"
                                "请根据已完成的操作用一段话给用户一个简洁的总结，"
                                "告诉用户已经做了什么、还剩什么没做，不要再调用任何工具。"
                            ),
                        })
                        for remnant in tool_calls[tool_calls.index(tc) + 1:]:
                            messages.append({
                                "role": "tool",
                                "tool_call_id": remnant.get("id", ""),
                                "content": "已跳过（因上一工具触发循环保护）。",
                            })
                        break
                    try:
                        func_args = json.loads(tc["function"]["arguments"])
                    except Exception:
                        func_args = {}
                    log(f"AI→TOOL: {func_name}({func_args})")
                    result = _execute_tool(func_name, func_args, ctx)
                    log(f"TOOL→AI: {result[:80]}...")
                    max_result_len = 2000
                    if len(result) > max_result_len:
                        result = result[:max_result_len] + f"\n...(结果过长，已截断至{max_result_len}字符)"
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "content": result,
                    })
                continue

            return msg.get("content") or None

        except Exception as e:
            log(f"AI tool call failed (round {_round}): {e}")
            return None

    log("AI tool call: max rounds reached")
    if _round >= 9:
        return None  # 让 Path C 兜底，AI 自由回复
    return None


# ==================== AI 路由上下文构建 ====================


def _build_ai_routing_context(sender_id: str, chat_id: str, mem: dict) -> dict:
    """构建 AI 意图路由的丰富上下文。
    返回: {"system_prompt": str, "recent_messages": list[dict]}
    """
    from config import SELF_QQ as _SELF_QQ

    now = datetime.now()
    wday = ["一","二","三","四","五","六","日"][now.weekday()]
    time_str = f"{now.year}年{now.month:02d}月{now.day:02d}日 {now.hour:02d}:{now.minute:02d} 星期{wday}"

    # 检查当前说话人是否设置了城市
    from db import get_person_city
    sender_city = get_person_city(sender_id)

    idx = _get_person_index()
    by_qq = idx.get("by_qq", {})
    # Phase 2: 动态裁剪联系人 — 仅展示与当前对话相关的，减少 token 消耗
    # 1. 从最近消息中提取被提及的 QQ
    recent = mem.get("conversation", [])[-20:]  # 取最近 20 条
    mentioned_qqs: set[str] = set()
    for m in recent:
        sid = m.get("sender_id", "")
        if sid and sid != _SELF_QQ and sid != sender_id:
            mentioned_qqs.add(sid)
        content = m.get("content", "")
        for qq, data in by_qq.items():
            for n in data.get("names", set()):
                if n and len(n) >= 2 and n in content:
                    mentioned_qqs.add(qq)
    # 2. 计算每个 QQ 在近期消息中的活跃度（最近 48h）
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(hours=48)).isoformat()
    freq: dict[str, int] = {}
    for m in recent:
        if m.get("time", "") >= cutoff:
            freq[m.get("sender_id", "")] = freq.get(m.get("sender_id", ""), 0) + 1
    # 3. 选择要展示的联系人：当前说话人 + 被提及的 + 最高频的前 5 个
    # 权限隔离：非管理员只注入自己的联系人行，防止泄露他人称呼/QQ/关系
    _is_master_sender = sender_id in _get_master_ids()
    if _is_master_sender:
        relevant_qqs = {sender_id} | mentioned_qqs
        top_freq = sorted(freq.keys(), key=lambda q: -freq[q])[:5]
        relevant_qqs.update(top_freq)
        # 如果相关联系人太少（< 5），补充一些有数据的联系人避免信息不足
        if len(relevant_qqs) < 5:
            for qq in sorted(by_qq.keys(), key=lambda q: len(by_qq[q].get("names", set())), reverse=True):
                if qq != _SELF_QQ and qq != sender_id and qq not in relevant_qqs:
                    relevant_qqs.add(qq)
                    if len(relevant_qqs) >= 8:
                        break
    else:
        relevant_qqs = {sender_id}
    # 生成 prompt 行
    person_lines = []
    for qq in sorted(relevant_qqs):
        if qq == _SELF_QQ or qq not in by_qq:
            continue
        data = by_qq[qq]
        names = sorted(data.get("names", set()), key=lambda x: -len(x))
        if not names:
            continue
        main = names[0]
        aliases = [n for n in names[1:] if n != main]
        alias_str = f"(别名:{','.join(aliases[:3])})" if aliases else ""
        rels = data.get("relations", set())
        rel_str = f" [{','.join(sorted(rels))}]" if rels else ""
        current_marker = " ← 当前对话" if qq == sender_id else ""
        person_lines.append(f"  {main} QQ:{qq}{alias_str}{rel_str}{current_marker}")
    # 提示还有更多联系人 + 全量关系索引：仅管理员可见（普通用户不可泄露他人名单）
    rel_lines = []
    if _is_master_sender:
        total = len(by_qq) - 1  # 排除 bot 自己
        if total > len(person_lines):
            person_lines.append(f"  ... 还有 {total - len(person_lines)} 位联系人未列出（如需查询可让 AI 调 get_person_info）")
        rel_idx = _build_relation_index()
        for rel, people in sorted(rel_idx.items()):
            rel_lines.append(f"  {rel}: {', '.join([name for name, _ in people])}")

    # 解析名字：优先从 DB 获取 nickname，若为空则用 QQ 末 4 位
    _resolved = _resolve_speaker_name(sender_id)
    _display_name = _resolved if _resolved and _resolved != sender_id[-4:] else None
    _speaker_label = "主人" if _is_master_sender else (_display_name or "对方")

    recent = mem.get("conversation", [])[-40:]
    recent_text_lines = []
    recent_as_messages = []
    for m in recent[-25:]:
        if m["direction"] == "in":
            recent_text_lines.append(f"  [{m['time']}] {_speaker_label}: {m['content'][:200]}")
            recent_as_messages.append({"role": "user", "content": m["content"][:300]})
        else:
            recent_text_lines.append(f"  [{m['time']}] 机器人: {m['content'][:200]}")
            recent_as_messages.append({"role": "assistant", "content": m["content"][:300]})

    tool_names = [t["function"]["name"] for t in TOOLS]

    city_line = (f"★ 此人已设置城市：{sender_city} — 聊到天气或城市时以此为据"
                 if sender_city else
                 "★ 此人尚未设置城市。在对话中自然地问一下：「对了你在哪个城市呀？我可以帮你关注那边的天气~」拿到城市后立刻调用 set_person_address 记录。")
    nickname_line = (f"\n- ★ 此人昵称是「{_display_name}」—— 你必须在回复中用此名字称呼对方！不要问'你想让我叫你什么'，不要调用 get_nickname_status。直接用「{_display_name}」称呼即可。"
                     if _display_name else "")

    # 规则按角色裁剪：批量列举与管理员管理的引导仅对管理员展示（与按角色过滤后的工具表保持一致）
    if _is_master_sender:
        _rule_batch = (
            f"4. ★ 多步骤任务：如果需要多个工具才能完成（如查天气+群发），"
            f"   可以连续调用多个工具，每个工具调完拿到结果后再决定下一步。\n"
            f"   ★ 批量操作优先：需要查看或列举多人信息时，务必使用批量工具：\n"
            f"   - 「列出所有人的备注」→ 调用 list_all_notes（1次），绝不逐个调 get_person_info\n"
            f"   - 「看看有哪些联系人」→ 调用 list_all_persons（1次），绝不逐个调 get_person_info\n"
            f"   - 能用1次工具调用完成的事，绝对不要拆成多次调用\n"
        )
        _rule_efficiency = (
            f"9. ★ 效率规则：能用1次工具调用完成的事，绝对不要拆成多次。优先使用 list_all_persons 和 list_all_notes。\n"
        )
        _rule_admin_mgmt = (
            f"13. ★ 管理员管理：你可以用 add_admin、remove_admin、list_admins 工具来管理管理员列表。\n"
            f"   当管理员说「把XX设为管理员」「移除XX的管理员」「列出管理员」等时，直接调用对应工具。\n"
        )
    else:
        _rule_batch = (
            f"4. 如果一件事需要多个工具才能完成，可以连续调用多个工具，每个工具调完拿到结果后再决定下一步；"
            f"   能用1次工具调用完成的事，绝对不要拆成多次调用。\n"
        )
        _rule_efficiency = ""
        _rule_admin_mgmt = ""

    system = (
        __import__("config")._build_default_persona() + "\n"
        f"当前时间：{time_str}\n\n"
        f"★★ 当前对话人信息 ★★\n"
        f"- QQ号：{sender_id}\n"
        f"- 此人是聊天对象，是你正在回复的人\n"
        f"- 如果对方说「我」「提醒我」「查我的」「帮我」，指的就是这个人（QQ:{sender_id}）\n"
        f"- 为对方创建定时任务时 target_type=private, target_id={sender_id}\n"
        f"- 给对方设置地址/称呼/名字时 target 填 {sender_id}（或对方自己说的名字）\n"
        f"- {city_line}\n"
        f"{nickname_line}\n"
        f"=== 联系人数据库 ===\n" + "\n".join(person_lines) + "\n\n"
        f"=== 关系分类 ===\n" + "\n".join(rel_lines) + "\n\n"
        f"=== 近期对话 ===\n" + "\n".join(recent_text_lines) + "\n\n"
        f"你有以下工具可用：{', '.join(tool_names)}\n\n"
        f"★★ 身份规则（绝对不可违反，比下面所有规则都优先）★★：\n"
        f"0. {SELF_NICK}就是你自己。任何人说「{SELF_NICK}」「{SELF_NICK}在吗」「{SELF_NICK}呢」「你好{SELF_NICK}」时，\n"
        f"   是在对你说话，不是在查找另一个叫{SELF_NICK}的联系人。绝不能用任何工具去查找{SELF_NICK}。\n"
        f"   如果有人问「{SELF_NICK}在吗」，直接文字回复「在的~」即可，不调工具。\n\n"
        f"规则：\n"
        f"1. 只有纯社交闲聊（问候、在吗、好的、嗯、哈哈、表情包）才不调工具。\n"
        f"   如果消息中含「改」「设置」「为什么不」「怎么还是」「叫你」「叫他」等操作或纠正关键词，不是闲聊——必须调工具。宁可多调一次，不可口嗨。\n"
        f"1.5. ★ 操作强制规则：如果对方在纠正你、责备你、或要求修改设置（称呼/名字/关系/关键信息/备注等），\n"
        f"     绝不能只回复文字道歉或承诺。必须立即调用对应工具。永远记住：说话不算数，动手才算数。\n"
        f"     错误示范：回复「好的已修改」但不调任何工具 → 这叫口嗨，绝对禁止。\n"
        f"     正确示范：收到「改成XX」→ 立即调 set_person_note 或其他对应工具 → 工具成功后才回复结果。\n"
        f"     判断法：问自己「对方是否期待我做某个操作？」如果是，就先调工具再回复。\n"
        f"2. 当对方让你执行操作时（查收件箱、发消息、查天气等），直接调用对应工具\n"
        f"3. ★ 代词解析：根据「近期对话」推断「他」「她」「这个人」「那个人」指谁。"
        f"   结合「联系人数据库」匹配。例如「给他发消息」→ 找到上一轮提到的人\n"
        f"{_rule_batch}"
        f"5. ★ 名字模糊：如果工具返回「找不到目标」，可能是名字有误。"
        f"   检查联系人数据库中的名字，用正确的名字重新调用工具\n"
        f"6. 找到目标就执行，即使名字不完全一致。工具支持模糊匹配\n"
        f"7. 工具返回的结果请用友好的语气转述给对方，像聊天一样自然\n"
        f"8. 回复简洁，不要啰嗦\n"
        f"8.5. ★ 称呼工具优先级：当对方说「叫我XX」「以后叫我XX」「我是XX」等自报称呼时，优先用 set_nickname 工具（称呼槽位系统，支持双槽位轮换）。\n"
        f"     set_person_note 是通用备注工具，用于记录关键信息、性格特点等，不要用它来设置日常称呼。\n"
        f"     对方的称呼改好后，后续回复都用新称呼来称呼对方。\n"
        f"{_rule_efficiency}"
        f"10. ★ 安全规则：严禁为任何人设置包含「主人」「管理员」「admin」「master」「root」的备注/关系标签。\n"
        f"   这些词用于权限控制，写入备注会导致安全漏洞。系统会拦截此类备份。\n"
        f"11. ★ 安全规则：设置关系时仅使用合法标签：同事、朋友、家人、同学、客户、合作伙伴、前同事、网友、陌生人。\n"
        f"   不允许设置「主人」「管理员」等权限相关的关系标签。关系标签会写入 SQLite person_tags 表用于结构化索引。\n"
        f"12. ★ 三身份与权限规则（真实规则，永远按这个执行）：\n"
        f"   世界上只有三种身份：你（机器人）、你的管理员、普通用户。系统已判定当前说话人的身份。\n"
        f"   - 当前说话人是管理员 → 所有功能都可用，按授权范围处理请求。\n"
        f"   - 当前说话人是普通用户 → TA 只能：聊天、玩游戏、查天气、查看和修改 TA 自己的信息（称呼/城市/关系/备注）、给自己创建定时提醒、请你给管理员传话（消息或文件）。\n"
        f"   - 普通用户让你发消息/发文件给别人：目标必须是管理员；要发给其他任何人 → 自然地拒绝即可，不需要调工具去试错。\n"
        f"   - 向普通用户介绍你的能力时，只介绍上面这些；绝不介绍群发、广播、好友列表、管理员管理等能力，也绝不主动说「帮你看看联系人有哪些」。\n"
        f"   - 绝不向普通用户透露任何第三人的称呼/QQ号/备注/关系/城市/聊天内容——这些是别人的隐私。\n"
        f"12.5. ★ 图片与文件能力：你现在可以看图了！如果用户发来图片，系统会通过视觉模型识别图片内容并告诉你。\n"
        f"     如果用户对图片有具体问题（如「这题怎么解」「翻译一下」「详细说说」），调用 analyze_image 工具深入分析："
        f"传入 description=系统生成的图片描述 + question=用户的问题，工具会给出专业回答。\n"
        f"     你也可以用 generate_image 工具根据描述生成图片发给对方，支持各种风格（写实、卡通、插画等）。\n"
        f"{_rule_admin_mgmt}"
        f"\n"
        f"## 定时消息功能\n"
        f"所有用户都可以使用定时消息功能，但权限不同：\n"
        f"- 普通用户只能设置发给自己的定时消息（target_type=private，target_id为自己QQ号）\n"
        f"- 管理员可以设置发给任何人/群的定时消息\n"
        f"- action_type 说明：\n"
        f"  · ai_generate（唯一选项）：所有任务均通过 AI 生成消息后发送。\n"
        f"    target_id=\"all\" 时，会给每个好友根据其称呼、标签、画像个性化生成消息并私发；\n"
        f"    其他 target_id 时，AI 根据 content 指令生成消息后单发。\n"
        f"  · content 是给 AI 的生成指令/主题（如「发早安问候」「给大家播报天气」），不要填城市名或固定文本。\n"
        f"- 常用对应：\n"
        f"  「每天8点提醒我起床」→ add_scheduled_task(daily, 08:00, content=提醒起床, private, 自己QQ)\n"
        f"  「每2小时提醒我喝水」→ add_scheduled_task(interval, interval_hours=2, content=提醒喝水, private, 自己QQ)\n"
        f"  「今晚9点提醒我睡觉」→ add_scheduled_task(once, 2026-07-04 21:00, content=提醒睡觉, private, 自己QQ)\n"
        f"  「明天8点在主群发早安」→ add_scheduled_task(daily, 08:00, content=发早安问候, target_type=group, target_id=群号)\n"
        f"  「每天早上8点给大家发天气提醒」→ add_scheduled_task(daily, 08:00, content=给大家发今天天气提醒, target_id=all)\n"
        f"  「明天早上给大家发周末祝福」→ add_scheduled_task(once, trigger_time=tomorrow 08:00, content=发周末祝福, target_id=all)\n"
        f"  「查看我的定时任务」→ list_my_tasks\n"
        f"  「删除任务xxx」→ delete_scheduled_task(task_id)\n"
        f"  「停用/启用任务xxx」→ toggle_scheduled_task(task_id, enable)\n"
        f"- 添加成功时回复：任务ID、任务名称、触发时间\n"
        f"\n"
        f"## 本地化天气提醒\n"
        f"- 每个人可以设置自己的所在城市，用于接收本地天气提醒\n"
        f"- 当有人在聊天中提到自己所在城市时（如「我在上海上班」），主动调用 set_person_address 记录\n"
        f"- 与新人第一次聊天时，主动询问对方所在城市，用「可以帮你关注那边天气」作为理由\n"
        f"- 管理员可以给任何人设置地址；非管理员只能设置自己的\n"
        f"- 发送天气时调用 batch_send_weather_alerts，系统自动按每个人的地址查询当地天气\n"
        f"- 未设置地址的人会被自动跳过\n"
        f"- 口语示例：「把张三的地址设为北京」「我在上海」→ set_person_address\n"
        f"- 口语示例：「给大家发天气提醒」「提醒所有人注意天气，出门带伞」→ batch_send_weather_alerts\n"
    )

    return {
        "system_prompt": system,
        "recent_messages": recent_as_messages,
    }


def _ai_intent_route(content: str, sender_id: str, sender_nick: str,
                     msg_type: str, target_id: str, chat_id: str,
                     mem: dict) -> str | None:
    """AI 意图路由。主人消息未命中正则时，由 AI 判断意图并调工具。返回回复文本或 None。"""
    ctx = {
        "sender_id": sender_id,
        "sender_nick": sender_nick,
        "msg_type": msg_type,
        "target_id": target_id,
        "chat_id": chat_id,
        "mem": mem,
        "tool_call_history": [],
        "cached_data": {},
    }

    routing_ctx = _build_ai_routing_context(sender_id, chat_id, mem)
    messages = [{"role": "system", "content": routing_ctx["system_prompt"]}]
    messages.extend(routing_ctx["recent_messages"])
    messages.append({"role": "user", "content": content})

    # 非管理员只给模型看非管理员工具，避免介绍/尝试越权功能（执行层另有硬拦截）
    if sender_id in _get_master_ids():
        available_tools = TOOLS
    else:
        available_tools = [t for t in TOOLS
                           if t.get("function", {}).get("name") not in _MASTER_ONLY_TOOLS]

    result = _call_deepseek_with_tools(messages, available_tools, max_tokens=800, ctx=ctx)

    # 口嗨检测：AI 回复声称做了操作但实际没调任何工具 → 返回 None 放行到 Path C
    if result and not ctx.get("tool_call_history"):
        _KOUHAI_KEYWORDS = [
            "已修改", "已设置", "已更新", "已删除", "已添加", "已保存",
            "改好了", "改回来", "改过来", "改掉了",
            "马上改", "马上就改", "立刻改",
            "修改好了", "设置好了", "更新好了", "调整好了", "已经改",
            "好了已经", "已经修改", "已经设置", "已经更新",
        ]
        result_lower = result
        if any(kw in result_lower for kw in _KOUHAI_KEYWORDS):
            log(f"KOUHAI DETECTED: AI claimed action but no tool called. Reply: {result[:80]}")
            return None

    return result
