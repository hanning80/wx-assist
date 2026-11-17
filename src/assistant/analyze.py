"""分析：把已有材料（和某人的聊天、自己的日记）读成结论，而不是空口说。

口径（硬规则）：
- 只写材料里出现的内容，每条结论要能指回某句原话；
- 不推断动机、不编造数字与承诺；
- 材料不足就写"信息不足"，不许拿常识补；
- 不写"建议保持沟通"这类空话，下一步要能当天动手。
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Optional

from src.assistant.chat_summary import _llm, collect, resolve_chat

PROMPT_CHAT = """你是分析助手。材料是"我"和「{name}」最近 {days} 天的聊天流水（{start} 至 {end}）。
只根据这份材料写，不要用常识补，不要推断对方动机，不要编造材料里没有的数字、日期、承诺。

按下面五段输出，用中文，不要 markdown 表格：

一、这段时间到底发生了什么（按"事"归并，不按消息条数）
二、反复出现的信号（同一件事/同一个诉求出现几次，各指哪句原话）
三、风险与隐患（只写材料支持的，没支持就不写）
四、可执行的下一步（每条要具体到当天能动手，写清找谁、做什么）
五、信息不足的地方（明确写"材料里没说"，不要猜）

流水如下：
"""

PROMPT_DIARY = """你是分析助手。材料是"我"最近 {days} 天的日记（{start} 至 {end}），每条带日期。
只根据这些日记写，不要补充日记之外的经历，不要编造数字和结论。

按下面五段输出，用中文，不要 markdown 表格：

一、这段时间发生了哪些事（按事归并）
二、反复出现的信号（同一问题/同一件事出现几次，各指哪天哪句）
三、风险与隐患（只写日记支持的）
四、可执行的下一步（具体到当天能动手）
五、信息不足的地方（日记没写的，明说"没记"）

日记如下：
"""


PROMPT_QUOTES = """下面这段是"我"和「{name}」的聊天流水的一部分（第 {idx}/{total} 段）。
任务：只把跟「{topic}」有关的原话挑出来，逐条原样摘录。
{scope}
硬规矩：
1. 每条形如 "日期 时间 说话人：原话"，原话必须是这段材料里连续的一段，不许改写、不许拼接两句、不许换词；
2. 不许总结成观点，不许加评价、不许补材料里没有的内容；
3. 按时间顺序；一句里既有别的事也有本主题，只截本主题那一截，要读得通；
4. 同一主题的不同说法也要收：正面夸、反面嫌、比较两只、问"行不行/咋样/值不值"、说具体部位（毛色、颜色、条子、身条、眼、嘴、腿、爪、成色、体型）都算，宁可多收不要漏；
5. 这一段没有相关原话，就只回一句"无"；
6. 有几条列几条，不要自己设上限，不要为了让条数好看而凑。

流水如下：
"""

# 主题口径：有些词在这行有固定含义，不写清模型就会把"病"当"品相"
SCOPES = (
    (("品相", "成色", "卖相"),
     "主题口径：品相说的是按品种看外观卖相——颜色花纹、体型条子、毛、眼、嘴、腿、爪、尾、整体周正，"
     "以及值不值这个价。生病（闹眼、毛滴虫、单眼伤风）、用药、死亡、运输打包、喂养方式都不算品相，"
     "只有落到「这鸟外观好不好、能不能卖这个价」的才收。\n"),
    (("品种",),
     "主题口径：品种说的是桂皮、山东黄、雪花、卷毛、红袍、辣椒红、黄小格、马赛克、白红眼、豆绿、丝塔芙、"
     "黄袍、蜡黄这类叫法以及它们怎么分、怎么配、哪个品种什么行情。\n"),
)


def _scope(topic: str) -> str:
    for keys, text in SCOPES:
        if any(k in topic for k in keys):
            return text
    return ""



def _chunks(rows: list, size: int) -> list:
    return [rows[i:i + size] for i in range(0, len(rows), size)]


def extract_quotes(name: str, topic: str, days: int = 365, limit: int = 3000,
                   transcribe: bool = True, max_voices: int = 40,
                   batch: int = 50, max_batches: int = 12,
                   save: bool = True) -> dict:
    """从"我和某人"的聊天里把关于某主题的原话逐条摘出来。

    分段扫，不是一次塞给模型：一段 50 条，最多 12 段（600 条），
    免得材料一长模型就只挑一两条就收工。
    """
    target = resolve_chat(name)
    if not target:
        return {"ok": False, "error": "没找到这个联系人/会话：%s" % name}

    data = collect(target["chat_id"], days=days, limit=limit,
                   transcribe=transcribe, max_voices=max_voices)
    rows = data["rows"]
    if not rows:
        return {"ok": False, "error": "这段时间没有可读的消息（%s）" % target["name"]}

    start, end = rows[0]["time"], rows[-1]["time"]
    parts = _chunks(rows, batch)
    skipped_batches = 0
    if len(parts) > max_batches:
        skipped_batches = len(parts) - max_batches
        parts = parts[-max_batches:]

    picked, t0 = [], time.time()
    for i, part in enumerate(parts, 1):
        body = "\n".join("%s %s：%s" % (r["time"], r["who"], r["text"]) for r in part)
        prompt = (PROMPT_QUOTES.format(name=target["name"], topic=topic, scope=_scope(topic),
                                       idx=i, total=len(parts)) + body)
        try:
            out = _llm(prompt).strip()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": "挑原话失败（第 %d 段）：%s" % (i, e)}
        if not out or out.strip("。 ") == "无" or "没有相关" in out:
            continue
        picked.append(out)

    text = "\n".join(picked).strip() or "材料里没有相关内容"
    note = ""
    if skipped_batches:
        note = "\n（只翻了最近 %d 段，前面的 %d 段没翻）" % (len(parts), skipped_batches)
    result = {"ok": True, "target": target["name"], "rows": len(rows),
              "voices": data["voices"], "seconds": round(time.time() - t0, 1),
              "quotes": text + note}
    if save:
        result["path"] = _save("原话", "%s-%s" % (target["name"], topic[:12]),
                               "「%s」里关于「%s」的原话（%s 至 %s）" % (
                                   target["name"], topic, start, end), text + note)
    return result


def _save(kind: str, tag: str, head: str, body: str) -> str:
    out_dir = _repo_root() / "data" / "journal" / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / ("分析-%s-%s-%s.md" % (kind, tag.replace("/", "_"),
                                          datetime.now().strftime("%Y%m%d-%H%M")))
    path.write_text("## %s\n\n%s\n" % (head, body), encoding="utf-8")
    return str(path)


def _repo_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[2]


def analyze_chat(name: str, days: int = 7, limit: int = 400,
                 transcribe: bool = True, max_voices: int = 40,
                 save: bool = True) -> dict:
    """分析"我和某人最近 N 天聊的事"。"""
    target = resolve_chat(name)
    if not target:
        return {"ok": False, "error": "没找到这个联系人/会话：%s" % name}

    data = collect(target["chat_id"], days=days, limit=limit,
                   transcribe=transcribe, max_voices=max_voices)
    rows = data["rows"]
    if not rows:
        return {"ok": False, "error": "这段时间没有可读的消息（%s）" % target["name"]}

    start, end = rows[0]["time"], rows[-1]["time"]
    body = "\n".join("%s %s：%s" % (r["time"], r["who"], r["text"]) for r in rows)
    if len(body) > 12000:
        body = body[-12000:]

    prompt = PROMPT_CHAT.format(name=target["name"], days=days, start=start, end=end) + body
    t0 = time.time()
    try:
        out = _llm(prompt).strip()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "分析失败：%s" % e}

    result = {"ok": True, "target": target["name"], "rows": len(rows),
              "voices": data["voices"], "seconds": round(time.time() - t0, 1),
              "analysis": out}
    if save:
        result["path"] = _save("聊天", target["name"],
                               "与 %s 的聊天分析（%s 至 %s）" % (target["name"], start, end), out)
    return result


def analyze_diary(days: int = 7, save: bool = True) -> dict:
    """分析"我最近 N 天的日记"。"""
    from src.journal.store import JournalStore

    store = JournalStore()
    try:
        end = date.today()
        start = end - timedelta(days=days - 1)
        rows = store._rows(
            "SELECT entry_date, kind, summary, full_text, topics FROM diary_entry "
            "WHERE deleted=0 AND entry_date>=? AND entry_date<=? ORDER BY entry_date, id",
            (start.isoformat(), end.isoformat()))
    finally:
        pass
    if not rows:
        store.close()
        return {"ok": False, "error": "最近 %d 天没有日记" % days}

    lines = []
    for r in rows:
        r = dict(r)
        text = (r.get("full_text") or r.get("summary") or "").strip()
        topics = r.get("topics") or ""
        lines.append("%s（%s）%s%s" % (r["entry_date"], r["kind"], text,
                                      (" 话题：" + topics) if topics else ""))
    body = "\n".join(lines)
    store.close()

    prompt = PROMPT_DIARY.format(days=days, start=start.isoformat(),
                                 end=end.isoformat()) + body
    t0 = time.time()
    try:
        out = _llm(prompt).strip()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "分析失败：%s" % e}

    result = {"ok": True, "entries": len(rows), "seconds": round(time.time() - t0, 1),
              "analysis": out}
    if save:
        result["path"] = _save("日记", "%d天" % days,
                               "日记分析（%s 至 %s，共 %d 条）" % (
                                   start.isoformat(), end.isoformat(), len(rows)), out)
    return result
