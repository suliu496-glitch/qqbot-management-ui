#!/usr/bin/env python3
"""QQ Bot 游戏引擎 — 6 个游戏：猜数字、数字炸弹、谁是卧底、脑筋急转弯、你画我猜、真心话大冒险"""
import time

from messaging import log, _send_text
from config import _resolve_speaker_name, _get_master_ids

# ==================== 游戏引擎 ====================

_game_sessions: dict[str, dict] = {}

# 脑筋急转弯题库
_RIDDLES = [
    {"q": "什么东西越洗越脏？", "a": "水"},
    {"q": "什么东西只能加不能减？", "a": "年龄"},
    {"q": "什么人一年只上一天班？", "a": "圣诞老人"},
    {"q": "什么东西有口却不能说话？", "a": "瓶子"},
    {"q": "什么东西你拿走一个，反而会多出一个？", "a": "镜子"},
    {"q": "什么球不能拍？", "a": "地球"},
    {"q": "什么东西破了比不破好？", "a": "纪录"},
    {"q": "什么东西越多人用越干净？", "a": "路"},
    {"q": "什么样的山不能爬？", "a": "伪山（衬衫）"},
    {"q": "世界上最小的岛是什么？", "a": "安全岛"},
    {"q": "什么书从来不单独卖？", "a": "说明书"},
    {"q": "什么东西越大越没用？", "a": "窟窿"},
    {"q": "什么事你明明没做过，却要受罚？", "a": "做作业（你没做作业要受罚）"},
    {"q": "什么东西往前走的越快，越看不见？", "a": "时间"},
    {"q": "什么花只能看不能摘？", "a": "雪花"},
]

# 你画我猜词库
_WORD_GUESS_WORDS = [
    "熊猫", "西瓜", "自行车", "眼镜", "火锅", "跳舞", "游泳", "冰箱",
    "彩虹", "恐龙", "键盘", "冰淇淋", "枕头", "跑步", "超市", "风筝",
    "牙刷", "向日葵", "闹钟", "雨伞", "沙发", "拍照", "炒饭", "气球",
]

# 真心话题库
_TRUTH_QUESTIONS = [
    "说出你最近一次尴尬的经历。",
    "如果给你一次穿越的机会，你想去哪个朝代？",
    "你最害怕的东西是什么？",
    "你做过最勇敢的一件事是什么？",
    "如果明天是世界末日，你今天会做什么？",
    "你最近哭是因为什么？",
    "你最想拥有什么超能力？",
    "说一件你瞒着所有人的事。",
]

# 大冒险题库
_DARES = [
    "模仿一种动物的叫声。",
    "用方言介绍自己。",
    "唱两句你最熟悉的歌。",
    "做一个你最喜欢的表情包动作并用文字描述。",
    "对你左边的人说一句土味情话。",
    "用三个emoji描述你此刻的心情。",
    "模仿机器人说三句话。",
    "对你右边的人说「你是最棒的」并给出理由。",
]


def _get_game_id(msg_type: str, group_id: str, chat_id: str) -> str:
    """获取游戏会话ID。群聊用group_id（多人共享），私聊用chat_id（单人）。"""
    if msg_type == "group" and group_id:
        return f"group_{group_id}"
    return chat_id


def _get_player_name(sender_id: str) -> str:
    """获取玩家显示名，用QQ号确保身份不混淆。管理员加★标记。"""
    name = _resolve_speaker_name(sender_id)
    if sender_id in _get_master_ids():
        return f"{name}★"
    return name


def _start_game(game_id: str, game_type: str, sender_id: str,
                chat_id: str, msg_type: str) -> str:
    """开始一个新游戏。"""
    if game_id in _game_sessions and _game_sessions[game_id].get("is_active"):
        cur = _game_sessions[game_id]["game"]
        return f"当前已有进行中的游戏「{cur}」。说「结束游戏」或「不玩了」来结束当前游戏，再开新的。"

    import random

    session = {
        "game": game_type,
        "players": {},
        "state": {},
        "started_by": sender_id,
        "started_at": time.time(),
        "chat_id": chat_id,
        "msg_type": msg_type,
        "is_active": True,
    }

    sender_name = _get_player_name(sender_id)

    if game_type == "guess_number":
        session["players"][sender_id] = {"name": sender_name, "score": 0}
        session["state"] = {"target": random.randint(1, 100), "attempts": 0, "min": 1, "max": 100}
        return f"🎮 猜数字游戏开始！我想了一个 1~100 之间的数字，猜猜看？\n（{sender_name}发起的游戏，说数字即可）"

    elif game_type == "number_bomb":
        session["players"][sender_id] = {"name": sender_name, "score": 0}
        session["state"] = {
            "min": 1, "max": 100, "bomb": random.randint(1, 100),
            "player_order": [sender_id], "current_index": 0
        }
        return f"💣 数字炸弹开始！范围 1~100，已设置炸弹数字。\n轮到 {sender_name}，请说一个数字。\n（其他人说「加入游戏」可以参与，轮流报数缩小范围，踩中炸弹的人输！）"

    elif game_type == "undercover":
        if msg_type != "group":
            return "「谁是卧底」是多人游戏，请在群聊中发起。私聊可以玩猜数字或脑筋急转弯。"
        session["players"][sender_id] = {"name": sender_name, "score": 0, "word": "", "role": ""}
        session["state"] = {"phase": "waiting", "round": 0, "word_pairs": [
            ("苹果", "梨子"), ("空调", "风扇"), ("高铁", "火车"),
            ("口红", "唇膏"), ("手机", "电脑"), ("雨伞", "雨衣"),
            ("包子", "饺子"), ("咖啡", "奶茶"), ("跑步", "走路"),
            ("钢琴", "吉他"), ("月亮", "太阳"), ("医生", "护士"),
        ]}
        return f"🕵️ 谁是卧底准备开始！由 {sender_name} 发起。\n其他人说「加入游戏」参与（至少3人）。说「开始」或「发词」开始游戏。"

    elif game_type == "riddle":
        riddle = random.choice(_RIDDLES)
        session["players"][sender_id] = {"name": sender_name, "score": 0}
        session["state"] = {"riddle": riddle, "attempts": 0, "max_attempts": 3}
        return f"🧠 脑筋急转弯：{riddle['q']}\n（{sender_name}发起的游戏，说出你的答案即可，共{session['state']['max_attempts']}次机会）"

    elif game_type == "word_guess":
        word = random.choice(_WORD_GUESS_WORDS)
        session["players"][sender_id] = {"name": sender_name, "score": 0}
        session["state"] = {"word": word, "attempts": 0, "max_attempts": 5, "hint": f"共{len(word)}个字"}
        hint = ""
        if len(word) == 2:
            hint = f"提示：{word[0]}_"
        elif len(word) == 3:
            hint = f"提示：{word[0]}__"
        session["state"]["hint"] = hint if hint else f"共{len(word)}个字"
        return f"🎨 你画我猜（文字版）：请根据描述猜词！\n提示：{session['state']['hint']}\n（{sender_name}发起的游戏，说出你的答案即可，共{session['state']['max_attempts']}次机会）"

    elif game_type == "truth_dare":
        choice = random.choice(["truth", "dare"])
        if choice == "truth":
            q = random.choice(_TRUTH_QUESTIONS)
            session["players"][sender_id] = {"name": sender_name, "challenge": q, "type": "truth"}
            session["state"] = {}
            return f"🎲 真心话大冒险 — {sender_name} 抽到了【真心话】\n问题：{q}\n请 {sender_name} 回答！回答后说「继续」抽下一轮。"
        else:
            d = random.choice(_DARES)
            session["players"][sender_id] = {"name": sender_name, "challenge": d, "type": "dare"}
            session["state"] = {}
            return f"🎲 真心话大冒险 — {sender_name} 抽到了【大冒险】\n任务：{d}\n请 {sender_name} 执行！完成后说「继续」抽下一轮。"

    else:
        return f"未知游戏类型：{game_type}。可选：guess_number、number_bomb、undercover、riddle、word_guess、truth_dare"


def _handle_game_action(game_id: str, sender_id: str, action: str) -> str:
    """处理玩家的游戏操作。"""
    import random

    if game_id not in _game_sessions or not _game_sessions[game_id].get("is_active"):
        return "当前没有进行中的游戏。说「玩游戏」来开始一个。"

    session = _game_sessions[game_id]
    player_name = _get_player_name(sender_id)

    if action in ("结束游戏", "不玩了", "退出", "end", "exit"):
        return _end_game(game_id)

    # === 加入游戏（仅数字炸弹和卧底支持） ===
    if action in ("加入游戏", "参与", "join", "加入", "我来"):
        game = session["game"]
        if game not in ("number_bomb", "undercover"):
            return f"当前游戏「{game}」不需要额外加入，直接操作即可。"
        if sender_id in session["players"]:
            return f"{player_name}，你已经在游戏中了。"
        session["players"][sender_id] = {"name": player_name, "score": 0}
        if game == "number_bomb":
            session["state"].setdefault("player_order", []).append(sender_id)
        return f"✅ {player_name} 加入了游戏！当前玩家：{'、'.join([p['name'] for p in session['players'].values()])}"

    # === 卧底专属：发词/开始 ===
    if session["game"] == "undercover" and action in ("开始", "发词", "start"):
        if len(session["players"]) < 3:
            return f"至少需要3人才能开始。当前有{len(session['players'])}人。"
        pair = random.choice(session["state"]["word_pairs"])
        civilian_word, undercover_word = pair
        players = list(session["players"].keys())
        undercover = random.choice(players)
        for pid in players:
            session["players"][pid]["word"] = undercover_word if pid == undercover else civilian_word
            session["players"][pid]["role"] = "卧底" if pid == undercover else "平民"
        session["state"]["phase"] = "describe"
        session["state"]["round"] = 1
        session["state"]["voted_out"] = []
        session["state"]["describer_index"] = 0
        session["state"]["describer_order"] = list(players)
        random.shuffle(session["state"]["describer_order"])
        # 私聊发送词语
        for pid in players:
            word = session["players"][pid]["word"]
            role = session["players"][pid]["role"]
            _send_text("private", pid, f"🕵️ 谁是卧底 — 你的词语是：【{word}】\n你的角色：{role}")
        first_player = session["state"]["describer_order"][0]
        first_name = session["players"][first_player]["name"]
        return f"🕵️ 谁是卧底开始！共{len(players)}位玩家。词语已私聊发送给每个人。\n第一轮，请 {first_name} 描述你的词语（可以说一句话描述，不要直接说出词语）。"

    # === 卧底专属：投票 ===
    if session["game"] == "undercover" and action.startswith("投票"):
        target = action.replace("投票", "").replace("@", "").strip()
        if session["state"].get("phase") != "vote":
            return "现在不是投票阶段。等所有人描述完后才能投票。"
        return _undercover_vote(game_id, sender_id, target, player_name)

    if session["game"] == "undercover" and not action.startswith("投票"):
        if session["state"].get("phase") == "describe":
            return _undercover_describe(game_id, sender_id, action, player_name)
        if session["state"].get("phase") == "vote":
            return f"现在是投票阶段，请说「投票 @某人」来选择你认为是卧底的人。"

    # 分派到各个游戏
    game = session["game"]
    if game == "guess_number":
        return _play_guess_number(game_id, sender_id, action, player_name)
    elif game == "number_bomb":
        return _play_number_bomb(game_id, sender_id, action, player_name)
    elif game == "riddle":
        return _play_riddle(game_id, sender_id, action, player_name)
    elif game == "word_guess":
        return _play_word_guess(game_id, sender_id, action, player_name)
    elif game == "truth_dare":
        return _play_truth_dare(game_id, sender_id, action, player_name)
    elif game == "undercover":
        return f"在{_game_status_text(game_id)}\n描述阶段请直接发描述文字；投票阶段请说「投票 @某人」。"

    return f"未知的游戏：{game}"


def _end_game(game_id: str) -> str:
    """结束游戏并显示结果。"""
    if game_id not in _game_sessions or not _game_sessions[game_id].get("is_active"):
        return "当前没有进行中的游戏。"
    session = _game_sessions.pop(game_id)
    game = session["game"]

    players = session.get("players", {})
    scores = []
    for pid, pdata in players.items():
        scores.append(f"  {pdata['name']}：{pdata.get('score', 0)}分")

    result = f"🏁 游戏「{game}」已结束！\n"
    if scores:
        result += "成绩：\n" + "\n".join(scores)
    if game == "undercover":
        result += "\n\n游戏详情："
        for pid, pdata in players.items():
            result += f"\n  {pdata['name']} — 词语「{pdata.get('word', '?')}」，角色：{pdata.get('role', '?')}"
    return result


def _game_status_text(game_id: str) -> str:
    """返回游戏状态文本。"""
    if game_id not in _game_sessions or not _game_sessions[game_id].get("is_active"):
        return "当前没有进行中的游戏。"

    session = _game_sessions[game_id]
    game = session["game"]
    players = [p["name"] for p in session["players"].values()]
    state = session["state"]

    if game == "guess_number":
        return (f"🎮 猜数字：范围 {state['min']}~{state['max']}，"
                f"已猜{state['attempts']}次。\n玩家：{'、'.join(players)}")
    elif game == "number_bomb":
        idx = state.get("current_index", 0)
        order = state.get("player_order", [])
        current_player = session["players"].get(order[idx], {}).get("name", "?") if order and idx < len(order) else "?"
        return (f"💣 数字炸弹：范围 {state['min']}~{state['max']}。\n"
                f"玩家：{'、'.join(players)}，轮到 {current_player}")
    elif game == "undercover":
        phase = state.get("phase", "waiting")
        if phase == "waiting":
            return f"🕵️ 谁是卧底：准备阶段。当前玩家：{'、'.join(players)}。说「开始」或「发词」开始游戏。"
        elif phase == "describe":
            desc_order = state.get("describer_order", [])
            desc_idx = state.get("describer_index", 0)
            if desc_idx < len(desc_order):
                current = session["players"].get(desc_order[desc_idx], {}).get("name", "?")
                return f"🕵️ 谁是卧底：第{state.get('round', 1)}轮描述中。当前玩家：{'、'.join(players)}。轮到 {current} 描述。"
            else:
                return f"🕵️ 谁是卧底：第{state.get('round', 1)}轮描述完成。请投票选出卧底！说「投票 @某人」"
        elif phase == "vote":
            return f"🕵️ 谁是卧底：投票阶段。已出局：{', '.join(state.get('voted_out', []))}。请说「投票 @某人」"
        else:
            return f"🕵️ 谁是卧底：进行中"
    elif game == "riddle":
        riddle = state.get("riddle", {})
        return (f"🧠 脑筋急转弯：{riddle.get('q', '?')}\n"
                f"玩家：{'、'.join(players)}，已猜{state.get('attempts', 0)}/{state.get('max_attempts', 3)}次")
    elif game == "word_guess":
        return (f"🎨 你画我猜：{state.get('hint', '?')}\n"
                f"玩家：{'、'.join(players)}，已猜{state.get('attempts', 0)}/{state.get('max_attempts', 5)}次")
    elif game == "truth_dare":
        challenge = list(players) if players else []
        return f"🎲 真心话大冒险：{'、'.join(challenge)}进行中。说「继续」抽下一轮。"
    else:
        return f"未知游戏「{game}」进行中。玩家：{'、'.join(players)}"


# ==================== 各游戏实现 ====================

def _play_guess_number(game_id: str, sender_id: str, action: str, player_name: str) -> str:
    session = _game_sessions[game_id]
    state = session["state"]
    try:
        guess = int(action)
    except (ValueError, TypeError):
        return f"请输入一个数字（{state['min']}~{state['max']}）。"

    if sender_id not in session["players"]:
        session["players"][sender_id] = {"name": player_name, "score": 0}

    state["attempts"] += 1

    if guess < state["target"]:
        state["min"] = max(state["min"], guess + 1)
        return f"📈 太小了！范围 {state['min']}~{state['max']}（第{state['attempts']}次）"
    elif guess > state["target"]:
        state["max"] = min(state["max"], guess - 1)
        return f"📉 太大了！范围 {state['min']}~{state['max']}（第{state['attempts']}次）"
    else:
        score = max(100 - state["attempts"] * 10, 10)
        session["players"][sender_id]["score"] = session["players"][sender_id].get("score", 0) + score
        result = f"🎉 恭喜 {player_name} 猜对了！答案就是 {state['target']}，用了 {state['attempts']} 次（+{score}分）"
        session["is_active"] = False
        _game_sessions.pop(game_id, None)
        return result


def _play_number_bomb(game_id: str, sender_id: str, action: str, player_name: str) -> str:
    session = _game_sessions[game_id]
    state = session["state"]
    order = state["player_order"]
    idx = state["current_index"]

    if not order:
        return "游戏异常：无玩家。"

    current_player = order[idx]
    if sender_id != current_player:
        current_name = session["players"].get(current_player, {}).get("name", "?")
        return f"还没轮到你，现在轮到 {current_name}。"

    try:
        num = int(action)
    except (ValueError, TypeError):
        return f"请输入一个数字（{state['min']}~{state['max']}）。"

    if num <= state["min"] or num >= state["max"]:
        return f"请输入 {state['min']}~{state['max']} 之间的数字。"

    if num == state["bomb"]:
        result = f"💥 BOOM！{player_name} 踩中了炸弹数字 {state['bomb']}！游戏结束。"
        session["is_active"] = False
        _game_sessions.pop(game_id, None)
        return result
    elif num < state["bomb"]:
        state["min"] = num
    else:
        state["max"] = num

    state["current_index"] = (idx + 1) % len(order)
    next_player = session["players"].get(order[state["current_index"]], {}).get("name", "?")
    return f"🔢 {player_name} 报了 {num}。范围缩小到 {state['min']}~{state['max']}。轮到 {next_player}"


def _play_undercover(game_id: str, sender_id: str, action: str, player_name: str) -> str:
    """兜底：如果 _handle_game_action 没匹配到，就返回帮助。"""
    phase = _game_sessions[game_id]["state"].get("phase", "waiting")
    if phase == "waiting":
        return f"说「开始」或「发词」开始游戏。"
    elif phase == "describe":
        return f"请描述你的词语（说一句话描述，不要直接说出词语）。"
    elif phase == "vote":
        return f"现在是投票阶段，请说「投票 @某人」来选择你认为是卧底的人。"
    return "请按游戏提示操作。"


def _undercover_describe(game_id: str, sender_id: str, description: str, player_name: str) -> str:
    session = _game_sessions[game_id]
    state = session["state"]
    order = state["describer_order"]
    idx = state["describer_index"]

    if sender_id not in session["players"]:
        return "你不是本局玩家，无法描述。"

    current_describer = order[idx]
    if sender_id != current_describer:
        current_name = session["players"].get(current_describer, {}).get("name", "?")
        return f"还没轮到你描述，现在轮到 {current_name}。"

    state["describer_index"] = idx + 1
    descriptions = state.setdefault("descriptions", [])
    descriptions.append({"player": player_name, "desc": description})

    if state["describer_index"] >= len(order):
        state["phase"] = "vote"
        player_list = []
        for pid in order:
            name = session["players"].get(pid, {}).get("name", "?")
            player_list.append(f"@{name}")
        desc_lines = "\n".join([f"  {d['player']}：{d['desc']}" for d in descriptions])
        return (f"📋 第{state['round']}轮描述汇总：\n{desc_lines}\n\n"
                f"进入投票阶段！请说「投票 @某人」来投票。可选：{'、'.join(player_list)}")
    else:
        next_describer = order[state["describer_index"]]
        next_name = session["players"].get(next_describer, {}).get("name", "?")
        return f"✅ {player_name} 的描述已记录。下一个轮到 {next_name}。"


def _undercover_vote(game_id: str, voter_id: str, target_name: str, voter_name: str) -> str:
    session = _game_sessions[game_id]
    state = session["state"]
    players = session["players"]

    # 找到被投者
    voted_out = None
    for pid, pdata in players.items():
        if pdata.get("name", "") == target_name or target_name in pdata.get("name", ""):
            voted_out = pid
            break
    if not voted_out:
        # 尝试纯名字匹配
        for pid, pdata in players.items():
            if target_name in pdata.get("name", ""):
                voted_out = pid
                break

    if not voted_out:
        return f"找不到玩家「{target_name}」。请用 @玩家名 格式投票。"

    votes = state.setdefault("current_votes", {})
    votes[voter_id] = voted_out
    voted_name = players.get(voted_out, {}).get("name", voted_out)
    result = f"🗳️ {voter_name} 投票给 {voted_name}。已投票：{len(votes)}/{len(players)}人。"

    if len(votes) >= len(players):
        # 统计票数
        tally = {}
        for v_id, t_id in votes.items():
            tally[t_id] = tally.get(t_id, 0) + 1
        max_votes = max(tally.values())
        top_ids = [t_id for t_id, cnt in tally.items() if cnt == max_votes]
        top_names = [players.get(t_id, {}).get("name", t_id) for t_id in top_ids]

        if len(top_ids) == 1:
            eliminated = top_ids[0]
            eliminated_name = top_names[0]
            eliminated_role = players[eliminated].get("role", "?")
            state.setdefault("voted_out", []).append(eliminated_name)
            state["current_votes"] = {}

            # 检查游戏结束条件
            remaining = [pid for pid in players if pid not in state.get("voted_out_by_id", [])]
            undercover_remaining = sum(1 for pid in remaining if players[pid].get("role") == "卧底")
            civilian_remaining = sum(1 for pid in remaining if players[pid].get("role") == "平民")

            out_lines = [f"🗳️ 投票结果：{eliminated_name} 被票选出局（{max_votes}票）。角色：{eliminated_role}"]
            state["voted_out_by_id"] = state.get("voted_out_by_id", [])
            state["voted_out_by_id"].append(eliminated)

            if undercover_remaining == 0:
                session["is_active"] = False
                out_lines.append(f"🎉 所有卧底已找出！平民获胜！")
                _game_sessions.pop(game_id, None)
            elif undercover_remaining >= civilian_remaining:
                session["is_active"] = False
                out_lines.append(f"🕵️ 卧底存活到最后！卧底「{players.get([pid for pid in players if players[pid].get('role') == '卧底'][0], {}).get('name', '?')}」获胜！")
                _game_sessions.pop(game_id, None)
            else:
                # 继续下一轮
                state["round"] += 1
                state["phase"] = "describe"
                state["describer_index"] = 0
                state["descriptions"] = []
                order = [pid for pid in state["describer_order"] if pid != eliminated]
                state["describer_order"] = order
                random.shuffle(order)
                first_player = session["players"].get(order[0], {}).get("name", "?")
                out_lines.append(f"继续第{state['round']}轮。请 {first_player} 开始描述。")
            return "\n".join(out_lines)

        else:
            state["current_votes"] = {}
            return f"🗳️ 票数相同（{max_votes}票）：{'、'.join(top_names)}。请重新投票。说「投票 @某人」"

    return result


def _play_riddle(game_id: str, sender_id: str, action: str, player_name: str) -> str:
    session = _game_sessions[game_id]
    state = session["state"]
    riddle = state["riddle"]
    answer = riddle["a"]

    if sender_id not in session["players"]:
        session["players"][sender_id] = {"name": player_name, "score": 0}

    state["attempts"] += 1

    if action.strip().lower() in ("提示", "hint", "给个提示"):
        if state["attempts"] >= state["max_attempts"] - 1:
            return f"💡 提示：答案是 {len(answer)} 个字，以「{answer[0]}」开头。"
        return "再猜猜看，还有机会的时候给你提示。"

    # 模糊匹配答案
    if action.strip() == answer or answer in action.strip():
        score = max(50 - state["attempts"] * 10, 10)
        session["players"][sender_id]["score"] = session["players"][sender_id].get("score", 0) + score
        session["is_active"] = False
        _game_sessions.pop(game_id, None)
        return f"🎉 正确！{player_name} 答对了！答案就是「{answer}」（+{score}分）"
    elif state["attempts"] >= state["max_attempts"]:
        session["is_active"] = False
        _game_sessions.pop(game_id, None)
        return f"😅 机会用完了！正确答案是「{answer}」。"
    else:
        left = state["max_attempts"] - state["attempts"]
        return f"❌ 不对哦～还有 {left} 次机会。再想想！（说「提示」可以给提示）"


def _play_word_guess(game_id: str, sender_id: str, action: str, player_name: str) -> str:
    session = _game_sessions[game_id]
    state = session["state"]
    word = state["word"]

    if sender_id not in session["players"]:
        session["players"][sender_id] = {"name": player_name, "score": 0}

    state["attempts"] += 1

    if action.strip().lower() in ("提示", "hint", "给个提示"):
        hint = ""
        if len(word) >= 3:
            hint = f"提示：第一个字是「{word[0]}」，最后一个字是「{word[-1]}」"
        else:
            hint = f"提示：第一个字是「{word[0]}」"
        return f"💡 {hint}"

    if action.strip() == word:
        score = max(60 - state["attempts"] * 10, 10)
        session["players"][sender_id]["score"] = session["players"][sender_id].get("score", 0) + score
        session["is_active"] = False
        _game_sessions.pop(game_id, None)
        return f"🎉 正确！{player_name} 答对了！答案是「{word}」（+{score}分）"
    elif state["attempts"] >= state["max_attempts"]:
        session["is_active"] = False
        _game_sessions.pop(game_id, None)
        return f"😅 机会用完了！正确答案是「{word}」。"
    else:
        left = state["max_attempts"] - state["attempts"]
        hint = ""
        for i, ch in enumerate(word):
            if i == 0:
                hint += ch
            else:
                hint += "_"
        return f"❌ 不对哦～还有 {left} 次机会。{hint}（说「提示」可以给更多提示）"


def _play_truth_dare(game_id: str, sender_id: str, action: str, player_name: str) -> str:
    import random

    session = _game_sessions[game_id]

    if sender_id not in session["players"]:
        session["players"][sender_id] = {"name": player_name, "challenge": "", "type": ""}

    if action in ("继续", "再来", "下一轮", "next"):
        choice = random.choice(["truth", "dare"])
        if choice == "truth":
            q = random.choice(_TRUTH_QUESTIONS)
            session["players"][sender_id] = {"name": player_name, "challenge": q, "type": "truth"}
            return f"🎲 {player_name} 抽到了【真心话】\n问题：{q}\n请 {player_name} 回答！回答后说「继续」抽下一轮。"
        else:
            d = random.choice(_DARES)
            session["players"][sender_id] = {"name": player_name, "challenge": d, "type": "dare"}
            return f"🎲 {player_name} 抽到了【大冒险】\n任务：{d}\n请 {player_name} 执行！完成后说「继续」抽下一轮。"

    elif action in ("结束", "不玩了", "退出"):
        session["is_active"] = False
        _game_sessions.pop(game_id, None)
        return "🎲 真心话大冒险结束。下次再玩！"

    return f"说「继续」抽下一轮，说「结束」退出游戏。"
