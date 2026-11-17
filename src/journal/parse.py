"""把一句人话读成结构化的事（阶段4 模块三）。

输入：转写后的文字，例如"鸟房今天开始喂白苏子，喂七天"
输出：固定字段的字典，交给存储层建日记和提醒链。

原则：
  - 模型只做抽取，不做加工；字段缺失就留空，绝不编造
  - 解析不住（超时、返回不是 JSON、字段不合规）一律标 needs_review，原文照样进日记
  - 时刻只做"提示"，最终以用户回话为准
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from typing import Any, Optional

import requests

from .store import combine, today_str

logger = logging.getLogger(__name__)

DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-flash"

SYSTEM_PROMPT = """你是日记整理助手。用户会发来一句话，多半是生活或养殖上的事。
你要把它读成结构化的记录，只抽取，不推测，不补充用户没说的内容。

只输出一个 JSON 对象，不要任何解释、不要 markdown 代码块。字段如下：

{
  "kind": "diary | reminder | cancel | answer | chitchat",
  "summary": "一句话概括，不超过 20 字",
  "full_text": "整理后的完整表述，保留原意和细节",
  "topics": ["话题词"],
  "entities": {"对象": "鸟房 大白"],  // 处理对象、地点、品种等
  "mood": "",
  "reminder": {
     "title": "要提醒的事，如 喂白苏子",
     "object": "对象，如 鸟房",
     "action": "动作，如 喂",
     "days": 7,              // 连续多少天；只说"明天提醒一次"就填 1
     "start_date": "YYYY-MM-DD",   // 从哪天开始；"今天"就是今天
     "daily_time_hint": "08:00 | 晚上七点 | 空",  // 原文里提到的时刻，没提就空
     "cancel_target": ""     // kind=cancel 时填要取消的事
  },
  "confidence": 0.9            // 0~1，你真的读懂了才给高分
}

判定规则：
- 只是叙述发生了什么、没有"要去做/将来要做"的意思 → kind=diary，reminder 字段全空或省略
- 明确有一件将来要连续做的事 → kind=reminder，days 必须 ≥1
- 句子里有“提醒我/通知我/通知一下/到点叫我/喊我/别让我忘”，并且带将来要做的事 → 一律 kind=reminder，不能当成 diary
- "不喂了""取消了" → kind=cancel
- 用户只是在回答系统的问题（如"七点""晚上八点"）→ kind=answer
- 闲聊问候 → kind=chitchat
绝不编造用户没说的数字、日期、时长。读不出来就把 days 留空、confidence 给低分。"""


def _today() -> str:
    return today_str()


def parse_time_hint(text: str) -> Optional[str]:
    """从"晚上七点""8:30""七点半"里抠出 HH:MM；抠不出返回 None。"""
    if not text:
        return None
    t = text.strip()
    m = re.search(r"(\d{1,2})\s*[:：]\s*(\d{1,2})", t)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return "%02d:%02d" % (h, mi)
    cn = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}
    m = re.search(r"(早上|上午|中午|下午|傍晚|晚上|夜里|凌晨)?\s*"
                  r"([一二两三四五六七八九十]{1,3}|\d{1,2})\s*[点時时]", t)
    if not m:
        return None
    prefix, token = m.group(1) or "", m.group(2)
    hour = cn.get(token) if not token.isdigit() else int(token)
    if hour is None:
        return None
    half = 30 if "半" in t else 0
    if prefix in ("下午", "傍晚", "晚上", "夜里") and hour < 12:
        hour += 12 if hour != 12 else 0
    if prefix == "中午" and hour < 12:
        hour = hour if hour >= 11 else hour + 12
    if prefix == "凌晨" and hour == 12:
        hour = 0
    if not 0 <= hour <= 23:
        return None
    return "%02d:%02d" % (hour, half)


def parse_times_hint(text: str) -> list[str]:
    """从一句话里把所有时刻都抠出来，支持"早上10点、下午5点"这种一天多次。

    返回 ['10:00','17:00']（按时间先后排），一个都没有就返回空列表。
    """
    if not text:
        return []
    found: list[str] = []

    def _push(hhmm: Optional[str]) -> None:
        if hhmm and hhmm not in found:
            found.append(hhmm)

    # 先抓"8:05""10：30"这种带冒号的
    for m in re.finditer(r"(\d{1,2})\s*[:：]\s*(\d{1,2})", text):
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            _push("%02d:%02d" % (h, mi))

    # 再抓"晚上七点""早上八点半"
    cn = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}
    pattern = re.compile(r"(早上|上午|中午|下午|傍晚|晚上|夜里|凌晨)?\s*"
                         r"([一二两三四五六七八九十]{1,3}|\d{1,2})\s*[点時时](半)?")
    for m in pattern.finditer(text):
        prefix, token, half = m.group(1) or "", m.group(2), m.group(3)
        hour = cn.get(token) if not token.isdigit() else int(token)
        if hour is None:
            continue
        if prefix in ("下午", "傍晚", "晚上", "夜里") and hour < 12:
            hour = hour + 12 if hour != 12 else 0
        if prefix == "中午" and hour < 12:
            hour = hour if hour >= 11 else hour + 12
        if prefix == "凌晨" and hour == 12:
            hour = 0
        if 0 <= hour <= 23:
            _push("%02d:%02d" % (hour, 30 if half else 0))

    return sorted(found, key=lambda x: (int(x[:2]), int(x[3:])))


def _coerce_days(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if 1 <= n <= 365:
        return n
    return None


def _coerce_date(value: Any, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip()[:10]).isoformat()
        except ValueError:
            pass
    return fallback


def normalize(result: dict, raw_text: str, today: Optional[str] = None,
              now_hhmm: Optional[str] = None) -> dict:
    """把模型输出规整成存储层能直接用的形状；不合规就标 needs_review。"""
    today = today or _today()
    out = {
        "kind": "diary",
        "summary": "",
        "full_text": raw_text,
        "topics": [],
        "entities": {},
        "mood": "",
        "confidence": 0.0,
        "needs_review": False,
        "reminder": None,
        "reason": "",
    }
    if not isinstance(result, dict):
        out["needs_review"] = True
        out["reason"] = "模型没有返回对象"
        return out

    kind = str(result.get("kind") or "diary").strip()
    if kind not in ("diary", "reminder", "cancel", "answer", "chitchat"):
        kind = "diary"
    out["kind"] = kind
    out["summary"] = str(result.get("summary") or "")[:40]
    out["full_text"] = str(result.get("full_text") or raw_text)
    topics = result.get("topics") or []
    out["topics"] = [str(t)[:20] for t in topics] if isinstance(topics, list) else []
    ents = result.get("entities")
    out["entities"] = ents if isinstance(ents, dict) else {}
    out["mood"] = str(result.get("mood") or "")[:10]
    try:
        out["confidence"] = max(0.0, min(1.0, float(result.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        out["confidence"] = 0.0

    if kind == "reminder":
        rem = result.get("reminder") or {}
        if not isinstance(rem, dict):
            rem = {}
        days = _coerce_days(rem.get("days"))
        start = _coerce_date(rem.get("start_date"), today)
        title = str(rem.get("title") or "").strip()
        hint = rem.get("daily_time_hint")
        parsed_hint = parse_time_hint(str(hint)) if hint else None
        if not parsed_hint and hint:
            parsed_hint = None
        if not parsed_hint:
            parsed_hint = parse_time_hint(raw_text)

        if not title or days is None:
            out["needs_review"] = True
            out["reason"] = "缺少事项名称或天数，无法建提醒"
        else:
            out["reminder"] = {
                "title": title[:40],
                "object": str(rem.get("object") or "")[:20],
                "action": str(rem.get("action") or "")[:20],
                "days": days,
                "start_date": start,
                "daily_time_hint": parsed_hint,
                "cancel_target": str(rem.get("cancel_target") or "")[:40],
            }
            if start > today and days is not None:
                pass  # 未来开始也算合法，交给用户确认
            if parsed_hint is None and now_hhmm:
                logger.debug("时刻未提及，将走追问；当前 %s", now_hhmm)

    if kind == "cancel" and not str((result.get("reminder") or {}).get("cancel_target") or "").strip():
        out["needs_review"] = True
        out["reason"] = "说了取消但没说取消哪件事"

    if out["confidence"] < 0.6:
        out["needs_review"] = True
        if not out["reason"]:
            out["reason"] = "模型自评不确定"
    return out


class DeepSeekParser:
    """调用 DeepSeek 做抽取。密钥缺失时降级为"只记日记不建提醒"。"""

    def __init__(self, api_key: Optional[str] = None, model: str = DEFAULT_MODEL,
                 url: str = DEEPSEEK_URL, timeout: float = 12.0):
        self.api_key = api_key or ""
        self.model = model
        self.url = url
        self.timeout = timeout

    def parse(self, text: str, today: Optional[str] = None,
              now_hhmm: Optional[str] = None) -> dict:
        text = (text or "").strip()
        if not text:
            return normalize({}, "", today, now_hhmm)
        if not self.api_key:
            res = normalize({"kind": "diary", "summary": text[:20],
                             "confidence": 0.5}, text, today, now_hhmm)
            res["reason"] = "缺少模型密钥，只记日记不建提醒"
            return res
        try:
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": "今天是 %s。这句话是：%s" % (
                        today or _today(), text)},
                ],
                "temperature": 0.1,
                "response_format": {"type": "json_object"},
                "max_tokens": 600,
            }
            resp = requests.post(
                self.url, json=payload, timeout=self.timeout,
                headers={"Authorization": "Bearer %s" % self.api_key},
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            return normalize(_loads(content), text, today, now_hhmm)
        except Exception as e:  # noqa: BLE001
            logger.warning("解析失败，标待复核: %s", e)
            res = normalize({"kind": "diary", "summary": text[:20], "confidence": 0.3},
                            text, today, now_hhmm)
            res["needs_review"] = True
            res["reason"] = "解析失败：%s" % str(e)[:80]
            return res


def _loads(content: str) -> dict:
    """模型偶尔包一层代码块或多说两句，这里兜一下。"""
    content = (content or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\n?|```$", "", content).strip()
    try:
        return json.loads(content)
    except ValueError:
        m = re.search(r"\{.*\}", content, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                pass
    return {}


def entry_date_of(parsed: dict, today: Optional[str] = None) -> str:
    """日记记在哪一天：有提醒就记在开始日，否则记今天。"""
    today = today or _today()
    rem = parsed.get("reminder")
    if rem and rem.get("start_date"):
        return rem["start_date"]
    return today
