"""媒体资产入库：图片/视频原件进 OSS，文字才进检索。

设计口径：
- 原件按 SHA-256 去重，OSS 只存一份；同一条媒体出现在日记、收藏、聊天里时
  只在 media_ref 里多记一个来源，不重复占空间。
- 缩略图、OCR 文字、视频转写文字分别作为独立对象存放，数据库只留 key 和文字。
- 图片文字走 mediakit-cli；视频先用 ffmpeg 抽 16k 单声道音频，再走硅基流动。
- 本地原件只在“上传成功”后删除，而且只删附件暂存目录里的文件。
"""
from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
TMP_ROOT = REPO_ROOT / "data" / "journal" / "export" / "media_tmp"
FFMPEG = REPO_ROOT / "tools" / "ffmpeg" / "ffmpeg.exe"
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".wmv", ".flv"}
MEDIA_PREFIX = ["摘星日记", "媒体"]


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def media_upload_enabled() -> bool:
    """是否把图片/视频原件上云；默认开，关闭后不占云端存储。"""
    return _env_bool("JOURNAL_MEDIA_UPLOAD", True)


def media_rag_enabled() -> bool:
    """图片/视频的 OCR/转写文字是否参与问答检索；默认关，避免媒体噪声。"""
    return _env_bool("JOURNAL_MEDIA_RAG", False)


def _sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _asset_id(sha: str) -> str:
    return sha[:16]


def _mime(path: str | Path) -> str:
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def _kind_of(path: str | Path, hint: str = "") -> str:
    ext = Path(path).suffix.lower()
    if hint in ("video", "video_thumb"):
        return "video" if hint == "video" else "image"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    return "file"


def _upload(client, local_path: str, parts: list[str], name: str) -> dict:
    pdir = client.ensure_path(parts)
    return client.upload(local_path, pdir, filename=name)


def _safe_ext(path: str | Path) -> str:
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXTS | VIDEO_EXTS:
        return ext
    return ".bin"


def _tmp_dir() -> Path:
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    return TMP_ROOT


def _cleanup(path: str | Path | None) -> None:
    if not path:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _run(cmd: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8",
        errors="ignore", timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _video_meta(path: str | Path) -> dict:
    """用 ffmpeg 的 stderr 取时长和分辨率，不依赖 ffprobe。"""
    out = {"duration_ms": 0, "width": 0, "height": 0}
    if not FFMPEG.exists():
        return out
    try:
        cp = _run([str(FFMPEG), "-hide_banner", "-i", str(path)], timeout=30)
        text = (cp.stderr or "") + "\n" + (cp.stdout or "")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
        if m:
            out["duration_ms"] = int((int(m.group(1)) * 3600 + int(m.group(2)) * 60
                                      + float(m.group(3))) * 1000)
        m = re.search(r"Stream.*?Video:.*?(\d{2,5})x(\d{2,5})", text)
        if m:
            out["width"], out["height"] = int(m.group(1)), int(m.group(2))
    except Exception as e:  # noqa: BLE001
        logger.debug("读取视频元数据失败 %s: %s", path, e)
    return out


def _image_preview(src: str | Path, dst: str | Path, max_edge: int = 1600) -> dict:
    out = {"width": 0, "height": 0, "ok": False}
    try:
        from PIL import Image
        with Image.open(src) as im:
            out["width"], out["height"] = im.size
            im = im.convert("RGBA")
            im.thumbnail((max_edge, max_edge), Image.LANCZOS)
            # 保留透明信息；无透明时仍用 WebP，体积小且浏览器都支持。
            im.save(dst, "WEBP", quality=82, method=4)
        out["ok"] = True
    except Exception as e:  # noqa: BLE001
        logger.debug("生成图片缩略图失败 %s: %s", src, e)
    return out


def _video_preview(src: str | Path, dst: str | Path) -> bool:
    if not FFMPEG.exists():
        return False
    for seek in ("00:00:01", "00:00:00"):
        try:
            cp = _run([str(FFMPEG), "-y", "-ss", seek, "-i", str(src),
                       "-frames:v", "1", "-q:v", "3", str(dst)], timeout=90)
            if dst.exists() and dst.stat().st_size > 0:
                return True
        except Exception:
            continue
    return False


def _extract_audio(src: str | Path, dst: str | Path, max_seconds: int = 1800) -> bool:
    if not FFMPEG.exists():
        return False
    try:
        cp = _run([str(FFMPEG), "-y", "-i", str(src), "-t", str(max_seconds),
                   "-vn", "-ac", "1", "-ar", "16000", "-f", "wav", str(dst)],
                  timeout=600)
        return dst.exists() and dst.stat().st_size > 0
    except Exception as e:  # noqa: BLE001
        logger.debug("视频抽音频失败 %s: %s", src, e)
        return False


def _transcribe_video(src: str | Path) -> dict:
    """抽音频后走现有硅基流动；失败不阻塞原件上云。"""
    wav = _tmp_dir() / ("asr-%s.wav" % datetime.now().strftime("%Y%m%d%H%M%S%f"))
    try:
        if not _extract_audio(src, wav):
            return {"ok": False, "text": "", "error": "ffmpeg 抽音频失败"}
        try:
            from .asr_provider import get_shared_asr
            asr = get_shared_asr()
        except Exception:
            from .asr_silicon import SiliconFlowAsr
            asr = SiliconFlowAsr()
        result = asr.transcribe(str(wav))
        return {"ok": bool(result.get("ok")), "text": (result.get("text") or "").strip(),
                "error": result.get("error") or ""}
    except Exception as e:  # noqa: BLE001
        logger.warning("视频转写失败 %s: %s", src, e)
        return {"ok": False, "text": "", "error": str(e)[:200]}
    finally:
        _cleanup(wav)


def _json_upload(client, payload: dict, parts: list[str], name: str) -> str:
    tmp = _tmp_dir() / name
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        res = _upload(client, str(tmp), parts, name)
        return str(res.get("file_id") or "")
    finally:
        _cleanup(tmp)


def upload_manifest(store, client=None) -> str:
    """生成知识库媒体清单；不是列桶，是给换机恢复和外部工具看的索引。"""
    if client is None:
        from .cloud.provider import make_client
        client = make_client()
    rows = store.list_media_assets(limit=10000)
    items = []
    for r in rows:
        a = dict(r)
        items.append({
            "asset_id": a.get("asset_id"),
            "sha256": a.get("sha256"),
            "kind": a.get("kind"),
            "mime": a.get("mime"),
            "bytes": a.get("bytes"),
            "width": a.get("width"),
            "height": a.get("height"),
            "duration_ms": a.get("duration_ms"),
            "created_at": a.get("created_at"),
            "original_key": a.get("original_key"),
            "preview_key": a.get("preview_key"),
            "text_key": a.get("text_key"),
            "ocr_text": (a.get("ocr_text") or "")[:300],
            "asr_text": (a.get("asr_text") or "")[:300],
            "caption": (a.get("caption") or "")[:200],
        })
    return _json_upload(client, {"version": 1,
                                 "updated_at": datetime.now().isoformat(timespec="seconds"),
                                 "count": len(items), "assets": items},
                        ["媒体"], "清单.json")


def signed_url(client, key: str) -> str:
    if not key:
        return ""
    try:
        return str((client.download_url(key) or {}).get("https_download_url") or "")
    except Exception as e:  # noqa: BLE001
        logger.debug("媒体签名链接失败 %s: %s", key, e)
        return ""


def ingest_media(store, source_path: str | Path, *, kind: str = "",
                 source_kind: str = "attachment", source_id: str = "",
                 message_id: str = "", local_id: int = 0,
                 entry_id: int = 0, fav_id: int = 0,
                 role: str = "original", caption: str = "",
                 extracted_text: str = "", client=None,
                 remove_local: bool = False) -> dict:
    """把一份本地媒体原件变成云端资产；返回 ok/asset_id/去重标记。"""
    if not media_upload_enabled():
        return {"ok": False, "error": "图片/视频上云已关闭", "disabled": True, "asset_id": ""}
    path = Path(source_path)
    if not path.is_file():
        return {"ok": False, "error": "媒体文件不存在", "asset_id": ""}
    k = _kind_of(path, kind)
    if k == "file":
        return {"ok": False, "error": "不是图片或视频", "asset_id": ""}

    sha = _sha256(path)
    aid = _asset_id(sha)
    existing = store.media_asset_by_sha(sha)
    if existing:
        store.add_media_ref(asset_id=existing["asset_id"], source_kind=source_kind,
                            source_id=source_id, message_id=message_id,
                            local_id=local_id, entry_id=entry_id,
                            fav_id=fav_id, role=role)
        if remove_local:
            _remove_local_if_staged(path)
        return {"ok": True, "asset_id": existing["asset_id"], "deduplicated": True,
                "sha256": sha, "kind": existing["kind"]}

    if client is None:
        from .cloud.provider import make_client
        client = make_client()

    suffix = _safe_ext(path)
    base = sha[:2]
    original_name = sha + suffix
    try:
        res = _upload(client, str(path), MEDIA_PREFIX + ["原始",
                       "图片" if k == "image" else "视频", base], original_name)
        original_key = str(res.get("file_id") or "")
    except Exception as e:  # noqa: BLE001
        logger.warning("媒体原件上云失败 %s: %s", path, e)
        return {"ok": False, "error": str(e)[:300], "asset_id": "", "sha256": sha}

    preview_key = ""
    text_key = ""
    width = height = duration_ms = 0
    ocr_text = asr_text = ""
    meta_extra: dict = {}
    tmp_preview: Optional[Path] = None
    try:
        if k == "image":
            try:
                from PIL import Image
                with Image.open(path) as im:
                    width, height = im.size
            except Exception:
                pass
            tmp_preview = _tmp_dir() / ("preview-%s.webp" % sha[:16])
            pv = _image_preview(path, tmp_preview)
            if pv.get("ok"):
                try:
                    pres = _upload(client, str(tmp_preview),
                                   MEDIA_PREFIX + ["预览", "图片", base],
                                   sha + ".webp")
                    preview_key = str(pres.get("file_id") or "")
                except Exception as e:  # noqa: BLE001
                    meta_extra["preview_error"] = str(e)[:120]
            ocr_text = (extracted_text or "").strip()
            if not ocr_text:
                try:
                    from .knowledge import ocr_image
                    ocr_text = (ocr_image(str(path)) or "").strip()
                except Exception as e:  # noqa: BLE001
                    meta_extra["ocr_error"] = str(e)[:120]
        else:
            meta = _video_meta(path)
            width, height = meta["width"], meta["height"]
            duration_ms = meta["duration_ms"]
            tmp_preview = _tmp_dir() / ("preview-%s.jpg" % sha[:16])
            if _video_preview(path, tmp_preview):
                try:
                    pres = _upload(client, str(tmp_preview),
                                   MEDIA_PREFIX + ["预览", "视频", base],
                                   sha + ".jpg")
                    preview_key = str(pres.get("file_id") or "")
                except Exception as e:  # noqa: BLE001
                    meta_extra["preview_error"] = str(e)[:120]
            tr = _transcribe_video(path)
            asr_text = tr.get("text") or ""
            if not tr.get("ok"):
                meta_extra["asr_error"] = tr.get("error") or "转写失败"

        text_payload = {
            "asset_id": aid, "sha256": sha, "kind": k,
            "caption": caption or path.name,
            "ocr_text": ocr_text, "asr_text": asr_text,
        }
        text_key = _json_upload(client, text_payload,
                                MEDIA_PREFIX + ["文字"], sha + ".json")
        meta_payload = {
            **text_payload, "mime": _mime(path), "bytes": path.stat().st_size,
            "width": width, "height": height, "duration_ms": duration_ms,
            "original_key": original_key, "preview_key": preview_key,
            "text_key": text_key, "source_kind": source_kind,
            "source_id": source_id, "message_id": message_id,
            "local_id": local_id, "entry_id": entry_id, "fav_id": fav_id,
            "role": role, "created_at": datetime.now().isoformat(timespec="seconds"),
            **meta_extra,
        }
        _json_upload(client, meta_payload, MEDIA_PREFIX + ["元数据"], sha + ".json")

        asset = store.upsert_media_asset(
            asset_id=aid, sha256=sha, kind=k, mime=_mime(path),
            bytes_=path.stat().st_size, width=width, height=height,
            duration_ms=duration_ms, original_key=original_key,
            preview_key=preview_key, text_key=text_key,
            ocr_text=ocr_text, asr_text=asr_text,
            caption=caption or path.name, state="uploaded",
        )
        store.add_media_ref(asset_id=aid, source_kind=source_kind,
                            source_id=source_id, message_id=message_id,
                            local_id=local_id, entry_id=entry_id,
                            fav_id=fav_id, role=role)
        try:
            upload_manifest(store, client=client)
        except Exception as e:  # noqa: BLE001
            logger.debug("媒体清单更新失败（不影响入库）: %s", e)
        if remove_local:
            _remove_local_if_staged(path)
        logger.info("媒体已上云 %s %s（%d 字节）", k, aid, path.stat().st_size)
        return {"ok": True, "asset_id": aid, "deduplicated": False,
                "sha256": sha, "kind": k, "asset": dict(asset)}
    except Exception as e:  # noqa: BLE001
        logger.warning("媒体入库失败 %s: %s", path, e)
        return {"ok": False, "error": str(e)[:300], "asset_id": aid, "sha256": sha}
    finally:
        _cleanup(tmp_preview)


def _remove_local_if_staged(path: Path) -> bool:
    """只删附件暂存目录内、已成功上云的原件；别碰用户自己的文件。"""
    try:
        from .knowledge import ATTACH_ROOT
        root = ATTACH_ROOT.resolve()
        target = Path(path).resolve()
        if root not in target.parents:
            return False
        try:
            os.chmod(str(target), 0o666)
        except OSError:
            pass
        target.unlink()
        logger.info("媒体已上云，删除本地暂存 %s", target.name)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("媒体已上云，本地暂存没删掉 %s: %s", path, e)
        return False


def process_attachment(store, attachment_id: int, client=None,
                       remove_local: bool = True) -> dict:
    """处理一条 attachment：上传原件、缩略图、OCR/转写、写库、清本地。"""
    row = store.attachment_asset(int(attachment_id))
    if row is None:
        return {"ok": False, "error": "附件不存在"}
    if row["asset_id"]:
        return {"ok": True, "asset_id": row["asset_id"], "deduplicated": True}
    kind = row["asset_kind"] if "asset_kind" in row.keys() and row["asset_kind"] else row["kind"]
    if (row["kind"] or "") not in ("image", "video", "video_thumb"):
        return {"ok": False, "error": "不是媒体附件"}
    path = row["local_path"] or ""
    if not path:
        return {"ok": False, "error": "没有本地原件，无法回溯"}

    result = ingest_media(
        store, path, kind=kind,
        source_kind="attachment", source_id=str(attachment_id),
        message_id=str(row["local_id"] or ""), local_id=int(row["local_id"] or 0),
        entry_id=int(row["entry_id"] or 0), role="cover" if row["kind"] == "video_thumb" else "original",
        caption=row["filename"] or "", extracted_text=row["extracted_text"] or "",
        client=client, remove_local=remove_local,
    )
    if result.get("ok") and result.get("asset_id"):
        store.set_attachment_asset(int(attachment_id), result["asset_id"],
                                   result.get("asset", {}).get("original_key", ""))
        if remove_local and not Path(path).exists():
            store.mark_attachment_local_purged(int(attachment_id))
        try:
            from .recall import index_attachment
            index_attachment(int(attachment_id))
        except Exception as e:  # noqa: BLE001
            logger.debug("媒体附件索引失败: %s", e)
    else:
        store.mark_attachment_error(int(attachment_id), result.get("error") or "媒体上云失败")
    return result


def scan_attachments(store, limit: int = 5, client=None) -> dict:
    """把还没 asset_id 的图片/视频补一遍；适合后台定时器和后台按钮。"""
    if not media_upload_enabled():
        return {"total": 0, "ok": 0, "failed": 0, "deduplicated": 0, "items": [],
                "disabled": True}
    rows = store.unprocessed_media_attachments(limit=limit)
    out = {"total": len(rows), "ok": 0, "failed": 0, "deduplicated": 0, "items": []}
    for row in rows:
        try:
            r = process_attachment(store, row["id"], client=client)
            out["items"].append({"id": row["id"], **r})
            if r.get("ok"):
                out["ok"] += 1
                if r.get("deduplicated"):
                    out["deduplicated"] += 1
            else:
                out["failed"] += 1
        except Exception as e:  # noqa: BLE001
            out["failed"] += 1
            out["items"].append({"id": row["id"], "ok": False, "error": str(e)[:200]})
    return out


def _sniff_ext(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"\xff\xd8"):
        return ".jpg"
    if data.startswith(b"GIF8"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[4:12] in (b"ftypmp42", b"ftypisom", b"ftypM4V "):
        return ".mp4"
    return ".jpg"


def _scan_other_accounts_enabled() -> bool:
    """是否把非当前登录的微信号目录也纳入扫描。

    默认关闭：一台机器同一时刻只有一个微信号在线，只有它的 V2 密钥可推导。
    已经退出登录的号目录里那批原件解不开，反复试只是空转，还会把“别的号的
    数据”混进来。需要时置 JOURNAL_SCAN_OTHER_ACCOUNTS=1 再开。
    """
    raw = (os.getenv("JOURNAL_SCAN_OTHER_ACCOUNTS") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _current_account_names() -> set:
    """按 WXID 挑出当前登录号的目录名，例如 wxid_abh6zf6773ta22_c8de。

    目录名 = 微信号 + "_" + 4位后缀，所以先精确匹配，再按前缀匹配；
    不能按 "_" 切分（wxid_ 本身带下划线，切了会变成 "wxid"）。
    """
    try:
        from src.config import load_config
        from src.wechat.account_paths import list_account_dirs
        cfg = load_config()
        data_dir = getattr(cfg, "wechat_data_dir", "") or os.getenv("WECHAT_DATA_DIR", "")
        wxid = (os.getenv("WXID") or getattr(cfg, "wxid", "") or "").strip()
        if not data_dir or not wxid:
            return set()
        names = {d.name for d in list_account_dirs(Path(data_dir))}
        if wxid in names:
            return {wxid}
        return {n for n in names if n.startswith(wxid)}
    except Exception:  # noqa: BLE001
        return set()


def _decrypt_fav_any(fav_id: int, fullmd5: str, fullsize: Optional[int]) -> Optional[bytes]:
    """收藏图片优先在当前登录号目录解；其余号默认不碰（见上方开关）。"""
    try:
        import os as _os
        from pathlib import Path as _Path
        from src.wechat.v2_cache_decrypt import V2CacheManager
        from src.wechat.account_paths import list_account_dirs
        data_dir = _os.getenv("WECHAT_DATA_DIR", "")
        if not data_dir:
            return None
        accounts = list_account_dirs(_Path(data_dir))
        if not accounts:
            return None
        if not _scan_other_accounts_enabled():
            current = _current_account_names()
            if current:
                accounts = [a for a in accounts if a.name in current] or accounts[:1]
        manager = V2CacheManager.get_instance(data_dir)
        for acc in accounts:
            try:
                data = manager.decrypt_fav_image(
                    int(fav_id), acc.name, size="original",
                    fullmd5=fullmd5 or None, fullsize=fullsize)
                if data:
                    return data
            except Exception:
                continue
    except Exception as e:  # noqa: BLE001
        logger.debug("收藏图片解密失败 %s: %s", fav_id, e)
    return None


def reclassify_gone(db_path: str) -> dict:
    """把“原件已不存在”的历史失败归到 gone，不再反复重试。

    只有两类属于确实没救：历史清洗时没留 md5（本来就没有原件指针）、
    微信缓存已清（原图/视频被客户端删掉）。其余失败保留 failed 继续重试。
    """
    import sqlite3
    patterns = ("没有图片 md5%", "没有视频 md5%",
                "微信缓存里找不到图片%", "微信缓存里找不到视频/封面%")
    out = {"moved": 0}
    try:
        conn = sqlite3.connect(str(db_path))
        cond = " OR ".join("error LIKE ?" for _ in patterns)
        out["moved"] = conn.execute(
            "UPDATE media_raw SET state='gone', updated_at=?"
            " WHERE state='failed' AND (%s)" % cond,
            (int(time.time()),) + patterns).rowcount
        conn.commit()
        conn.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("媒体失败归类失败: %s", e)
    return out


def media_stats(db_path: str) -> dict:
    import sqlite3
    out = {"done": 0, "failed": 0, "pending": 0, "gone": 0, "skipped": 0}
    try:
        conn = sqlite3.connect(str(db_path))
        for state, n in conn.execute("SELECT state, COUNT(*) FROM media_raw GROUP BY state"):
            out[state] = n
        conn.close()
    except Exception:
        pass
    return out


def scan_chat_media(store, limit: int = 4) -> dict:
    """扫描 messages.db 的 media_raw，回微信缓存取聊天图片/视频并上云。

    只处理入库时旁路保存过原始 XML 的消息；历史消息若当时已被清洗成
    “[图片]”，没有 md5 可回溯，不能伪造原件。
    """
    import sqlite3
    db = Path(store.db_path).parent / "messages.db"
    out = {"total": 0, "ok": 0, "failed": 0, "skipped": 0, "items": []}
    if not media_upload_enabled():
        out["disabled"] = True
        return out
    if not db.exists():
        return out
    reclassify_gone(str(db))
    try:
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='media_raw'").fetchone()
        if not exists:
            conn.close()
            return out
        rows = conn.execute(
            "SELECT * FROM media_raw WHERE state='pending' ORDER BY created_at ASC LIMIT ?",
            (max(1, int(limit)),)).fetchall()
        conn.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("读取聊天媒体旁路失败: %s", e)
        return out

    try:
        from src.config import load_config
        cfg = load_config()
        data_dir = getattr(cfg, "wechat_data_dir", "") or os.getenv("WECHAT_DATA_DIR", "")
        from src.wechat.account_paths import list_account_dirs
        accounts = list_account_dirs(Path(data_dir)) if data_dir else []
        if accounts and not _scan_other_accounts_enabled():
            current = _current_account_names()
            if current:
                accounts = [a for a in accounts if a.name in current] or accounts[:1]
    except Exception:
        accounts = []

    from .knowledge import parse_image_meta, parse_video_meta, decrypt_image, locate_video

    def mark(mid: str, state: str, error: str = ""):
        try:
            c = sqlite3.connect(str(db))
            c.execute("UPDATE media_raw SET state=?, attempts=attempts+1,"
                      " error=?, updated_at=? WHERE message_id=?",
                      (state, error[:300], int(time.time()), mid))
            c.commit()
            c.close()
        except Exception:
            pass

    for row in rows:
        out["total"] += 1
        mid = row["message_id"]
        raw = row["raw_content"] or ""
        mt = int(row["msg_type"] or 0)
        try:
            if mt == 3:
                meta = parse_image_meta(raw)
                md5 = meta.get("md5") or ""
                if not md5:
                    mark(mid, "gone", "没有图片 md5，历史清洗时没留原件信息")
                    out["failed"] += 1
                    continue
                data = None
                for acc in accounts:
                    try:
                        data = decrypt_image(str(acc), md5, row["chat_id"], int(row["msg_time"] or 0))
                    except Exception:
                        data = None
                    if data:
                        break
                if not data:
                    mark(mid, "gone", "微信缓存里已没有这张原图")
                    out["failed"] += 1
                    continue
                tmp = _tmp_dir() / ("chat-%s%s" % (mid[:12], _sniff_ext(data)))
                try:
                    tmp.write_bytes(data)
                    r = ingest_media(store, tmp, kind="image", source_kind="chat",
                                     source_id=mid, message_id=mid,
                                     local_id=int(row["local_id"] or 0),
                                     caption="聊天图片 %s" % row["chat_id"],
                                     remove_local=False)
                finally:
                    _cleanup(tmp)
            elif mt == 43:
                meta = parse_video_meta(raw)
                md5 = meta.get("md5") or ""
                if not md5:
                    mark(mid, "gone", "没有视频 md5，历史清洗时没留原件信息")
                    out["failed"] += 1
                    continue
                found = {}
                for acc in accounts:
                    try:
                        found = locate_video(str(acc), md5, int(row["msg_time"] or 0),
                                             raw_md5=meta.get("raw_md5", "")) or {}
                    except Exception:
                        found = {}
                    if found.get("video") or found.get("thumb"):
                        break
                src = found.get("video") or found.get("thumb")
                if not src:
                    mark(mid, "gone", "微信缓存里已没有这段视频/封面")
                    out["failed"] += 1
                    continue
                kind = "video" if found.get("video") else "video_thumb"
                r = ingest_media(store, src, kind=kind, source_kind="chat",
                                 source_id=mid, message_id=mid,
                                 local_id=int(row["local_id"] or 0),
                                 role="cover" if kind == "video_thumb" else "original",
                                 caption="聊天视频 %s" % row["chat_id"],
                                 remove_local=False)
            else:
                mark(mid, "skipped", "不是图片/视频")
                out["skipped"] += 1
                continue
            out["items"].append({"message_id": mid, **r})
            if r.get("ok"):
                mark(mid, "done")
                out["ok"] += 1
            else:
                mark(mid, "failed", r.get("error") or "上云失败")
                out["failed"] += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("聊天媒体处理失败 %s: %s", mid, e)
            mark(mid, "failed", str(e))
            out["failed"] += 1
    return out


def sync_favorite_images(store, limit: int = 120) -> dict:
    """把普通收藏里的图片/视频补进媒体库；视频号/朋友圈(4/20)不做任何利用。"""
    db = Path(store.db_path).parent / "messages.db"
    out = {"total": 0, "ok": 0, "failed": 0, "deduplicated": 0,
           "skipped": 0, "items": [], "reasons": {}}
    if not media_upload_enabled():
        out["disabled"] = True
        return out
    if not db.exists():
        return out
    try:
        import sqlite3
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT fav_id, type, media_json FROM fav_cache"
            " WHERE type NOT IN (4,20) AND media_json IS NOT NULL"
            " AND media_json!='' AND media_json!='[]'"
            " ORDER BY fav_id ASC LIMIT ?", (max(1, int(limit)),)).fetchall()
        conn.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("读取收藏媒体失败: %s", e)
        return out

    try:
        from src.config import load_config
        cfg = load_config()
        if getattr(cfg, "wechat_data_dir", ""):
            os.environ.setdefault("WECHAT_DATA_DIR", str(cfg.wechat_data_dir))
    except Exception:
        pass
    from src.web.api_handlers import _decrypt_fav_v2_media
    # 保留导入只为兼容旧环境；实际优先逐账号尝试。

    for row in rows:
        try:
            items = json.loads(row["media_json"] or "[]")
        except Exception:
            items = []
        if isinstance(items, dict):
            items = [items]
        for it in items:
            if not isinstance(it, dict):
                continue
            fullmd5 = str(it.get("fullmd5") or it.get("datakey") or "")
            if not fullmd5:
                out["skipped"] += 1
                out["reasons"]["没有原图标识"] = out["reasons"].get("没有原图标识", 0) + 1
                continue
            data = _decrypt_fav_any(int(row["fav_id"]), fullmd5,
                                    int(it.get("fullsize") or 0) or None)
            if not data:
                data = _decrypt_fav_v2_media(int(row["fav_id"]), fullmd5,
                                             int(it.get("fullsize") or 0) or None)
            if not data:
                out["skipped"] += 1
                if _scan_other_accounts_enabled():
                    reason = "缓存原件解不开（多为副账号密钥不可推导）"
                else:
                    reason = "原件不在当前登录微信号目录（其它微信号不纳入监控）"
                out["reasons"][reason] = out["reasons"].get(reason, 0) + 1
                continue
            ext = _sniff_ext(data)
            tmp = _tmp_dir() / ("fav-%s-%s%s" % (row["fav_id"], fullmd5[:8], ext))
            try:
                tmp.write_bytes(data)
                kind = "video" if ext in VIDEO_EXTS else "image"
                r = ingest_media(store, tmp, kind=kind, source_kind="fav",
                                 source_id=str(row["fav_id"]), fav_id=int(row["fav_id"]),
                                 caption="收藏图片/视频 %s" % row["fav_id"],
                                 remove_local=False)
                out["items"].append({"fav_id": row["fav_id"], **r})
                if r.get("ok"):
                    out["ok"] += 1
                    if r.get("deduplicated"):
                        out["deduplicated"] += 1
                else:
                    out["failed"] += 1
                out["total"] += 1
            finally:
                _cleanup(tmp)
    return out


def media_item(store, row, client=None) -> dict:
    """给后台/面板的展示结构；不含本地路径，只给签名 URL。"""
    d = dict(row)
    if client is None:
        try:
            from .cloud.provider import make_client
            client = make_client()
        except Exception:
            client = None
    d["preview_url"] = signed_url(client, d.get("preview_key") or "") if client else ""
    d["original_url"] = signed_url(client, d.get("original_key") or "") if client else ""
    d["display_url"] = d["preview_url"] or d["original_url"]
    # 面板/微信内置浏览器经常直接访问 OSS 外链失败；给同源代理地址，原件仍只存 OSS。
    aid = d.get("asset_id") or ""
    d["preview_proxy_url"] = ("/api/journal/media/%s/file?role=preview" % aid) if (aid and d.get("preview_key")) else ""
    d["original_proxy_url"] = ("/api/journal/media/%s/file?role=original" % aid) if (aid and d.get("original_key")) else ""
    d["refs"] = [dict(r) for r in store.media_refs(d.get("asset_id") or "")]
    return d
