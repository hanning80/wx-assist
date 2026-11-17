"""知识库正文抽取：把文件、网页、图片尽量变成可检索的文字。

原则：
- 原件先落到本地 attachments 目录，云盘只做备份；正文抽不出来不算丢原件。
- 能抽多少抽多少，抽不出来就明确返回空串，不编内容。
- 所有函数都不抛到调用方，调用方按空结果降级。
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import os
import re
import shutil
import subprocess
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
ATTACH_ROOT = REPO_ROOT / "data" / "journal" / "attachments"
MAX_TEXT = 20000
TEXT_EXTS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log",
             ".xml", ".yaml", ".yml", ".ini", ".srt", ".ass"}


def _safe_name(name: str) -> str:
    name = (name or "附件").strip().replace("\x00", "")
    name = re.sub(r'[<>:"/\\|?*\r\n\t]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        name = "附件"
    stem, ext = os.path.splitext(name)
    if len(stem) > 100:
        stem = stem[:100]
    return stem + ext[:20]


def _unique_path(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / _safe_name(name)
    if not p.exists():
        return p
    stem, ext = p.stem, p.suffix
    for i in range(2, 1000):
        q = folder / ("%s-%d%s" % (stem, i, ext))
        if not q.exists():
            return q
    return folder / ("%s-%d%s" % (stem, int(time.time()), ext))


def stage_bytes(data: bytes, filename: str, when: Optional[datetime] = None) -> dict:
    """把收到的附件字节落到本地，返回路径/大小/摘要。"""
    when = when or datetime.now()
    folder = ATTACH_ROOT / when.strftime("%Y-%m")
    path = _unique_path(folder, filename)
    path.write_bytes(data)
    return {"path": str(path), "filename": path.name,
            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def stage_file(src: str, filename: str = "", when: Optional[datetime] = None) -> dict:
    """把微信目录里的原件复制到本地知识库目录。"""
    src = str(src)
    name = filename or os.path.basename(src)
    when = when or datetime.now()
    folder = ATTACH_ROOT / when.strftime("%Y-%m")
    path = _unique_path(folder, name)
    shutil.copy2(src, path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {"path": str(path), "filename": path.name,
            "bytes": path.stat().st_size, "sha256": h.hexdigest()}


def _read_text(path: str) -> str:
    data = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "ignore")


def _strip_xml(xml: str) -> str:
    xml = re.sub(r"<a:br\s*/>", "\n", xml, flags=re.I)
    xml = re.sub(r"</(?:w:p|a:p|text:p)>", "\n", xml, flags=re.I)
    xml = re.sub(r"<[^>]+>", "", xml)
    return html.unescape(xml)


def _docx_text(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "ignore")
    return re.sub(r"\n{3,}", "\n\n", _strip_xml(xml)).strip()


def _pptx_text(path: str) -> str:
    out = []
    with zipfile.ZipFile(path) as z:
        names = sorted(n for n in z.namelist()
                       if re.fullmatch(r"ppt/slides/slide\d+\.xml", n))
        for n in names:
            out.append(_strip_xml(z.read(n).decode("utf-8", "ignore")))
    return "\n\n".join(x.strip() for x in out if x.strip())


def _xlsx_text(path: str) -> str:
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            xml = z.read("xl/sharedStrings.xml").decode("utf-8", "ignore")
            for si in re.findall(r"<si[^>]*>(.*?)</si>", xml, re.S):
                shared.append(_strip_xml(si).strip())
        lines = list(shared)
        for n in sorted(n for n in z.namelist()
                        if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)):
            xml = z.read(n).decode("utf-8", "ignore")
            for cell in re.findall(r"<c[^>]*>(.*?)</c>", xml, re.S):
                txt = _strip_xml(cell).strip()
                if txt:
                    lines.append(txt)
    return "\n".join(lines)


def _pdf_text(path: str) -> str:
    try:
        from pypdf import PdfReader
    except Exception:
        return ""
    try:
        reader = PdfReader(path)
        out = []
        for page in reader.pages[:80]:
            out.append(page.extract_text() or "")
        return "\n".join(out)
    except Exception as e:  # noqa: BLE001
        logger.debug("PDF 抽取失败 %s: %s", path, e)
        return ""


def extract_text(path: str, kind: str = "") -> str:
    """按扩展名抽取正文；失败返回空串。"""
    p = Path(path)
    if not p.exists() or not p.is_file():
        return ""
    ext = p.suffix.lower()
    try:
        if ext in TEXT_EXTS:
            text = _read_text(str(p))
        elif ext == ".pdf":
            text = _pdf_text(str(p))
        elif ext == ".docx":
            text = _docx_text(str(p))
        elif ext == ".pptx":
            text = _pptx_text(str(p))
        elif ext == ".xlsx":
            text = _xlsx_text(str(p))
        elif ext in (".html", ".htm"):
            from bs4 import BeautifulSoup
            text = BeautifulSoup(_read_text(str(p)), "html.parser").get_text("\n")
        elif kind == "image" or ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"):
            text = ocr_image(str(p))
        else:
            text = ""
        text = re.sub(r"[ \t]+", " ", text or "")
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text[:MAX_TEXT]
    except Exception as e:  # noqa: BLE001
        logger.debug("正文抽取失败 %s: %s", path, e)
        return ""


def fetch_web_text(url: str, timeout: int = 15) -> dict:
    """抓网页标题和正文；登录页/反爬页抓不到就返回 ok=False。"""
    if not url or not re.match(r"^https?://", url, re.I):
        return {"ok": False, "error": "不是 http/https 链接"}
    try:
        import requests
        from bs4 import BeautifulSoup
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "缺少网页解析依赖: %s" % e}

    host = (urlparse(url).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1") or host.startswith("192.168."):
        return {"ok": False, "error": "只抓公网页面"}

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            "Referer": "https://mp.weixin.qq.com/",
        }
        resp = requests.get(url, timeout=timeout, headers=headers)
        if resp.status_code >= 400:
            return {"ok": False, "error": "HTTP %s" % resp.status_code}
        if "html" not in (resp.headers.get("Content-Type") or "").lower():
            return {"ok": False, "error": "不是网页"}
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "iframe"]):
            tag.decompose()

        # 公众号文章只取正文区，避免把“原创/作者/阅读器”这些页面壳当成正文。
        is_wechat_article = host == "mp.weixin.qq.com"
        title = ""
        body = ""
        author = ""
        if is_wechat_article:
            title_node = soup.select_one("#activity-name") or soup.title
            title = title_node.get_text(" ", strip=True) if title_node else ""
            author_node = soup.select_one("#js_name") or soup.select_one(
                ".rich_media_meta_nickname")
            author = author_node.get_text(" ", strip=True) if author_node else ""
            content = soup.select_one("#js_content") or soup.select_one(".rich_media_content")
            if content is not None:
                body = content.get_text("\n", strip=True)
                # 清掉公众号正文上方的页面导航噪声。
                for noise in ("在小说阅读器读本章", "去阅读", "在公众号小说中沉浸阅读"):
                    body = body.replace(noise, "")
                body = re.sub(r"\n{2,}", "\n", body).strip()
            # 公众号偶尔先返回只有“赞/在看”的壳页面，正文很短；隔一会儿再抓一次。
            if len(body) < 100:
                time.sleep(0.8)
                retry = requests.get(url, timeout=timeout, headers=headers)
                if retry.status_code < 400:
                    retry_soup = BeautifulSoup(retry.text, "html.parser")
                    title_node = retry_soup.select_one("#activity-name") or retry_soup.title
                    if title_node and not title:
                        title = title_node.get_text(" ", strip=True)
                    author_node = retry_soup.select_one("#js_name") or retry_soup.select_one(
                        ".rich_media_meta_nickname")
                    if author_node and not author:
                        author = author_node.get_text(" ", strip=True)
                    content = retry_soup.select_one("#js_content") or retry_soup.select_one(
                        ".rich_media_content")
                    if content is not None:
                        body = content.get_text("\n", strip=True)
                        for noise in ("在小说阅读器读本章", "去阅读", "在公众号小说中沉浸阅读"):
                            body = body.replace(noise, "")
                        body = re.sub(r"\n{2,}", "\n", body).strip()
        if not body:
            title = (soup.title.get_text(" ", strip=True) if soup.title else "").strip()
            body = soup.get_text("\n", strip=True)
            body = re.sub(r"\n{2,}", "\n", body).strip()
        if is_wechat_article and len(body) < 80:
            return {"ok": False, "error": "公众号正文只拿到页面壳，未取到正文",
                    "title": title[:120], "author": author[:80], "url": url}
        return {"ok": bool(body), "title": title[:120],
                "author": author[:80], "text": body, "url": url}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:120]}


def _image_ext(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"GIF8"):
        return ".gif"
    if data.startswith(b"BM"):
        return ".bmp"
    return ".jpg"


def decrypt_image(account_dir: str, md5: str, talker: str,
                  create_time: int = 0) -> Optional[bytes]:
    """解微信聊天图片；失败返回 None。"""
    try:
        from src.wechat.v2_cache_decrypt import V2CacheManager
        data_dir = os.getenv("WECHAT_DATA_DIR", "") or str(Path(account_dir).parent)
        mgr = V2CacheManager.get_instance(data_dir)
        wxid = Path(account_dir).name
        return mgr.decrypt_chat_image(md5, talker, create_time=create_time,
                                      wxid=wxid, size="original")
    except Exception as e:  # noqa: BLE001
        logger.debug("图片解密失败 %s: %s", md5, e)
        return None


def locate_file(account_dir: str, filename: str, md5: str = "") -> Optional[str]:
    """在微信 msg/file 下找文件本体；找不到返回 None。"""
    base = Path(account_dir) / "msg" / "file"
    if not base.exists():
        return None
    name = os.path.basename(filename or "")
    if name:
        for p in base.rglob(name):
            if p.is_file():
                return str(p)
    if md5:
        for p in base.rglob("*%s*" % md5):
            if p.is_file():
                return str(p)
    return None


def locate_video(account_dir: str, md5: str, create_time: int = 0,
                 raw_md5: str = "") -> dict:
    """找视频原文件/缩略图；微信多数只留缩略图。

    文件名不一定等于消息里的 md5。先用 hardlink.db 的
    ``video_hardlink_info_v4`` 把消息 md5 / rawmd5 还原成真实文件名，
    再回退到按 md5 直接找。
    """
    base = Path(account_dir) / "msg" / "video"
    out = {"video": None, "thumb": None, "raw": None}
    if not base.exists():
        return out

    # 1) hardlink 精确解析（消息 md5 -> 真实文件名）
    for key in (md5, raw_md5):
        if not key:
            continue
        try:
            from src.web.api_handlers import get_wcdb_client
            client = get_wcdb_client()
            if client:
                p = client.resolve_video_hardlink(key)
                if p and Path(p).is_file():
                    if Path(p).suffix.lower() in (".mp4", ".mov", ".m4v"):
                        out["video"] = p
                    elif Path(p).suffix.lower() in (".jpg", ".jpeg", ".png"):
                        out["thumb"] = p
        except Exception as e:  # noqa: BLE001
            logger.debug("视频硬链接解析失败 %s: %s", key, e)

    if md5:
        try:
            from src.web.api_handlers import get_wcdb_client
            client = get_wcdb_client()
            rp = client.resolve_video_hardlink(md5, raw=True) if client else ""
            if rp and Path(rp).is_file():
                out["raw"] = rp
        except Exception:
            pass

    # 2) 直接按 md5 找（旧版布局/无 hardlink 时）
    dirs = []
    if create_time:
        try:
            dirs.append(base / datetime.fromtimestamp(create_time).strftime("%Y-%m"))
        except Exception:
            pass
    dirs.append(base)
    for d in dirs:
        if not d.exists():
            continue
        for suffix in (".mp4", ".mov", ".m4v", ""):
            for p in d.rglob(md5 + suffix) if suffix else d.rglob(md5):
                if p.is_file() and p.suffix.lower() in (".mp4", ".mov", ".m4v"):
                    out["video"] = str(p)
                    break
            if out["video"]:
                break
        if out["video"]:
            break

    for p in (base.rglob(md5 + "_thumb.jpg")):
        if p.is_file():
            out["thumb"] = str(p)
            break
    return out


def ocr_image(path: str) -> str:
    """可选：调用 mediakit-cli 做图片文字识别；没装/没权限就返回空。"""
    exe = shutil.which("mediakit-cli")
    if not exe:
        return ""
    try:
        cp = subprocess.run(
            [exe, "image", "image-ocr", "--image-url", str(path)],
            capture_output=True, text=True, encoding="utf-8",
            errors="ignore", timeout=60,
        )
        if cp.returncode != 0 or not cp.stdout.strip():
            logger.debug("图片识别失败: %s", (cp.stderr or cp.stdout)[:120])
            return ""
        data = json.loads(cp.stdout)
        blocks = data.get("ocr_result") or []
        if not blocks and isinstance(data.get("data"), dict):
            blocks = data["data"].get("ocr_result") or []
        return "\n".join(str(b.get("content") or "") for b in blocks
                        if isinstance(b, dict) and b.get("content")).strip()
    except Exception as e:  # noqa: BLE001
        logger.debug("图片识别异常: %s", e)
        return ""


def parse_image_meta(content: str) -> dict:
    """从图片消息 XML 里取 md5/length。"""
    text = content or ""
    md5 = ""
    m = re.search(r'md5="([0-9a-fA-F]{32})"', text)
    if m:
        md5 = m.group(1).lower()
    if not md5:
        m = re.search(r"<md5>([0-9a-fA-F]{32})</md5>", text)
        if m:
            md5 = m.group(1).lower()
    size = 0
    m = re.search(r"(?<![A-Za-z])(?:length|totallen)=\"(\d+)\"", text)
    if m:
        size = int(m.group(1))
    return {"md5": md5, "size": size}


def parse_video_meta(content: str) -> dict:
    text = content or ""
    m = re.search(r'md5="([0-9a-fA-F]{32})"', text) or \
        re.search(r"<md5>([0-9a-fA-F]{32})</md5>", text)
    md5 = m.group(1).lower() if m else ""
    r = re.search(r'rawmd5="([0-9a-fA-F]{32})"', text) or \
        re.search(r"<rawmd5>([0-9a-fA-F]{32})</rawmd5>", text)
    raw_md5 = r.group(1).lower() if r else ""
    return {"md5": md5, "raw_md5": raw_md5}

