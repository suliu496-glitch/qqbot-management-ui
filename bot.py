#!/usr/bin/env python3
"""
QQ 后台自动回复机器人 v2
- 多会话隔离（每个 chat 独立 mute 状态）
- 指令预解析层（/mute /unmute /owner 等不经过 AI）
- 五层 Prompt 架构（身份→主人→人设→规则→上下文）
- 角色配置表驱动人设
- 按人独立记忆 + 自动身份锚定
"""
import asyncio, atexit, json, os, re, sys, threading, time
import ui_runtime
from datetime import datetime
from pathlib import Path

from messaging import log, _api, _send_text, _send_image, _print_lock, _processed_ids, _MAX_PROCESSED_IDS, NAP_CAT_HTTP, _parse_message_segments, _init_file_logger
from config import (
    NAP_CAT_WS, MY_QQ, DATA_DIR, DEEPSEEK_MODEL, DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL,
    ZHIPU_API_KEY, ZHIPU_BASE_URL, PERSONA_FILE, SELF_NICK, SELF_QQ,
    _friend_cache, _group_cache, _cache_loaded,
    _refresh_contact_cache, _get_master_ids, _save_master_ids,
    _is_master, _owner_add, _owner_remove, _migrate_master_list,
    _get_person_role, _set_person_role,
    _load_memory, _save_memory, _add_to_memory, _get_person_profile,
    _add_to_group_memory, _load_group_context,
    _build_prompt, _build_default_persona, _build_identity_anchor,
    _apply_learned_traits, _extract_relation,
    _get_contact_summary, _build_relation_index, _get_person_index,
    _invalidate_person_index, _resolve_targets, _resolve_speaker_name,
    _fuzzy_match_person, _person_key, _chat_id,
    _is_chat_muted, _set_chat_muted, _chat_mute,
    _validate_note, _upsert_person_note, _remove_person_note, _cleanup_person_notes,
    _migrate_roles_json_to_data, _build_relation_instruction,
    _auto_switch_all_nicknames,
)
from tools import (
    TOOLS, _execute_tool, _call_deepseek, _call_deepseek_with_tools,
    _ai_intent_route, _polish_message, _generate_person_report,
    _check_inbox_for_master, _summarize_inbox, _summarize_person_dialog,
    _get_weather, _should_search_ai, _web_search, _extract_search_query,
    SEARCH_REALTIME_PATTERNS, _read_file_content, _collect_recent_summary, _cleanup_inconsistent_data,
)
from db import (
    init_db, close_db, migrate_json_to_db, migrate_v2_consolidate_names,
    get_resolved_name, set_fixed_name, upsert_profile, get_profile,
    query_recent_chats, query_chat_history_by_sender,
    get_due_tasks, update_task_last_run, toggle_scheduled_task,
    verify_data_integrity,
)
from games import (_start_game, _handle_game_action, _end_game, _game_status_text, _game_sessions)
from safety import SafetyNet

# ==================== Phase 6: 去重集合裁剪 ====================
def _prune_processed_ids():
    """Phase 4.3: LRU 淘汰 — 超过上限时从头部删除最旧条目。"""
    while len(_processed_ids) > _MAX_PROCESSED_IDS:
        _processed_ids.popitem(last=False)

# ==================== 群聊冷却 & 限频 ====================
_chat_cooldown: dict[str, tuple[float, str]] = {}
_chat_reply_count: dict[str, list[float]] = {}
_group_reply_count: dict[str, list[float]] = {}


def _simple_hash(text: str) -> str:
    """消息指纹，判断两条消息是否太相似"""
    import hashlib
    clean = re.sub(r'[^一-龥a-zA-Z0-9]+', '', text.strip())[:50]
    return hashlib.md5(clean.encode()).hexdigest()[:8] if clean else ""


# 非主人称呼对话窗口：记录谁刚通过 Path B2 设置了称呼
# {person_key: expiry_timestamp}，30 分钟内该用户在群里的后续消息不 @bot 也能通过门控
_nickname_conversation: dict[str, float] = {}

# 跨消息图片/文件缓存：记录每个人最后一次收到的媒体描述
# {person_key: {"description": str, "time": float, "type": "image"|"file"}}
# 有效期 5 分钟，后续文字指令可引用最近一张图/文件
_last_media_cache: dict[str, dict] = {}
_MEDIA_CACHE_TTL = 300  # 秒

# ==================== 图片日调用限制（效果七）====================
_image_call_counter: dict[str, int] = {}      # {date_str: count}
_IMAGE_CALL_DAILY_LIMIT = 100                  # 每天最多识别 100 张图

def _check_image_call_limit() -> bool:
    """检查今日图片识别是否超限。返回 True=允许调用，False=超限。"""
    today = datetime.now().strftime("%Y-%m-%d")
    # 新日期 → 重置计数
    keys_to_remove = [k for k in _image_call_counter if k != today]
    for k in keys_to_remove:
        del _image_call_counter[k]
    count = _image_call_counter.get(today, 0)
    if count >= _IMAGE_CALL_DAILY_LIMIT:
        return False
    _image_call_counter[today] = count + 1
    return True

def _inject_media_context(content: str, person_key: str) -> str:
    """如果 5 分钟内该用户发过图片/文件，将其描述注入到当前 content 供 agent 引用。"""
    cached = _last_media_cache.get(person_key)
    if not cached:
        return content
    if time.time() - cached["time"] > _MEDIA_CACHE_TTL:
        # 过期清理
        _last_media_cache.pop(person_key, None)
        return content
    # 已有图片描述（同条消息）→ 不重复注入
    if "[图片描述]" in content or "[最近发过的" in content:
        return content
    media_label = "图片" if cached["type"] == "image" else "文件"
    return content + f"\n[最近发过的{media_label}] {cached['description']}"


def _is_cooled_down(chat_id: str, content: str) -> bool:
    """10秒内刚回复过此会话 或者 当前消息和上轮回复太像 → 冷却中"""
    now = time.time()
    if chat_id in _chat_cooldown:
        last_time, last_hash = _chat_cooldown[chat_id]
        if now - last_time < 10:
            return True
        if last_hash and _simple_hash(content) == last_hash:
            return True
    return False


def _update_cooldown(chat_id: str, content: str):
    _chat_cooldown[chat_id] = (time.time(), _simple_hash(content[:50]))




def _rate_limit(chat_id: str, sender_id: str, max_replies: int = 3, window: int = 300) -> bool:
    """5分钟窗口内，同一群聊对同一个人最多回 max_replies 条"""
    key = f"{chat_id}_{sender_id}"
    now = time.time()
    ts = [t for t in _chat_reply_count.get(key, []) if now - t < window]
    if len(ts) >= max_replies:
        return True
    ts.append(now)
    _chat_reply_count[key] = ts
    return False


def _group_rate_limited(group_id: str, max_replies: int = 3, window: int = 60) -> bool:
    """非主人群聊回复限频：每群每分钟最多 N 次回复。"""
    now = time.time()
    ts = [t for t in _group_reply_count.get(group_id, []) if now - t < window]
    if len(ts) >= max_replies:
        return True
    ts.append(now)
    _group_reply_count[group_id] = ts
    return False


# ==================== 群聊门控 ====================

def _get_bot_all_names(self_nick: str = SELF_NICK) -> set[str]:
    """返回 bot 的所有称呼变体"""
    n = self_nick.strip()
    if not n:
        return set()
    return {n, n + n, "小" + n, n + "酱", n + "宝", n + "仔",
            "小" + n + n, "阿" + n, n + "子", "@" + n,
            n + "儿", n + "崽", "老" + n, n + "呀", n + "~",
            "我的" + n, n + "姐", n + "妹"}


def _mentions_bot(content: str, self_nick: str = SELF_NICK) -> bool:
    """检测消息文本中是否提到了 bot（自然语言中的 @名、别称、名字）"""
    for name in _get_bot_all_names(self_nick):
        if name and name in content:
            return True
    return False


def _should_reply_in_group(content: str, recent_msgs: list, self_nick: str, self_qq: str,
                          sender_id: str = "", mem: dict = None) -> bool:
    """群聊中智能判断是否回复（仅适用于非主人——主人在上游已放行）。

    回复触发条件（满足任一即触发）：
    1. CQ @bot → 永远触发
    2. 提到 bot 任何别称 → 永远触发（不管有没有问号、消息长短）
    3. 对方最近 3 条内跟 bot 有对话 且 当前消息提到 bot → 触发
    4. 有问号且消息提到 bot/主人相关 且 消息≥8字 → 触发
    """
    if not content.strip():
        return False

    if f"[CQ:at,qq={self_qq}]" in content:
        return True

    if _mentions_bot(content, self_nick):
        return True

    for m in recent_msgs[-3:]:
        if m.get("direction") == "out":
            if _mentions_bot(content, self_nick):
                return True
            return False

    if ("?" in content or "？" in content) and len(content) > 8:
        if _mentions_bot(content, self_nick):
            return True

    return False


def _get_master_nicks() -> list:
    """获取主人的 QQ 昵称列表"""
    nicks = []
    for m_id in _get_master_ids():
        key = f"private_{m_id}"
        mem = _load_memory(key)
        name = mem.get("name", "").strip()
        if name:
            nicks.append(name)
    return nicks


# ==================== 指令预解析层（Path A） ====================

def _rewrite_spoken_command(content: str) -> str | None:
    """把口语化指令重写为标准 '/指令 参数' 格式。仅主人消息调用。
    max_tokens=50，超时 3s，失败返回 None（兜底不重写）。"""
    import httpx
    try:
        resp = httpx.post(
            f"{DEEPSEEK_BASE_URL}/v1/chat/completions",
            json={
                "model": DEEPSEEK_MODEL,
                "max_tokens": 50,
                "messages": [
                    {"role": "system", "content": (
                        "你是一个指令重写器。把用户的自然语言指令转成标准格式。"
                        "支持的指令：\n"
                        "/note QQ 备注内容 — 给某人设备注（如 称呼:X, 名字:X, 关系:X）\n"
                        "/delnote QQ 备注内容 — 删除备注\n"
                        "/role QQ — 查看人设\n"
                        "/relate QQ 关系 — 设置关系\n"
                        "/report QQ — 人物分析报告\n"
                        "/who 关系 — 按关系查人\n"
                        "/send 目标 消息 — 代发消息\n"
                        "/inbox — 查看收件箱\n"
                        "/friends — 好友列表\n"
                        "/status — 状态\n"
                        "/profile QQ 标签:a,b,c — 编辑档案\n"
                        "/set QQ 性格:X;称呼:Y — 设置人设\n"
                        "/mute QQ — 静默\n"
                        "/unmute QQ — 取消静默\n"
                        "/owner list — 管理员列表\n"
                        "/help — 帮助\n\n"
                        "规则：\n"
                        "1. 如果消息是纯聊天（问候、确认、感叹、日常对话），回复「CHAT」\n"
                        "2. 查询类消息（谁找我、有新消息吗、看看消息、收件箱）→ 回复「CHAT」（AI 会用系统内置摘要回答）\n"
                        "3. 如果消息是明确的操作指令（改备注、设关系、发消息、查人设等），重写为标准格式，一行一个\n"
                        "4. 推断目标：提到名字时用名字（如 小明→小明），提到QQ号时用QQ号\n"
                        "5. 称呼变更（把X的称呼改成Y、给X改名叫Y）→ /note 目标 称呼:新称呼\n\n"
                        "示例：\n"
                        "- 最近谁找我聊天 → CHAT\n"
                        "- 看看收件箱 → CHAT\n"
                        "- 有新消息吗 → CHAT\n"
                        "- 称呼改了改小明 → /note 小明 称呼:小明\n"
                        "- 把小明的称呼改成小明 → /note 小明 称呼:小明\n"
                        "- 最近谁给我发消息 → /inbox\n"
                        "- 帮我查查某人是什么人 → /report 某人\n"
                        "- 在吗 → CHAT\n"
                        "- 好的 → CHAT\n"
                    )},
                    {"role": "user", "content": content},
                ],
            },
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            },
            timeout=3.0,
        )
        resp.raise_for_status()
        body = resp.json()
        text = body.get("choices", [{}])[0].get("message", {}).get("content", "")
        text = text.strip()
        if text.upper() == "CHAT" or not text:
            return None
        return text
    except Exception:
        return None


def _parse_command(content: str, sender_id: str, mem: dict,
                   chat_id: str, msg_type: str, target_id: str,
                   sender_nick: str = "") -> str | None:
    """
    管理指令。仅保留系统级关键命令（mute/unmute/owner/clearall/clearmem/cleannotes/reload/refresh/status/stats/friends/prompt/help）。
    返回 str = 直接回复（不调 AI）；返回 None = 不是指令。
    """
    text = content.strip()

    if not _is_master(sender_id, mem):
        return None

    _bot_call_prefix = re.compile(rf"^(?:{'|'.join(_get_bot_all_names())})[，,、\s]+")
    clean_text = _bot_call_prefix.sub("", text).strip() if msg_type == "group" else text

    def _try_match(pattern: str, *texts: str):
        for t in texts:
            m = re.match(pattern, t)
            if m:
                return m
        return None

    # --- 静默指令 ---
    if m := _try_match(r'^(?:/mute|/静默|闭嘴)\s*(.+)?$', text, clean_text):
        target = m.group(1).strip() if m.group(1) else ""
        cid = _chat_id(msg_type, target if target else target_id)
        _set_chat_muted(cid, True)
        return f"好的，已静默 {cid} (◕ᴗ◕✿)"

    if m := _try_match(r'^(?:/unmute|/解除静默|/恢复|可以说话了)\s*(.+)?$', text, clean_text):
        target = m.group(1).strip() if m.group(1) else ""
        cid = _chat_id(msg_type, target if target else target_id)
        _set_chat_muted(cid, False)
        return f"恢复回复啦 {cid} (◕ᴗ◕✿)"

    # --- 管理员管理 ---
    if m := re.match(r'^(?:/owner\s+add|/添加主人|/添加管理员)\s+(\d{5,20})\s*$', text):
        qq = m.group(1)
        ok = _owner_add(qq)
        if ok:
            from db import log_audit
            log_audit("add_admin", sender_id, f"添加 {qq} 为管理员", qq)
        return f"✅ 已将 {qq} 设为管理员" if ok else f"{qq} 已经是管理员"

    if m := re.match(r'^(?:/owner\s+remove|/移除主人|/删除主人|/移除管理员|/删除管理员)\s+(\d{5,20})\s*$', text):
        qq = m.group(1)
        ok = _owner_remove(qq)
        if ok:
            from db import log_audit
            log_audit("remove_admin", sender_id, f"移除 {qq} 的管理员权限", qq)
        return f"✅ 已移除 {qq} 的管理员权限" if ok else f"{qq} 不是管理员"

    if re.match(r'^(?:/owner\s+list|/主人列表|/管理员列表|/owners|/admins)\s*$', text):
        masters = _get_master_ids()
        names = []
        for m_id in sorted(masters):
            name = _resolve_speaker_name(m_id, "")
            names.append(f"{name}({m_id})" if name != m_id[-4:] else m_id)
        return f"当前管理员（{len(masters)}人）：{', '.join(names) if masters else '无'}"

    # --- 清除所有数据（二次确认，效果四）---
    if re.match(r'^(?:/clearall|/清除所有|/重置所有|/抹除全部|清除所有数据|重置所有数据|抹除全部数据)\s*$', text):
        confirm_key = f"clearall_confirm_{person_key}"
        if _chat_mute.get(confirm_key):  # 已经确认过 → 执行
            _chat_mute.pop(confirm_key, None)
        else:
            # 第一次 → 要求确认，利用 _chat_mute 缓存确认状态（60 秒有效）
            _chat_mute[confirm_key] = True
            from db import log_audit
            log_audit("clearall_confirm", sender_id, "请求清除所有数据，等待确认")
            return "⚠️ 此操作将清除所有对话和备注数据，且不可恢复！\n如果确定要清除，请再发一次 /clearall 确认。"
        total_conv = 0
        total_notes = 0
        files_cleared = 0
        for f in DATA_DIR.glob("*.json"):
            if f.name == "masters.json":
                continue
            try:
                mem_j = json.loads(f.read_text("utf-8"))
                conv_count = len(mem_j.get("conversation", []))
                notes_count = len(mem_j.get("notes", []))
                mem_j["conversation"] = []
                mem_j["notes"] = []
                mem_j["message_ids"] = {}
                f.write_text(json.dumps(mem_j, ensure_ascii=False, indent=2), "utf-8")
                total_conv += conv_count
                total_notes += notes_count
                files_cleared += 1
            except Exception as e:
                log(f"CLEARALL error on {f.name}: {e}")
        _invalidate_person_index()
        log(f"CLEARALL: master {sender_id} cleared {total_conv} conversations + {total_notes} notes from {files_cleared} files")
        from db import log_audit
        log_audit("clearall", sender_id, f"清除 {total_conv} 条对话 + {total_notes} 条备注（{files_cleared} 文件）")
        return f"✅ 已清除所有数据：{total_conv} 条对话 + {total_notes} 条备注（共 {files_cleared} 个文件）。masters.json 和角色设定已保留。"

    # --- 清除记忆 ---
    if m := re.match(r'^(?:/clearmem|/清空记忆|/清除记忆)\s+(\d{5,20})\s*$', text):
        qq = m.group(1)
        total = 0
        pk = f"private_{qq}"
        pm = _load_memory(pk)
        total += len(pm.get("conversation", []))
        pm["conversation"] = []
        pm["message_ids"] = {}
        _save_memory(pk, pm)
        for gf in DATA_DIR.glob(f"group_*_person_{qq}.json"):
            try:
                gm = json.loads(gf.read_text("utf-8"))
                total += len(gm.get("conversation", []))
                gm["conversation"] = []
                gm["message_ids"] = {}
                gf.write_text(json.dumps(gm, ensure_ascii=False, indent=2), "utf-8")
            except Exception:
                pass
        return f"已清除 {qq} 的全部对话记忆（共 {total} 条）。备注和人设保留。"

    # --- 清理备注冲突 ---
    if m := re.match(r'^(?:/cleannotes|/清理备注)\s+(\S+)\s*$', text):
        target_str = m.group(1).strip()
        targets, unresolved = _resolve_targets(target_str)
        if not targets:
            hint = f"找不到「{target_str}」。"
            if unresolved:
                hint += f" 未识别：{', '.join(unresolved)}"
            return hint
        _, qq, name = targets[0]
        n = _cleanup_person_notes(qq)
        return f"✅ 已清理 {name}({qq}) 的备注，移除了 {n} 条冲突或无效备注。"

    # --- 重载配置 ---
    if m := re.match(r'^(?:/reload|/重载|/重载配置|/刷新配置)\s*$', text):
        persona_ok = PERSONA_FILE.exists()
        _refresh_contact_cache()
        return (
            f"配置已重载。\n"
            f"联系人: {len(_friend_cache)} 好友, {len(_group_cache)} 群\n"
            f"Persona: {'已加载' if persona_ok else '未找到'}"
        )

    # --- 调试：查看实际 prompt ---
    if m := re.match(r'^(?:/prompt|/调试prompt)\s+(\S+)\s*$', text):
        qq = m.group(1)
        mem_ = _get_person_profile(qq)
        if not mem_:
            return f"找不到 {qq} 的 profile 文件。"
        name = _resolve_speaker_name(qq, "")
        prompt = _build_prompt(name, mem_, qq)
        return f"实际 Prompt（{qq}）:\n{prompt[:2000]}"

    # --- 刷新联系人缓存 ---
    if _try_match(r'^(?:/refresh|/刷新联系人|/刷新好友|/刷新缓存)\s*$', text, clean_text):
        _refresh_contact_cache()
        return f"联系人缓存已刷新：{len(_friend_cache)} 好友, {len(_group_cache)} 群"

    # --- 状态 ---
    if _try_match(r'^(?:/status|/状态|/chats|/会话)\s*$', text, clean_text):
        muted = [k for k, v in _chat_mute.items() if v]
        total_msgs = sum(1 for _ in DATA_DIR.glob("*.json"))
        return f"活跃会话: {len(_chat_mute)} 个 | 静默: {muted if muted else '无'} | 记忆文件: {total_msgs} 个"

    # --- 好友/群列表 ---
    if _try_match(r'^(?:/friends|/好友|/群列表|/联系人列表)\s*$', text, clean_text):
        summary = _get_contact_summary(detail=True)
        if not summary:
            return "联系人缓存尚未加载，请等待启动完成。"
        return summary

    # --- 统计面板（含错误计数，Phase 3）---
    if _try_match(r'^(?:/stats|/统计|/用量)\s*$', text, clean_text):
        total_msgs = 0
        for f in DATA_DIR.glob("*.json"):
            try:
                mem_j = json.loads(f.read_text("utf-8"))
                total_msgs += len(mem_j.get("conversation", []))
            except Exception:
                pass
        dedup = len(_processed_ids)
        muted = [k for k, v in _chat_mute.items() if v]
        masters = _get_master_ids()
        # Phase 3: 错误计数
        from monitor import get_error_counts
        errors = get_error_counts()
        error_lines = "\n".join(f"  {k}: {v}" for k, v in sorted(errors.items()) if v > 0)
        error_section = f"\n错误统计:\n{error_lines}" if error_lines else ""
        return f"""【统计面板】
总消息记录: {total_msgs:>8} 条
去重消息ID: {dedup:>8} 个
静默会话:   {len(muted):>8} 个 ({', '.join(muted) if muted else '无'})
主人数量:   {len(masters):>8} 人 ({', '.join(sorted(masters))})
模型:       {DEEPSEEK_MODEL}
搜索:       智谱 GLM-4-Flash{error_section}"""

    # --- 帮助 ---
    if re.match(r'^(?:/help|/帮助|/指令|/菜单)\s*$', text):
        return """支持指令：
/mute [会话] — 静默
/unmute [会话] — 解除静默
/owner add QQ号 — 添加主人
/owner remove QQ号 — 移除主人
/owner list — 主人列表
/clearmem QQ号 — 清除某人全部对话记忆（保留备注和人设）
/cleannotes QQ号 — 清理备注冲突和无效条目
/clearall — 清除所有记忆数据（保留管理员）
/refresh — 刷新联系人缓存
/status — 状态
/stats — 统计面板
/friends — 显示所有好友和群列表
/reload — 热重载配置
/prompt QQ号 — 调试 prompt
/help — 此菜单
其他口语指令（发消息、查天气、设称呼、查收件箱等）直接跟机器人说就行～"""

    return None


def _reply_and_log(msg_type: str, target_id: str, sender_id: str, sender_nick: str,
                    text: str, person_key: str) -> bool:
    """发送消息并写入记忆。群聊时：写入私聊文件 + 群共享记忆。"""
    # 效果四：AI 输出敏感词过滤
    from config import _filter_output
    text = _filter_output(text)
    ok = _send_text(msg_type, target_id, text)
    if ok:
        if msg_type == "group":
            _add_to_memory(f"private_{sender_id}", sender_id, sender_nick, text, "out")
            # 群聊共享记忆也写入 bot 的回复
            group_id = person_key.split("_person_")[0].replace("group_", "") if "_person_" in person_key else ""
            if group_id:
                _add_to_group_memory(group_id, SELF_QQ, SELF_NICK, text, "out")
        else:
            _add_to_memory(person_key, sender_id, sender_nick, text, "out")
    else:
        log("SEND FAILED")
    return ok


# ==================== AI 聊天处理 ====================

# ==================== 深度任务（claude -p 触发器） ====================
_deep_cfg = {
    "max_parallel": int(os.getenv("DEEP_TASK_MAX_PARALLEL", "2")),
    "nonadmin_cooldown": int(os.getenv("DEEP_TASK_NONADMIN_COOLDOWN", "600")),
    "timeout": int(os.getenv("DEEP_TASK_TIMEOUT", "600")),
    "patterns": [p.strip() for p in os.getenv(
        "DEEP_TASK_PATTERNS",
        "深度任务,深度处理,交给claude,claude处理,让claude,深度分析,深度整理"
    ).split(",") if p.strip()],
}
_deep_running: dict[str, dict] = {}       # task_id -> {sender, started,...}
_deep_user_running: dict[str, int] = {}   # 非管理员单飞：sender -> running 数
_deep_last_submit: dict[str, float] = {}  # 非管理员冷却：sender -> 上次提交时间
_deep_lock = threading.Lock()
_deep_counter = 0

_DEEP_CLASSIFY_SYS = (
    "判断这条用户消息是否在下达一个『需要深度处理/多步工具操作/较长处理时间的复杂任务』"
    "（例如：整理分析多人资料并生成报告、调查对比多项信息、多步骤数据处理、需要先查多处再综合的任务）。"
    "普通闲聊、简单问答、单个工具就能完成的操作（查天气、发消息、改称呼、设提醒）都算 NO。"
    "只回答 YES 或 NO，不要解释。"
)


def _deep_strip_prefix(content: str) -> str:
    """去掉开头的 @某人 / bot 称呼前缀。"""
    c = re.sub(r"^@某人\s*", "", content)
    for name in sorted(_get_bot_all_names(), key=len, reverse=True):
        if c.startswith(name):
            c = c[len(name):].lstrip("，,、 :：")
            break
    return c.strip()


def _deep_match_pattern(content: str) -> str | None:
    """快层：触发词匹配。命中返回任务正文，未命中返回 None。"""
    for pat in _deep_cfg["patterns"]:
        idx = content.find(pat)
        if idx != -1:
            after = content[idx + len(pat):].lstrip("：: ，,。")
            return after if after else content.strip()
    return None


def _deep_classify(content: str) -> bool:
    """语义层兜底：用 DeepSeek 判断是否复杂任务（便宜、4秒内、只答 YES/NO）。"""
    try:
        ans = _call_deepseek(_DEEP_CLASSIFY_SYS, content, max_tokens=5) or ""
        return "YES" in ans.upper()
    except Exception:
        return False


def _maybe_deep_task(content: str, sender_id: str, sender_nick: str, msg_type: str,
                     target_id: str, chat_id: str, is_at_me: bool,
                     person_key: str, resolved_name: str) -> bool:
    """发行版默认禁用深度任务，避免将聊天内容传给高权限执行器。"""
    return False
    # 静默会话不触发；内容太短不触发
    if _is_chat_muted(chat_id):
        return False
    if not content or len(content.strip()) < 4:
        return False
    # 群聊必须 @ 机器人才可能触发
    if msg_type == "group" and not is_at_me:
        return False

    clean = _deep_strip_prefix(content)
    task_text = _deep_match_pattern(clean)
    if task_text is None:
        # 语义兜底分类（消息太短不分类）
        if len(clean) < 8:
            return False
        if not _deep_classify(clean):
            return False
        task_text = clean
    task_text = task_text.strip()
    if not task_text:
        return False

    is_admin = _is_master(sender_id, mem=None) or sender_id in _get_master_ids()
    now = time.time()
    global _deep_counter
    with _deep_lock:
        # 全局并行上限（管理员同样受限）
        if len(_deep_running) >= _deep_cfg["max_parallel"]:
            _reply_and_log(msg_type, target_id, sender_id, sender_nick,
                           f"⚙️ 已有 {len(_deep_running)} 个深度任务在跑，通道满了，等几分钟再交给我吧～",
                           person_key)
            return True
        if not is_admin:
            # 非管理员单飞：上一个没跑完不能提新的
            if _deep_user_running.get(sender_id, 0) > 0:
                _reply_and_log(msg_type, target_id, sender_id, sender_nick,
                               "⏳ 你上一个深度任务还没出结果，等它跑完再交新的哦～",
                               person_key)
                return True
            # 非管理员冷却
            last = _deep_last_submit.get(sender_id, 0)
            wait = _deep_cfg["nonadmin_cooldown"] - (now - last)
            if last and wait > 0:
                _reply_and_log(msg_type, target_id, sender_id, sender_nick,
                               f"⏳ 深度任务冷却中，还要等 {int(wait // 60) + 1} 分钟才能再交哦～",
                               person_key)
                return True
        # 占位
        _deep_counter += 1
        tid = f"deep_{int(now)}_{_deep_counter}"
        _deep_running[tid] = {"sender": sender_id, "started": now,
                              "type": msg_type, "target": target_id}
        if not is_admin:
            _deep_user_running[sender_id] = _deep_user_running.get(sender_id, 0) + 1
            _deep_last_submit[sender_id] = now

    _reply_and_log(msg_type, target_id, sender_id, sender_nick,
                   f"⚙️ 收到，深度处理中（最长 {_deep_cfg['timeout'] // 60} 分钟）…结果一出来马上发你",
                   person_key)
    log(f"[deep] {tid} 受理 来自 {sender_nick}({sender_id}): {task_text[:60]}")
    threading.Thread(target=_deep_run,
                     args=(tid, task_text, msg_type, target_id, sender_id, sender_nick),
                     daemon=True).start()
    return True


def _deep_run(tid: str, task_text: str, msg_type: str, target_id: str,
              sender_id: str, sender_nick: str):
    """发行版禁用深度任务执行器。"""
    return
    import shutil as _shutil
    import subprocess
    ok_text = None
    started = time.time()
    try:
        exe = _shutil.which("claude") or "claude"
        # 额外授权目录（如 Obsidian 知识库），逗号分隔，仅加真实存在的目录
        add_dirs = [d.strip() for d in os.getenv("DEEP_TASK_ADD_DIRS", "").split(",")
                    if d.strip() and os.path.isdir(d.strip())]
        _now = datetime.now()
        _wday = "一二三四五六日"[_now.weekday()]
        _time_str = f"{_now.year}-{_now.month:02d}-{_now.day:02d} {_now.hour:02d}:{_now.minute:02d} 星期{_wday}"
        prompt = (
            f"你是QQ机器人『机器人』背后的深度任务引擎。QQ用户「{sender_nick}」(QQ:{sender_id}) 下达了任务：\n"
            f"{task_text}\n\n"
            f"【当前真实时间：{_time_str}】——任务里说的『今天/本周/最近』都以这个时间为基准计算。\n\n"
            "要求：\n"
            "1. 必须基于 qq-manage MCP 工具查到的真实数据完成任务，禁止凭感觉编造：\n"
            "   - 统计/排名/记录类任务 → 优先用 data_query 或 search_conversations 查询\n"
            "   - 人物档案/关系 → get_person_info / list_all_persons / get_person_stats\n"
            "   - 其他需求自行选择合适的工具，可以连续调用多个\n"
            "2. 不要调用 send_message（系统会把你的最终输出回发给用户，避免重复发送）。\n"
            "3. 最终输出：面向用户的完整结果文本，口语化像真人总结，直接给出结论和关键数据\n"
            "   （比如排名就列出名次+人名+数字）。\n"
            "4. 工具失败就换思路或如实说明缺什么数据，绝不编造。\n"
            + (f"5. 本机已授权以下本地目录（主人的知识库/笔记），任务涉及知识库内容时用 Glob/Grep/Read 文件工具直接检索这些目录："
               f"{'；'.join(add_dirs)}\n" if add_dirs else "")
        )
        try:
            # prompt 走 stdin（Windows 下 .cmd 参数会经 cmd.exe 转发，换行/特殊字符会被截断）
            # allowedTools 是白名单：MCP 全部工具 + 内置只读文件工具（Read/Glob/Grep）
            _cmd = [exe, "-p", "--allowedTools",
                    "mcp__qq-manage", "Read", "Glob", "Grep"]
            for d in add_dirs:
                _cmd += ["--add-dir", d]
            _cmd += ["--max-turns", "15"]
            proc = subprocess.run(
                _cmd,
                input=prompt,
                capture_output=True, text=True, timeout=_deep_cfg["timeout"],
                encoding="utf-8", errors="replace",
                cwd=os.path.dirname(os.path.abspath(__file__)),
            )
            out = (proc.stdout or "").strip()
            if out:
                ok_text = out
            else:
                log(f"[deep] {tid} claude -p 无输出 rc={proc.returncode} "
                    f"stderr={(proc.stderr or '')[:200]}")
        except subprocess.TimeoutExpired:
            log(f"[deep] {tid} 超时（{_deep_cfg['timeout']}s）被强杀")
    except Exception as e:
        log(f"[deep] {tid} 启动失败: {e}")

    # 清理占用
    with _deep_lock:
        _deep_running.pop(tid, None)
        if _deep_user_running.get(sender_id):
            _deep_user_running[sender_id] -= 1

    if not ok_text:
        ok_text = "⚙️ 深度任务没能完成（内部出错或超时了），稍后再试一次？"

    # 结果回发（超长分段）
    chunks = [ok_text[i:i + 1200] for i in range(0, len(ok_text), 1200)]
    for c in chunks:
        _send_text(msg_type, target_id, c)
        time.sleep(0.5)
    log(f"[deep] {tid} 完成 耗时{time.time() - started:.0f}s 结果{len(ok_text)}字 "
        f"预览: {ok_text[:150]} → {msg_type}/{target_id}")


def _think_and_reply(event: dict):
    # Phase 1.1: 用 SafetyNet.protect 替代裸 try/except，异常不逃逸进程
    SafetyNet.protect(_do_think_and_reply, context="think")(event)


def _do_think_and_reply(event: dict):
    """全链路同步执行 — 不做异常吞没"""
    msg_type   = event["message_type"]
    target_id  = event["target_id"]
    sender_id  = event["sender_id"]
    sender_nick = event["sender_nick"]
    content    = event.get("content", "")
    group_id   = event.get("group_id", "")
    is_at_me   = event.get("is_at_me", False)
    group_name = event.get("group_name", "")
    resolved_name = _resolve_speaker_name(sender_id, group_id)

    chat_id = _chat_id(msg_type, target_id)
    person_key = _person_key(msg_type, group_id, sender_id)

    if msg_type == "group":
        mem = _add_to_memory(f"private_{sender_id}", sender_id, sender_nick, content, "in", group_id=group_id)
        # Phase 5: 群聊共享记忆 — 所有人消息写入 group_{GID}.json
        _add_to_group_memory(group_id, sender_id, resolved_name, content, "in")
        # 从群聊共享记忆加载上下文（全貌视角），而非单人 private 文件
        context_str, speakers_seen = _load_group_context(group_id, limit=30)
    else:
        mem = _add_to_memory(person_key, sender_id, sender_nick, content, "in")
    recent = mem["conversation"][-30:]

    # Record incoming messages even while automatic replies are paused.
    if ui_runtime.paused:
        return

    if msg_type == "group":
        # 群聊上下文已从 _load_group_context 获取，跳过旧的逐条解析
        pass
    else:
        # Phase 2.4: 长对话启用智能上下文压缩
        from config import _compress_context
        context_str = _compress_context(recent)
    # 备注来源：JSON notes（向后兼容）+ SQLite free_notes/key_facts（AI 工具写入）
    _notes_parts = [n.get("note", "") for n in mem.get("notes", []) if n.get("note", "")]
    if sender_id:
        try:
            from db import get_free_notes, get_key_facts
            _free = get_free_notes(sender_id)
            if _free:
                _notes_parts.append(_free)
            for _fact in get_key_facts(sender_id):
                _notes_parts.append(f"关键信息: {_fact}")
        except Exception:
            pass
    notes_str = "\n".join(f"- {n}" for n in _notes_parts) if _notes_parts else "无"

    log(f"RECV [{sender_nick}]: {content[:50]}")

    # 图片识别：有 image_urls 时用视觉模型识别，注入到对话
    image_descriptions = []
    image_urls = event.get("image_urls", [])
    file_urls = event.get("file_urls", [])
    file_names = event.get("file_names", [])
    if image_urls and not _check_image_call_limit():
        # 日限制超限 → 提示用户
        _reply_and_log(msg_type, target_id, sender_id, sender_nick,
                       "今日图片识别额度已用完（100张/天），明天再试哦~ 可以打字告诉我内容代替图片~",
                       person_key)
        return
    if image_urls:
        for url in image_urls:
            desc = _describe_image(url)
            if desc:
                image_descriptions.append(desc)
                log(f"IMAGE DESC [{sender_nick}]: {desc[:80]}")
        # 缓存最后一张图的描述，供后续文字指令引用
        if image_descriptions:
            _last_media_cache[person_key] = {
                "description": image_descriptions[-1],
                "time": time.time(),
                "type": "image",
            }
    has_media = event.get("has_media", False)
    media_types = event.get("media_types", set())

    # 纯媒体消息处理
    text_only = re.sub(
        r'\[(?:图片|表情|贴纸|视频|语音|文件|回复|at|bface|dice|rps|shake|share|music|forward|json|xml)\]\s*',
        '', content
    ).strip()
    if has_media and not text_only:
        if image_descriptions:
            content = "用户发了一张图片。" + " ".join(image_descriptions)
        elif "file" in media_types:
            try:
                file_url = (file_urls or [""])[0]
                file_name = (file_names or ["文件"])[0]
                content = _read_file_content(file_url, file_name)
                log(f"读取文件: {file_name}")
                # Bug 2 fix: 读取失败（返回错误字符串以"（"开头）→ 直接友好回复，不经过 AI 路由
                if content.startswith("（"):
                    _reply_and_log(msg_type, target_id, sender_id, sender_nick,
                                   f"收到文件「{file_name}」但无法读取内容哦~ 可以打字告诉我文件里写了什么。",
                                   person_key)
                    return
            except Exception as _fe:
                log(f"文件读取失败: {_fe}")
                _reply_and_log(msg_type, target_id, sender_id, sender_nick,
                               f"收到文件「{file_name}」但无法读取内容哦~",
                               person_key)
                return
        else:
            _reply_and_log(msg_type, target_id, sender_id, sender_nick,
                           "发了个什么呀？我暂时看不到图片和表情，可以打字告诉我内容哦~",
                           person_key)
            return
    elif image_descriptions:
        content = content + "\n[图片描述] " + " ".join(image_descriptions)

    # 跨消息媒体上下文：5 分钟内收到过图片/文件 → 注入描述供当前文字指令引用
    content = _inject_media_context(content, person_key)

    # 0. 群聊门控：只有真实 @ 机器人才回复（管理员与普通用户一致）
    if msg_type == "group":
        if not is_at_me:
            return
        if _is_master(sender_id, mem):
            pass
        elif _group_rate_limited(group_id, max_replies=3, window=60):
            return

    # 1. 指令预解析（Path A：系统级关键命令直通车）
    cmd_result = _parse_command(content, sender_id, mem, chat_id, msg_type, target_id, resolved_name)
    if cmd_result is not None:
        _reply_and_log(msg_type, target_id, sender_id, sender_nick, cmd_result, person_key)
        log(f"CMD: {cmd_result[:50]}")
        return

    # 1.4. 深度任务网关（claude -p 触发器）：命中则独占处理，不再走 Path B/C
    if _maybe_deep_task(content, sender_id, sender_nick, msg_type, target_id,
                        chat_id, is_at_me, person_key, resolved_name):
        return

    # 1.5. AI 工具路由（所有人统一走 function calling，工具内部判断权限）
    ai_result = _ai_intent_route(content, sender_id, sender_nick,
                                  msg_type, target_id, chat_id, mem)
    if ai_result is not None:
        reply = ai_result.strip()
        if reply:
            if _reply_and_log(msg_type, target_id, sender_id, sender_nick, reply, person_key):
                log(f"AI-TOOL: {reply[:50]}")
            return
        else:
            log("AI-TOOL EMPTY REPLY")
    else:
        # 工具路由为 None 表示口嗨检测触发 → agent 识别到指令但没调工具
        # 不给普通用户暴露错误细节，统一走 Path C 纯聊天兜底
        pass

    # 1.6. 群聊冷却+限频（主人不受限）
    if msg_type == "group" and not _is_master(sender_id, mem):
        now_ts = time.time()
        if chat_id in _chat_cooldown:
            last_time, last_hash = _chat_cooldown[chat_id]
            if now_ts - last_time < 6 and last_hash == _simple_hash(content):
                log(f"冷却拦截 chat={chat_id}")
                return
        if _rate_limit(chat_id, sender_id, max_replies=10, window=300):
            log(f"限频拦截 {sender_id}")
            return
        _update_cooldown(chat_id, content)

    # 2. 静默
    if _is_chat_muted(chat_id):
        if _is_master(sender_id, mem) and re.search(r"(?:可以了|继续|说话吧|取消静默|解除)", content):
            _set_chat_muted(chat_id, False)
            _send_text(msg_type, target_id, "好的，已恢复回复。")
        return

    # 3. 构建 prompt
    now = datetime.now()
    wday = ["一","二","三","四","五","六","日"][now.weekday()]
    time_str = f"{now.year}年{now.month:02d}月{now.day:02d}日 {now.hour:02d}:{now.minute:02d} 星期{wday}"

    merged_mem = mem
    if msg_type == "group":
        profile = _get_person_profile(sender_id)
        if profile.get("name") and not mem.get("name"):
            mem["name"] = profile["name"]
        if profile.get("notes") and not mem.get("notes"):
            mem["notes"] = profile["notes"]
        if profile.get("role") and not mem.get("role"):
            mem["role"] = profile["role"]
        merged_mem = mem

    system_prompt = f"【当前时间：{time_str}。你对时间的认知以这个为准】\n{_build_prompt(resolved_name, merged_mem, chat_id)}"

    # 主人消息：注入全局收件箱摘要（让 AI 知道最近谁发过消息）
    if _is_master(sender_id, mem):
        inbox = _summarize_inbox(sender_id, hours=24)
        if inbox:
            system_prompt += f"\n\n【全局收件箱（最近24h非主人私聊）】：\n{inbox}\n你可以基于以上信息回答「谁找我」「有新消息吗」等问题。"

    # P24.1: token 分配
    max_tok = 800

    enhanced_content = content
    has_context_boost = False
    if len(content.strip()) < 10 and len(recent) >= 2:
        prev = recent[-2]
        if prev.get("direction") == "in" and len(prev.get("content", "")) > 20:
            enhanced_content = f"（接上面的对话：{prev['content'][:200]}）\n{content}"
            has_context_boost = True
            max_tok = max(max_tok, 600)

    context_header = ""
    if len(recent) >= 3:
        last_out = [m for m in recent if m.get("direction") == "out"]
        last_in  = [m for m in recent if m.get("direction") == "in"]
        if last_out:
            context_header += f"你最晚回复说的是：「{last_out[-1]['content'][:100]}」\n"
        if last_in and last_in[-1].get("content") != content:
            context_header += f"他上一条说的是：「{last_in[-1]['content'][:150]}」\n"

    # Phase 3.3: 群聊参与者名册 — 给 AI 稳定的人物映射锚点
    cast_str = ""
    if msg_type == "group" and speakers_seen:
        cast_lines = [f"  - {name} (QQ:{sid})" for sid, name in speakers_seen.items()]
        cast_str = "\n当前对话参与者：\n" + "\n".join(cast_lines) + "\n"

    if has_context_boost:
        user_msg = f"""备注信息：
{notes_str}

{context_header}{cast_str}场景：{"群聊 " + group_name if msg_type == "group" else "私聊"}
对方说：{enhanced_content}
{'（@了你）' if is_at_me else ''}

对话历史：
{context_str}

请根据规则回复。知识问题请给出有信息量的回答。"""
    else:
        user_msg = f"""备注信息：
{notes_str}

{context_header}{cast_str}场景：{"群聊 " + group_name if msg_type == "group" else "私聊"}
对方说：{enhanced_content}
{'（@了你）' if is_at_me else ''}

对话历史：
{context_str}

请根据规则回复。"""

    # 4. 搜索
    search_context = ""
    if _should_search_ai(content, context_str):
        log(f"SEARCH: {content[:30]}")
        if re.search(r"天气|气温|下雨|多少度", content):
            city = re.search(r"(\S{2,4})(?:的)?(?:天气|气温|多少度|下雨)", content)
            city_name = city.group(1) if city else os.getenv("DEFAULT_CITY", "").strip()
            for m in recent[-5:]:
                if m.get("direction") == "in":
                    cm = re.search(r"(\S{2,4})(?:的)?(?:天气|气温)", m.get("content", ""))
                    if cm:
                        city_name = cm.group(1)
            weather = _get_weather(city_name) if city_name else None
            if weather:
                search_context = f"\n\n【实时天气查询结果，绝对以此为准】：\n{weather}"
                log(f"天气: {weather[:80]}...")
            else:
                sr = _web_search(_extract_search_query(content, context_str))
                if sr:
                    search_context = f"\n\n【搜索结果，优先以此为准】：\n{sr}"
        else:
            sq = _extract_search_query(content, context_str)
            log(f"  search query: {sq}")
            sr = _web_search(sq)
            if sr:
                search_context = f"\n\n【以下为实时搜索结果，绝对以此为准回答】：\n{sr}"

    # 5. 调 AI（Path C：纯聊天）
    log(f"CALLING AI max_tok={max_tok}...")
    reply = _call_deepseek(system_prompt, user_msg + search_context, max_tokens=max_tok)
    if not reply or not reply.strip():
        log("AI EMPTY")
        return

    reply = reply.strip()
    log(f"REPLY [{sender_nick}]: {reply[:60]}")

    _reply_and_log(msg_type, target_id, sender_id, sender_nick, reply, person_key)


# ==================== WebSocket 主循环 ====================

async def ws_main():
    import websockets

    while True:
        try:
            log("连接 WebSocket...")
            async with websockets.connect(NAP_CAT_WS, ping_interval=30,
                                          ping_timeout=10, max_size=2**20) as ws:
                log("WebSocket 已连接")
                ui_runtime.ws_connected = True

                try:
                    info = _api("get_login_info")
                    if info.get("status") == "ok":
                        import config
                        config.SELF_QQ   = str(info["data"].get("user_id", MY_QQ))
                        log(f"你好 {config.SELF_NICK}，QQ={config.SELF_QQ}")
                except Exception:
                    pass

                friends = _api("get_friend_list").get("data", [])
                groups  = _api("get_group_list").get("data", [])
                _refresh_contact_cache()
                log(f"{len(friends)} 好友, {len(groups)} 群")
                async for raw in ws:
                    try:
                        data = json.loads(raw)
                        ui_runtime.last_event = time.time()
                        pt = data.get("post_type", "")
                        if pt != "message":
                            if pt in ("meta_event",):
                                continue
                            log(f"RAW({pt}): {str(raw)[:80]}")
                            continue

                        msg_type   = data.get("message_type", "")
                        sender     = data.get("sender", {})
                        sender_id  = str(sender.get("user_id", ""))
                        sender_nick = sender.get("nickname", "?")
                        parsed = _parse_message_segments(data)
                        content     = parsed[0]
                        raw_content = data.get("raw_message", "") or ""
                        has_media   = parsed[1]
                        media_types = parsed[2]
                        image_urls  = parsed[3]
                        file_urls  = parsed[4] if len(parsed) > 4 else []
                        file_names = parsed[5] if len(parsed) > 5 else []
                        self_id    = str(data.get("self_id", MY_QQ))
                        message_id = str(data.get("message_id", ""))
                        is_at_me   = f"[CQ:at,qq={self_id}]" in content
                        # 真实 @ 检测：at 段在解析时被替换成"@某人"，丢失了 qq 信息，
                        # 从原始消息段恢复判断（NapCat message 数组格式）
                        if not is_at_me:
                            try:
                                for _seg in data.get("message") or []:
                                    if (isinstance(_seg, dict) and _seg.get("type") == "at"
                                            and str((_seg.get("data") or {}).get("qq", "")) == self_id):
                                        is_at_me = True
                                        break
                            except Exception:
                                pass
                        # 兜底：NapCat 若配置 at 转文本，真实 @ 会以 "@昵称" 文本形式出现
                        if not is_at_me and config.SELF_NICK:
                            is_at_me = f"@{config.SELF_NICK}" in content
                        if not is_at_me and SELF_NICK and SELF_NICK != config.SELF_NICK:
                            is_at_me = f"@{SELF_NICK}" in content

                        group_id  = str(data.get("group_id", "")) if msg_type == "group" else ""
                        target_id = group_id if msg_type == "group" else sender_id

                        if msg_type == "group" and not is_at_me:
                            person_key = _person_key(msg_type, group_id, sender_id)
                            mem_ = _load_memory(person_key)
                            recent3 = mem_.get("conversation", [])[-3:]
                            in_conv = any(m.get("direction") == "out" for m in recent3)
                            if not in_conv:
                                continue

                        if sender_id == self_id:
                            continue

                        if ui_runtime.paused:
                            continue

                        if message_id:
                            if message_id in _processed_ids:
                                continue
                            # Phase 4.3: LRU — 新条目添加到末尾（最新）
                            _processed_ids[message_id] = None
                            # 超限时从头部淘汰最旧
                            if len(_processed_ids) > _MAX_PROCESSED_IDS:
                                _processed_ids.popitem(last=False)

                        event = {
                            "message_type": msg_type,
                            "target_id": target_id,
                            "sender_id": sender_id,
                            "sender_nick": sender_nick,
                            "content": content,
                            "raw_content": raw_content,
                            "has_media": has_media,
                            "media_types": media_types,
                            "image_urls": image_urls,
                            "file_urls": file_urls,
                            "file_names": file_names,
                            "is_at_me": is_at_me,
                            "group_id": group_id,
                            "group_name": data.get("group_name", ""),
                        }

                        log(f"→ {sender_nick}: {content[:40]}")
                        # Phase 5.1: 记录消息吞吐量
                        try:
                            from monitor import Monitor
                            Monitor.record("message_in_rate", time.time())
                        except Exception:
                            pass
                        threading.Thread(target=_think_and_reply, args=(event,),
                                         daemon=True).start()

                    except Exception:
                        pass

        except Exception as e:
            ui_runtime.ws_connected = False
            # Phase 4.3: 指数退避重连（5s → 10s → 20s → 最大 60s）
            _ws_reconnect_delay = min(getattr(ws_main, '_reconnect_delay', 5) * 2, 60)
            ws_main._reconnect_delay = _ws_reconnect_delay
            log(f"WS 断开: {e}，{_ws_reconnect_delay:.0f}秒后重连")
            await asyncio.sleep(_ws_reconnect_delay)
        else:
            ui_runtime.ws_connected = False
            # 连接成功 → 重置退避
            ws_main._reconnect_delay = 5


# ==================== Phase 4.3: WS 重连退避状态 ====================
ws_main._reconnect_delay = 5  # type: ignore[attr-defined]

# ==================== 定时任务 ====================
_scheduler_last_run: dict[str, float] = {}


async def _scheduler():
    """每 60 秒检查一次定时任务"""
    while True:
        await asyncio.sleep(60)
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        hour = now.hour

        # 称呼轮换：每60分钟检查一次（在整点附近执行）
        task_key_nickname = f"nickname_switch_{today}_{hour}"
        if task_key_nickname not in _scheduler_last_run:
            _scheduler_last_run[task_key_nickname] = time.time()
            try:
                _auto_switch_all_nicknames()
            except Exception as e:
                log(f"称呼轮换失败: {e}")

        # 早安摘要（效果六）：8 点推送一条消息 = 天气 + 最近消息 + 今日提醒
        task_key = f"morning_digest_{today}"
        if os.getenv("QQBOT_AUTO_DIGEST", "0") == "1" and hour >= 8 and hour < 9 and task_key not in _scheduler_last_run:
            _scheduler_last_run[task_key] = time.time()
            try:
                await _build_morning_digest()
            except Exception as e:
                log(f"早安摘要失败: {e}")

        # 午/晚消息汇总（仅最近消息，不含天气和提醒）
        for _period, _hour in [("noon",12), ("afternoon",18)]:
            _sk = f"summary_{today}_{_period}"
            if os.getenv("QQBOT_AUTO_DIGEST", "0") == "1" and hour == _hour and _sk not in _scheduler_last_run:
                _scheduler_last_run[_sk] = time.time()
                try:
                    _summary = _collect_recent_summary(hours=8)
                    for _mid in _get_master_ids():
                        _send_text("private", _mid, _summary)
                except Exception as _se:
                    log(f"时段汇总失败({_period}): {_se}")
        # ★ P0 修复：定时任务派发给独立线程执行，避免阻塞 asyncio 事件循环
        # WS 消息处理在主循环中，如果 scheduler 阻塞会导致用户消息卡死
        try:
            loop = asyncio.get_running_loop()
            if not getattr(_scheduler, '_task_running', False):
                _scheduler._task_running = True  # type: ignore[attr-defined]
                try:
                    await loop.run_in_executor(
                        None, _execute_scheduled_tasks_sync, now, today)
                finally:
                    _scheduler._task_running = False  # type: ignore[attr-defined]
        except Exception as e:
            log(f"定时任务执行失败: {e}")

        # Phase 2.3: 每日 02:00 生成当日摘要
        task_key_mem = f"nightly_summary_{today}"
        if hour == 2 and task_key_mem not in _scheduler_last_run:
            _scheduler_last_run[task_key_mem] = time.time()
            try:
                from config import MemoryManager
                MemoryManager.nightly_summarize()
            except Exception as e:
                log(f"夜间摘要失败: {e}")

        task_key2 = f"prune_{today}"
        if hour == 0 and task_key2 not in _scheduler_last_run:
            _scheduler_last_run[task_key2] = time.time()
            items = list(_scheduler_last_run.items())
            cutoff = time.time() - 7 * 86400
            for k, ts in items:
                if ts < cutoff:
                    del _scheduler_last_run[k]

        # Phase 1.4: 周期性健康检查
        try:
            await _health_check_routine()
        except Exception as e:
            log(f"健康检查异常: {e}")

        # Phase 5.1: 监控告警
        try:
            from monitor import Monitor
            alert = Monitor.alert_if_needed()
            if alert:
                log(f"[MONITOR] {alert}")
                for mid in _get_master_ids():
                    _send_text("private", mid, alert)
        except Exception:
            pass


async def _health_check_routine():
    """每 60 秒检查各组件健康状态，连续告警通知管理员。"""
    problems = []

    # 1. NapCat HTTP 可达性
    try:
        r = _api("get_login_info")
        if r.get("status") != "ok":
            problems.append(f"NapCat 异常: {r.get('message', 'unknown')}")
    except Exception as e:
        problems.append(f"NapCat 不可达: {e}")

    # 2. 数据目录写入
    try:
        test_file = DATA_DIR / ".health_check"
        test_file.write_text("ok")
        test_file.unlink()
    except Exception as e:
        problems.append(f"数据目录写入异常: {e}")

    # 3. 记忆文件过多
    file_count = len(list(DATA_DIR.glob("*.json")))
    if file_count > 2000:
        problems.append(f"记忆文件过多: {file_count}")

    # 4. 最近错误率（依赖 Logger）
    try:
        from messaging import Logger
        recent_errors = Logger.get_recent_errors(minutes=5)
        if len(recent_errors) > 10:
            problems.append(f"最近 5 分钟错误数: {len(recent_errors)}")
    except Exception:
        pass

    if problems:
        log(f"[HEALTH] 发现问题 {len(problems)} 项")


def _advance_first_trigger(task: dict):
    """在任务执行前推进 first_trigger 到下一次触发时间，同时记录 last_run。
    即使执行崩溃，first_trigger 已推进 → 不会重复触发。
    - daily/cron: first_trigger + 1 天
    - interval: now + interval_hours
    - once: 不推进（执行后由 _report_scheduled_task 关闭任务）
    """
    task_id = task["id"]
    task_type = task["task_type"]
    first_trigger = task.get("first_trigger", "")
    now_iso = datetime.now().isoformat()
    from db import update_task_last_run, _get_conn, _parse_time_to_ts
    # 记录 last_run（调试/审计用）
    update_task_last_run(task_id, now_iso)
    # 推进 first_trigger
    if task_type in ("daily", "cron"):
        ft_ts = _parse_time_to_ts(first_trigger)
        if ft_ts:
            from datetime import timedelta
            next_ft = datetime.fromtimestamp(ft_ts) + timedelta(days=1)
            conn = _get_conn()
            conn.execute("UPDATE scheduled_tasks SET first_trigger=? WHERE id=?",
                        (next_ft.isoformat(), task_id))
            conn.commit()
    elif task_type == "interval":
        interval_hours = float(task.get("interval_hours", 0))
        if interval_hours > 0:
            from datetime import timedelta
            next_ft = datetime.now() + timedelta(hours=interval_hours)
            conn = _get_conn()
            conn.execute("UPDATE scheduled_tasks SET first_trigger=? WHERE id=?",
                        (next_ft.isoformat(), task_id))
            conn.commit()


def _execute_scheduled_tasks(now: datetime, today: str):
    # Serialize management edits with dispatch so a deleted/paused task cannot
    # execute from a stale due-task snapshot in this process.
    with ui_runtime.task_lock:
        return _execute_scheduled_tasks_locked(now, today)


def _execute_scheduled_tasks_locked(now: datetime, today: str):
    """遍历到期定时任务并执行消息发送。根据 action_type 分流执行。
    P3: 失败分级 + 自动重试（网络错误重试 1 次）。
    """
    from users import is_muted, get_mute_status
    now_ts = now.timestamp()
    now_iso = now.isoformat()
    due = get_due_tasks(now_ts, today)
    if not due:
        return

    for task in due:
        task_id = task["id"]
        action_type = task.get("action_type", "static")
        _result_extra = {}
        # ★★★ 关键：执行前推进 first_trigger，防止崩溃/重启后重复触发 ★★★
        _advance_first_trigger(task)
        # P3: 执行 + 重试逻辑
        _last_error = None
        for attempt in range(2):  # 最多 2 次（首次 + 重试 1 次）
            try:
                _execute_single_task(task, action_type, _result_extra)
                _last_error = None
                break  # 成功 → 跳出重试
            except Exception as e:
                _last_error = e
                if attempt == 0:
                    log(f"定时任务 [{task_id}] 首次失败，5s 后重试: {e}")
                    time.sleep(5)  # 网络错误等 5s 再试
        if _last_error:
            # 重试耗尽 → 记录失败
            log(f"定时任务发送失败 [{task_id}]: {_last_error}")
            _report_scheduled_task(task, f"❌ 执行失败（已重试）: {_last_error}", _result_extra)
            continue
        # 向管理员汇报任务结果（含成功/失败人数）
        _report_scheduled_task(task, None, _result_extra)


def _execute_single_task(task: dict, action_type: str, _result_extra: dict):
    """执行单个定时任务（供 retry 包装调用）。
    统一路径：所有任务都经过 AI 生成消息后发送。
    - target_id="all" → 遍历好友，逐个读取画像，AI 个性化生成后私发
    - 其他 target_id → AI 根据 prompt 生成消息后单发
    报错机制：AI 调用失败降级为直接发送原内容；单条发送失败追踪并汇报管理员。
    """
    from users import is_muted, get_mute_status
    task_id = task["id"]
    _target_id = task.get("target_id", "")

    # 静默检查：目标是单个用户且处于静默 → 跳过并通知管理员
    if _target_id and _target_id != "all" and is_muted(_target_id):
        log(f"定时任务跳过靜默用户 {_target_id}：{task['name']}({task_id})")
        mute_status = get_mute_status(_target_id)
        for _mid in _get_master_ids():
            _send_text("private", _mid,
                       f"⏭️ 定时任务 [{task['name']}] 已跳过静默用户 {_target_id}（{mute_status}）")
        return

    prompt = task["content"].strip()
    _now_dt = datetime.now()
    wday_list = ["一","二","三","四","五","六","日"]
    now_str = f"{_now_dt.year}年{_now_dt.month:02d}月{_now_dt.day:02d}日 {_now_dt.hour:02d}:{_now_dt.minute:02d} 星期{wday_list[_now_dt.weekday()]}"

    if task.get("target_id") == "all":
        # 个性化广播：遍历好友，逐个读取画像，AI 个性化生成后发送
        from db import get_all_contacts, get_resolved_name, get_person_tags, get_user_summary
        from config import _friend_cache
        valid_contacts = get_all_contacts(friend_cache=_friend_cache if _friend_cache else None)
        if not valid_contacts:
            log(f"定时任务 [{task_id}] 无好友可发送")
            _result_extra["detail"] = "无好友"
            return
        count = 0
        failed_list = []
        muted_skipped = []
        for c in valid_contacts:
            qq = c["qq"]
            if is_muted(qq):
                muted_skipped.append(qq)
                continue
            name = get_resolved_name(qq) or c.get("fixed_name", "") or qq
            tags = get_person_tags(qq)
            summary = get_user_summary(qq) or ""
            city = c.get("city", "")
            context_parts = [f"收信人称呼：{name}"]
            if city:
                context_parts.append(f"所在城市：{city}")
            if tags:
                context_parts.append(f"关系标签：{', '.join(tags)}")
            if summary:
                context_parts.append(f"人物画像：{summary}")
            context = "\n".join(context_parts)
            # AI 个性化生成（带异常兜底）
            try:
                personalized = _call_deepseek(
                    _build_default_persona() + "\n请根据用户指令和收信人信息，写一条简短自然的消息。"
                    "只输出消息文本，不要加引号或额外说明。"
                    f"当前真实时间：{now_str}。请根据当前时间调整消息语气和内容。",
                    f"用户指令：{prompt}\n\n收信人信息：\n{context}\n\n请生成消息：",
                    max_tokens=400,
                )
            except Exception as e:
                log(f"定时任务 AI 调用失败 [{task_id}] 收信人 {name}({qq}): {e}")
                personalized = None
            if isinstance(personalized, str) and personalized.strip():
                if _send_text("private", qq, personalized.strip()):
                    count += 1
                else:
                    failed_list.append(name)
            else:
                # AI 失败或返回空 → 模板替换兜底
                fallback = prompt.replace("{name}", name).replace("{city}", city)
                if _send_text("private", qq, fallback):
                    count += 1
                else:
                    failed_list.append(name)
            time.sleep(1)
        detail_parts = [f"已发送 {count}/{len(valid_contacts)} 人"]
        if failed_list:
            detail_parts.append(f"发送失败: {', '.join(failed_list[:10])}")
        _result_extra["detail"] = "；".join(detail_parts)
        log(f"定时任务 [{task_id}] {task['name']}: " + _result_extra["detail"])
        # 静默/失败反馈给管理员
        if muted_skipped:
            for _mid in _get_master_ids():
                _send_text("private", _mid,
                           f"🔇 定时任务 [{task['name']}] 已跳过 {len(muted_skipped)} 位静默用户")
        if failed_list:
            for _mid in _get_master_ids():
                _send_text("private", _mid,
                           f"⚠️ 定时任务 [{task['name']}] {len(failed_list)} 人发送失败: {', '.join(failed_list[:5])}")
    else:
        # 单发：AI 生成后发送
        try:
            generated = _call_deepseek(
                _build_default_persona() + "\n请根据用户指令生成一条简短自然的消息。"
                f"只输出消息文本，不要加引号或额外说明。"
                f"当前真实时间：{now_str}。请根据当前时间调整消息语气和内容（比如晚上就不要说早安）。",
                prompt,
                max_tokens=400,
            )
        except Exception as e:
            log(f"定时任务 AI 调用失败 [{task_id}]: {e}，降级为发送原始内容")
            generated = None
        if isinstance(generated, str) and generated.strip():
            _send_text(task["target_type"], task["target_id"], generated.strip())
            _result_extra["detail"] = "已发送"
        else:
            # AI 返回空或调用失败 → 直接发送原内容
            if generated is None:
                log(f"定时任务 AI 调用异常 [{task_id}]，发送原始内容兜底")
            else:
                log(f"定时任务 AI 生成为空 [{task_id}]，发送原始内容兜底")
            _send_text(task["target_type"], task["target_id"], prompt)
            _result_extra["detail"] = "AI 未生成，已发原始内容"

    log(f"定时任务执行: [{task_id}] {task['name']}({action_type}) → {task['target_type']}/{task['target_id']}")


def _report_scheduled_task(task: dict, error_msg: str | None, result_extra: dict | None = None, now_iso: str = ""):
    """向管理员汇报定时任务的执行结果（含成功/失败人数）。"""
    task_id = task["id"]
    name = task["name"]
    action_type = task.get("action_type", "static")
    if error_msg:
        summary = error_msg
    else:
        detail = (result_extra or {}).get("detail", "")
        summary = f"已完成（类型: {action_type}）" + (f"\n{detail}" if detail else "")
    for _master_id in _get_master_ids():
        try:
            _send_text("private", _master_id, f"✅ 定时任务完成: [{task_id}] {name}\n{summary}")
        except Exception:
            pass
    if task["task_type"] == "once":
        toggle_scheduled_task(task_id, False)


def _execute_scheduled_tasks_sync(now: datetime, today: str):
    """同步版本的定时任务执行器（用于 run_in_executor）。
    P3: 增强失败处理 — 分类、记录、重试、通知。
    """
    _execute_scheduled_tasks(now, today)


def _get_weather_text(city: str = "") -> str:
    """通过 wttr.in 获取实时天气，返回文字描述。"""
    if not city.strip():
        return "请先配置城市"
    weather = _get_weather(city)
    if weather:
        return weather
    return f"暂时无法获取{city}的天气数据"


def _describe_image(image_url: str) -> str | None:
    """通过智谱 GLM-4V-Flash 识别图片内容，返回中文描述。"""
    try:
        import httpx
        import base64

        # 下载图片并转 base64（NapCat 返回的是本地地址，智谱服务器无法直接访问）
        try:
            img_resp = httpx.get(image_url, timeout=10.0)
            img_resp.raise_for_status()
            img_bytes = img_resp.content
        except Exception as e:
            log(f"图片下载失败（将尝试直接URL）: {e}")
            # 回退：直接用 URL（如果恰好是公网可访问的）
            img_b64 = None
            img_mime = "image/jpeg"

        if img_bytes:
            img_b64 = base64.b64encode(img_bytes).decode("utf-8")
            # 检测 MIME 类型
            if img_bytes[:4] == b'\x89PNG':
                img_mime = "image/png"
            elif img_bytes[:2] == b'\xff\xd8':
                img_mime = "image/jpeg"
            elif img_bytes[:3] == b'GIF':
                img_mime = "image/gif"
            elif img_bytes[:4] == b'RIFF':
                img_mime = "image/webp"
            else:
                img_mime = "image/jpeg"

        # 构建图片 content
        if img_b64:
            img_content = {
                "type": "image_url",
                "image_url": {"url": f"data:{img_mime};base64,{img_b64}"},
            }
        else:
            img_content = {"type": "image_url", "image_url": {"url": image_url}}

        resp = httpx.post(
            f"{ZHIPU_BASE_URL}/chat/completions",
            json={
                "model": "glm-4v-flash",
                "max_tokens": 300,
                "messages": [{
                    "role": "user",
                    "content": [
                        img_content,
                        {"type": "text", "text": "请用中文简要描述这张图片的内容，包括图中的文字（如有），控制在3-5句话以内。"},
                    ],
                }],
            },
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {ZHIPU_API_KEY}",
            },
            timeout=20.0,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return content.strip() if content else None
    except Exception as e:
        log(f"图片识别失败: {e}")
        return None


async def _send_morning_weather():
    """给主人发早安+天气预报（保留向后兼容，实际由 _build_morning_digest 替代）"""
    masters = _get_master_ids()
    if not masters:
        return
    try:
        city = os.getenv("DEFAULT_CITY", "").strip()
        weather = _get_weather_text(city) if city else ""
        for qq in masters:
            from db import get_resolved_name
            name = get_resolved_name(qq) or qq
            msg = f"{name}早上好，新的一天开始了！" + (f"\n\n今天{city}天气：{weather}" if city else "")
            _send_text("private", qq, msg)
        log(f"早安天气已发送给 {len(masters)} 位主人")
    except Exception as e:
        log(f"早安天气失败: {e}")


def _get_today_tasks_summary() -> str:
    """获取今日提醒事项摘要（效果六：早安摘要含今日提醒）。"""
    from db import get_scheduled_tasks
    tasks = get_scheduled_tasks(enabled_only=True)
    if not tasks:
        return ""
    today = datetime.now().strftime("%Y-%m-%d")
    # 筛选今日会触发的任务：daily 类型 + once 类型且触发日期是今天
    today_tasks = []
    for t in tasks:
        task_type = t.get("task_type", "static")
        trigger_time = t.get("trigger_time", "")
        name = t.get("name", "")
        if task_type == "daily":
            today_tasks.append(f"  · {trigger_time} {name}")
        elif task_type == "once" and trigger_time[:10] == today:
            today_tasks.append(f"  · {trigger_time[11:]} {name}")
        elif task_type == "interval":
            interval = t.get("interval_hours", 0)
            today_tasks.append(f"  · 每{interval}小时 {name}")
    if not today_tasks:
        return ""
    return "📋 今日提醒：\n" + "\n".join(today_tasks[:10])


async def _build_morning_digest():
    """构建并发送完整的早安摘要（效果六）：天气 + 最近消息 + 今日提醒，合为一条消息。"""
    masters = _get_master_ids()
    if not masters:
        return
    try:
        city = os.getenv("DEFAULT_CITY", "").strip()
        weather = _get_weather_text(city) if city else ""
        # 最近消息摘要（取前 3 人 + 条数）
        recent_summary = _get_recent_messages_summary()
        # 今日提醒
        tasks_summary = _get_today_tasks_summary()
        for qq in masters:
            from db import get_resolved_name
            name = get_resolved_name(qq) or qq
            parts = [f"{name}早上好，新的一天开始了！"]
            if city:
                parts.extend(["", f"☀️ 今天{city}天气：{weather}"])
            if recent_summary:
                parts.append("")
                parts.append(recent_summary)
            if tasks_summary:
                parts.append("")
                parts.append(tasks_summary)
            msg = "\n".join(parts)
            _send_text("private", qq, msg)
        log(f"早安摘要已发送给 {len(masters)} 位主人")
    except Exception as e:
        log(f"早安摘要失败: {e}")


def _get_recent_messages_summary() -> str:
    """获取最近 8 小时的消息摘要（前 3 人 + 条数）。"""
    from db import query_recent_chats
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(hours=8)).isoformat()
    chats = [c for c in query_recent_chats(days=1, limit=100)
             if c.get("send_time", "") >= cutoff]
    if not chats:
        return ""
    # 按人聚合条数
    by_sender: dict[str, int] = {}
    for c in chats:
        by_sender[c["sender"]] = by_sender.get(c["sender"], 0) + 1
    # 按条数降序
    sorted_senders = sorted(by_sender.items(), key=lambda x: -x[1])
    lines = [f"💬 最近 8 小时 {len(by_sender)} 人发来 {len(chats)} 条消息："]
    for sender, count in sorted_senders[:3]:
        display = _resolve_speaker_name(sender) if sender != "0" else "系统"
        # 称呼优先：如果 display 就是 QQ 号（回退链没找到名字），只显示 QQ 号不重复
        if display == sender or display == sender[-4:]:
            lines.append(f"  · {sender}：{count}条")
        else:
            lines.append(f"  · {display}：{count}条")
    return "\n".join(lines)


def _health_check() -> list[str]:
    """启动前健康检查，返回警告列表。"""
    warnings = []
    if not DEEPSEEK_API_KEY:
        warnings.append("⚠ DEEPSEEK_API_KEY 未设置，AI 回复不可用")
    if not ZHIPU_API_KEY:
        warnings.append("⚠ ZHIPU_API_KEY 未设置，搜索和早安天气不可用")
    if not PERSONA_FILE.exists():
        warnings.append(f"⚠ 人物设定文件不存在: {PERSONA_FILE}")
    try:
        r = _api("get_login_info")
        if r.get("status") != "ok":
            warnings.append(f"⚠ NapCat 连接失败: {r.get('message', '未知')}")
    except Exception as e:
        warnings.append(f"⚠ NapCat HTTP 不可达 ({NAP_CAT_HTTP}): {e}")
    return warnings


def main():
    _init_file_logger(str(DATA_DIR))
    init_db()
    migrate_json_to_db(DATA_DIR)
    migrate_v2_consolidate_names(DATA_DIR)
    _migrate_master_list()
    _migrate_roles_json_to_data()
    import shutil
    _old_persona = Path(__file__).parent / "persona.txt"
    if _old_persona.exists() and not PERSONA_FILE.exists():
        shutil.copy2(_old_persona, PERSONA_FILE)
        log(f"已迁移 persona.txt 到桌面: {PERSONA_FILE}")
    warnings = _health_check()
    # Phase 1.4: 名字一致性检查
    from config import _check_name_consistency
    name_warnings = _check_name_consistency()
    if name_warnings:
        warnings.extend(name_warnings)
    # Phase 6: 数据完整性验证
    db_warnings = verify_data_integrity()
    if db_warnings:
        warnings.extend(db_warnings)
    cleanup_changes = _cleanup_inconsistent_data()
    # Phase 7: 启动时清理孤立的 user_profile（不在好友列表 + 无有效数据 + 无聊天记录）
    try:
        from config import _friend_cache
        if _friend_cache:
            from db import cleanup_orphan_profiles
            cleaned = cleanup_orphan_profiles(set(_friend_cache.keys()))
            if cleaned > 0:
                log(f"[CLEANUP] 移除 {cleaned} 条孤立 user_profile（非好友且无数据）")
    except Exception as e:
        log(f"[CLEANUP] 孤立清理失败: {e}")
    if warnings:
        for w in warnings:
            log(w)
    log(f"QQ Auto Bot v2 启动")
    # 启动时清理历史备注污染
    for f in sorted(DATA_DIR.glob("private_*.json")):
        qq = f.stem.split("_", 1)[1]
        try:
            n = _cleanup_person_notes(qq)
            if n > 0:
                log(f"清理备注 {qq}: 移除了 {n} 条")
        except Exception:
            pass
    # Phase 1.1: 安装全局异常钩子 + 优雅关闭
    SafetyNet.install_excepthook()
    atexit.register(SafetyNet.graceful_shutdown)

    # Phase 4.1: 统一配置
    from settings import settings as _settings
    log(f"配置加载: model={_settings.deepseek_model}, data_dir={_settings.data_dir}")

    # Phase 4.2: 注册连接池关闭钩子
    from resources import ConnectionPool
    SafetyNet.register_shutdown_handler(
        lambda: asyncio.get_event_loop().run_until_complete(ConnectionPool.close_all())
        if not asyncio.get_event_loop().is_running() else None)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    if os.environ.get("QQBOT_UI_ENABLED", "1") == "1":
        try:
            from ui_server import start_ui
            _ui_server = start_ui()
            atexit.register(_ui_server.server_close)
            log(f"管理界面已启动: http://127.0.0.1:{_ui_server.server_port}")
        except Exception as exc:
            log(f"管理界面启动失败，机器人继续运行: {type(exc).__name__}: {exc}")
    loop.create_task(_scheduler())
    loop.run_until_complete(ws_main())


if __name__ == "__main__":
    main()
