"""把全量知识库包和聊天增量合并成一份新的迁移包。

用于换机恢复和每天压缩：
- 全量包是底稿；
- 增量只是补充；
- 同一 message_id 只保留一条；
- 后来的增量如果带同一条 message_id（例如语音占位补成文字），用新内容覆盖旧内容；
- 按时间和 ID 重新排序；
- 输出仍是标准 JSON，别的工具可以直接读。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from .text_export import write_json

logger = logging.getLogger(__name__)


def _as_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _read(path: str | Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("不是知识库对象: %s" % path)
    return data


def merge_package(base: dict, increments: list[dict],
                  when: Optional[datetime] = None) -> dict:
    """合并增量，返回新对象；不修改传入的 base。"""
    when = when or datetime.now()
    out = dict(base)
    messages = list(base.get("messages") or [])
    index_by_id = {
        str(m.get("message_id") or ""): i
        for i, m in enumerate(messages)
        if m.get("message_id")
    }
    max_rowid = _as_int(base.get("last_rowid"))

    for inc in increments:
        for m in inc.get("messages") or []:
            mid = str(m.get("message_id") or "")
            if not mid:
                continue
            if mid in index_by_id:
                # 语音占位后来补成文字时，同一条 message_id 以较新的增量覆盖旧内容。
                messages[index_by_id[mid]] = m
            else:
                index_by_id[mid] = len(messages)
                messages.append(m)
        try:
            max_rowid = max(max_rowid, _as_int(inc.get("to_rowid")))
        except (TypeError, ValueError):
            pass

    def _sort_key(m: dict):
        # 不同来源的 timestamp 可能是数字，也可能是数字字符串；按字符串
        # 排序会出现 "99" 排在 "100" 后面，统一转成数字再排。
        return (_as_int(m.get("timestamp")),
                str(m.get("message_id") or ""))
    messages.sort(key=_sort_key)
    out["messages"] = messages
    counts = dict(out.get("counts") or {})
    counts["messages"] = len(messages)
    out["counts"] = counts
    out["merged_at"] = when.isoformat(timespec="seconds")
    if max_rowid:
        out["last_rowid"] = max_rowid
    return out


def merge_from_dir(base_path: str | Path, increment_dir: str | Path,
                   out_path: str | Path,
                   when: Optional[datetime] = None) -> dict:
    base = _read(base_path)
    incs = []
    bad: list[str] = []
    for p in sorted(Path(increment_dir).glob("聊天增量-*.json")):
        try:
            incs.append(_read(p))
        except Exception as e:  # noqa: BLE001
            logger.warning("跳过损坏的增量包 %s: %s", p.name, e)
            bad.append("%s：%s" % (p.name, str(e)[:120]))
    merged = merge_package(base, incs, when=when)
    if bad:
        merged["bad_increments"] = bad
    write_json(merged, out_path)
    return merged


def merge_cloud_daily(store, client=None, when: Optional[datetime] = None) -> dict:
    """每天兜底：下载云端最新全量 + 全部增量，合并后上传一份“合并包”。

    全量包每天会重新生成；合并包用于全量失败或全量比增量旧的场景。
    本地临时目录用完即删，云端合并包按天一份。
    """
    from .restore import restore_bundle
    from .storage import CloudStore

    when = when or datetime.now()
    bundle = restore_bundle(client=client)
    out_dir = Path(bundle["out_dir"])
    merged_path = Path(bundle["merged"])
    day = when.strftime("%Y%m%d")
    name = "知识库全文-合并-%s.json" % day
    try:
        cs = CloudStore(store=store, client=client)
        rec = cs.put_knowledge_file(str(merged_path), name, when=when,
                                    kind="knowledge_merge")
        state_path = Path(store.db_path).parent / "journal" / "export" / "chat_merge_state.json"
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "last_merge_day": day,
            "last_merge_at": when.isoformat(timespec="seconds"),
            "cloud_name": name,
            "object_key": rec.get("object_key", ""),
            "counts": bundle.get("counts", {}),
            "increments": bundle.get("increments", 0),
            "manifest": bundle.get("manifest", {}),
        }
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        logger.info("每日增量合并完成：%s（聊天 %s 条）", name,
                    (bundle.get("counts") or {}).get("messages"))
        return state
    finally:
        # restore_bundle 的中间目录里是明文包；上传成功或失败都不长期留着。
        shutil.rmtree(out_dir, ignore_errors=True)

def main() -> None:
    base = sys.argv[1] if len(sys.argv) > 1 else \
        r"F:\微信监控\wx-assist\data\journal\export\知识库全文-20261116.json"
    inc_dir = sys.argv[2] if len(sys.argv) > 2 else \
        r"F:\微信监控\wx-assist\data\journal\export\增量"
    out = sys.argv[3] if len(sys.argv) > 3 else \
        str(Path(base).with_name("知识库全文-合并-%s.json" % datetime.now().strftime("%Y%m%d")))
    data = merge_from_dir(base, inc_dir, out)
    print("已合并：%s（聊天 %d 条）" % (out, data["counts"]["messages"]))


if __name__ == "__main__":
    main()
