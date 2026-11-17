"""ClawBot 里能直接办的两件事：看提醒、总结和某人的聊天。

只在没接对话助手的时候用（独立日记线程模式），接了助手就交给助手，免得两边都回一遍。
"""

from __future__ import annotations

import re
from typing import Optional

LIST_PAT = re.compile(r"(提醒列表|查看提醒|有哪些提醒|有什么提醒|还有什么提醒|在跑什么|我的提醒|生活提醒|定时任务)")
# 改提醒：必须带“改/调/换”和提醒语义，避免把普通聊天当成改提醒
EDIT_PAT = re.compile(r"(改|调|换).*(提醒|每天|一共|共|点|次|天)")
# 一共几天：兼容阿拉伯数字和“七天/两天”
DAYS_ANY_PAT = re.compile(r"(?:一共|共|总共)?\s*([零一二三四五六七八九十百两\d]+)\s*天")
# 每天几次：兼容“每天两次/一天三次”
PER_DAY_PAT = re.compile(r"(?:每天|一天)\s*([零一二三四五六七八九十两\d]+)\s*次")
# 具体时刻：8点、8:30、17点、八点半
TIME_ANY_PAT = re.compile(r"([零一二三四五六七八九十两\d]{1,3})\s*(?:点|:|：)\s*(半|[零一二三四五六七八九十两\d]{1,2})?")
# "总结一下，我跟大姚最近都说了什么？" 这种口语也要认
SUM_PAT = re.compile(r"(?:帮我)?总结(?:一下|下)?[，,、\s]*(?:我)?(?:和|跟|与)([^，。,\s？?！!]{1,60})")
# 分析类：必须带对象，否则不接
AN_CHAT_PAT = re.compile(r"分析(?:一下|下)?[，,、\s]*(?:我)?(?:和|跟|与)([^，。,\s？?！!]{1,60})")
AN_DIARY_PAT = re.compile(r"分析(?:一下|下)?[，,、\s]*(?:最近|这|近)?\s*(?:\d+\s*天)?\s*(?:的)?日记")
DAYS_PAT = re.compile(r"(\d+)\s*天")
# 名字后面常见的口语尾巴，去掉才是人名
NAME_TAIL = re.compile(r"(最近|这周|这两天|这几天|近期|都|说了|聊了|什么|的|呢|啊|\d+\s*天|\d+)+$")
# 名字到哪儿为止：这些词一出现，后面就不是名字了
NAME_STOP = re.compile(
    r"所有|全部|最近|这周|这两天|这几天|近期|聊天|记录|消息|对话|中|里|说过|提到|关于|判断|发过|讲过|的内容")
# 说明是在"从聊天里挑原话"而不是"写总结"
EXTRACT_CUE = re.compile(r"(列出来|找出来|挑出来|摘出来|有哪些|原话|的内容|关于|提到|判断|说过|讲过|发过)")
# 主题前后的口水壳子
TOPIC_HEAD = re.compile(r"^(所有|全部|的|聊天(儿)?记录|聊天(儿)?|记录|消息|对话|中|里|里面|，|,|、|\s)+")
TOPIC_TAIL = re.compile(r"(的内容|列出来给我看|列出来|找出来|给我看|的话|内容|，|,|。|\s)+$")


def _split_name(raw: str) -> tuple[str, str]:
    """把"大姚所有聊天记录中判断品相"切成（大姚，"所有聊天记录中判断品相"）。"""
    raw = (raw or "").strip()
    m = NAME_STOP.search(raw)
    idx = m.start() if m else len(raw)
    name = raw[:idx].strip(" 的")
    for _ in range(3):
        name = NAME_TAIL.sub("", name).strip()
    return name, raw[idx:]


def _tail(text: str, m, name: str) -> str:
    """名字之后剩下的整句（含逗号之后的部分），用来判断是"挑原话"还是"写总结"。"""
    raw = m.group(1) or ""
    return raw[len(name):] + text[m.end():]


def _clean_topic(rest: str, name: str) -> str:
    """从名字后面那截里抠出主题："…中，判断鸟品相好坏的内容，列出来给我看" → "判断鸟品相好坏"。"""
    t = rest
    for _ in range(4):
        t = TOPIC_HEAD.sub("", t)
    for _ in range(4):
        t = TOPIC_TAIL.sub("", t)
    t = t.strip(" 的了呢啊")
    return t[:40]


def _list_replies(svc) -> str:
    rows = svc._running_rows()
    if not rows:
        return "现在没有在跑的生活提醒。"
    lines = []
    for r in rows:
        r = dict(r)
        times = r.get("daily_time") or "（还没定时刻）"
        lines.append("· %s（编号 %s）：每天 %s，共 %s 天，%s 起" % (
            r["title"], r["id"], times.replace(",", "、"), r["days"], r["start_date"]))
    return "生活提醒（在跑的）：\n" + "\n".join(lines)


_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _num(raw: str) -> Optional[int]:
    """把“7/七/十一/两”这类口语数字换成整数。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    if raw == "十":
        return 10
    if raw.startswith("十"):
        tail = _CN_DIGITS.get(raw[1:], None)
        return 10 + tail if tail is not None else None
    if "十" in raw:
        head, _, tail = raw.partition("十")
        h = _CN_DIGITS.get(head, None)
        t = _CN_DIGITS.get(tail, 0) if tail else 0
        if h is None:
            return None
        return h * 10 + t
    return _CN_DIGITS.get(raw)


def _times_from_text(text: str) -> list[str]:
    """从“每天八点和十七点/8:30”里抠出 HH:MM。"""
    out = []
    for m in TIME_ANY_PAT.finditer(text or ""):
        h = _num(m.group(1))
        if h is None or h < 0 or h > 23:
            continue
        minute_raw = m.group(2) or "0"
        if minute_raw == "半":
            minute = 30
        else:
            minute = _num(minute_raw)
        if minute is None or minute < 0 or minute > 59:
            continue
        hhmm = "%02d:%02d" % (h, minute)
        if hhmm not in out:
            out.append(hhmm)
    return out


def _edit_reply(text: str, svc) -> Optional[str]:
    """改在跑的提醒：只改用户明确说到的天数/时刻，没说到的保持原样。"""
    rows = svc._running_rows()
    if not rows:
        return "现在没有在跑的生活提醒，没什么可改的。"
    hit = None
    for r in rows:
        title = (r["title"] or "").strip()
        if title and title in text:
            hit = r
            break
    if hit is None:
        # 只有一条在跑时不用再让用户报名字；语音里说“改成一共七天”就够了
        if len(rows) == 1:
            hit = rows[0]
    if hit is None:
        names = "、".join(r["title"] for r in rows)
        return "要改哪一条？现在在跑的是：%s。回我名字，或者直接说“改提醒”。" % names

    dm = DAYS_ANY_PAT.search(text)
    days = _num(dm.group(1)) if dm else None
    if days is not None and (days < 1 or days > 366):
        return "天数要在 1 到 366 天之间。"

    times = _times_from_text(text)
    pm = PER_DAY_PAT.search(text)
    per_day = _num(pm.group(1)) if pm else None
    old_times = [t for t in (hit["daily_time"] or "").split(",") if t]
    if times:
        daily_time = ",".join(times)
    else:
        daily_time = None
        if per_day is not None and len(old_times) != per_day:
            return ("「%s」现在每天 %s。你说改成每天 %s 次，得告诉我具体几点，"
                    "比如“改成每天八点和十七点”。" % (
                        hit["title"], "、".join(old_times) or "时刻待定", per_day))
    if days is None and daily_time is None:
        return "你说了要改「%s」，但没说改成几天或几点。比如“改成一共七天，每天八点和十七点”。" % hit["title"]

    try:
        updated = svc.store.update_chain(
            int(hit["id"]), days=days, daily_time=daily_time)
    except Exception as e:  # noqa: BLE001
        return "没改成：%s" % e
    when = (updated.get("daily_time") or "时刻待定").replace(",", "、")
    return ("已改好：%s，%s 起，共 %d 天，每天 %s。从今天起按新计划发，不补发过去。" % (
        updated["title"], updated["start_date"], updated["days"], when))


def try_handle(text: str, svc) -> Optional[str]:
    """认得的指令就执行并返回回执文本；不认识返回 None。"""
    text = (text or "").strip()
    if not text:
        return None

    # 新建/删除提醒的确认步：先接“确认/取消”，不能当普通聊天。
    if getattr(svc, "has_pending_confirmation", None) and svc.has_pending_confirmation():
        result = svc.confirm_pending(text, say=False)
        if result:
            return result.get("reply")

    if EDIT_PAT.search(text):
        reply = _edit_reply(text, svc)
        if reply:
            return reply

    if LIST_PAT.search(text):
        return _list_replies(svc)

    m = SUM_PAT.search(text)
    if m:
        name, _ = _split_name(m.group(1))
        if not name:
            return None
        rest = _tail(text, m, name)
        dm = DAYS_PAT.search(text)
        days = max(1, min(int(dm.group(1)) if dm else 7, 365))

        # 要的是"把原话挑出来"，不是"写归纳"
        if EXTRACT_CUE.search(rest):
            topic = _clean_topic(rest, name)
            if not topic:
                return _need_topic(name)
            from src.assistant.analyze import extract_quotes

            # 说"所有/全部"就按一年翻，别偷偷只翻 7 天
            if re.search(r"所有|全部|一直以来|之前|以前", text) and not dm:
                days = 365
            r = extract_quotes(name, topic, days=days, save=True)
            if not r.get("ok"):
                return "没挑成：%s" % (r.get("error") or "未知原因")
            return "「%s」里关于「%s」的原话（%d 条消息里翻出来的，语音 %d 条已转文字）\n\n%s" % (
                r["target"], topic, r["rows"], r["voices"], r["quotes"])

        from src.assistant.chat_summary import summarize_chat

        r = summarize_chat(name, days=days, save=True)
        if not r.get("ok"):
            return "总结没做成：%s" % (r.get("error") or "未知原因")
        return "和「%s」最近 %d 天：%d 条消息（语音 %d 条已转文字）\n\n%s" % (
            r["chat"], days, r["rows"], r["voices"], r["summary"])

    # ── 分析：必须有对象 ──────────────────────────────────────
    m = AN_CHAT_PAT.search(text)
    if m:
        name, _ = _split_name(m.group(1))
        if not name:
            return _need_target()
        rest = _tail(text, m, name)
        dm = DAYS_PAT.search(text)
        days = max(1, min(int(dm.group(1)) if dm else 7, 365))
        if EXTRACT_CUE.search(rest):
            topic = _clean_topic(rest, name)
            if not topic:
                return _need_topic(name)
            from src.assistant.analyze import extract_quotes

            # 说"所有/全部"就按一年翻，别偷偷只翻 7 天
            if re.search(r"所有|全部|一直以来|之前|以前", text) and not dm:
                days = 365
            r = extract_quotes(name, topic, days=days, save=True)
            if not r.get("ok"):
                return "没挑成：%s" % (r.get("error") or "未知原因")
            return "「%s」里关于「%s」的原话（%d 条消息，语音 %d 条已转文字）\n\n%s" % (
                r["target"], topic, r["rows"], r["voices"], r["quotes"])
        from src.assistant.analyze import analyze_chat

        r = analyze_chat(name, days=days, save=True)
        if not r.get("ok"):
            return "分析没做成：%s" % (r.get("error") or "未知原因")
        return "分析「%s」最近 %d 天（%d 条消息，语音 %d 条已转文字）\n\n%s" % (
            r["target"], days, r["rows"], r["voices"], r["analysis"])

    if AN_DIARY_PAT.search(text):
        dm = DAYS_PAT.search(text)
        days = max(1, min(int(dm.group(1)) if dm else 7, 365))
        from src.assistant.analyze import analyze_diary

        r = analyze_diary(days=days, save=True)
        if not r.get("ok"):
            return "分析没做成：%s" % (r.get("error") or "未知原因")
        return "分析最近 %d 天日记（%d 条）\n\n%s" % (days, r["entries"], r["analysis"])

    # 只说了"分析这个/分析一下"，没给对象：不猜，直接要对象
    if re.search(r"^\s*分析|分析(一下|下|这个|那个|这条|这篇|这些|刚才|分析)([。！？!?]?)$", text) and len(text) <= 30:
        return _need_target()
    return None


def _need_topic(name: str) -> str:
    return ("要挑原话得说清挑什么。比如："
            "「总结我和%s所有聊天里判断鸟品相好坏的内容，列出来给我看」" % name)


def _need_target() -> str:
    return ("分析得有对象，我不猜「这个」是哪个。三种说法我都能办：\n"
            "· 分析我和大姚最近7天\n"
            "· 分析最近7天的日记\n"
            "· 总结我和大姚最近7天（只要流水，不要结论）")
