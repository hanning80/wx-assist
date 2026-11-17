"""网页入口：把周复盘 / 月复盘 / 日记列表接到现有面板上（阶段4 模块六的展示侧）。

三条地址：
  /journal            总览：本周、本月、历史复盘文件、最近日记、在跑的提醒
  /journal/now        现场生成本周这一份，直接显示
  /journal/file?name= 打开已生成的复盘网页

刻意不做的：不做搜索、不做编辑、不做排名打分。复盘页就是"看这一周做了什么、
后面还要做什么"，一眼看完就关掉。
"""

from __future__ import annotations

import html
import logging
import mimetypes
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, quote, urlparse

from .store import JournalStore
from .weekly import OUT_DIR, generate, month_range, week_range, year_range
from . import answer_rule

logger = logging.getLogger(__name__)

# 是否在日记总览里显示“历史复盘”文件列表。
# 关闭时只是隐藏入口，复盘文件仍保留在本地，不影响已生成的周/月/年复盘。
SHOW_REVIEW_HISTORY = False

INDEX_HEAD = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>日记与复盘</title>
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
  window.__applyJournalTheme = applyTheme;
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
:root{--bg:#f5f8f6;--card:#ffffff;--line:#dde5e0;--ink:#1f2a25;--muted:#68766f;--brand:#2f6b4f;--brand-soft:#286b4b;--head:#14211b;--btn-ink:#ffffff;--input-bg:#ffffff;--input-line:#cbd8d1;--tag-bg:#e9f5ef;--tag-line:#c7e1d4;--shadow:0 10px 24px rgba(20,40,30,.08)}
html.dark{--bg:#0b0f0e;--card:#121817;--line:#26312d;--ink:#e8ece9;--muted:#8b9a93;--brand:#2f6b4f;--brand-soft:#9be7c4;--head:#f3f7f4;--btn-ink:#effff7;--input-bg:#0f1513;--input-line:#34413b;--tag-bg:#173a2c;--tag-line:#24503c;--shadow:0 10px 24px rgba(0,0,0,.18)}
*{box-sizing:border-box}
body{margin:0;padding:22px 20px 60px;font:15px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
 background:var(--bg);color:var(--ink);transition:background .18s ease,color .18s ease}
.wrap{max-width:980px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px;color:var(--head);font-weight:650}
h2{font-size:14px;margin:26px 0 10px;color:var(--muted);font-weight:600;padding-left:10px;border-left:3px solid var(--brand)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;margin:10px 0;
 box-shadow:var(--shadow)}
a{color:var(--brand-soft);text-decoration:none}a:hover{text-decoration:underline}
.muted{color:var(--muted);font-size:13px}
.btn{display:inline-block;background:var(--brand);color:var(--btn-ink);border-radius:9px;padding:8px 14px;margin-right:8px;
 font-size:14px;border:1px solid transparent}
.btn.ghost{background:transparent;border:1px solid var(--line);color:var(--brand-soft)}
.btn.danger{background:#b42318;color:#fff;padding:5px 11px;font-size:13px}
button.btn{cursor:pointer;font:inherit}
textarea{width:100%;box-sizing:border-box;margin:8px 0;padding:9px 10px;border:1px solid var(--input-line);border-radius:9px;
 font:14px/1.6 inherit;background:var(--input-bg);color:var(--ink);resize:vertical}
input:not([type=checkbox]):not([type=hidden]){box-sizing:border-box;padding:7px 9px;border:1px solid var(--input-line);
 border-radius:8px;font:14px/1.4 inherit;background:var(--input-bg);color:var(--ink)}
label{display:flex;align-items:center;gap:8px;font-weight:600}
.row{display:flex;justify-content:space-between;align-items:center;gap:12px}
.list{display:flex;flex-direction:column;gap:10px}
.entry{display:flex;align-items:flex-start;gap:12px;background:var(--card);border:1px solid var(--line);
 border-radius:14px;padding:12px 14px;box-shadow:var(--shadow)}
.entry .summary{font-weight:600;color:var(--head)}
.entry .attach{margin-top:7px;font-size:13px}
.entry .attach a{display:inline-block;margin-right:10px;padding:2px 8px;border:1px solid var(--tag-line);
 border-radius:7px;background:var(--tag-bg)}
.entry .tags{margin-top:5px}
.tag{display:inline-block;background:var(--tag-bg);color:var(--brand-soft);border:1px solid var(--tag-line);border-radius:20px;
 padding:2px 9px;font-size:12px;margin:4px 6px 0 0}
</style></head><body><div class="wrap">"""


STATUS_CN = {"active": "进行中", "pending_time": "等你说时刻",
             "finished": "已完成", "cancelled": "已取消"}


def _esc(s) -> str:
    return html.escape(str(s or ""), quote=True)


def _status_cn(s) -> str:
    return STATUS_CN.get(str(s or ""), str(s or ""))


def _review_files() -> list[Path]:
    if not OUT_DIR.exists():
        return []
    files = sorted(OUT_DIR.glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:40]


def render_index(store: Optional[JournalStore] = None, err: str = "",
                 notice: str = "") -> str:
    store = store or JournalStore()
    today = date.today()
    w_start, w_end = week_range(today)
    m_start, m_end = month_range(today)
    y_start, y_end = year_range(today)

    entries = store._rows(
        "SELECT id, entry_date, kind, summary, topics FROM diary_entry"
        " WHERE deleted=0 ORDER BY entry_date DESC, id DESC LIMIT 12"
    )
    chains = [c for c in store.chains() if c["status"] in ("active", "pending_time")]
    w_stats = store.weekly_stats(w_start, w_end)
    m_stats = store.weekly_stats(m_start, m_end)

    parts = [INDEX_HEAD]
    parts.append("<h1>日记与复盘</h1><div class='muted'>每周一自动生成，也可以随时点开重看。</div>")
    parts.append("<div style='margin:8px 0 14px'><a class='btn ghost' href='/journal/media'>媒体库</a></div>")
    if notice:
        parts.append("<div class='card' style='border-color:var(--brand);color:var(--brand-soft)'>%s</div>"
                     % _esc(notice))
    if err:
        parts.append("<div class='card' style='border-color:#dc2626;color:#dc2626'>%s</div>" % _esc(err))

    # 长期回答规则：后台可见、可改、可关；和日记完全分开存。
    rule = answer_rule.get_rule(store)
    checked = " checked" if int(rule.get("enabled") or 0) else ""
    rule_text = rule.get("rule_text") or answer_rule.DEFAULT_RULE_TEXT
    updated = rule.get("updated_at") or "还没手动改过，使用默认口径"
    parts.append(
        "<h2>回答规则</h2><div class='card'>"
        "<div class='muted' style='margin-bottom:10px'>只在你回答用到“我以前记过/发过的内容”时引用原话，"
        "不是每条回答都硬引；天气、闲聊这类没有原文的问题不硬引。</div>"
        "<form method='post' action='/journal/rule'>"
        "<label><input type='checkbox' name='enabled' value='1'%s> 开启“回答引用原话”</label>"
        "<textarea name='rule_text' rows='4' maxlength='500'>%s</textarea>"
        "<div class='muted' style='margin-bottom:10px'>当前状态：%s · %s</div>"
        "<button class='btn' type='submit'>保存规则</button>"
        "</form></div>"
        % (checked, _esc(rule_text),
           "已开启" if checked else "已关闭", _esc(updated))
    )

    parts.append("<h2>本周与本月</h2>")

    week_url = "/journal/file?name=" + _esc(_week_fname(w_start, w_end))
    week_card = (
        "<div class='card'><div class='row'><div><b>%s 至 %s 这一周</b>"
        "<div class='muted'>记录 %d 条 · 发出提醒 %d 次 · 在跑 %d 件</div></div>"
        "<div><a class='btn' href='%s'>看本周</a></div></div></div>"
    ) % (_esc(w_start), _esc(w_end), w_stats["entries"], w_stats["tasks_sent"], len(chains), week_url)
    parts.append(week_card)

    month_url = "/journal/file?name=" + _esc(_month_fname(m_start))
    month_card = (
        "<div class='card'><div class='row'><div><b>%d年%d月</b>"
        "<div class='muted'>记录 %d 条 · 发出提醒 %d 次</div></div>"
        "<div><a class='btn ghost' href='%s'>看本月</a></div></div></div>"
    ) % (date.fromisoformat(m_start).year, date.fromisoformat(m_start).month,
         m_stats["entries"], m_stats["tasks_sent"], month_url)
    parts.append(month_card)

    year_url = "/journal/file?name=" + _esc("年复盘-%s.html" % y_start[:4])
    y_stats = store.weekly_stats(y_start, y_end)
    year_card = (
        "<div class='card'><div class='row'><div><b>%d年</b>"
        "<div class='muted'>记录 %d 条 · 发出提醒 %d 次</div></div>"
        "<div><a class='btn ghost' href='%s'>看本年</a></div></div></div>"
    ) % (date.fromisoformat(y_start).year, y_stats["entries"], y_stats["tasks_sent"], year_url)
    parts.append(year_card)

    # 历史复盘列表先隐藏：生成的复盘文件仍保留在本地，需要时可重新打开。
    # 要恢复入口，把下面这个开关改成 True 即可。
    if SHOW_REVIEW_HISTORY:
        parts.append("<h2>历史复盘</h2><div class='card'>")
        files = _review_files()
        if not files:
            parts.append("<div class='muted'>还没有生成过。点上面那个按钮生成一份。</div>")
        for f in files:
            stamp = datetime.fromtimestamp(f.stat().st_mtime).strftime("%m-%d %H:%M")
            parts.append(
                "<div class='row'><a href='/journal/file?name=%s'>%s</a>"
                "<span class='muted'>%s</span></div>" % (_esc(f.name), _esc(f.stem), _esc(stamp))
            )
        parts.append("</div>")

    parts.append("<h2>最近的日记</h2>")
    if not entries:
        parts.append("<div class='card'><div class='muted'>还没有记录。往文件传输助手发一条语音就行。</div></div>")
    else:
        parts.append("<div class='list'>")
        for e in entries:
            tags = "".join("<span class='tag'>%s</span>" % _esc(t) for t in (e["topics"] or "").split("、") if t)
            atts = store.attachment_rows(int(e["id"]))
            att_html = ""
            if atts:
                links = []
                for a in atts:
                    name = a["filename"] or "附件"
                    links.append(
                        "<a href='/journal/attachment?id=%d' target='_blank' rel='noopener'>%s</a>"
                        % (int(a["id"]), _esc(name))
                    )
                att_html = "<div class='attach'>附件：%s <span class='muted'>（点文件名查看）</span></div>" % " ".join(links)
            parts.append(
                "<div class='entry'>"
                "<div style='flex:1;min-width:0'><span class='muted'>%s</span> "
                "<span class='summary'>%s</span><div class='tags'>%s</div>%s%s</div>"
                "<form method='post' action='/journal/delete'"
                " onsubmit=\"return confirm('彻底删除会同时删掉这条日记、来源记录、关联附件记录、提醒、本地暂存文件和微云原件；删除后这条微信消息不会自动重新入库。确定彻底删除？')\">"
                "<input type='hidden' name='entry_id' value='%d'>"
                "<button class='btn danger' type='submit'>彻底删除</button>"
                "</form></div>"
                % (_esc(e["entry_date"]), _esc(e["summary"]), tags,
                   _voice_badge(e["kind"]), att_html, int(e["id"]))
            )
        parts.append("</div>")

    parts.append("<h2>生活提醒（在跑）</h2><div class='card'>"
                 "<div class='muted' style='margin-bottom:10px'>这里只管生活提醒（喂鸟、喂食、日常事项）。"
                 "群摘要、榜单、自动推送在侧边栏“采集与维护”，不是这里。</div>")
    if not chains:
        parts.append("<div class='muted'>现在没有在跑的生活提醒。</div>")
    for c in chains:
        when = (c["daily_time"] or "").replace(",", "、")
        parts.append(
            "<div class='row' style='align-items:flex-start;gap:16px;padding:12px 0;"
            "border-bottom:1px solid rgba(127,127,127,.18)'>"
            "<div style='flex:1;min-width:0'><div><b>%s</b> "
            "<span class='muted'>%s 起 %d 天 · %s · %s</span></div>"
            "<form method='post' action='/journal/reminder' style='margin-top:8px;"
            "display:flex;flex-wrap:wrap;gap:8px;align-items:center'>"
            "<input type='hidden' name='chain_id' value='%d'>"
            "<label class='muted'>内容 <input name='title' value='%s' style='width:150px'></label>"
            "<label class='muted'>起始 <input type='date' name='start_date' value='%s'></label>"
            "<label class='muted'>天数 <input type='number' min='1' max='366' name='days' value='%d' style='width:70px'></label>"
            "<label class='muted'>每天时刻 <input name='daily_time' value='%s' placeholder='08:00,17:00' style='width:150px'></label>"
            "<button class='btn' type='submit'>保存修改</button>"
            "</form><div class='muted' style='margin-top:4px'>改完从今天起按新计划发，不补发过去。</div>"
            "</div></div>"
            % (_esc(c["title"]), _esc(c["start_date"]), c["days"], _esc(when or "时刻待定"),
               _esc(_status_cn(c["status"])), int(c["id"]), _esc(c["title"]),
               _esc(c["start_date"]), c["days"], _esc(c["daily_time"] or ""))
        )
    parts.append("</div>")
    parts.append("</div></body></html>")
    return "".join(parts)


def _voice_badge(kind: str) -> str:
    """语音类日记：说明原始音频按清理规则处理，不长期保留。"""
    if kind != "voice":
        return ""
    return "<div class='muted' style='margin-top:4px'>语音已转成文字；原始音频按清理规则处理，不长期保留</div>"


def _week_fname(start: str, end: str) -> str:
    return "周复盘-%s至%s.html" % (start, end)


def _month_fname(start: str) -> str:
    return "月复盘-%s.html" % start[:7]


def render_now(period: str = "week", store: Optional[JournalStore] = None) -> str:
    """现场生成一份并返回其 HTML（同时落盘，历史里也能找到）。"""
    store = store or JournalStore()
    r = generate(store, period, date.today())
    return Path(r["path"]).read_text(encoding="utf-8")


def read_review(name: str) -> Optional[str]:
    """按文件名取复盘页；文件不在就先按文件名推算区间现场生成。

    只允许 OUT_DIR 里的 .html，防越权读取。
    """
    if not name:
        return None
    safe = Path(name).name
    if not safe.endswith(".html"):
        return None
    p = OUT_DIR / safe
    if p.exists() and p.parent == OUT_DIR:
        return p.read_text(encoding="utf-8")

    # 点名要看某一份但还没生成：按文件名里的日期现场补一份
    m = re.match(r"周复盘-(\d{4}-\d{2}-\d{2})至", safe)
    if m:
        r = generate(JournalStore(), "week", date.fromisoformat(m.group(1)))
        return Path(r["path"]).read_text(encoding="utf-8")
    m = re.match(r"月复盘-(\d{4}-\d{2})\.html$", safe)
    if m:
        r = generate(JournalStore(), "month", date.fromisoformat(m.group(1) + "-01"))
        return Path(r["path"]).read_text(encoding="utf-8")
    return None


def _write_html(handler, body: str, code: int = 200) -> None:
    handler.send_response(code)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body.encode("utf-8"))))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body.encode("utf-8"))


def _redirect(handler, location: str) -> None:
    handler.send_response(303)
    handler.send_header("Location", location)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def _read_form(handler) -> dict:
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except ValueError:
        length = 0
    if length <= 0 or length > 10000:
        return {}
    body = handler.rfile.read(length).decode("utf-8", "ignore")
    return parse_qs(body)


def _purge_local_original(local_path: str) -> bool:
    """只删除附件暂存目录内的本地原件，防止误删其他文件。"""
    if not local_path:
        return False
    try:
        from . import kb
        root = Path(kb.ATTACH_ROOT).resolve()
        path = Path(local_path).resolve()
        if root not in path.parents:
            logger.warning("拒绝删除附件目录外的路径：%s", local_path)
            return False
        if not path.is_file():
            return False
        try:
            os.chmod(str(path), 0o666)
        except OSError:
            pass
        os.remove(str(path))
        return True
    except OSError as e:
        logger.warning("本地附件没删掉：%s（%s）", local_path, e)
        return False


def _purge_entry_originals(store: JournalStore, info: dict) -> list[str]:
    """删除这条日记的本地暂存文件和微云原件；返回失败说明。"""
    errors: list[str] = []

    for att in info.get("attachments") or []:
        local_path = att.get("local_path") or ""
        if local_path and _purge_local_original(local_path):
            logger.info("彻底删除：已删本地附件 %s", att.get("filename") or local_path)

    cloud_objects = info.get("cloud_objects") or []
    if not cloud_objects:
        return errors

    try:
        from .cloud import cloud_enabled
        if not cloud_enabled():
            errors.append("微云未配置，云端原件没有删除")
            return errors
        from .cloud.storage import CloudStore
        cs = CloudStore(store=store)
    except Exception as e:  # noqa: BLE001
        logger.warning("彻底删除：微云初始化失败 %s", e)
        errors.append("微云初始化失败：%s" % e)
        return errors

    for obj in cloud_objects:
        # 上传成功后本地暂存会被清掉，但云端对象还在；不能把 purged
        # 当成“云端已删除”而跳过，否则彻底删除会漏掉微云原件。
        key = obj.get("object_key") or ""
        pdir = obj.get("pdir_key") or ""
        name = obj.get("name") or key
        if not key:
            store.mark_cloud_purged(int(obj["id"]))
            continue
        try:
            cs.client.delete(key, pdir, complete=True)
            store.mark_cloud_purged(int(obj["id"]))
            logger.info("彻底删除：已删微云原件 %s", name)
        except Exception as e:  # noqa: BLE001
            logger.warning("彻底删除：微云原件没删掉 %s（%s）", name, e)
            errors.append("微云原件没删掉：%s（%s）" % (name, e))

    return errors


def _refresh_reviews(store: JournalStore, days: list[date]) -> None:
    """删除后自动刷新周/月/年复盘；同一天只生成一次。"""
    seen = set()
    for day in days:
        if day in seen:
            continue
        seen.add(day)
        for period in ("week", "month", "year"):
            generate(store, period, day)


def _serve_local_attachment(handler, path: Path, filename: str) -> None:
    """把附件目录里的图片/视频直接交给浏览器打开。"""
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    size = path.stat().st_size
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(size))
    handler.send_header("Content-Disposition", "inline; filename*=UTF-8''%s" % quote(filename))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            handler.wfile.write(chunk)


def _serve_attachment(handler, store: JournalStore, attachment_id: int) -> None:
    """优先读本地暂存；本地已清理时从微云取得临时下载地址并转发。"""
    rows = store._rows(
        "SELECT * FROM attachment WHERE id=?", (attachment_id,))
    if not rows:
        _write_html(handler, "<h1>没找到这个附件</h1><p><a href='/journal'>回到日记</a></p>", 404)
        return
    att = rows[0]
    filename = att["filename"] or "附件"

    local_path = att["local_path"] or ""
    if local_path:
        try:
            from . import kb
            root = Path(kb.ATTACH_ROOT).resolve()
            path = Path(local_path).resolve()
            if root in path.parents and path.is_file():
                _serve_local_attachment(handler, path, filename)
                return
        except OSError:
            logger.exception("读取本地附件失败：%s", local_path)

    # 本地暂存已按规则清掉，从微云取回。
    try:
        from .cloud import cloud_enabled
        if not cloud_enabled():
            raise RuntimeError("微云未配置")
        from .cloud.storage import CloudStore
        cs = CloudStore(store=store)
        entry_id = int(att["entry_id"] or 0)
        cloud_name = att["cloud_name"] or ""
        objs = store._rows(
            "SELECT * FROM cloud_object WHERE entry_id=? AND kind='attachment'"
            " AND (name=? OR name LIKE ?) ORDER BY id DESC",
            (entry_id, cloud_name, "%" + cloud_name),
        )
        if not objs:
            objs = store._rows(
                "SELECT * FROM cloud_object WHERE entry_id=? AND kind='attachment'"
                " ORDER BY id DESC LIMIT 1", (entry_id,))
        if not objs:
            raise RuntimeError("微云里没有这条附件")
        rec = cs.client.download_url(objs[0]["object_key"], objs[0]["pdir_key"])
        url = rec.get("https_download_url") or rec.get("download_url") or rec.get("url")
        if not url:
            raise RuntimeError("微云没有返回下载地址")
        headers = {}
        if rec.get("cookie"):
            headers["Cookie"] = rec["cookie"]
        import requests
        upstream = requests.get(url, headers=headers, stream=True, timeout=30)
        if upstream.status_code != 200:
            raise RuntimeError("微云下载失败（%s）" % upstream.status_code)
        content_type = upstream.headers.get("Content-Type") or mimetypes.guess_type(filename)[0] \
            or "application/octet-stream"
        length = upstream.headers.get("Content-Length")
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        if length:
            handler.send_header("Content-Length", str(length))
        handler.send_header("Content-Disposition", "inline; filename*=UTF-8''%s" % quote(filename))
        handler.send_header("Cache-Control", "no-store")
        handler.end_headers()
        for chunk in upstream.iter_content(1 << 20):
            if chunk:
                handler.wfile.write(chunk)
        upstream.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("读取附件失败：%s（%s）", filename, e)
        _write_html(
            handler,
            "<h1>这个附件暂时取不到</h1><p>%s</p><p><a href='/journal'>回到日记</a></p>"
            % _esc(str(e)),
            404,
        )


def render_media(store: Optional[JournalStore] = None) -> str:
    """媒体库：图上只显示缩略图，原文/原视频走短时签名链接。"""
    store = store or JournalStore()
    try:
        from .media import media_item
        from .cloud.provider import make_client
        client = make_client()
    except Exception:
        media_item = None
        client = None
    parts = [INDEX_HEAD]
    parts.append("<h1>媒体库</h1><div class='muted'>图片和视频原件只存在云端；"
                 "这里显示缩略图和识别/转写出来的文字，点开可看原件。</div>")
    parts.append("<div style='margin:10px 0'><a class='btn ghost' href='/journal'>← 回到日记</a>"
                 "<button class='btn ghost' type='button' onclick=\"fetch('/api/journal/media/scan',"
                 "{method:'POST'}).then(function(){location.reload()})\">补扫待处理媒体</button></div>")
    parts.append("<div style='margin:-4px 0 12px'><button class='btn ghost' type='button' "
                 "onclick=\"fetch('/api/journal/media/favorites',{method:'POST'})"
                 ".then(function(){location.reload()})\">补扫普通收藏图片（视频号/朋友圈不做）</button></div>")
    parts.append("<div style='margin:-4px 0 12px'><button class='btn ghost' type='button' "
                 "onclick=\"fetch('/api/journal/media/chat',{method:'POST'})"
                 ".then(function(){location.reload()})\">补扫聊天图片/视频</button></div>")
    counts = store.media_counts()
    parts.append("<div class='muted' style='margin:8px 0'>图片 %d 张 · 视频 %d 个 · 合计 %.1f MB</div>"
                 % (int(counts.get("image", 0)), int(counts.get("video", 0)),
                    float(counts.get("bytes", 0)) / 1048576.0))
    if not media_item:
        parts.append("<div class='card'>云端暂时没连上，媒体列表不可用。</div>")
    else:
        rows = store.list_media_assets(limit=200)
        if not rows:
            parts.append("<div class='card'>还没有媒体资产。往文件传输助手发一张图或一段视频，"
                         "系统会自动上云。</div>")
        else:
            parts.append("<div style='display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px'>")
            for r in rows:
                d = media_item(store, r, client=client)
                kind = "视频" if d.get("kind") == "video" else "图片"
                title = _esc(d.get("caption") or d.get("asset_id") or "")
                preview = _esc(d.get("preview_proxy_url") or d.get("display_url") or "")
                original = _esc(d.get("original_proxy_url") or d.get("original_url") or preview)
                text = (d.get("asr_text") if d.get("kind") == "video" else d.get("ocr_text")) or ""
                text = _esc(text[:220])
                if d.get("kind") == "video" and preview:
                    visual = ("<video src='%s' poster='%s' controls preload='metadata' "
                              "style='width:100%%;border-radius:10px;background:#000'></video>"
                              % (original, preview))
                elif preview:
                    visual = ("<a href='%s' target='_blank' rel='noopener'>"
                              "<img src='%s' alt='%s' loading='eager' "
                              "style='width:100%%;height:150px;object-fit:cover;border-radius:10px'></a>"
                              % (original, preview, title))
                else:
                    visual = ("<div style='height:150px;display:flex;align-items:center;"
                              "justify-content:center;background:var(--tag-bg);border-radius:10px'>"
                              "云端链接暂时不可用</div>")
                parts.append("<div class='card' style='padding:10px'>%s"
                             "<div style='margin-top:6px;font-weight:600'>%s</div>"
                             "<div class='muted'>%s · %s</div>%s</div>"
                             % (visual, title, kind,
                                _esc((d.get("created_at") or "")[:10]),
                                ("<div class='muted' style='margin-top:6px'>%s</div>" % text) if text else ""))
            parts.append("</div>")
    parts.append("</div></body></html>")
    return "".join(parts)


def handle(handler) -> bool:
    """在网页服务的请求分发里调用：是 /journal 开头的就处理，返回 True。"""
    parsed = urlparse(handler.path)
    if not parsed.path.startswith("/journal"):
        return False
    qs = parse_qs(parsed.query or "")
    store = JournalStore()
    try:
        if parsed.path == "/journal/rule" and getattr(handler, "command", "GET") == "POST":
            form = _read_form(handler)
            enabled = bool((form.get("enabled") or [""])[0])
            rule_text = (form.get("rule_text") or [""])[0].strip()
            answer_rule.save_rule(enabled, rule_text, store=store)
            _redirect(handler, "/journal")
            return True

        if parsed.path == "/journal/reminder" and getattr(handler, "command", "GET") == "POST":
            form = _read_form(handler)
            try:
                chain_id = int((form.get("chain_id") or [""])[0])
                title = (form.get("title") or [""])[0].strip()
                start_date = (form.get("start_date") or [""])[0].strip()
                days = int((form.get("days") or ["0"])[0])
                daily_time = (form.get("daily_time") or [""])[0].strip()
                store.update_chain(chain_id, title=title, start_date=start_date,
                                   days=days, daily_time=daily_time)
            except (TypeError, ValueError, KeyError) as e:
                _write_html(
                    handler,
                    "<h1>提醒没改成功</h1><p>%s</p><p><a href='/journal'>回到日记</a></p>"
                    % _esc(str(e)),
                    400,
                )
                return True
            _redirect(handler, "/journal?notice=" + quote("提醒已修改，今天起按新计划发送。", safe=""))
            return True

        if parsed.path == "/journal/delete" and getattr(handler, "command", "GET") == "POST":
            form = _read_form(handler)
            entry_id = (form.get("entry_id") or [""])[0]
            info = store.purge_entry(entry_id)
            if info is None:
                _write_html(handler, "<h1>没找到这条日记</h1><p><a href='/journal'>回到日记</a></p>", 404)
                return True

            # 物理删除后，必须把语义索引里的块也摘掉，否则问答还会召回旧内容。
            try:
                from . import recall
                recall.forget_entry(info["id"])
                for aid in info["attachment_ids"]:
                    recall.forget_attachment(aid)
            except Exception:
                logger.exception("删除日记索引失败: %s", info["id"])

            # 物理删除后先删原件，再刷新复盘；原件删除失败会带回页面提示。
            errors = _purge_entry_originals(store, info)

            # 已生成的周/月/年复盘是静态页；这次把被删日记所在周期和当前周期一起刷新。
            try:
                days = [date.fromisoformat(info["entry_date"]), date.today()]
                _refresh_reviews(store, days)
            except Exception:
                logger.exception("删除日记后重生成复盘失败: %s", info["id"])

            notice = "已彻底删除，周/月/年复盘已自动更新。"
            if errors:
                notice += " 有原件没删干净：" + "；".join(errors)
            _redirect(handler, "/journal?notice=" + quote(notice, safe=""))
            return True

        if parsed.path == "/journal/attachment":
            try:
                attachment_id = int((qs.get("id") or [""])[0])
            except ValueError:
                _write_html(handler, "<h1>附件编号不对</h1><p><a href='/journal'>回到日记</a></p>", 400)
                return True
            _serve_attachment(handler, store, attachment_id)
            return True

        if parsed.path == "/journal/media":
            _write_html(handler, render_media(store))
            return True

        if parsed.path == "/journal/file":
            body = read_review((qs.get("name") or [""])[0])
            if body is None:
                _write_html(handler, "<h1>没找到这份复盘</h1><p><a href='/journal'>回到列表</a></p>", 404)
            else:
                _write_html(handler, body)
            return True

        if parsed.path == "/journal/now":
            p = (qs.get("period") or [""])[0]
            period = p if p in ("week", "month", "year") else "week"
            _write_html(handler, render_now(period, store))
            return True

        if parsed.path in ("/journal", "/journal/"):
            period = (qs.get("period") or [""])[0]
            if period in ("week", "month", "year"):
                _write_html(handler, render_now(period, store))
            else:
                notice = (qs.get("notice") or [""])[0]
                _write_html(handler, render_index(store, notice=notice))
            return True

        _write_html(handler, "<h1>没有这个页面</h1><p><a href='/journal'>回到列表</a></p>", 404)
        return True
    finally:
        store.close()
