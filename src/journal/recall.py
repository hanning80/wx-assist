"""我的记忆：把日记/分享做成可被语义召回的记忆块。

两件事：
1. index_entry(id)：新日记落库后，把它的文字并进独立向量库，之后问什么都查得到。
2. recall(query)：按意思查"我以前记过/收藏过什么"，返回日期、类型、原文。

向量库单独放在 data/chroma-journal，不和聊天索引抢锁；两边互不影响。
"""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from .media import media_rag_enabled

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
JOURNAL_DB = DATA_DIR / "journal.db"
STORE_PATH = DATA_DIR / "chroma-journal"
SOURCE = "journal"

# 哪些话该先查日记库：用户问“以前/上次/记过/发过”，或问已经存过的品相/主题。
# 集中放这里，Agent 和面板普通对话共用同一套判断，避免两边漏词。
JOURNAL_QUERY_RE = re.compile(
    r"(以前|之前|上次|记过|记录过|收藏|收藏过|存过|发过|说过|"
    r"原话|怎么说的|存的|保存过|我说过|"
    r"位置|地址|在哪|导航|地图|发给我|发一下|"
    r"文件|笔记|聊天记录|附件|"
    r"日记|知识库|查一下|有没有|记不记得|还记得|品相|辣椒红)"
)

_lock = threading.RLock()
_engine = None  # (store, embedder)


def _open():
    """懒加载向量库 + 嵌入模型（同一进程只开一次）。"""
    global _engine
    with _lock:
        if _engine is not None:
            return _engine
        from src.assistant.rag.embedder import FastEmbedder
        from src.assistant.rag.vector_store import ChromaStore

        store = ChromaStore(path=str(STORE_PATH))
        store.warmup()
        embedder = FastEmbedder()
        embedder.warmup()
        _engine = (store, embedder)
        logger.info("[记忆] 就绪：path=%s count=%d", STORE_PATH, store.count())
        return _engine


def close():
    global _engine
    with _lock:
        if _engine is not None:
            try:
                _engine[0].close()
            except Exception:
                pass
            _engine = None


KIND_LABEL = {
    "voice": "语音",
    "text": "文字",
    "attachment": "附件",
    "share": "分享",
    "image": "图片",
    "video": "视频",
}


def _text_of(row: sqlite3.Row) -> str:
    """一条日记喂给向量库的文字：日期 + 类型 + 标题 + 全文 + 话题。"""
    title = (row["summary"] or "").strip()
    body = (row["full_text"] or row["raw_text"] or "").strip()
    topics = (row["topics"] or "").strip()
    kind = KIND_LABEL.get(row["kind"] or "", row["kind"] or "")
    bits = ["%s 的%s日记" % (row["entry_date"], kind)]
    if title:
        bits.append(title)
    if body and body not in title:
        bits.append(body)
    if "提醒" in title or "提醒" in body:
        bits.append("这条日记里包含提醒")
    if topics:
        bits.append("话题：" + topics)
    return "｜".join(bits)


CHUNK_CHARS = 400
CHUNK_OVERLAP = 80


def chunk_text(text: str) -> list[str]:
    """把长文本切成可检索的小块，保留少量重叠，避免句子被切断后搜不到。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= CHUNK_CHARS:
        return [text]
    out = []
    start = 0
    while start < len(text):
        part = text[start:start + CHUNK_CHARS].strip()
        if part:
            out.append(part)
        if start + CHUNK_CHARS >= len(text):
            break
        start += CHUNK_CHARS - CHUNK_OVERLAP
    return out


def _hydrate_body(row) -> dict:
    """本地正文为空时，按 entry_id 从微云取回；取不到就按摘要继续。"""
    if row is None:
        return {}
    d = dict(row)
    if (d.get("full_text") or d.get("raw_text") or "").strip():
        d["_body_source"] = "local"
        return d
    try:
        from src.journal.store import JournalStore
        from src.journal.cloud.body_store import hydrate_entry
        store = JournalStore(JOURNAL_DB)
        try:
            return hydrate_entry(store, d)
        finally:
            store.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("取回日记正文失败 id=%s: %s", d.get("id"), e)
        d["_body_source"] = "missing"
        return d


def _row(entry_id: int) -> Optional[dict]:
    conn = sqlite3.connect(str(JOURNAL_DB))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id, entry_date, created_at, kind, raw_text, summary, full_text, topics"
            " FROM diary_entry WHERE id=? AND deleted=0", (entry_id,)).fetchone()
    finally:
        conn.close()
    return _hydrate_body(row) if row is not None else None


def _attachment_row(attachment_id: int) -> Optional[sqlite3.Row]:
    conn = sqlite3.connect(str(JOURNAL_DB))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT id, kind, filename, local_path, extracted_text, note, status,"
            " cloud_pdir, cloud_name, created_at, entry_id, asset_id"
            " FROM attachment WHERE id=?", (attachment_id,)).fetchone()
    finally:
        conn.close()


def _media_asset_row(asset_id: str) -> Optional[sqlite3.Row]:
    if not asset_id:
        return None
    conn = sqlite3.connect(str(JOURNAL_DB))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM media_asset WHERE asset_id=?", (str(asset_id),)).fetchone()
    finally:
        conn.close()


def _media_text(asset: sqlite3.Row, filename: str = "") -> str:
    label = {"image": "图片", "video": "视频"}.get(asset["kind"] or "", "媒体")
    bits = ["%s 的%s" % ((asset["created_at"] or "")[:10], label)]
    if filename:
        bits.append("《%s》" % filename)
    if (asset["caption"] or "").strip():
        bits.append((asset["caption"] or "").strip())
    if (asset["ocr_text"] or "").strip():
        bits.append("图片文字：" + (asset["ocr_text"] or "").strip())
    if (asset["asr_text"] or "").strip():
        bits.append("视频语音：" + (asset["asr_text"] or "").strip())
    return "｜".join(bits)


def _attachment_text(row: sqlite3.Row) -> str:
    labels = {"file": "文件", "image": "图片", "video": "视频",
              "video_thumb": "视频封面"}
    bits = ["%s 的附件" % (row["created_at"] or "")[:10],
            labels.get(row["kind"] or "", row["kind"] or "文件"),
            "《%s》" % (row["filename"] or "")]
    note = (row["note"] or "").strip()
    if note:
        bits.append("说明：" + note)
    body = (row["extracted_text"] or "").strip()
    if body:
        bits.append("正文：" + body)
    asset_id = ""
    try:
        asset_id = row["asset_id"] or ""
    except (KeyError, IndexError):
        pass
    if asset_id:
        asset = _media_asset_row(asset_id)
        if asset is not None:
            mt = _media_text(asset, row["filename"] or "")
            if mt:
                bits.append(mt)
    if row["cloud_pdir"]:
        bits.append("已存云端：%s/%s" % (row["cloud_pdir"], row["cloud_name"] or ""))
    return "｜".join(bits)


def index_entry(entry_id: int) -> bool:
    """把一条日记拆成全文小块并进记忆库（重复调用覆盖同一条）。"""
    row = _row(entry_id)
    if row is None:
        return False
    text = _text_of(row)
    parts = chunk_text(text)
    if not parts:
        return False

    from src.assistant.rag_types import Chunk

    chunks = []
    for i, part in enumerate(parts):
        chunks.append(Chunk(
            id="journal_%d_p%d" % (entry_id, i),
            source=SOURCE,
            source_id=str(entry_id),
            chat_id="",
            sender_name="我",
            content=part,
            created_at=(row["created_at"] or row["entry_date"] or "")[:19],
            metadata={"kind": row["kind"], "date": row["entry_date"],
                      "title": (row["summary"] or "")[:80], "part": i},
        ))
    store, embedder = _open()
    # 旧版本只存了前 600 字；先清掉旧块，再按全文重建。
    store.delete_by_source(SOURCE, [str(entry_id)])
    store.add(chunks, embedder.encode([c.content for c in chunks]))
    return True


def forget_entry(entry_id: int) -> bool:
    """从日记记忆库里删除一条日记的向量块；数据库软删除后必须调用。"""
    try:
        store, _ = _open()
        store.delete_by_source(SOURCE, [str(entry_id)])
        return True
    except Exception:
        logger.exception("[记忆] 删除日记索引失败: %s", entry_id)
        return False


def forget_attachment(attachment_id: int) -> bool:
    """从附件记忆库里删除一个附件的向量块。"""
    try:
        store, _ = _open()
        store.delete_by_source("attachment", [str(attachment_id)])
        return True
    except Exception:
        logger.exception("[记忆] 删除附件索引失败: %s", attachment_id)
        return False


def index_attachment(attachment_id: int) -> bool:
    """把附件名、说明、抽出来的正文拆块并进记忆；同一附件重复调用覆盖。"""
    row = _attachment_row(attachment_id)
    if row is None or (row["status"] or "") == "deleted":
        return False
    # 图片/视频不再“整条不入库”：有 OCR/转写文字时只索引文字，没有文字就不建块。
    if (row["kind"] or "") in ("image", "video", "video_thumb"):
        forget_attachment(attachment_id)
        if not media_rag_enabled():
            return False
        return index_media_asset(row["asset_id"], filename=row["filename"] or "")

    text = _attachment_text(row)
    parts = chunk_text(text)
    if not parts:
        return False

    from src.assistant.rag_types import Chunk

    chunks = []
    for i, part in enumerate(parts):
        chunks.append(Chunk(
            id="attachment_%d_p%d" % (attachment_id, i),
            source="attachment",
            source_id=str(attachment_id),
            chat_id="filehelper",
            sender_name="我",
            content=part,
            created_at=(row["created_at"] or "")[:19],
            metadata={"kind": row["kind"] or "附件",
                      "date": (row["created_at"] or "")[:10],
                      "title": row["filename"] or "", "part": i},
        ))
    store, embedder = _open()
    store.delete_by_source("attachment", [str(attachment_id)])
    store.add(chunks, embedder.encode([c.content for c in chunks]))
    return True


def index_media_asset(asset_id: str, filename: str = "") -> bool:
    """只把媒体的文字（OCR/视频转写）进检索；原图/原视频不进向量库。"""
    if not media_rag_enabled():
        try:
            store, _ = _open()
            store.delete_by_source("media", [str(asset_id)])
        except Exception:
            pass
        return False
    asset = _media_asset_row(asset_id)
    if asset is None:
        return False
    text = _media_text(asset, filename=filename)
    parts = chunk_text(text)
    if not parts:
        try:
            store, _ = _open()
            store.delete_by_source("media", [str(asset_id)])
        except Exception:
            pass
        return False
    from src.assistant.rag_types import Chunk

    chunks = []
    for i, part in enumerate(parts):
        chunks.append(Chunk(
            id="media_%s_p%d" % (asset_id, i),
            source="media",
            source_id=str(asset_id),
            chat_id="",
            sender_name="我",
            content=part,
            created_at=(asset["created_at"] or "")[:19],
            metadata={"kind": asset["kind"] or "媒体",
                      "date": (asset["created_at"] or "")[:10],
                      "title": filename or (asset["caption"] or ""), "part": i},
        ))
    store, embedder = _open()
    store.delete_by_source("media", [str(asset_id)])
    store.add(chunks, embedder.encode([c.content for c in chunks]))
    return True


def purge_media_index() -> int:
    """把媒体文字块从记忆库移除；关闭“参与问答检索”时调用。"""
    try:
        conn = sqlite3.connect(str(JOURNAL_DB))
        try:
            ids = [r[0] for r in conn.execute("SELECT asset_id FROM media_asset")]
        finally:
            conn.close()
        if not ids:
            return 0
        store, _ = _open()
        for mid in ids:
            store.delete_by_source("media", [str(mid)])
        return len(ids)
    except Exception as e:  # noqa: BLE001
        logger.warning("[记忆] 清理媒体索引失败: %s", e)
        return 0


def index_all(limit: int = 0) -> int:
    """补历史：把所有日记一次性灌进记忆库。limit>0 时只处理最近若干条。"""
    conn = sqlite3.connect(str(JOURNAL_DB))
    try:
        sql = "SELECT id FROM diary_entry WHERE deleted=0 ORDER BY id DESC"
        if limit:
            sql += " LIMIT %d" % int(limit)
        ids = [r[0] for r in conn.execute(sql)]
    finally:
        conn.close()
    n = 0
    for eid in ids:
        try:
            if index_entry(eid):
                n += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("[记忆] 索引第 %s 条失败: %s", eid, e)
    # 附件也补一遍：文件名、说明、抽出来的正文同样要能被查到
    try:
        conn = sqlite3.connect(str(JOURNAL_DB))
        try:
            aids = [r[0] for r in conn.execute(
                "SELECT id FROM attachment WHERE status!='deleted' ORDER BY id DESC")]
        finally:
            conn.close()
        for aid in aids:
            try:
                if index_attachment(aid):
                    n += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("[记忆] 索引附件 %s 失败: %s", aid, e)
    except Exception as e:  # noqa: BLE001
        logger.warning("[记忆] 附件补索引失败: %s", e)
    # 媒体资产只把 OCR/转写文字补进索引，原件仍只在 OSS；后台关闭时整段跳过。
    if media_rag_enabled():
        try:
            conn = sqlite3.connect(str(JOURNAL_DB))
            try:
                mids = [r[0] for r in conn.execute(
                    "SELECT asset_id FROM media_asset ORDER BY created_at DESC")]
            finally:
                conn.close()
            for mid in mids:
                try:
                    if index_media_asset(mid):
                        n += 1
                except Exception as e:  # noqa: BLE001
                    logger.warning("[记忆] 索引媒体 %s 失败: %s", mid, e)
        except Exception as e:  # noqa: BLE001
            logger.warning("[记忆] 媒体补索引失败: %s", e)
    else:
        # 后台关闭媒体检索时，把历史遗留的媒体块一起清掉，避免“关了还搜得到”。
        purge_media_index()
    return n


def _query_terms(query: str) -> list[str]:
    """把用户问句切成用于兜底检索的短词；不做语义判断，只防漏。"""
    stops = (
        "我给你", "给你", "给我", "我跟", "跟", "以前", "之前", "上次",
        "发过", "说过", "记过", "记录过", "收藏过", "存过", "如何",
        "怎么", "怎样", "辨别", "判断", "方法", "日记", "知识库",
        "请问", "帮我", "一下", "那个", "那段", "内容", "东西",
        "的", "了", "吗", "呢", "吧", "啊", "是", "有", "和", "与",
    )
    clean = query or ""
    for w in sorted(stops, key=len, reverse=True):
        clean = clean.replace(w, " ")
    parts = re.findall(r"[\u4e00-\u9fa5A-Za-z0-9]+", clean)
    terms: list[str] = []
    for p in parts:
        if 2 <= len(p) <= 6:
            terms.append(p)
            # 中文短句再补二字片段：问“语音提醒”时，原文可能只写了
            # “提醒我两次”，不补片段就永远命不中。
            if len(p) >= 3:
                terms.extend(p[i:i + 2] for i in range(len(p) - 1))
        elif len(p) > 6:
            # 长句取 2~4 字片段，覆盖“辣椒红品相”这类连续词
            for n in (2, 3, 4):
                terms.extend(p[i:i + n] for i in range(len(p) - n + 1))
    out = []
    for t in terms:
        if t and t not in out:
            out.append(t)
    return out[:24]


def _snippet(text: str, term: str, width: int = 180) -> str:
    body = (text or "").strip()
    pos = body.find(term) if term else -1
    if pos < 0:
        return body[:width]
    return body[max(0, pos - 50):pos + width]


def keyword_recall(query: str, top_k: int = 5) -> list[dict]:
    """语义库暂时不可用时的本地兜底：直接按原文短词查 journal.db。

    只在向量检索报错或零命中时使用；命中的仍是日记/附件原文，
    不把关键词分数伪装成语义相关度。
    """
    terms = _query_terms(query)
    if not terms:
        return []
    found: dict[str, dict] = {}
    conn = sqlite3.connect(str(JOURNAL_DB))
    conn.row_factory = sqlite3.Row
    try:
        for term in terms:
            like = "%" + term + "%"
            for row in conn.execute(
                "SELECT id, entry_date, kind, summary, full_text, raw_text"
                " FROM diary_entry WHERE deleted=0 AND"
                " (summary LIKE ? OR full_text LIKE ? OR raw_text LIKE ?)",
                (like, like, like),
            ):
                key = "journal_%d" % row["id"]
                item = found.setdefault(key, {
                    "source": "journal",
                    "source_id": str(row["id"]),
                    "date": row["entry_date"] or "",
                    "kind": row["kind"] or "",
                    "title": row["summary"] or "",
                    "text": "",
                    "score": 0.0,
                    "_hits": 0,
                    "_term": term,
                })
                item["_hits"] += 1
                item["_term"] = term
                item["text"] = _snippet(
                    row["full_text"] or row["raw_text"] or "", term)
            media_rag = media_rag_enabled()
            for row in conn.execute(
                "SELECT a.id, a.kind, a.filename, a.extracted_text, a.note,"
                " a.created_at, a.asset_id, a.cloud_pdir, a.cloud_name"
                " FROM attachment a LEFT JOIN media_asset m ON m.asset_id=a.asset_id"
                " WHERE a.status!='deleted' AND"
                " (? OR a.kind NOT IN ('image','video','video_thumb')) AND"
                " (a.filename LIKE ? OR a.extracted_text LIKE ? OR a.note LIKE ?"
                " OR m.ocr_text LIKE ? OR m.asr_text LIKE ? OR m.caption LIKE ?)",
                (1 if media_rag else 0, like, like, like, like, like, like),
            ):
                key = "attachment_%d" % row["id"]
                item = found.setdefault(key, {
                    "source": "attachment",
                    "source_id": str(row["id"]),
                    "date": (row["created_at"] or "")[:10],
                    "kind": row["kind"] or "附件",
                    "title": row["filename"] or "",
                    "text": "",
                    "score": 0.0,
                    "_hits": 0,
                    "_term": term,
                })
                item["_hits"] += 1
                item["_term"] = term
                item["text"] = _snippet(
                    row["extracted_text"] or row["note"] or "", term)
    finally:
        conn.close()
    out = []
    for item in sorted(found.values(), key=lambda x: (-x["_hits"], x["date"]),
                       reverse=False)[:max(1, top_k)]:
        hits = item.pop("_hits")
        item.pop("_term", None)
        item["score"] = round(min(1.0, hits / max(1, len(terms))), 3)
        out.append(item)
    return out


def recall(query: str, top_k: int = 5) -> list[dict]:
    """按意思回忆自己记过/收藏过的东西；向量库不可用时退回原文兜底。"""
    if not query or not query.strip():
        return []
    try:
        store, embedder = _open()
        hits = store.search(embedder.encode([query])[0], top_k=top_k)
        out = []
        seen = set()
        for h in hits:
            c = h.chunk
            meta = c.metadata if isinstance(getattr(c, "metadata", None), dict) else {}
            # 同一篇长文的不同块要保留；只去掉完全重复的同一块。
            key = (c.source, str(c.source_id or ""),
                   hashlib.sha1((c.content or "")[:160].encode("utf-8")).hexdigest())
            if key in seen:
                continue
            seen.add(key)
            try:
                score = float(h.score or 0)
            except (TypeError, ValueError):
                score = 0.0
            out.append({
                "source": c.source,
                "source_id": c.source_id,
                "date": meta.get("date") or (c.created_at or "")[:10],
                "kind": meta.get("kind", ""),
                "title": meta.get("title", ""),
                "text": c.content,
                "score": round(score, 3),
            })
        if out:
            # 语义分数偶尔会把“原话里明明有这个词”的条目压下去（例如
            # 问“语音提醒”时，真正的语音日记被附件说明挤掉）。原文命中
            # 是更硬的证据，补进来并按命中强度排在前面，但不改写原文。
            try:
                literal = keyword_recall(query, top_k=top_k)
            except Exception:
                literal = []
            have = {(x["source"], str(x["source_id"]), x["text"][:80]) for x in out}
            for item in literal:
                key = (item.get("source"), str(item.get("source_id")), (item.get("text") or "")[:80])
                if key in have:
                    continue
                item = dict(item)
                item["match"] = "原文命中"
                item["score"] = max(float(item.get("score") or 0), 0.55)
                out.append(item)
            out.sort(key=lambda x: float(x.get("score") or 0), reverse=True)
            return out[:top_k]
    except Exception as e:  # noqa: BLE001
        logger.warning("[记忆] 语义检索不可用，改用原文兜底: %s", e)
    return keyword_recall(query, top_k=top_k)
