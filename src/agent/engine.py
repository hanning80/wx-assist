"""ReAct Loop engine — the core reasoning + acting cycle.

Usage:
    engine = AgentEngine(summarizer, tool_executor)
    reply = engine.run("帮我查下系统状态")
"""

import json
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Memory ──────────────────────────────────────────────────────────
AGENT_MEMORY_DB = Path("data/agent_memory.db")
MEMORY_HISTORY_LIMIT = 10   # 短期记忆达到此阈值后触发总结
MEMORY_LOAD_COUNT = 3       # 每次注入 system prompt 的长期记忆条数

# ── System prompt for the Agent ────────────────────────────────────

AGENT_SYSTEM_PROMPT = """\
你是微信助手 Agent，运行在用户的个人微信机器人上。你不仅能聊天，还能调用工具完成用户的需求。

## 你的身份
- 用户通过微信私聊与你对话
- 你可以调用系统工具获取信息或执行操作

## 可用工具
{tool_descriptions}

## 工作方式
你运行在 ReAct（推理 + 行动）循环中：

1. **Thought** — 分析用户想要什么，需要哪些信息
2. **Action** — 选择最合适的工具来获取信息或执行操作
3. **Observation** — 查看工具返回的结果
4. **Repeat** — 重复直到任务完成，然后回复用户

## 安全规则
- 执行写操作（生成摘要、配置预警、配置定时任务等）之前，必须先调用 confirm_action 获取用户确认
- 用户确认后，才能继续执行写操作工具
- 如果用户取消，回复用户操作已取消
- 查询类操作（查状态、搜索记录、查看配置）不需要确认

## 规则
- 只在确实需要信息时才调用工具，不要过度使用
- 调用工具时必须使用正确的参数
- 如果用户指令不清晰，直接询问澄清
- 不要编造工具返回的结果
- 用户问“在跑的提醒/我有什么提醒/现在有什么定时任务”时，默认指生活提醒，用 list_journal_reminders；只有明确说“采集与维护/群摘要/榜单/自动推送”才用 list_crons。回答时把前者叫“生活提醒”，不要叫“定时任务”；采集与维护类才叫“采集与维护任务”。
- 回答涉及用户以前记过、收藏过、聊过的内容时，检索顺序固定为：日记 → 收藏 → 聊天记录；多个渠道都有结果时，播报也按这个顺序，不要把聊天记录放到前面。
- 用户要改在跑的提醒（每天几次、一共几天、几点）时，先 list_journal_reminders 确认是哪条，再 update_journal_reminder；不要新建另一套定时任务，也不要说“系统不能定时提醒”。
- 用户要新建生活提醒时，先把原话交给 create_journal_reminder，它会返回确认问句；必须等用户明确回“确认/建吧”再调 confirm_journal_reminder(accepted=true)。用户说取消就传 false。
- 用户要删除生活提醒时，先 list_journal_reminders 找到编号或名字，再调 delete_journal_reminder；等用户确认后再调 confirm_journal_reminder。不要跳过确认直接删。
- 用户问“把某某的位置发给我/某某在哪/导航到某某的位置/某人标签里的地址”时，先调 find_saved_location；它固定按日记、收藏标签、聊天记录的顺序查，并把结果按同样顺序给用户。查到就把地点、时间、原文或高德链接直接给用户，并注明这是日记、收藏或聊天里存的，不是实时定位；不要因为聊天缓存没显示就说没有。
- 任务完成后，用自然语言回复用户

## 回复风格
- 只陈述本轮已经发生的事实：解开了、入库了、上云了、没解开、只记了来源。
- 不要写“以后同类……不会再……”“保证以后……”“永远不会……”这类承诺；以后的事以后再说。
- 一句话能说清就一句话，不要自我表扬或加多余保证。

## 视频号 / 公众号分享
- 用户要“看懂、记录、收藏”分享内容时，读分享里可见的标题、作者、文案，帮他记下来；不要一听“解析”就拒绝。
- 用户要“下载地址、去水印、批量抓取”时，说明版权和平台限制，不给绕过方法。
- 分不清时先问一句：“你是要读内容，还是要下载文件？”
- 本系统已有日记入口，用户说“记进日记/收藏”就是入库，不要再反问存在哪里。

## 停止条件（必须遵守）

你的行动轮数上限为 {max_steps} 步，每一步都在消耗资源，不要浪费。

**⚡ 强制规则：连续 3 次调用同一个工具仍未取得有效进展时，必须立即停止。**
所谓有效进展是指：返回了新的、有用的、可以推进任务的数据。
如果返回空、返回无关内容、反复报错、或每次都是差不多的结果，都视为无进展。

此时：
1. **禁止**再调用任何工具
2. **必须**直接回复用户当前情况
3. 即使用户的问题没解决，也要说"我没找到，请提供更精确的描述"
4. 不要换参数重试——换个关键词搜还是同一个工具，算连续调用
"""


class AgentEngine:
    """ReAct Loop engine with confirm_action state machine.

    All operations are synchronous.  No async/await needed.
    Runs in iLink receiver's background thread.

    Args:
        summarizer: AbstractSummarizer instance with agent_chat() support.
        tool_executor: ToolExecutor instance.
        max_steps: Maximum ReAct iterations before giving up.
        progress_callback: Optional. Called with (step, max_steps, tool_names, reasoning) after
                          each tool-calling step, so callers can push progress
                          to the user via iLink or other channels.
    """

    def __init__(self, summarizer, tool_executor,
                 max_steps: int = 8,
                 progress_callback=None):
        self._llm = summarizer
        self._tools = tool_executor
        self._max_steps = max_steps
        self._progress_callback = progress_callback
        # Per-conversation state: IM platforms must not share user context.
        self._state_lock = threading.RLock()
        self._conversation_locks: dict[str, threading.RLock] = {}
        self._histories: dict[str, list[tuple[str, str]]] = {"default": []}
        self._pending_confirms: dict[str, dict] = {}
        self._bypass_confirms: set[str] = set()
        self._init_memory_db()

    # ── Public API ─────────────────────────────────────────────────

    def run(self, user_message: str, *, source_platform: str = "wechat",
            source_target: str | None = None,
            conversation_key: str = "default") -> str:
        """Run the ReAct loop and return the final reply.

        If there is a pending confirmation from a previous run(),
        handles the user's response first.

        Args:
            user_message: The user's message content.

        Returns:
            Final reply text to send back to the user.
        """

        key = conversation_key or "default"
        with self._state_lock:
            conversation_lock = self._conversation_locks.setdefault(key, threading.RLock())
        with conversation_lock:
            return self._run_conversation(
                user_message, source_platform=source_platform,
                source_target=source_target, conversation_key=key,
            )

    def _run_conversation(self, user_message: str, *, source_platform: str,
                          source_target: str | None, conversation_key: str) -> str:
        key = conversation_key or "default"

        # “以后回答引用原话”是长期规则，不是普通聊天；先落库再回执。
        # 不放进日记，也不让模型用一句“好的”糊弄过去。
        try:
            from src.journal.answer_rule import maybe_handle
            rule_reply = maybe_handle(user_message)
            if rule_reply:
                return rule_reply
        except Exception as e:  # noqa: BLE001
            logger.warning("[Agent] 回答规则识别失败: %s", e)

        with self._state_lock:
            pending = self._pending_confirms.get(key)

        # ── Phase 1: Handle pending confirmation ─────────────────────
        if pending is not None:
            logger.info("[Agent] Pending confirm exists, routing to _handle_confirm_response")
            messages = [{"role": "user", "content": user_message}]
            return self._handle_confirm_response(
                user_message,
                messages,
                source_platform=source_platform,
                source_target=source_target,
                conversation_key=conversation_key,
            )

        # ── Phase 2: Build messages with memory context ──────────────
        messages: list[dict] = []

        with self._state_lock:
            history = self._histories.setdefault(key, [])
            history_snapshot = list(history[-MEMORY_HISTORY_LIMIT:])
        # Inject short-term history (last N exchanges)
        for user_msg, agent_reply in history_snapshot:
            messages.append({"role": "user", "content": user_msg})
            messages.append({"role": "assistant", "content": agent_reply})

        messages.append({"role": "user", "content": user_message})

        # ── Phase 3: ReAct loop ──────────────────────────────────────
        logger.info("[Agent] run() — user_message='%s', history=%d entries",
                    user_message[:80], len(history_snapshot))

        system = self._build_system_prompt(conversation_key=key)
        system += self._journal_memory_block(user_message)

        logger.info("[Agent] Entering _react_loop — tools=%s",
                    [t["function"]["name"] for t in self._tools.registry.get_all_schemas()])

        reply = self._react_loop(
            system,
            messages,
            source_platform=source_platform,
            source_target=source_target,
            conversation_key=key,
        )

        # Save to per-conversation short-term history.
        with self._state_lock:
            history = self._histories.setdefault(key, [])
            history.append((user_message, reply))
            history_size = len(history)
        logger.info("[Agent] History now %d entries", history_size)

        if history_size >= MEMORY_HISTORY_LIMIT:
            self._consolidate_memory(key)

        return reply

    # ── One-shot execution (for cron agent_task) ──────────────────────

    def run_once(self, prompt: str, system_override: str = "") -> str:
        """一次性执行，不保留历史。供定时任务等外部场景调用。

        跟 run() 用的是同一个 _react_loop，同一个工具集（含 MCP）。
        不装历史，不触发记忆合 consolidation。
        """
        system = system_override or self._build_system_prompt()
        system += self._journal_memory_block(prompt)
        messages = [{"role": "user", "content": prompt}]
        logger.info("[Agent] run_once — prompt='%s'", prompt[:80])
        return self._react_loop(system, messages)

    # ── Streaming execution (for Voice Companion) ─────────────────────

    def clear_conversation(self, conversation_key: str = "voice") -> None:
        """清空指定会话的短期历史和待确认状态（语音"重新开始"用）。"""
        key = conversation_key or "default"
        with self._state_lock:
            self._histories.pop(key, None)
            self._pending_confirms.pop(key, None)
            self._bypass_confirms.discard(key)
        logger.info("[Agent] Cleared conversation: %s", key)

    def run_stream(self, user_message: str, *,
                   conversation_key: str = "voice",
                   cancel_event=None):
        """流式 ReAct 循环（语音助手专用）。

        生成器，yield 事件 dict：
        - {"type":"token","content": str}        最终回复的文本增量
        - {"type":"tool_start","name": str}       即将调用工具
        - {"type":"tool_done","name": str,"preview": str} 工具返回摘要
        - {"type":"confirm","question": str}      写操作需用户语音确认
        - {"type":"final","content": str}         本轮最终回复
        - {"type":"cancelled"}                    被 barge-in 取消
        - {"type":"error","message": str}         失败

        与 run() 共享同一套短期历史 / 长期记忆 / 确认状态机，
        但使用独立 conversation_key（默认 "voice"），与微信私聊互不串上下文。

        Args:
            cancel_event: threading.Event，set 后尽快终止（barge-in 用）。
        """
        key = conversation_key or "voice"
        # 注意：不能在这里 `with conversation_lock: return generator`——
        # with 会在 return（生成器对象创建）时立即释放，迭代根本还没开始。
        # 锁在 _run_stream_conversation 的生成器体内 acquire/release。
        return self._run_stream_conversation(user_message, key, cancel_event)

    def _run_stream_conversation(self, user_message: str, key: str,
                                 cancel_event):
        """实际生成器。

        RLock 必须在迭代线程内获取/释放（整个迭代都在同一个 HTTP 线程）。
        非阻塞获取：上一轮还没结束（barge-in 的 cancel 传到后端有延迟）时
        快速失败，由前端决定忽略还是重试。
        """
        conversation_lock = self._conversation_locks.setdefault(key, threading.RLock())
        acquired = conversation_lock.acquire(blocking=False)
        if not acquired:
            yield {"type": "error",
                   "message": "上一轮对话还在处理中，请稍等一下再说"}
            return
        try:
            yield from self._stream_inner(user_message, key, cancel_event)
        finally:
            conversation_lock.release()

    def _stream_inner(self, user_message: str, key: str, cancel_event):
        # 语音里说“以后回答引用原话”也要能落规则，不能只当一次闲聊。
        try:
            from src.journal.answer_rule import maybe_handle
            rule_reply = maybe_handle(user_message)
            if rule_reply:
                yield {"type": "final", "content": rule_reply}
                return
        except Exception as e:  # noqa: BLE001
            logger.warning("[Agent] 语音回答规则识别失败: %s", e)

        # ── 待确认状态：语音回答"确定/取消"走非流式确认状态机 ──
        with self._state_lock:
            has_pending = key in self._pending_confirms

        if has_pending:
            reply = self._handle_confirm_response(
                user_message,
                [{"role": "user", "content": user_message}],
                source_platform="voice",
                conversation_key=key,
            )
            # _handle_confirm_response 可能返回追问（未清 pending），
            # 也可能已执行完动作进入新一轮 ReAct；两种情况回复都直接播
            with self._state_lock:
                history = self._histories.setdefault(key, [])
                history.append((user_message, reply))
                history_size = len(history)
            if history_size >= MEMORY_HISTORY_LIMIT:
                # 后台总结，避免最后一帧之后多卡几秒
                threading.Thread(
                    target=self._consolidate_memory, args=(key,),
                    daemon=True,
                ).start()
            yield {"type": "final", "content": reply}
            return

        # ── 构建带短期历史的消息序列 ──
        messages: list[dict] = []
        with self._state_lock:
            history = self._histories.setdefault(key, [])
            history_snapshot = list(history[-MEMORY_HISTORY_LIMIT:])
        for u, a in history_snapshot:
            messages.append({"role": "user", "content": u})
            messages.append({"role": "assistant", "content": a})
        messages.append({"role": "user", "content": user_message})

        system = self._build_system_prompt(conversation_key=key)
        system += self._voice_system_suffix()
        system += self._journal_memory_block(user_message)

        reply = None
        try:
            for ev in self._react_loop_stream(system, messages, key, cancel_event):
                t = ev.get("type")
                if t == "final":
                    reply = ev.get("content", "")
                yield ev
                if t == "cancelled":
                    return
        except Exception as e:
            logger.exception("[Agent] run_stream failed")
            yield {"type": "error", "message": f"AI 调用失败：{e}"}
            return

        # ── 落短期历史（被取消的半轮回覆不保存，避免污染上下文）──
        if reply:
            with self._state_lock:
                history = self._histories.setdefault(key, [])
                history.append((user_message, reply))
                history_size = len(history)
            if history_size >= MEMORY_HISTORY_LIMIT:
                # 后台总结，避免阻塞 SSE 收尾
                threading.Thread(
                    target=self._consolidate_memory, args=(key,),
                    daemon=True,
                ).start()

    @staticmethod
    def _voice_system_suffix() -> str:
        """语音场景的 system prompt 补充：当前时间 + 口语化风格约束。"""
        weekday_cn = "一二三四五六日"[time.localtime().tm_wday]
        now_str = time.strftime("%Y年%m月%d日（周" + weekday_cn + "）%H:%M",
                                time.localtime())
        return f"""

## 当前时间
{now_str}

## 语音对话风格（用户正在用打电话的方式与你交流）
- 用口语化、自然、简洁的中文回答，先给结论再补充，像真人通话一样
- 禁止使用 Markdown 表格、代码块、标题符号(#)、星号(**)和长编号列表；
  需要分点时用"第一、第二"或"首先、另外"这种口语表达
- 单次回答尽量控制在 300 字以内；信息量大时先说最重要的，
  用户会继续追问，不要一次把所有细节倒完
- 提到日期用相对说法（今天、昨天、本周），不要念"2026-10-14"这种格式
- 查询类问题优先调用 query_my_feed 等工具拿到事实再回答，禁止编造"""

    def _react_loop_stream(self, system: str, messages: list[dict],
                           conversation_key: str, cancel_event):
        """流式 ReAct 主循环。yield 事件 dict，最后一帧为 final/cancelled。"""
        for step in range(1, self._max_steps + 1):
            if cancel_event is not None and cancel_event.is_set():
                yield {"type": "cancelled"}
                return

            logger.info("[Agent/Voice] Step %d/%d messages=%d",
                        step, self._max_steps, len(messages))

            # ── 流式调用 LLM，累积 content / tool_calls ──
            content_parts: list[str] = []
            tool_calls = None
            reasoning = ""
            try:
                for kind, payload in self._llm.agent_chat_stream(
                    system_prompt=system,
                    messages=messages,
                    tools=self._tools.registry.get_all_schemas(),
                ):
                    if cancel_event is not None and cancel_event.is_set():
                        yield {"type": "cancelled"}
                        return
                    if kind == "delta":
                        content_parts.append(payload)
                        # 工具轮通常没有 delta；一旦最终有 tool_calls，
                        # 前端会按 tool_start 丢弃本轮朗读
                        yield {"type": "token", "content": payload}
                    elif kind == "done":
                        tool_calls = payload.get("tool_calls")
                        if not content_parts and payload.get("content"):
                            # 非流式降级后端：content 只在 done 里
                            content_parts.append(payload["content"])
                            yield {"type": "token", "content": payload["content"]}
                        reasoning = payload.get("reasoning", "") or ""
            except Exception as e:
                logger.exception("[Agent/Voice] LLM stream failed at step %d", step)
                yield {"type": "error", "message": f"AI 调用失败：{e}"}
                return

            content = "".join(content_parts)

            # ── 无工具调用：本轮即最终回复 ──
            if not tool_calls:
                yield {"type": "final", "content": content or "完成。"}
                return

            # ── 分流 confirm_action 与普通工具 ──
            confirm_tc = None
            action_tcs: list[dict] = []
            for tc in tool_calls:
                if tc["function"]["name"] == "confirm_action":
                    confirm_tc = tc
                else:
                    action_tcs.append(tc)

            # 语音链路不触发 IM progress_callback（否则用户微信会收到调试消息）

            if confirm_tc is not None:
                try:
                    args = json.loads(confirm_tc["function"]["arguments"])
                except (json.JSONDecodeError, KeyError):
                    args = {"action": "执行操作", "details": ""}
                action = args.get("action", "执行操作")
                details = args.get("details", "")
                question = f"需要你确认：{action}"
                if details:
                    question += f"\n{details}"
                question += "\n说「确定」执行，说「取消」放弃。"

                all_tcs = [confirm_tc] + action_tcs
                messages.append({
                    "role": "assistant",
                    "content": content or None,
                    "reasoning_content": reasoning,
                    "tool_calls": all_tcs,
                })
                with self._state_lock:
                    self._pending_confirms[conversation_key] = {
                        "question": question,
                        "messages": messages,
                        "system": system,
                        "confirm_tool_call_id": confirm_tc["id"],
                        "action_tcs": action_tcs,
                    }
                yield {"type": "confirm", "question": question}
                return

            # 记录工具调用轮（DeepSeek 要求保留 reasoning_content）
            messages.append({
                "role": "assistant",
                "content": None,
                "reasoning_content": reasoning,
                "tool_calls": tool_calls,
            })

            # ── 逐个执行工具 ──
            with self._state_lock:
                bypass = conversation_key in self._bypass_confirms
                self._bypass_confirms.discard(conversation_key)

            for tc in action_tcs:
                if cancel_event is not None and cancel_event.is_set():
                    yield {"type": "cancelled"}
                    return

                name = tc["function"]["name"]
                try:
                    args = self._parse_args(tc)
                except json.JSONDecodeError:
                    args = {}

                logger.info("[Agent/Voice] Tool call: %s(%s)", name, args)
                yield {"type": "tool_start", "name": name}

                tool = self._tools.registry.get(name)
                if tool and getattr(tool, "requires_confirm", False) and not bypass:
                    result = f"工具「{name}」是写操作，必须先调用 confirm_action 获取用户确认。"
                else:
                    try:
                        result = self._tools.registry.execute(name, args)
                    except Exception as e:
                        result = f"工具「{name}」执行出错: {e}"
                        logger.warning("[Agent/Voice] tool %s failed: %s", name, e)

                result = result or "（工具返回为空）"
                logger.info("[Agent/Voice] Tool result: %s...", result[:120])
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })
                yield {"type": "tool_done", "name": name,
                       "preview": result[:160]}

        logger.warning("[Agent/Voice] Exceeded max_steps=%d", self._max_steps)
        yield {"type": "final", "content": "我还在思考中，能再说一遍吗？"}

    # ── ReAct loop core ────────────────────────────────────────────

    def _react_loop(self, system: str,
                    messages: list[dict], *,
                    source_platform: str = "wechat",
                    source_target: str | None = None,
                    conversation_key: str = "default") -> str:
        """The main ReAct reasoning + acting loop."""
        for step in range(1, self._max_steps + 1):
            logger.info("[Agent] Step %d/%d — messages=%d",
                        step, self._max_steps, len(messages))

            # ── Call LLM ─────────────────────────────────────────
            try:
                content, tool_calls, reasoning = self._llm.agent_chat(
                    system_prompt=system,
                    messages=messages,
                    tools=self._tools.registry.get_all_schemas(),
                )
            except Exception as e:
                logger.exception("[Agent] LLM call failed at step %d", step)
                return f"AI 调用失败：{e}"

            logger.info("[Agent] LLM response — content=%s, tool_calls=%d",
                        content[:60] if content else "(null)",
                        len(tool_calls) if tool_calls else 0)
            if reasoning:
                logger.info("[Agent] Step %d reasoning: %s",
                            step, reasoning)

            # ── Push progress via callback (iLink etc.) ─────────────
            if tool_calls and self._progress_callback:
                tool_names = ", ".join(
                    tc["function"]["name"] for tc in tool_calls
                )
                try:
                    self._progress_callback(
                        step,
                        self._max_steps,
                        tool_names,
                        reasoning,
                        source_platform=source_platform,
                        source_target=source_target,
                    )
                except Exception as e:
                    logger.warning("[Agent] progress_callback failed: %s", e)

            # ── LLM chose to reply — task complete ────────────────
            if not tool_calls:
                logger.info("[Agent] Complete after %d steps", step)
                return content or "完成。"

            # ── Process tool calls ────────────────────────────────
            # 1) Check for confirm_action (intercept before execution)
            # 2) Check requires_confirm — only gates config/file-changing writes,
            #    NOT pipeline triggers like run_digest/run_oa_digest

            with self._state_lock:
                bypass_this_round = conversation_key in self._bypass_confirms
                self._bypass_confirms.discard(conversation_key)

            confirm_tc = None
            action_tcs: list[dict] = []
            for tc in tool_calls:
                tc_name = tc["function"]["name"]
                if tc_name == "confirm_action":
                    confirm_tc = tc
                else:
                    action_tcs.append(tc)

            if confirm_tc is not None:
                logger.info("[Agent] Intercepting confirm_action — %d pending action(s): %s",
                            len(action_tcs),
                            [t["function"]["name"] for t in action_tcs])
                return self._intercept_confirm(
                    system, messages, confirm_tc, action_tcs,
                    content or "", reasoning or "", conversation_key=conversation_key,
                )

            # ── Execute tools ───────────────────────────────────────
            for tc in action_tcs:
                name = tc["function"]["name"]
                try:
                    args = self._parse_args(tc)
                except json.JSONDecodeError:
                    args = {}

                logger.info("[Agent] Tool call: %s(%s)", name, args)

                # Check requires_confirm
                tool = self._tools.registry.get(name)
                if tool and tool.requires_confirm and not bypass_this_round:
                    logger.info("[Agent] Tool '%s' requires confirm but bypass=False — rejecting", name)
                    messages.append({
                        "role": "assistant",
                        "content": None,
                        "reasoning_content": "",
                        "tool_calls": [tc],
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": f"工具「{name}」是写操作，必须先调用 confirm_action 获取用户确认。",
                    })
                    continue

                # Execute tool
                result = self._tools.registry.execute(name, args)

                logger.info("[Agent] Tool result: %s...",
                            result[:120] if result else "(empty)")

                # Append tool call + result to conversation
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": reasoning,
                    "tool_calls": [tc],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result or "（工具返回为空）",
                })

        # Exceeded max steps
        logger.warning("[Agent] Exceeded max_steps=%d", self._max_steps)
        return "我还在思考中，能再说一遍吗？"

    # ── confirm_action state machine ───────────────────────────────

    CONFIRM_YES_KEYWORDS = (
        "确定", "确认", "好", "是", "嗯", "行",
        "可以", "ok", "yes", "y", "对的", "没错",
        "同意", "要",
    )
    CONFIRM_NO_KEYWORDS = (
        "不", "取消", "算了", "不用", "别",
        "no", "n", "不要",
    )

    def _intercept_confirm(self, system: str,
                           messages: list[dict],
                           confirm_tc: dict,
                           action_tcs: list[dict],
                           llm_content: str = "",
                           reasoning: str = "",
                           conversation_key: str = "default") -> str:
        """Intercept a confirm_action call from the LLM.

        Saves both the confirm state AND any pending action_tcs.
        On user confirm, action_tcs are executed directly without
        going back to the LLM for re-generation.

        Args:
            llm_content: The text content from the LLM's response
                         (may be empty if LLM only returned tool_calls).

        Returns:
            Confirmation question text for the user.
        """
        try:
            args = json.loads(confirm_tc["function"]["arguments"])
        except (json.JSONDecodeError, KeyError):
            args = {"action": "执行操作", "details": ""}

        action = args.get("action", "执行操作")
        details = args.get("details", "")

        question = f"⚠️ 需要确认：{action}"
        if details:
            question += f"\n{details}"
        question += "\n\n回复「确定」执行，回复「取消」放弃。"

        # Store ALL tool_calls (confirm + actions) in assistant message
        all_tcs = [confirm_tc] + (action_tcs or [])
        messages.append({
            "role": "assistant",
            "content": llm_content or None,
            "reasoning_content": reasoning,
            "tool_calls": all_tcs,
        })

        with self._state_lock:
            self._pending_confirms[conversation_key] = {
            "question": question,
            "messages": messages,
            "system": system,
            "confirm_tool_call_id": confirm_tc["id"],
            "action_tcs": action_tcs or [],
        }

        logger.info("[Agent] Pending confirm: action=%s, action_tcs=%s",
                    action, [t["function"]["name"] for t in (action_tcs or [])])
        return question

    def _handle_confirm_response(self,
                                 user_message: str,
                                 fresh_messages: list[dict],
                                 *,
                                 source_platform: str = "wechat",
                                 source_target: str | None = None,
                                 conversation_key: str = "default") -> str:
        """Handle user's response to a pending confirmation question.

        If the user confirms, executes pending action_tcs directly
        (no LLM re-generation needed) and continues the ReAct loop.
        If cancelled, injects cancellation for all pending actions.
        If unclear, re-asks the question.

        Returns:
            Either the continued ReAct loop result, or a re-ask.
        """
        with self._state_lock:
            pending = self._pending_confirms.get(conversation_key)
        if pending is None:
            return "当前没有待确认的操作。"

        clean = user_message.strip().lower()
        logger.info("[Agent] _handle_confirm_response — user_response='%s'", clean[:40])

        # ── User confirmed: execute pending actions directly ─────────
        if self._is_confirm_yes(clean):
            logger.info("[Agent] User CONFIRMED — executing %d pending action(s)",
                        len(pending.get("action_tcs", [])))

            # Inject confirm_action result
            pending["messages"].append({
                "role": "tool",
                "tool_call_id": pending["confirm_tool_call_id"],
                "content": "用户已确认操作，请继续执行。",
            })

            # Execute each pending action directly
            for tc in pending.get("action_tcs", []):
                name = tc["function"]["name"]
                args = self._parse_args(tc)
                logger.info("[Agent] Executing confirmed action: %s(%s)", name, args)
                try:
                    result = self._tools.registry.execute(name, args)
                    logger.info("[Agent] Confirmed action done: %s — result=%s",
                                name, (result or "")[:120])
                except Exception as e:
                    result = f"执行失败: {e}"
                    logger.warning("[Agent] Confirmed action failed: %s — %s", name, e)
                pending["messages"].append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

            # Append user's confirm message so LLM sees it
            pending["messages"].extend(fresh_messages)
            with self._state_lock:
                self._pending_confirms.pop(conversation_key, None)
                self._bypass_confirms.add(conversation_key)
            return self._react_loop(
                pending["system"], pending["messages"],
                source_platform=source_platform,
                source_target=source_target,
                conversation_key=conversation_key,
            )

        # ── User cancelled: inject cancellation for all actions ──────
        if self._is_confirm_no(clean):
            logger.info("[Agent] User CANCELLED — cancelling %d pending action(s)",
                        len(pending.get("action_tcs", [])))
            pending["messages"].append({
                "role": "tool",
                "tool_call_id": pending["confirm_tool_call_id"],
                "content": "用户取消了操作。请告知用户操作已取消。",
            })
            for tc in pending.get("action_tcs", []):
                pending["messages"].append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": "用户取消了操作，该操作未执行。",
                })
            # Append user's cancel message so LLM sees it
            pending["messages"].extend(fresh_messages)
            with self._state_lock:
                self._pending_confirms.pop(conversation_key, None)
            return self._react_loop(
                pending["system"], pending["messages"],
                source_platform=source_platform,
                source_target=source_target,
                conversation_key=conversation_key,
            )

        # ── Unclear — re-ask ─────────────────────────────────────
        logger.info("[Agent] Confirm response unclear, re-asking: '%s'", clean[:60])
        return pending.get("question", "请回复「确定」执行，回复「取消」放弃。")

    @staticmethod
    def _is_confirm_yes(text: str) -> bool:
        """Check if user text means 'yes' to a confirmation."""
        lower = text.lower()
        for kw in AgentEngine.CONFIRM_YES_KEYWORDS:
            if kw in lower:
                return True
        return False

    @staticmethod
    def _is_confirm_no(text: str) -> bool:
        """Check if user text means 'no' to a confirmation."""
        lower = text.lower()
        for kw in AgentEngine.CONFIRM_NO_KEYWORDS:
            if kw in lower:
                return True
        return False

    # ── Internal helpers ───────────────────────────────────────────

    @staticmethod
    def _parse_args(tc: dict) -> dict:
        """Parse tool call arguments (handles str or dict)."""
        raw = tc["function"].get("arguments")
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}

    def _build_system_prompt(self, conversation_key: str = "default") -> str:
        """Build the system prompt with tool descriptions and memories."""
        desc = self._tools.registry.get_descriptions()
        prompt = AGENT_SYSTEM_PROMPT.replace("{tool_descriptions}", desc)
        prompt = prompt.replace("{max_steps}", str(self._max_steps))

        # Append long-term memories
        memories = self._load_memories(MEMORY_LOAD_COUNT, conversation_key=conversation_key)
        if memories:
            memory_lines = "\n".join(f"- {m}" for m in memories)
            prompt += f"\n\n## 对话记忆\n{memory_lines}"

        # 长期回答规则：后台可关；关闭时 prompt_block 返回空串。
        try:
            from src.journal.answer_rule import prompt_block
            prompt += prompt_block()
        except Exception as e:  # noqa: BLE001
            logger.warning("[Agent] 加载回答规则失败: %s", e)

        return prompt

    # 用户问“以前/上次/记过/品相”这类问题时，先自动查知识库，
    # 不把“要不要调用工具”交给模型临场判断。
    def _journal_memory_block(self, query: str) -> str:
        """统一检索入口：日记/附件 → 收藏 → 公众号 → 聊天。朋友圈不参与。

        以前只有命中 JOURNAL_QUERY_RE 才查，像“咖啡”这种没有“记过/收藏”
        字样的普通问题会直接跳过知识库。现在改成默认就查；只有“你好/
        谢谢”这类寒暄跳过。没有命中关键词时把每路条数收小，减少噪声。
        """
        if not query or not query.strip():
            return ""
        text = query.strip()
        try:
            from src.journal.recall import JOURNAL_QUERY_RE
            matched = bool(JOURNAL_QUERY_RE.search(text))
        except Exception as e:  # noqa: BLE001
            logger.warning("[Agent] 加载日记召回失败: %s", e)
            return ""
        if not matched and re.fullmatch(r"(你好|您好|在吗|谢谢|好的|嗯|哈哈|测试)[。！？!? ]*", text):
            return ""

        main_rag = getattr(self, "_rag", None)
        if main_rag is None:
            main_rag = getattr(getattr(self, "_tools", None), "_rag", None)

        # 位置类问题不能只靠向量块：收藏位置和聊天位置的原件不在索引里，
        # 必须回查收藏标签和聊天库，才拿得到高德链接。
        location_block = ""
        if any(k in text for k in ("位置", "定位", "地址", "导航", "在哪", "哪里")):
            try:
                from src.journal.places import (
                    find_locations, format_locations, guess_person_name,
                )
                person = guess_person_name(text)
                loc = find_locations(person) if person else {}
                if loc.get("diary") or loc.get("favorites") or loc.get("chats"):
                    location_block = format_locations(loc, limit=5)
            except Exception as e:  # noqa: BLE001
                logger.warning("[Agent] 位置原件查询失败: %s", e)

        try:
            from src.assistant.rag.gateway import KnowledgeGateway
            groups = KnowledgeGateway(main_rag=main_rag).groups(
                query, top_k=(10 if matched else 6),
                per_source=(4 if matched else 2))
        except Exception as e:  # noqa: BLE001
            logger.warning("[Agent] 统一知识检索失败: %s", e)
            return location_block

        hits = groups.get("journal") or []
        fav_hits = groups.get("fav") or []
        other_hits = groups.get("other") or []
        if not hits and not fav_hits and not other_hits and not location_block:
            return ""

        lines = []
        if location_block:
            lines.extend([
                "## 日记/收藏/聊天里的原始位置记录（系统回查，不是实时定位）",
                location_block,
            ])
        if hits:
            top = float(hits[0].get("score") or 0)
            floor = max(0.30, top - 0.15)
            picked = [h for h in hits if float(h.get("score") or 0) >= floor][:3]
            if not picked:
                # 分数整体偏低时至少给前三条，不能因为阈值把真实条目全丢掉。
                picked = hits[:3]
            if picked:
                lines.extend([
                    "## 用户日记库检索结果（系统自动查到的真实原文，不是猜测）",
                    "用户这句话涉及以前记过/发过的内容。回答时优先依据下面这些原文；"
                    "如果原文确实不够，再说明信息不足。不要回答“看不到之前的消息记录”。",
                ])
                for i, h in enumerate(picked, 1):
                    text = (h.get("text") or "").strip()
                    lines.append("%d. %s｜%s｜相关度 %.3f\n%s" % (
                        i, h.get("date") or "", h.get("title") or "",
                        float(h.get("score") or 0), text[:1200]))
        if fav_hits:
            try:
                from src.journal.favorites import favorite_details
            except Exception:  # noqa: BLE001
                favorite_details = lambda _x: None
            lines.extend([
                "## 微信收藏检索结果（系统自动查到的真实收藏，不是猜测）",
                "涉及收藏里的位置、文件、笔记或聊天记录时，优先依据下面内容；"
                "需要发位置时，直接把对应的高德链接发给用户。"
                "如果标题/内容与用户说的人或地点对不上，要明确说没找到，"
                "不能用地名相似的位置冒充。",
            ])
            for i, r in enumerate(fav_hits, 1):
                detail = favorite_details(str(r.get("source_id") or "")) or {}
                title = detail.get("title") or r.get("title") or ""
                link = detail.get("link") or r.get("link") or ""
                lines.append("%d. %s｜%s｜%s\n%s%s" % (
                    i, (r.get("date") or "")[:10],
                    detail.get("type_name") or r.get("kind") or "收藏",
                    title, (r.get("text") or "")[:800],
                    ("\n高德地图：" + link) if link and detail.get("type") == 6
                    else (("\n原链接：" + link) if link else ""),
                ))
        if other_hits:
            lines.extend([
                "## 其他知识库检索结果（公众号/聊天，系统自动查到的真实原文）",
                "按日记、收藏、其他顺序参考；下面是补充材料，不能覆盖上面的原文。",
            ])
            for i, r in enumerate(other_hits, 1):
                label = r.get("source_label") or ""
                title = r.get("title") or ""
                text = (r.get("text") or "").strip()[:1200]
                link = r.get("link") or ""
                lines.append("%d. %s｜%s｜%s\n%s%s" % (
                    i, r.get("date") or "", label, title, text,
                    (("\n原链接：" + link) if link else ""),
                ))
        return "\n".join(lines)


    # ── Public accessors ─────────────────────────────────────────────

    def get_tool_descriptions(self) -> str:
        """Get the current tool descriptions text (for welcome message)."""
        return self._tools.registry.get_descriptions()

    def clear_pending_confirm(self, conversation_key: str = "default") -> None:
        """Clear pending confirmation for one conversation."""
        with self._state_lock:
            self._pending_confirms.pop(conversation_key or "default", None)
            self._bypass_confirms.discard(conversation_key or "default")

    # ── Memory system ────────────────────────────────────────────────

    def _init_memory_db(self) -> None:
        """Create agent_memory table if not exists."""
        try:
            AGENT_MEMORY_DB.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(AGENT_MEMORY_DB))
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS agent_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_key TEXT NOT NULL DEFAULT 'default',
                summary TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )""")
            columns = {row[1] for row in conn.execute("PRAGMA table_info(agent_memory)")}
            if "conversation_key" not in columns:
                conn.execute("ALTER TABLE agent_memory ADD COLUMN conversation_key TEXT NOT NULL DEFAULT 'default'")
            conn.commit()
            conn.close()
            logger.info("[Agent] Memory DB initialized")
        except Exception as e:
            logger.warning("[Agent] Failed to init memory DB: %s", e)

    def _load_memories(self, limit: int = 3, conversation_key: str = "default") -> list[str]:
        """Load recent long-term memories (oldest first)."""
        try:
            conn = sqlite3.connect(str(AGENT_MEMORY_DB))
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT summary FROM agent_memory WHERE conversation_key = ? ORDER BY id DESC LIMIT ?",
                (conversation_key or "default", limit),
            ).fetchall()
            conn.close()
            return [r["summary"] for r in reversed(rows)]  # oldest first
        except Exception as e:
            logger.warning("[Agent] Failed to load memories: %s", e)
            return []

    def _save_memory(self, summary: str, conversation_key: str = "default") -> None:
        """Save a long-term memory entry."""
        try:
            conn = sqlite3.connect(str(AGENT_MEMORY_DB))
            conn.execute(
                "INSERT INTO agent_memory (conversation_key, summary) VALUES (?, ?)",
                (conversation_key or "default", summary),
            )
            conn.commit()
            conn.close()
            logger.info("[Agent] Saved memory: %s", summary[:60])
        except Exception as e:
            logger.warning("[Agent] Failed to save memory: %s", e)

    def _consolidate_memory(self, conversation_key: str = "default") -> None:
        """Summarize one conversation's short-term history."""
        with self._state_lock:
            history = list(self._histories.get(conversation_key, []))
        if not history:
            return

        history_lines = []
        for user_msg, agent_reply in history:
            history_lines.append(f"用户: {user_msg[:100]}")
            history_lines.append(f"助手: {agent_reply[:100]}")
        history_text = "\n".join(history_lines)

        try:
            prompt = (
                "请用一句话总结以下对话中提到的用户需求、偏好或重要信息，"
                "以便后续对话中回顾。如果没有值得记住的内容，回复「无」。\n\n"
                f"{history_text}"
            )
            summary = self._llm.chat(
                message=prompt,
                requester_name="system",
                group_name="记忆总结",
            )
            summary = summary.strip()
            if summary and summary != "无":
                self._save_memory(summary, conversation_key=conversation_key)
            with self._state_lock:
                self._histories[conversation_key] = []
            logger.info("[Agent] Memory consolidated, history cleared for %s", conversation_key)
        except Exception as e:
            logger.warning("[Agent] Memory consolidation failed: %s", e)
