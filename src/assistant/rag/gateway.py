"""统一知识检索入口。

物理上仍有两套索引：
  - data/chroma          聊天、收藏、公众号（朋友圈与视频号一样不进入索引）
  - data/chroma-journal  日记、附件

逻辑上只对外暴露这里一个入口：一次查询两套，统一下降重、排序和出处。
排序按用户确认的顺序：日记 → 附件 → 收藏 → 公众号 → 聊天。
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Callable, Optional

logger = logging.getLogger(__name__)

PRIORITY = {
    "journal": 0,
    "attachment": 1,
    "fav": 2,
    "oa": 3,
    "msg": 4,
}
LABEL = {
    "journal": "日记",
    "attachment": "附件",
    "fav": "收藏",
    "oa": "公众号",
    "msg": "聊天",
}
MAIN_SOURCES = ("fav", "oa", "msg")


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").strip().lower())


def _score(value) -> float:
    try:
        score = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if score != score:  # NaN
        return 0.0
    return max(0.0, min(1.0, score))


def _fingerprint(item: dict) -> str:
    """同一篇长文的不同块不能互相顶掉，所以指纹要带上正文片段。"""
    text = hashlib.sha1(_norm((item.get("text") or "")[:160]).encode("utf-8")).hexdigest()
    link = (item.get("link") or "").strip()
    if link and link.startswith("http"):
        return "url:" + link + ":" + text
    title = _norm(item.get("title") or "")
    if title:
        return "title:" + hashlib.sha1(title.encode("utf-8")).hexdigest() + ":" + text
    return "text:" + text


class KnowledgeGateway:
    """两套索引的统一查询层；任何一套失败都不影响另一套。"""

    def __init__(self, main_rag=None,
                 recall_fn: Optional[Callable] = None):
        self.main_rag = main_rag
        self._recall_fn = recall_fn

    def _recall(self, query: str, top_k: int) -> list[dict]:
        if self._recall_fn is None:
            from src.journal.recall import recall as default_recall
            self._recall_fn = default_recall
        try:
            return self._recall_fn(query, top_k=top_k) or []
        except Exception as e:  # noqa: BLE001
            logger.warning("[网关] 日记/附件检索失败: %s", e)
            return []

    def _main_source(self, query: str, source: str, top_k: int) -> list[dict]:
        if self.main_rag is None:
            return []
        try:
            hits = self.main_rag.search(
                query=query, top_k=max(top_k * 2, 10), final_k=top_k,
                where={"source": source},
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("[网关] %s 检索失败: %s", LABEL.get(source, source), e)
            return []

        out = []
        for h in hits:
            c = h.chunk
            meta = getattr(c, "metadata", {}) or {}
            if not isinstance(meta, dict):
                meta = {}
            out.append({
                "source": (c.source or source),
                "source_id": str(c.source_id or ""),
                "date": str(meta.get("date") or c.created_at or "")[:10],
                "kind": meta.get("kind", "") or LABEL.get(source, ""),
                "title": meta.get("title", "") or "",
                "text": c.content or "",
                "score": round(_score(h.score), 3),
                "link": meta.get("link", "") or "",
                "chat_id": c.chat_id or "",
                "sender_name": c.sender_name or "",
            })
        return out

    def search(self, query: str, top_k: int = 10,
               per_source: int = 5) -> list[dict]:
        """一次查两套，返回统一结果；查不到返回空列表。"""
        if not query or not query.strip():
            return []
        query = query.strip()

        items: list[dict] = []
        # 日记/附件：recall 内部已按 source_id 去重
        for h in self._recall(query, top_k=per_source * 2):
            item = dict(h)
            item.setdefault("link", "")
            item.setdefault("chat_id", "")
            item.setdefault("sender_name", "")
            items.append(item)

        # 主索引：按来源分别查，避免聊天结果把日记/收藏挤掉
        for source in MAIN_SOURCES:
            items.extend(self._main_source(query, source, per_source))

        # 同一来源的同一条内容只留一次；同一篇长文的不同块保留（最多 3 块），
        # 否则长文章、长日记永远只能看到开头那 400 字。
        best: dict[tuple, dict] = {}
        chunk_count: dict[tuple, int] = {}
        for item in items:
            source = item.get("source", "")
            source_id = str(item.get("source_id", ""))
            text_key = hashlib.sha1(_norm((item.get("text") or "")[:160]).encode("utf-8")).hexdigest()
            key = (source, source_id)
            exact = (source, source_id, text_key)
            if exact in best:
                if _score(item.get("score")) > _score(best[exact].get("score")):
                    best[exact] = item
                continue
            if chunk_count.get(key, 0) >= 3:
                continue
            chunk_count[key] = chunk_count.get(key, 0) + 1
            best[exact] = item

        # 跨来源同文去重：同一链接在收藏和公众号里重复时，只留优先级高的一份；
        # 但同一来源的长文分块要继续保留，不能把 400 字段落全砍掉。
        seen_doc: set[str] = set()
        seen_link_source: dict[str, str] = {}
        ranked = []
        for item in sorted(
            best.values(),
            key=lambda x: (PRIORITY.get(x.get("source", ""), 9),
                           -_score(x.get("score"))),
        ):
            link = (item.get("link") or "").strip()
            if link.startswith("http"):
                owner = seen_link_source.get(link)
                if owner and owner != item.get("source", ""):
                    continue
                seen_link_source.setdefault(link, item.get("source", ""))
            fp = _fingerprint(item)
            if fp in seen_doc:
                continue
            seen_doc.add(fp)
            item["priority"] = PRIORITY.get(item.get("source", ""), 9)
            item["source_label"] = LABEL.get(item.get("source", ""), item.get("source", ""))
            ranked.append(item)

        return ranked[:max(1, top_k)]

    def groups(self, query: str, top_k: int = 10,
               per_source: int = 5) -> dict[str, list[dict]]:
        """按用户播报顺序分组，供提示词和面板使用。"""
        items = self.search(query, top_k=top_k, per_source=per_source)
        out = {"journal": [], "fav": [], "other": []}
        for item in items:
            source = item.get("source", "")
            if source in ("journal", "attachment"):
                out["journal"].append(item)
            elif source == "fav":
                out["fav"].append(item)
            else:
                out["other"].append(item)
        return out


__all__ = ["KnowledgeGateway", "PRIORITY", "LABEL"]
