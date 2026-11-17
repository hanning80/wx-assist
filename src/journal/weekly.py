"""周复盘 / 月复盘页面（阶段4 模块五）。

生成一个能直接双击打开的网页：这一周（或这一月）做了什么、哪些事在跑、
将来还排着什么、有多少提醒发出去/跳过/失败。

设计取向（照 Day One、flomo 那套"回顾"的做法，去掉打卡压力）：
  - 先给结论性概览，再给按日时间轴，原文保留、摘要在上
  - 不排名、不打分、不做连续天数
  - 空周也出页面，写清"这一周没有记录"，不留白
"""

from __future__ import annotations

import html
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from .store import JournalStore, now_iso

logger = logging.getLogger(__name__)

OUT_DIR = Path(__file__).resolve().parents[2] / "data" / "journal" / "review"

KIND_LABEL = {
    "voice": "语音",
    "text": "文字",
    "diary": "日记",
    "share": "分享",
    "attachment": "附件",
    "image": "图片",
    "video": "视频",
    "file": "文件",
    "link": "链接",
    "reminder": "提醒",
    "cancel": "取消",
    "answer": "回话",
    "chitchat": "闲话",
}


def week_range(day: Optional[date] = None) -> tuple[str, str]:
    """周一为起点的一周。"""
    day = day or date.today()
    monday = day - timedelta(days=day.weekday())
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def month_range(day: Optional[date] = None) -> tuple[str, str]:
    day = day or date.today()
    first = day.replace(day=1)
    if first.month == 12:
        nxt = first.replace(year=first.year + 1, month=1)
    else:
        nxt = first.replace(month=first.month + 1)
    return first.isoformat(), (nxt - timedelta(days=1)).isoformat()


def year_range(day: Optional[date] = None) -> tuple[str, str]:
    """自然年：1 月 1 日到 12 月 31 日。"""
    day = day or date.today()
    return date(day.year, 1, 1).isoformat(), date(day.year, 12, 31).isoformat()


def _cn_date(d: str) -> str:
    try:
        dt = date.fromisoformat(d)
        return "%d月%d日" % (dt.month, dt.day)
    except ValueError:
        return d


def _weekday_cn(d: str) -> str:
    names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    try:
        return names[date.fromisoformat(d).weekday()]
    except ValueError:
        return ""


def _esc(s: str) -> str:
    return html.escape(str(s or ""))


def build_html(store: JournalStore, start: str, end: str, title: str) -> str:
    entries = store.entries_between(start, end)
    stats = store.weekly_stats(start, end)
    running = [c for c in store.chains("active")]
    waiting = [c for c in store.chains("pending_time")]

    # 按日归堆
    by_day: dict[str, list] = {}
    for e in entries:
        by_day.setdefault(e["entry_date"], []).append(e)

    overview = [
        ("这一时段的记录", "%d 条" % stats["entries"]),
        ("提醒发出", "%d 次" % stats["tasks_sent"]),
        ("提醒跳过", "%d 次" % stats["tasks_skipped"]),
        ("正在跑的事", "%d 件" % len(running)),
    ]

    cards = "".join(
        '<div class="card"><div class="num">%s</div><div class="lab">%s</div></div>'
        % (_esc(v), _esc(k)) for k, v in overview
    )

    # 时间轴
    if by_day:
        blocks = []
        for d in sorted(by_day.keys()):
            items = []
            for e in by_day[d]:
                if not ((e["full_text"] or e["raw_text"] or "").strip()):
                    try:
                        from .cloud.body_store import hydrate_entry
                        e = hydrate_entry(store, e)
                    except Exception:
                        pass
                summary = e["summary"] or (e["full_text"] or "")[:24]
                full = e["full_text"] or ""
                tag = KIND_LABEL.get(e["kind"], e["kind"])
                review = '<span class="warn">待我复核</span>' if e["needs_review"] else ""
                items.append(
                    '<div class="item"><div class="head">%s <span class="tag">%s</span>%s</div>'
                    '<div class="body">%s</div>%s</div>'
                    % (_esc(summary), _esc(tag), review, _esc(full),
                       ('<div class="src">原文：%s</div>' % _esc(e["raw_text"])
                        if e["raw_text"] and e["raw_text"] != full else "")
                       )
                )

            blocks.append(
                '<div class="day"><div class="dayhead"><span class="d">%s</span>'
                '<span class="w">%s</span><span class="n">%d 条</span></div>%s</div>'
                % (_esc(_cn_date(d)), _esc(_weekday_cn(d)), len(by_day[d]), "".join(items))
            )
        timeline = "".join(blocks)
    else:
        timeline = ('<div class="empty">这一时段还没有任何记录。'
                    '发一条语音到文件传输助手，这里就会出现。</div>')

    # 正在跑的事
    if running:
        rows = []
        for c in running:
            done = store._rows(
                "SELECT COUNT(*) AS n FROM reminder_task WHERE chain_id=? AND status='sent'", (c["id"],)
            )[0]["n"]
            left = max(0, c["days"] - done)
            rows.append(
                '<tr><td>%s</td><td>%s 起 %d 天</td><td>每天 %s</td><td>已发 %d／还剩 %d</td></tr>'
                % (_esc(c["title"]), _esc(_cn_date(c["start_date"])), c["days"],
                   _esc(c["daily_time"] or "待定"), done, left)
            )
        future = ('<table><tr><th>事项</th><th>起止</th><th>提醒时刻</th><th>进度</th></tr>%s</table>'
                  % "".join(rows))
    else:
        future = '<div class="empty">眼下没有在跑的提醒。</div>'

    pending_note = ""
    if waiting:
        pending_note = ('<div class="note">还有 %d 件事在等你回提醒时刻：%s。</div>'
                        % (len(waiting), "、".join(_esc(c["title"]) for c in waiting)))

    topics = sorted({t for e in entries for t in (e["topics"] or "").replace("、", ",").split(",") if t.strip()})
    topic_html = ""
    if topics:
        topic_html = '<div class="chips">%s</div>' % "".join(
            '<span class="chip">%s</span>' % _esc(t) for t in topics[:24])

    return """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s</title>
<script>
(function () {
  function applyTheme(theme) {
    var dark = theme !== 'light';
    document.documentElement.classList.toggle('dark', dark);
    document.documentElement.classList.toggle('light', !dark);
  }
  var saved = '';
  try { saved = localStorage.getItem('theme') || ''; } catch (e) {}
  applyTheme(saved || (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'));
  window.addEventListener('storage', function (e) {
    if (e.key === 'theme' || e.key === null) {
      var next = '';
      try { next = localStorage.getItem('theme') || ''; } catch (err) {}
      applyTheme(next || 'dark');
    }
  });
  window.addEventListener('message', function (e) {
    if (e.data && e.data.type === 'journal-theme') applyTheme(e.data.theme);
  });
})();
</script>
<style>
:root{--ink:#20242c;--sub:#6b7280;--line:#e6e8ec;--bg:#f7f8fa;--brand:#2f6b4f}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
 font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif;line-height:1.7}
.wrap{max-width:880px;margin:0 auto;padding:32px 20px 60px}
h1{font-size:22px;margin:0 0 4px;font-weight:600}
.sub{color:var(--sub);font-size:13px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin:22px 0 8px}
.card{flex:1 1 150px;background:#fff;border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.card .num{font-size:20px;font-weight:600;color:var(--brand)}
.card .lab{color:var(--sub);font-size:12px;margin-top:2px}
h2{font-size:15px;margin:30px 0 12px;font-weight:600;padding-left:10px;border-left:3px solid var(--brand)}
.day{background:#fff;border:1px solid var(--line);border-radius:12px;margin-bottom:12px;overflow:hidden}
.dayhead{display:flex;gap:10px;align-items:baseline;padding:10px 16px;border-bottom:1px solid var(--line);background:#fcfcfd}
.dayhead .d{font-weight:600}
.dayhead .w{color:var(--sub);font-size:12px}
.dayhead .n{margin-left:auto;color:var(--sub);font-size:12px}
.item{padding:12px 16px;border-bottom:1px dashed var(--line)}
.item:last-child{border-bottom:none}
.item .head{font-weight:600}
.tag{font-size:11px;color:var(--brand);background:#eef5f1;border-radius:4px;padding:1px 6px;margin-left:6px}
.warn{font-size:11px;color:#a15c00;background:#fff4e2;border-radius:4px;padding:1px 6px;margin-left:6px}
.item .body{color:#3b4048;margin-top:4px}
.src{color:var(--sub);font-size:12px;margin-top:4px}
table{width:100%%;border-collapse:collapse;background:#fff;border:1px solid var(--line);border-radius:12px;overflow:hidden}
th,td{padding:10px 12px;text-align:left;font-size:13px;border-bottom:1px solid var(--line)}
th{background:#fcfcfd;color:var(--sub);font-weight:500}
tr:last-child td{border-bottom:none}
.empty{background:#fff;border:1px dashed var(--line);border-radius:12px;padding:22px;color:var(--sub);text-align:center}
.note{margin-top:12px;background:#fff8e8;border:1px solid #f0e2c4;border-radius:10px;padding:10px 14px;font-size:13px}
.chips{display:flex;gap:8px;flex-wrap:wrap}
.chip{background:#fff;border:1px solid var(--line);border-radius:20px;padding:3px 12px;font-size:12px;color:var(--sub)}
.foot{margin-top:36px;color:var(--sub);font-size:12px;text-align:center}
</style>
<style>
html.dark body{background:#0b0f0e!important;color:#e8ece9!important}
html.dark h1{color:#f3f7f4!important}html.dark h2{color:#dce8e2!important;border-left-color:#9be7c4!important}
html.dark .card,html.dark .day,html.dark table,html.dark .empty,html.dark .chip{background:#121817!important;border-color:#26312d!important}
html.dark .dayhead,html.dark th{background:#0f1513!important;border-color:#26312d!important}
html.dark .item,html.dark .dayhead,html.dark th,html.dark td{border-color:#26312d!important}
html.dark .item .head{color:#f0f5f2!important}html.dark .item .body{color:#b8c5bd!important}
html.dark .sub,html.dark .src,html.dark .foot,html.dark .dayhead .w,html.dark .dayhead .n,html.dark .card .lab,html.dark .chip{color:#8b9a93!important}
html.dark .card .num,html.dark .tag{color:#9be7c4!important}html.dark .tag{background:#173a2c!important}
html.dark .note{background:#2b2313!important;border-color:#5a4728!important;color:#f0d9a8!important}
html.dark a{color:#9be7c4!important}
</style></head><body><div class="wrap">
<h1>%s</h1>
<div class="sub">%s 至 %s · 生成于 %s</div>
<div class="cards">%s</div>
<h2>逐日记录</h2>
%s
<h2>还在跑的事</h2>
%s
%s
<h2>这段日子的话题</h2>
%s
<div class="foot">摘星 · 语音日记与提醒</div>
</div></body></html>""" % (
        _esc(title), _esc(title), _esc(start), _esc(end),
        _esc(now_iso()[:16].replace("T", " ")),
        cards, timeline, future, pending_note, topic_html,
    )


def generate(store: JournalStore, period: str = "week", day: Optional[date] = None,
             out_dir: Optional[Path] = None) -> dict:
    """生成周报、月报或年报，返回 {path, start, end, title}。"""
    out_dir = out_dir or OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    if period == "week":
        start, end = week_range(day)
        title = "%s 至 %s 这一周" % (_cn_date(start), _cn_date(end))
        tag = "周"
        fname = "周复盘-%s至%s.html" % (start, end)
    elif period == "year":
        start, end = year_range(day)
        title = "%d年" % date.fromisoformat(start).year
        tag = "年"
        fname = "年复盘-%s.html" % start[:4]
    else:
        start, end = month_range(day)
        title = "%d年%d月" % (date.fromisoformat(start).year, date.fromisoformat(start).month)
        tag = "月"
        fname = "月复盘-%s.html" % start[:7]

    body = build_html(store, start, end, title)
    path = out_dir / fname
    path.write_text(body, encoding="utf-8")

    stats = store.weekly_stats(start, end)
    narrative = "%s%s：记录 %d 条，提醒发出 %d 次。" % (tag, title, stats["entries"], stats["tasks_sent"])
    store.upsert_weekly(start, end, str(path), stats, narrative)
    logger.info("已生成%s复盘：%s", tag, path)
    return {"path": str(path), "start": start, "end": end, "title": title, "stats": stats}
