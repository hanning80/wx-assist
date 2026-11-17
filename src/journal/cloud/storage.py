"""云端存储层：只搬"文字类"的东西，音频一律不存。

为什么不上传语音：一段 3 秒语音 149 KB、10 分钟就 2 MB 上下，是唯一真正吃空间的
东西；而它的价值在转写出来的文字里。所以音频转完文字就删，本地和云上都不留。

上云的主要有四样：
  复盘页    → 生成后以明文 HTML 上云，本地留一份面板直接看
  数据库快照 → 每天一份明文 SQLite 快照上云，换机器能整库找回
  知识库全文 → 日记/聊天/收藏/附件文字导出成明文包，换机器和别的工具都能直接读
  附件原件   → 图片/文件/视频按需上云，桶里能直接看
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import crypto
from .provider import make_client

logger = logging.getLogger(__name__)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class CloudStore:
    """对上云动作做一层收口：加、传、记账。"""

    def __init__(self, store=None, client=None):
        self.store = store
        self.client = client or make_client()

    # ---------- 复盘页 ----------
    def put_review(self, html_path: str, when: Optional[datetime] = None) -> dict:
        when = when or datetime.now()
        pdir = self.client.ensure_path(["摘星日记", "复盘", when.strftime("%Y-%m")])
        name = os.path.basename(html_path)
        self._delete_same_name(pdir, name)
        res = self.client.upload(html_path, pdir, filename=name)
        rec = {
            "kind": "review",
            "entry_id": None,
            "object_key": res.get("file_id", ""),
            "pdir_key": pdir,
            "name": name,
            "bytes": os.path.getsize(html_path),
            "sha256": _sha256(html_path),
            "local_path": html_path,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        if self.store:
            self.store.add_cloud_object(**rec)
        return rec

    # ---------- 数据库快照 ----------
    def snapshot_db(self, db_path: str) -> str:
        """用 SQLite 自带的备份接口做一致性快照（不能直接拷 WAL 状态的库）。"""
        out_dir = Path(db_path).parent / "journal" / "export"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = str(out_dir / ("_快照-%s.db" % time.strftime("%Y%m%d-%H%M%S")))
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(out)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        return out

    def put_snapshot(self, db_path: str, when: Optional[datetime] = None,
                     keep: int = 7) -> Optional[dict]:
        """一天一份库快照；当天重复触发就替换，云上只留最近 7 天。"""
        when = when or datetime.now()
        day = when.strftime("%Y%m%d")
        snap = self.snapshot_db(db_path)
        enc = None
        try:
            pdir = self.client.ensure_path(["摘星日记", "数据库快照"])
            name = "日记库-%s.db" % day
            self._prune_snapshots(pdir, day, keep)   # 当天旧件先清掉，避免叠一堆同名副本
            res = self.client.upload(snap, pdir, filename=name)
            rec = {
                "kind": "db_snapshot",
                "entry_id": None,
                "object_key": res.get("file_id", ""),
                "pdir_key": pdir,
                "name": name,
                "bytes": os.path.getsize(snap),
                "sha256": _sha256(snap),
                "local_path": "",
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            if self.store:
                self.store.add_cloud_object(**rec)
            logger.info("数据库快照上云 %s", name)
            return rec
        finally:
            for p in (snap, enc):
                if p and os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass

    # ---------- 聊天增量 ----------
    def put_chat_increment(self, payload: dict,
                           when: Optional[datetime] = None,
                           name_suffix: str = "") -> dict:
        """上传一次聊天增量；本地临时文件上传后立即删除。

        云端按“聊天记录/增量/”存放，每个文件带 from_rowid/to_rowid，
        合并时按 message_id 去重；全量包仍然是最终底稿。
        name_suffix 用于同一秒内区分“新消息”和“语音补字更正”两类文件。
        """
        from .text_export import write_json

        when = when or datetime.now()
        if not self.store:
            raise RuntimeError("聊天增量上传需要 JournalStore 以确定导出目录")
        export_dir = Path(self.store.db_path).parent / "journal" / "export"
        suffix = ("-" + name_suffix.strip()) if name_suffix.strip() else ""
        name = "聊天增量-%s%s.json" % (when.strftime("%Y%m%d-%H%M%S"), suffix)
        local_path = export_dir / name
        write_json(payload, local_path)
        try:
            pdir = self.client.ensure_path(["摘星日记", "聊天记录", "增量"])
            res = self.client.upload(str(local_path), pdir, filename=name)
            rec = {
                "kind": "chat_increment",
                "entry_id": None,
                "object_key": res.get("file_id", ""),
                "pdir_key": pdir,
                "name": name,
                "bytes": os.path.getsize(str(local_path)),
                "sha256": _sha256(str(local_path)),
                "local_path": "",
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            if self.store:
                self.store.add_cloud_object(**rec)
            logger.info("聊天增量上云 %s（%d 条，rowid %s→%s）",
                        name, payload.get("count", 0),
                        payload.get("from_rowid"), payload.get("to_rowid"))
            return rec
        finally:
            if os.path.exists(str(local_path)):
                try:
                    os.remove(str(local_path))
                except OSError:
                    pass

    # ---------- 聊天清单 ----------
    def put_chat_manifest(self, manifest: dict,
                          when: Optional[datetime] = None) -> dict:
        """上传聊天记录清单；换机恢复时先看清单，再按清单补增量。

        清单只是元数据，不含正文。上传后同样删除本地临时文件。
        """
        from .text_export import write_json

        when = when or datetime.now()
        if not self.store:
            raise RuntimeError("聊天清单上传需要 JournalStore 以确定导出目录")
        export_dir = Path(self.store.db_path).parent / "journal" / "export"
        local_path = export_dir / "聊天记录清单.json"
        payload = dict(manifest or {})
        payload.setdefault("schema", "wx-assist-chat-manifest-v1")
        payload["updated_at"] = when.isoformat(timespec="seconds")
        write_json(payload, local_path)
        try:
            pdir = self.client.ensure_path(["摘星日记", "聊天记录"])
            self._delete_same_name(pdir, local_path.name)
            res = self.client.upload(str(local_path), pdir,
                                     filename=local_path.name)
            rec = {
                "kind": "chat_manifest",
                "entry_id": None,
                "object_key": res.get("file_id", ""),
                "pdir_key": pdir,
                "name": local_path.name,
                "bytes": os.path.getsize(str(local_path)),
                "sha256": _sha256(str(local_path)),
                "local_path": "",
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            if self.store:
                self.store.add_cloud_object(**rec)
            logger.info("聊天清单上云 %s（last_rowid=%s）",
                        local_path.name, payload.get("last_rowid"))
            return rec
        finally:
            if os.path.exists(str(local_path)):
                try:
                    os.remove(str(local_path))
                except OSError:
                    pass

    # ---------- 知识库全文 ----------
    def put_knowledge_file(self, local_path: str, name: str,
                           when: Optional[datetime] = None,
                           kind: str = "knowledge_export") -> dict:
        """把已经生成好的全文/合并包上传；上传后删除本地临时文件。"""
        when = when or datetime.now()
        pdir = self.client.ensure_path(["摘星日记", "知识库全文"])
        self._delete_same_name(pdir, name)
        res = self.client.upload(local_path, pdir, filename=name)
        rec = {
            "kind": kind,
            "entry_id": None,
            "object_key": res.get("file_id", ""),
            "pdir_key": pdir,
            "name": name,
            "bytes": os.path.getsize(local_path),
            "sha256": _sha256(local_path),
            "local_path": "",
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        if self.store:
            self.store.add_cloud_object(**rec)
        logger.info("知识库包上云 %s", name)
        try:
            os.remove(local_path)
        except OSError:
            pass
        return rec

    def put_knowledge_export(self, journal_db: str,
                             messages_db: Optional[str] = None,
                             when: Optional[datetime] = None,
                             encrypt: bool = False) -> dict:
        """把日记、聊天、收藏、附件文字导出一份可被别的 AI 直接读取的 JSON。

        默认明文上传：云端文件本身就是标准 JSON，换机器、换 AI 工具都能直接读。
        只有显式传 encrypt=True 时才额外生成 .enc 冷备，避免“主库只有密文、
        离开本项目就打不开”的死路。
        """
        from .text_export import build_export, write_json

        when = when or datetime.now()
        export_dir = Path(journal_db).parent / "journal" / "export"
        day = when.strftime("%Y%m%d")
        local_path = export_dir / ("知识库全文-%s.json" % day)
        payload = build_export(journal_db, messages_db, when=when,
                               cloud_prefix=getattr(self.client, "prefix", None) or None)
        write_json(payload, local_path)
        upload_path = str(local_path)
        name = "知识库全文-%s.json" % day
        enc_path = ""
        if encrypt:
            enc_path = str(local_path) + ".enc"
            crypto.encrypt_file(str(local_path), enc_path)
            upload_path = enc_path
            name += ".enc"
        try:
            pdir = self.client.ensure_path(["摘星日记", "知识库全文"])
            self._delete_same_name(pdir, name)
            res = self.client.upload(upload_path, pdir, filename=name)
            rec = {
                "kind": "knowledge_export",
                "entry_id": None,
                "object_key": res.get("file_id", ""),
                "pdir_key": pdir,
                "name": name,
                "bytes": os.path.getsize(upload_path),
                "sha256": _sha256(upload_path),
                # 上传成功后本地明文包会删除；这里不把本地路径当成长期存放点。
                "local_path": "",
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            if self.store:
                self.store.add_cloud_object(**rec)
            logger.info("知识库全文上云 %s（日记 %d 条、聊天 %d 条、附件 %d 个、收藏 %d 条）",
                        name, payload["counts"]["diary"],
                        payload["counts"].get("messages", 0),
                        payload["counts"]["attachments"],
                        payload["counts"]["favorites"])
            return rec
        finally:
            # 用户要求除聊天外本地不长期保存正文；上传成功就清掉明文临时包。
            # 失败时保留，便于重试和排查，不会丢数据。
            if 'res' in locals():
                try:
                    if os.path.exists(str(local_path)):
                        os.remove(str(local_path))
                    # 同目录里以前遗留的全文包也一起清掉，避免“云端已更新、
                    # 本地还躺着一份旧正文”的假安心。
                    for old in export_dir.glob("知识库全文-*.json"):
                        if old != local_path:
                            try:
                                old.unlink()
                            except OSError:
                                pass
                except OSError:
                    pass
            try:
                if enc_path and os.path.exists(enc_path):
                    os.remove(enc_path)
            except OSError:
                pass

    # ---------- 附件原件 ----------
    def put_attachment(self, local_path: str, entry_id: Optional[int] = None,
                       filename: Optional[str] = None,
                       when: Optional[datetime] = None) -> dict:
        """图片/文件/视频原件直接上云；不加密，微云里能直接看/下载。

        路径：摘星日记/附件/YYYY-MM/；文件名前加时间，避免微云重名报 1051。
        """
        when = when or datetime.now()
        if not os.path.exists(local_path):
            raise FileNotFoundError(local_path)
        src_name = filename or os.path.basename(local_path)
        safe_name = re.sub(r'[<>:"/\\|?*\r\n\t]', "_", src_name).strip() or "附件"
        if len(safe_name) > 100:
            stem, ext = os.path.splitext(safe_name)
            safe_name = stem[:90] + ext[:10]
        name = "%s-%s" % (when.strftime("%Y%m%d-%H%M%S"), safe_name)
        pdir = self.client.ensure_path(["摘星日记", "附件", when.strftime("%Y-%m")])
        self._delete_same_name(pdir, name)
        res = self.client.upload(local_path, pdir, filename=name)
        rec = {
            "kind": "attachment",
            "entry_id": entry_id,
            "object_key": res.get("file_id", ""),
            "pdir_key": pdir,
            "name": name,
            "bytes": os.path.getsize(local_path),
            "sha256": _sha256(local_path),
            "local_path": local_path,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        if self.store:
            rec["cloud_id"] = self.store.add_cloud_object(**rec)
        logger.info("附件上云 %s", name)
        return rec

    # ---------- 云端清场 ----------
    def _delete_same_name(self, pdir: str, name: str) -> int:
        """上传前把同名旧件删掉（微云碰重名会报 1051，而不是自动覆盖）。"""
        try:
            out = self.client.call("weiyun.list", {"limit": 50, "get_type": 2,
                                                   "dir_key": pdir, "pdir_key": pdir})
        except Exception:  # noqa: BLE001
            return 0
        n = 0
        for f in out.get("file_list", []):
            if (f.get("filename") or "") == name:
                self.client.delete(f["file_id"], pdir, complete=True)
                n += 1
        return n

    def _prune_snapshots(self, pdir: str, today: str, keep: int) -> int:
        """删掉当天的旧副本，并按天只保留最近 keep 份标准命名。返回删除数量。"""
        out = self.client.call("weiyun.list", {"limit": 50, "get_type": 2,
                                               "dir_key": pdir, "pdir_key": pdir})
        files = out.get("file_list", [])
        parsed = []
        for f in files:
            name = f.get("filename") or ""
            m = re.search(r"日记库-(\d{8})", name)
            parsed.append((m.group(1) if m else "", name, f))
        days = sorted({d for d, _, _ in parsed if d}, reverse=True)
        allowed = set(days[:keep])
        removed = 0
        for d, name, f in parsed:
            # 只留"保留期内、不是今天、且是标准命名"的那一份；今天的一律先删，等新件传
            if d and d != today and d in allowed and name == "日记库-%s.db" % d:
                continue
            self.client.delete(f["file_id"], pdir, complete=True)
            removed += 1
        if removed:
            logger.info("清掉旧快照 %d 份，云上只留最近 %d 天", removed, keep)
        return removed

    def clean_snapshot_dir(self, pdir: str, keep: int = 7) -> int:
        """现成目录的收口：只留最近 keep 天各一份标准命名。"""
        out = self.client.call("weiyun.list", {"limit": 50, "get_type": 2,
                                               "dir_key": pdir, "pdir_key": pdir})
        files = out.get("file_list", [])
        parsed = []
        for f in files:
            name = f.get("filename") or ""
            m = re.search(r"日记库-(\d{8})", name)
            parsed.append((m.group(1) if m else "", name, f))
        days = sorted({d for d, _, _ in parsed if d}, reverse=True)
        allowed = set(days[:keep])
        removed = 0
        for d, name, f in parsed:
            if d and d in allowed and name == "日记库-%s.db" % d:
                continue
            self.client.delete(f["file_id"], pdir, complete=True)
            removed += 1
        return removed

    # ---------- 本地清场 ----------
    def clean_voice_cache(self, voice_dir: str) -> dict:
        """把语音缓存目录扫一遍：已经转成文字的不再留音频。

        早期版本曾把音频留在本地（还上过云），这里做一次性清场；
        现在新来的语音在转写落库的那一步就删了，目录平时应为空。
        """
        removed = 0
        freed = 0
        if not os.path.isdir(voice_dir):
            return {"removed": 0, "freed": 0}
        for name in os.listdir(voice_dir):
            p = os.path.join(voice_dir, name)
            if not os.path.isfile(p):
                continue
            try:
                size = os.path.getsize(p)
                os.remove(p)
                removed += 1
                freed += size
            except OSError:
                continue
        if removed:
            logger.info("清掉本地语音缓存 %d 个，释放 %d 字节", removed, freed)
        return {"removed": removed, "freed": freed}
