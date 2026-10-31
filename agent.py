#!/usr/bin/env python3
"""
agent.py — 智能 Agent 核心：任务规划与有状态执行引擎

替代 tools.py `_call_deepseek_with_tools` 中的盲目 10 轮循环：
  1. Agent.plan()  → 将自然语言意图拆解为有依赖关系的步骤列表
  2. Agent.execute() → 按依赖图执行，支持中间结果缓存、步骤回滚

设计原则：
- 无状态实例（classmethod 为主），跨步骤状态存储在 TaskPlan.context
- 不引入重度依赖（无 LangChain/Celery），仅用 DeepSeek API 做规划
- 与现有 `_call_deepseek_with_tools` 透明集成：签名不变，内部实现替换
"""
import json
import re
import time
import uuid
import asyncio
from typing import Any, Optional

try:
    from messaging import log as _log
except Exception:
    def _log(msg):
        print(f"[qq-bot] {msg}", flush=True)


class TaskPlan:
    """多步骤任务规划"""

    def __init__(self, goal: str, steps: list[dict], context: dict):
        self.goal = goal
        self.steps = steps  # [{"step_id", "tool", "args", "depends_on", "fallback"}]
        self.context = context  # 跨步骤上下文缓存（含中间结果）

    def get_step(self, step_id) -> Optional[dict]:
        for s in self.steps:
            if str(s.get("step_id")) == str(step_id):
                return s
        return None

    def get_completed_result(self, step_id) -> Any:
        return self.context.get(f"result_{step_id}")


class Agent:
    """有状态任务规划与执行引擎"""

    # 最大执行步骤数，防止无限循环
    MAX_STEPS = 8
    # 规划用系统提示
    PLAN_SYSTEM = (
        "你是一个任务规划器。将用户的自然语言意图拆解为可执行的工具调用步骤。\n"
        "规则：\n"
        "1. 每个步骤只能调用一个工具\n"
        "2. 如果步骤 B 需要步骤 A 的结果，B 的 depends_on 填 A 的 step_id\n"
        "3. 工具参数可以使用 {{step_id.result}} 引用之前步骤的返回值\n"
        "4. 步骤数不超过 8 步\n"
        "5. 只输出 JSON 数组，不要解释\n"
        "6. ★ 角色词不是人名：「管理」「主人」「管理员」指有权限的管理员角色，不是某个人的名字。如果用户说「发给管理」「告诉主人」，应解析为发给管理员（用管理员 QQ 作为 target），绝不能当作一个叫「联系人甲」的人去查联系人列表。\n"
        "7. ★ 闲聊不规划：如果消息是纯聊天（问候、在吗、好的、感叹、没有操作意图），直接输出空数组 []，不要给闲聊规划工具调用。判断标准：消息中不含任何操作动词（发、改、设、查、删、给、帮、创建等）。\n"
        "8. ★ 先查后动：如果目标人不确定，先用 search_conversations 或 get_person_info 确认身份，再执行操作。\n"
        "9. ★ 打包文件场景：用户说「打包成文件」「导出文件」「整理成文档」时，必须先获取内容再调 send_file。"
        "如果是要打包当前对话，先用 get_person_conversation（target 填用户 QQ）获取聊天记录作为 content；"
        "如果是要打包某个话题，先用 search_conversations 获取相关内容。绝不能直接调 send_file({}) 空参数。\n"
        "10. ★ 查看详细记录：用户说「显示详细聊天记录」「每一条内容」「详细信息」时，"
        "调用 get_person_conversation(target=目标人, count=10+) 展示具体消息内容，不要只回复摘要数字。"
        "11. ★ 严格按基本信息称呼：用户要求改称呼时必须用 set_nickname 工具写入，绝不能口头答应「好的我叫你XX」。"
        "设置后立即生效，以后必须用新称呼叫对方。如果用户没设置称呼，优先引导用户完善基本信息："
        "「对了，我怎么称呼你呀？告诉我你的名字或想要的称呼~」+ 用 set_nickname 记录。"
        "称呼、城市、关系都只能有一个值，后来的替换之前的。"
        "12. ★ 模糊匹配必须确认：当通过名字/称呼匹配用户时，如果匹配结果不是 100% 精确（confidence < 1.0），"
        "必须先向用户确认「你指的是 XXX(QQ:xxx) 对吗？」等待用户确认后再执行工具。"
        "同音字（如 翔/祥/湘）按模糊匹配处理，必须确认。"
        "13. ★ 静默/解除静默必须调工具：用户说「别找了我」「静默」「mute」时必须调 set_mute；"
        "用户说「解除静默」「取消静默」「别静默了」时必须调 cancel_mute。绝不能口头答应而不调工具。"
        "14. ★ 发消息优先单步：用户说「给XX发消息」「告诉XX」时，尽量用单步 send_message(target=名字或QQ, message=内容)，"
        "不要把名字解析拆成多步。target 直接使用用户说的名字（如 target=联系人甲），工具内部会自动解析。"
        "如果必须多步，tool result 引用格式为 {{N.result}}（不含字段名如 .target/.nickname）。"
        "设定定时任务时不立刻发：只调 add_scheduled_task，不同时调 send_message。"
        "15. ★ 模糊匹配等待确认：当 send_message 返回「🔍 模糊匹配...请确认」时，"
        "必须将该提示转达给用户并等待确认。用户确认后，重新调 send_message 并加 confirmed=True。"
        "16. ★ 定时任务类型选择：所有定时任务统一使用 action_type=ai_generate，系统会自动判断："
        "target_id=\"all\" → 给每个好友根据其称呼、标签、画像 AI 个性化生成消息并私发"
        "其他 target_id → AI 根据 content 指令生成消息后单发"
        "「问好」「祝福」「打招呼」「天气」「提醒」等所有类型的发送都用同一个 action_type"
        "任务名称要是可读的描述（如「早安问好」「午间天气提醒」），绝不能是「new_task」「test」这种占位名。"
        "17. ★ 数据修改必须调工具：任何改变数据的操作（设置称呼/地址/关系/备注、创建/删除任务、发消息、改城市等），"
        "必须规划对应的工具调用步骤，绝不能只回复文字（如「好的！已设置」）而不调工具。说话不算数，动手才算数。"
        "18. ★ 工具分层选择：用户意图明确时优先用「直接工具」而非「查询+再操作」两步："
        "  ·「设称呼」→ 直接 set_nickname（不要先 get_person_info 再 set）"
        "  ·「发消息给XX」→ 直接 send_message(target=XX)"
        "  ·「改城市」→ 直接 set_person_address(target=XX, city=YYY)"
        "  ·「查天气」→ 直接 get_weather(city=XX)"
        "只有意图模糊时才先查后设（如「那个谁的城市是啥」→ get_person_info 先确认）。"
        "19. ★ 定时任务管理：用户说「删除任务」「停用任务」「查看定时」时，先调 list_my_tasks 列出所有任务及其 task_id，"
    "用户指定编号或名称后再调 delete_scheduled_task(task_id=...) 或 toggle_scheduled_task(task_id=...)。"
    "task_id 是 list_my_tasks 返回的 `[task_xxx]` 格式的完整ID。"
    "20. ★ 禁止编造系统信息：绝不能声称某人「不是好友」或「好友状态为否」——系统没有此功能，所有联系人均可正常发消息。"
    "能不能发消息的唯一判断标准是对方是否在联系人数据库中，而不是什么「好友状态」。"
    "21. ★ 定时任务interval能力：interval_hours 可以是任何正小数，如 0.016=1分钟、0.5=30分钟、1=1小时。"
    "用户说「每X分钟发消息」→ add_scheduled_task(task_type=interval, interval_hours=X/60)。不要编造系统限制。"
    "如果用户问的是「谁找我」，先调 check_inbox 获取列表，再对每个人调 get_person_conversation 展示详情。\n\n"
        "输出格式：\n"
        "[{\"step_id\": 1, \"tool\": \"工具名\", \"args\": {\"k\": \"v\"}, "
        "\"description\": \"说明\", \"depends_on\": [], \"fallback\": \"失败时的备选\"}]"
    )

    @classmethod
    async def plan(cls, goal: str, available_tools: list[str], context: dict,
                   tool_specs: list[dict] | None = None) -> Optional[TaskPlan]:
        """调用 LLM 生成 JSON 步骤计划。失败返回 None → 回退到简单循环模式。

        tool_specs: 可选，完整的工具定义列表（含 name/description/parameters），
                    传入后 planner 会看到每个工具的参数要求，生成更完整的计划。
        """
        try:
            # 自然对话 / 图片描述 → 不需要规划工具，返回 None 走自然聊天
            _natural_prefixes = ("用户发了一张图片。", "用户发了一个文件。", "用户发了一段")
            if any(goal.startswith(p) for p in _natural_prefixes):
                _log(f"[AGENT] natural content, skip planning: {goal[:40]}")
                return None
            # 构建工具描述：优先使用完整 spec（含参数），其次只用名字
            if tool_specs:
                _lines = []
                for t in tool_specs:
                    fn = t.get("function", {})
                    name = fn.get("name", "")
                    desc = fn.get("description", "")
                    params = fn.get("parameters", {})
                    required = params.get("required", [])
                    _lines.append(f"- {name}：{desc}")
                    if required:
                        _lines.append(f"  必需参数：{', '.join(required)}")
                tools_desc = "\n".join(_lines)
            else:
                tools_desc = ", ".join(available_tools)
            # 修复：注入最近对话历史，让 planner 能理解"这个""那个"等指代语句
            history = context.get("_recent_history", "")
            history_part = f"\n最近对话：\n{history}\n" if history else ""
            user_prompt = (
                f"可用工具（含参数要求）：\n{tools_desc}\n"
                f"当前上下文：{json.dumps(context, ensure_ascii=False)[:500]}\n"
                f"{history_part}"
                f"用户最新意图：{goal}\n\n请输出执行计划（每个步骤的 args 必须包含所有必需参数）："
            )
            raw = await cls._call_llm(cls.PLAN_SYSTEM, user_prompt, max_tokens=1000)
            if not raw:
                return None
            steps = cls._parse_plan_json(raw)
            if not steps:
                return None
            _log(f"[AGENT] planned {len(steps)} steps for: {goal[:50]}")
            return TaskPlan(goal=goal, steps=steps, context=context)
        except Exception as e:
            _log(f"[AGENT] plan failed: {e}")
            return None

    @classmethod
    async def execute(cls, plan: TaskPlan) -> str:
        """按依赖图执行步骤。返回最终结果文本。"""
        completed = set()
        results = {}
        max_iterations = len(plan.steps) * 2  # 防止死循环

        iteration = 0
        while len(completed) < len(plan.steps) and iteration < max_iterations:
            iteration += 1
            progressed = False
            for step in plan.steps:
                sid = str(step.get("step_id"))
                if sid in completed:
                    continue
                # 检查依赖是否完成
                deps = step.get("depends_on", [])
                deps_ready = all(str(d) in completed for d in deps)
                if not deps_ready:
                    continue
                # 解析参数中的 {{step.result}} 引用
                args = cls._resolve_args(step.get("args", {}), results)
                try:
                    result = await cls._execute_single_step(step, args, plan.context)
                    results[sid] = result
                    plan.context[f"result_{sid}"] = result
                    completed.add(sid)
                    progressed = True
                except Exception as e:
                    _log(f"[AGENT] step {sid} failed: {e}")
                    fallback = step.get("fallback")
                    if fallback:
                        results[sid] = fallback
                        plan.context[f"result_{sid}"] = fallback
                    else:
                        results[sid] = f"步骤 {sid} 失败: {e}"
                    completed.add(sid)
                    progressed = True
            if not progressed:
                # 存在循环依赖或不可执行步骤 → 跳出
                break

        # 用最后一步结果或汇总返回
        if plan.steps:
            last = str(plan.steps[-1].get("step_id"))
            return str(results.get(last, "执行完成"))
        return "执行完成"

    @classmethod
    def step_rollback(cls, plan: TaskPlan, step_id: str) -> None:
        """单步骤回滚：清除结果，标记为未完成"""
        plan.context.pop(f"result_{step_id}", None)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    @classmethod
    async def _execute_single_step(cls, step: dict, args: dict, ctx: dict) -> str:
        """执行单个工具调用"""
        from tools import _execute_tool
        tool_name = step.get("tool", "")
        inner_ctx = dict(ctx)
        inner_ctx.setdefault("tool_call_history", [])
        return _execute_tool(tool_name, args, inner_ctx)

    @classmethod
    def _resolve_args(cls, args: dict, results: dict) -> dict:
        """解析 args 中的 {{step_id.result}} 占位符（兼容 {{N.result.field}} 格式）"""
        resolved = {}
        for k, v in args.items():
            if isinstance(v, str) and "{{" in v and "}}" in v:
                # 替换 {{N.result}} 或 {{N.result.field}} → 都返回 results[N] 的完整字符串
                def repl(m):
                    sid = m.group(1)
                    r = results.get(str(sid), "")
                    return str(r)[:500] if r else ""
                v = re.sub(r"\{\{(\d+)\.result[^}]*\}\}", repl, v)
            resolved[k] = v
        return resolved

    @classmethod
    def _parse_plan_json(cls, raw: str) -> Optional[list[dict]]:
        """从 LLM 输出中提取 JSON 计划"""
        if not raw:
            return None
        # 先找 ```json ... ``` 代码块
        m = re.search(r"```(?:json)?\s*(\[[\s\S]*?\])\s*```", raw)
        if m:
            raw = m.group(1)
        else:
            # 找第一个 [ ... ]
            m = re.search(r"\[[\s\S]*\]", raw)
            if m:
                raw = m.group(0)
        try:
            steps = json.loads(raw)
            if isinstance(steps, list) and steps:
                return steps
        except json.JSONDecodeError:
            pass
        return None

    @classmethod
    async def _call_llm(cls, system: str, user: str, max_tokens: int = 800) -> Optional[str]:
        """调用 DeepSeek AI"""
        try:
            from tools import _call_deepseek
            # _call_deepseek 是同步函数 → 在线程中运行
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None, lambda: _call_deepseek(system, user, max_tokens))
        except Exception as e:
            _log(f"[AGENT] LLM call failed: {e}")
            return None
