"""按人名/标签找已保存的位置。

两个来源：
1. 微信收藏的标签：fav_tag_db_item -> fav_bind_tag_db_item -> fav_db_item；
2. 微信聊天里的位置消息：contacts_cache 找到 wxid，再读 Msg_xxx 的 type 48，
   解压 zstd 后解析 <location x="纬度" y="经度" poiname="地点">。

位置消息只会从微信只读库里取，不写回微信；回答里给高德标记链接。
"""
from __future__ import annotations

import html
import logging
import re
import sqlite3
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
MESSAGES_DB = REPO_ROOT / "data" / "messages.db"
JOURNAL_DB = REPO_ROOT / "data" / "journal.db"


def _clean_name(text: str) -> str:
    s = (text or "").strip()
    for _ in range(3):
        s = re.sub(r"(把|请|帮我|帮忙|查|找|要|看|发|给|我|的|位置|定位|地址|导航|到|在|哪|里|一下|发我|发给我|给我)$", "", s)
        s = re.sub(r"^(把|请|帮我|帮忙|查|找|要|看|发|给|我|的|导航|到)", "", s)
    return s.strip(" ，。！？,.!?：:")


def amap_link(lat: float, lng: float, name: str) -> str:
    """高德标记链接；x 是纬度，y 是经度。"""
    return (
        "https://uri.amap.com/marker?position=%s,%s&name=%s&src=wx-assist"
        % (lng, lat, quote(str(name or "位置"), safe=""))
    )


def amap_search_link(name: str) -> str:
    """只有地点文字、没有坐标时的兜底：给高德搜索链接，不伪造经纬度。"""
    return "https://uri.amap.com/search?keyword=%s&src=wx-assist" % quote(
        str(name or "位置"), safe="")


def _text_location(raw: str) -> Optional[dict]:
    """微信只存了文字位置（[位置] 某某地）时，保留文字并给搜索链接。"""
    text = str(raw or "").strip()
    if not text.startswith("[位置]"):
        return None
    title = text[len("[位置]"):].strip()
    if not title:
        return None
    return {"title": title, "label": title, "lat": 0.0, "lng": 0.0,
            "link": amap_search_link(title), "text_only": True}


def _decode_location_xml(raw: str) -> str:
    text = str(raw or "")
    if not text:
        return ""
    # 微信消息体是 zstd 压缩后的十六进制串；不是压缩串就按原样处理。
    if text.startswith("28b52ffd"):
        try:
            import zstandard as zstd
            text = zstd.ZstdDecompressor().decompress(bytes.fromhex(text)).decode("utf-8", "ignore")
        except Exception as exc:  # noqa: BLE001
            logger.debug("位置消息解压失败: %s", exc)
            return ""
    return text


def _parse_location(xml: str) -> Optional[dict]:
    if not xml:
        return None
    # 聊天位置是 <location x="纬度" y="经度" poiname="...">；
    # 收藏位置是 <locitem><lng>经度</lng><lat>纬度</lat><poiname>...</poiname>。
    if "<locitem" in xml:
        def tag_value(tag: str) -> str:
            m = re.search(r"<%s>([^<]*)</%s>" % (tag, tag), xml)
            return html.unescape(m.group(1)) if m else ""
        try:
            lat = float(tag_value("lat"))
            lng = float(tag_value("lng"))
        except (TypeError, ValueError):
            return None
        title = tag_value("poiname") or tag_value("label") or "位置"
        return {
            "title": title,
            "label": tag_value("label"),
            "lat": lat,
            "lng": lng,
            "link": amap_link(lat, lng, title),
        }
    if "<location" not in xml:
        return None
    def attr(name: str, default: str = "") -> str:
        m = re.search(r'%s="([^"]*)"' % re.escape(name), xml)
        return html.unescape(m.group(1)) if m else default

    try:
        lat = float(attr("x"))
        lng = float(attr("y"))
    except (TypeError, ValueError):
        return None
    title = attr("poiname") or attr("label") or "位置"
    return {
        "title": title,
        "label": attr("label"),
        "lat": lat,
        "lng": lng,
        "link": amap_link(lat, lng, title),
    }


def diary_locations(name: str, limit: int = 5) -> list[dict]:
    """先查日记：找正文里提到这个人/标签的条目。日记没有坐标时只给原文。"""
    name = _clean_name(name)
    if not name or not JOURNAL_DB.exists():
        return []
    try:
        conn = sqlite3.connect(str(JOURNAL_DB))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, entry_date, kind, summary, raw_text, full_text "
                "FROM diary_entry WHERE deleted=0 AND ("
                "raw_text LIKE ? OR summary LIKE ? OR IFNULL(full_text,'') LIKE ?"
                ") ORDER BY entry_date DESC, id DESC LIMIT 30",
                ("%%%s%%" % name, "%%%s%%" % name, "%%%s%%" % name),
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("查日记位置失败: %s", exc)
        return []

    out = []
    for r in rows:
        text = (r["raw_text"] or r["full_text"] or "").strip()
        link = re.search(r"https://uri\.amap\.com/marker[^\s<>\"']+", text)
        link = html.unescape(link.group(0)) if link else ""
        # 截取提到这个名字附近的一句，避免整篇日记太长。
        pos = text.find(name)
        if pos >= 0:
            snippet = text[max(0, pos - 40): pos + 120]
        else:
            snippet = text[:160]
        out.append({
            "title": (r["summary"] or "日记").strip(),
            "date": r["entry_date"] or "",
            "text": snippet.strip(),
            "link": link,
            "source": "日记",
        })
    return out[:limit]


def favorite_locations(name: str, limit: int = 5) -> list[dict]:
    """按收藏标签名查位置收藏。"""
    name = _clean_name(name)
    if not name:
        return []
    try:
        from src.wechat.wcdb_client import get_shared_client
        client = get_shared_client()
        tags = client.exec_query(
            "fav",
            sql="SELECT local_id, name FROM fav_tag_db_item WHERE name LIKE '%%%s%%' ORDER BY local_id"
            % name.replace("'", "''"),
        )
        if not tags:
            # 语音识别常把“大姚”听成“大窑”这种同音字；精确查不到时，
            # 在全部收藏标签里按相似度选一个最接近的，避免直接回答“没找到”。
            all_tags = client.exec_query(
                "fav",
                sql="SELECT local_id, name FROM fav_tag_db_item ORDER BY local_id",
            )
            scored = []
            for t in all_tags:
                tag_name = str(t.get("name") or "")
                if not tag_name:
                    continue
                ratio = SequenceMatcher(None, name, tag_name).ratio()
                # 至少共享首字且相似度过半，避免“大窑”随便配到“小姚”。
                if tag_name[:1] == name[:1] and ratio >= 0.5:
                    scored.append((ratio, t))
            if scored:
                tags = [max(scored, key=lambda x: x[0])[1]]
                logger.info("收藏标签按相似名匹配：%s → %s", name,
                            tags[0].get("name"))
        rows = []
        for tag in tags:
            items = client.exec_query(
                "fav",
                sql=(
                    "SELECT i.local_id, i.type, i.update_time, i.fromusr, i.content "
                    "FROM fav_bind_tag_db_item b JOIN fav_db_item i ON i.local_id=b.fav_local_id "
                    "WHERE b.tag_local_id=%d AND i.type=6 ORDER BY i.update_time DESC LIMIT %d"
                    % (int(tag["local_id"]), int(limit))
                ),
            )
            for item in items:
                loc = _parse_location(_decode_location_xml(item.get("content")))
                if loc:
                    loc.update({
                        "source": "收藏标签「%s」" % tag.get("name", name),
                        "date": datetime.fromtimestamp(int(item.get("update_time") or 0)).strftime("%Y-%m-%d"),
                    })
                    rows.append(loc)
        # 去重按地点名+坐标
        seen = set()
        out = []
        for r in rows:
            key = (r["title"], round(r["lat"], 5), round(r["lng"], 5))
            if key not in seen:
                seen.add(key)
                out.append(r)
        return out[:limit]
    except Exception as exc:  # noqa: BLE001
        logger.warning("查收藏标签位置失败: %s", exc)
        return []


def _contact_usernames(name: str) -> list[str]:
    name = _clean_name(name)
    if not name or not MESSAGES_DB.exists():
        return []
    try:
        conn = sqlite3.connect(str(MESSAGES_DB))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT username, display_name, remark, nick_name FROM contacts_cache "
                "WHERE display_name=? OR remark=? OR nick_name=? "
                "OR display_name LIKE ? OR remark LIKE ? OR nick_name LIKE ? "
                "LIMIT 10",
                (name, name, name, "%%%s%%" % name, "%%%s%%" % name, "%%%s%%" % name),
            ).fetchall()
            if not rows:
                # 语音识别同音字兜底：大窑 → 大姚。
                all_rows = conn.execute(
                    "SELECT username, display_name, remark, nick_name FROM contacts_cache"
                    " WHERE username IS NOT NULL AND username<>''"
                ).fetchall()
                scored = []
                for r in all_rows:
                    for field in ("display_name", "remark", "nick_name"):
                        candidate = str(r[field] or "")
                        if not candidate:
                            continue
                        ratio = SequenceMatcher(None, name, candidate).ratio()
                        if candidate[:1] == name[:1] and ratio >= 0.5:
                            scored.append((ratio, r))
                            break
                if scored:
                    best = max(scored, key=lambda x: x[0])[1]
                    rows = [best]
                    logger.info("联系人按相似名匹配：%s → %s", name,
                                best["display_name"] or best["remark"] or best["nick_name"])
            return [r["username"] for r in rows if r["username"]]
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("查联系人失败: %s", exc)
        return []


def chat_locations(name: str, limit: int = 5) -> list[dict]:
    """从联系人聊天记录里取最近的位置消息。"""
    name = _clean_name(name)
    if not name:
        return []
    usernames = _contact_usernames(name)
    if not usernames:
        return []
    try:
        from src.wechat.wcdb_client import get_shared_client
        client = get_shared_client()
    except Exception as exc:  # noqa: BLE001
        logger.warning("微信读取器不可用: %s", exc)
        return []

    rows = []
    me = Path(client.account_dir).name if getattr(client, "account_dir", "") else ""
    for username in usernames[:3]:
        try:
            # 只读最近 5000 条，按页取；位置消息可能比最近 500 条更早。
            for offset in range(0, 5000, 500):
                messages = client.get_messages(username, limit=500, offset=offset)
                if not messages:
                    break
                for m in messages:
                    if int(m.get("msg_type") or 0) != 48:
                        continue
                    # 只取对方发的，不把我们自己发的位置当成对方位置。
                    if m.get("is_send") == 1:
                        continue
                    if me and str(m.get("sender") or "").startswith(me):
                        continue
                    raw = _decode_location_xml(str(m.get("content") or m.get("message_content") or ""))
                    loc = _parse_location(raw) or _text_location(raw)
                    if not loc:
                        continue
                    loc.update({
                        "source": "聊天记录",
                        "date": datetime.fromtimestamp(
                            int(m.get("create_time") or 0)).strftime("%Y-%m-%d %H:%M"),
                    })
                    rows.append(loc)
                if len(messages) < 500:
                    break
        except Exception as exc:  # noqa: BLE001
            logger.warning("读聊天位置失败 %s: %s", username, exc)
            continue
    rows.sort(key=lambda r: r.get("date", ""), reverse=True)
    seen = set()
    out = []
    for r in rows:
        # 同一个地点在不同时间发过多次，只保留最新一条，避免答案里重复刷屏。
        key = (r["title"],)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out[:limit]


def find_locations(name: str, limit: int = 5) -> dict:
    """按固定顺序查：日记 -> 收藏标签 -> 聊天记录。

    语音识别常把“大姚”听成“大瑶”。收藏标签能按相似名纠正时，聊天记录也
    使用纠正后的标签名，避免把另一个叫“大瑶”的联系人位置当成“大姚”的。
    """
    favs = favorite_locations(name, limit=limit)
    chat_name = _clean_name(name)
    for item in favs:
        matched = re.search(r"收藏标签「(.+?)」", str(item.get("source") or ""))
        if matched:
            tag_name = matched.group(1).strip()
            if tag_name and tag_name != chat_name:
                logger.info("位置查询沿用收藏标签纠正名：%s → %s", chat_name, tag_name)
                chat_name = tag_name
            break
    return {
        "name": _clean_name(name),
        "diary": diary_locations(name, limit=limit),
        "favorites": favs,
        "chats": chat_locations(chat_name, limit=limit),
    }


def guess_person_name(text: str) -> str:
    """从“把大姚的位置发给我/大姚在哪/导航到大姚”里抠出人名。"""
    s = (text or "").strip(" ，。！？,.!?：:")
    s = re.sub(r"^(把|请|帮我|帮忙|查一下|查|找一下|找|要|看|发|给|我|导航到|导航|到)+", "", s)
    s = re.sub(r"(的)?(位置|定位|地址|导航)(在哪里|在哪|发给我|发我|给我|发过来|发一下|一下)?.*$", "", s)
    s = re.sub(r"(在|是)?(哪|哪里|什么地方).*$", "", s)
    return s.strip(" 的了，。！？,.!?：:")


def format_locations(result: dict, limit: int = 3) -> str:
    """按“日记 -> 收藏 -> 聊天记录”的顺序播报。"""
    name = (result or {}).get("name") or "这个人"
    diary = (result or {}).get("diary") or []
    favs = (result or {}).get("favorites") or []
    chats = (result or {}).get("chats") or []
    if not diary and not favs and not chats:
        return "没有找到和「%s」有关的已保存位置。" % name

    lines = ["和「%s」有关的位置，我按日记、收藏、聊天记录的顺序查到了这些：" % name]
    if diary:
        lines.append("日记里记过的：")
        for i, r in enumerate(diary[:limit], 1):
            lines.append("%d. %s（%s）" % (i, r["title"], r["date"]))
            if r.get("text"):
                lines.append("   原文：%s" % r["text"])
            if r.get("link"):
                lines.append("   高德地图：%s" % r["link"])
    if favs:
        lines.append("收藏标签里存的：")
        for i, r in enumerate(favs[:limit], 1):
            lines.append("%d. %s（%s）" % (i, r["title"], r["date"]))
            lines.append("   高德地图：%s" % r["link"])
    if chats:
        lines.append("聊天记录里最近发过的：")
        for i, r in enumerate(chats[:limit], 1):
            lines.append("%d. %s（%s）" % (i, r["title"], r["date"]))
            lines.append("   高德地图：%s" % r["link"])
    lines.append("这些是日记、收藏和聊天里已有的内容，不是实时定位。要哪一条我直接把链接发你。")
    return "\n".join(lines)
