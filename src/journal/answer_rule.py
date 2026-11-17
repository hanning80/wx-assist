"""长期回答规则：把“以后回答时引用原话”存成可查可改的偏好。

只做一件事：识别用户对“回答方式”的长期要求，落库；回答时再把规则
拼进系统提示。它和日记分开存，避免把一句规则当成日记流水，也避免
模型把“记下了”误当成“以后会执行”。

当前只支持 A 口径：回答用到用户以前记过/发过的内容时引用原文，
不是每条回答都硬引。
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from .store import JournalStore

logger = logging.getLogger(__name__)

RULE_KEY = "quote_sources"

DEFAULT_RULE_TEXT = (
    "回答用到我以前记过或发过的内容时，先逐字引用我的原话或日记原文，"
    "注明日期和来源，再补充解释；找不到原文就直接说没找到，不要编造。"
)

# 必须是“以后/每次/从现在起”这类长期要求，且同时出现“回答”和“引用/带原话”。
# 不靠单个“引用”二字，避免把普通聊天里的“引用某篇文章”误判成规则。
_SET_TIME = re.compile(r"(以后|从现在起|往后|每次|今后)")
_SET_ANSWER = re.compile(r"(回答|答复|回复)")
_SET_QUOTE = re.compile(r"(引用|带上|带原话|附上|附原话|保留原话)")
_SET_SOURCE = re.compile(r"(原话|原文|日记)")

# 关闭/开启规则；先判关闭，避免“以后不用引用”被当成开启。
_OFF = re.compile(
    r"(取消|不用|不要|别|关闭|关掉|停用).{0,12}(引用|原话|原文|日记)"
    r"|(引用|原话|原文|日记).{0,12}(取消|不用|不要|关闭|关掉|停用)"
)
_ON = re.compile(
    r"(开启|打开|恢复|启用).{0,12}(引用|原话|原文|日记)"
    r"|(引用|原话|原文|日记).{0,12}(开启|打开|恢复|启用)"
)


def _clean_rule_text(value: Optional[str]) -> str:
    text = (value or "").strip()
    if not text:
        return DEFAULT_RULE_TEXT
    # 规则是给人看、给模型读的短句；过长就截断，避免把整段聊天塞进去。
    return text[:500]


def _is_set_intent(text: str) -> bool:
    return bool(
        _SET_TIME.search(text)
        and _SET_ANSWER.search(text)
        and _SET_QUOTE.search(text)
        and _SET_SOURCE.search(text)
    )


def _reply_for(action: str) -> str:
    if action == "off":
        return "已关掉“回答引用原话”规则。以后回答不再强制引用；需要时再打开。"
    if action == "on":
        return ("已开启“回答引用原话”规则：只要回答用到你以前记过或发过的内容，"
                "就先逐字引用原话、注明日期和来源，再解释；找不到原文就说没找到。")
    return ("记下了：以后回答只要用到你以前记过或发过的内容，"
            "我会先逐字引用原话、注明日期和来源，再解释；"
            "找不到原文就说没找到，不编。")


def maybe_handle(text: str, store: Optional[JournalStore] = None) -> Optional[str]:
    """识别并保存回答规则；命中返回回执，没命中返回 None。

    调用方在“记日记/普通聊天”之前调用，确保规则不会被存成日记。
    """
    text = (text or "").strip()
    if not text or len(text) > 200:
        return None

    own_store = store is None
    store = store or JournalStore()
    try:
        if _OFF.search(text):
            store.save_answer_rule(False, "")
            return _reply_for("off")
        if _ON.search(text):
            store.save_answer_rule(True, "")
            return _reply_for("on")
        if _is_set_intent(text):
            store.save_answer_rule(True, DEFAULT_RULE_TEXT)
            return _reply_for("set")
        return None
    finally:
        if own_store:
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass


def get_rule(store: Optional[JournalStore] = None) -> dict:
    """读取当前规则；默认开启 A 口径，首次使用不需要先写一条记录。"""
    own_store = store is None
    store = store or JournalStore()
    try:
        return store.get_answer_rule()
    except Exception as e:  # noqa: BLE001
        logger.warning("读取回答规则失败: %s", e)
        return {"enabled": 1, "rule_text": DEFAULT_RULE_TEXT, "updated_at": ""}
    finally:
        if own_store:
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass


def save_rule(enabled: bool, rule_text: str = "",
               store: Optional[JournalStore] = None) -> None:
    """后台保存入口：空正文存空串，读取时回落到默认口径。"""
    own_store = store is None
    store = store or JournalStore()
    try:
        store.save_answer_rule(bool(enabled), _clean_rule_text(rule_text) if rule_text else "")
    finally:
        if own_store:
            try:
                store.close()
            except Exception:  # noqa: BLE001
                pass


def prompt_block(store: Optional[JournalStore] = None) -> str:
    """给系统提示用的规则块；关闭时返回空串。"""
    rule = get_rule(store)
    if not int(rule.get("enabled") or 0):
        return ""
    text = _clean_rule_text(rule.get("rule_text"))
    return (
        "\n\n## 长期回答规则（用户在后台开启）\n"
        "- 只在回答用到用户以前记过或发过的内容时引用原文；不要每条回答都硬引。\n"
        "- 引用必须逐字，用「」括起来，并注明日期与来源（日记标题或聊天时间）。\n"
        "- 先引用，再解释；不要把摘要改写成“原话”。\n"
        "- 没有找到原文时直接说“没找到原话”，不要用相近内容冒充，也不要编。\n"
        "- 与当前问题无关的旧日记不要硬引。\n"
        "- 用户规则原文：%s\n" % text
    )
