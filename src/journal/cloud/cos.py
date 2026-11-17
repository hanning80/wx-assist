"""腾讯云 COS 适配器。

与 OSS 适配器保持同一套 CloudBackend 契约：上层仍然只传“目录片段/对象 key”，
不感知签名、分页和厂商 SDK。切换只需改 .env 的 CLOUD_PROVIDER。

依赖 cos-python-sdk-v5；未安装时构造会给出明确错误，不会静默回退。
"""

from __future__ import annotations

import os
import posixpath
from pathlib import Path
from typing import Optional

from .base import CloudBackend, CloudError

DEFAULT_REGION = "ap-beijing"
DEFAULT_PREFIX = "摘星日记"

CosError = CloudError


def _env(*keys: str) -> str:
    """按顺序读环境变量，再读项目 .env；支持多个别名。"""
    for key in keys:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    path = Path(__file__).resolve().parents[3] / ".env"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() in keys:
                return v.strip()
    except OSError:
        pass
    return ""


def _mtime(value) -> int:
    """COS 的 LastModified 是 ISO 字符串（2026-11-17T02:36:46.000Z），统一转成秒。"""
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _join(*parts: str) -> str:
    clean = [p.strip().strip("/") for p in parts if p and p.strip().strip("/")]
    return "/".join(clean)


class CosClient(CloudBackend):
    name = "cos"

    def __init__(self, secret_id: Optional[str] = None,
                 secret_key: Optional[str] = None,
                 region: Optional[str] = None,
                 bucket: Optional[str] = None,
                 prefix: Optional[str] = None):
        self.secret_id = (secret_id or _env("COS_SECRET_ID", "TENCENT_COS_SECRET_ID")).strip()
        self.secret_key = (secret_key or _env("COS_SECRET_KEY", "TENCENT_COS_SECRET_KEY")).strip()
        self.region = (region or _env("COS_REGION", "TENCENT_COS_REGION") or DEFAULT_REGION).strip()
        self.bucket_name = (bucket or _env("COS_BUCKET", "TENCENT_COS_BUCKET")).strip()
        self.prefix = (prefix or _env("COS_PREFIX") or DEFAULT_PREFIX).strip().strip("/")
        if not self.secret_id or not self.secret_key:
            raise CosError("缺少 COS_SECRET_ID / COS_SECRET_KEY")
        if not self.bucket_name:
            raise CosError("缺少 COS_BUCKET")
        try:
            from qcloud_cos import CosConfig, CosS3Client
        except ImportError as exc:
            raise CosError("未安装 cos-python-sdk-v5，请先 pip install cos-python-sdk-v5") from exc
        config = CosConfig(Region=self.region, SecretId=self.secret_id,
                           SecretKey=self.secret_key, Scheme="https")
        self.client = CosS3Client(config)

    # ---- 目录 -> 前缀 ----
    def root_dir_key(self) -> str:
        return self.prefix

    def ensure_path(self, parts) -> str:
        items = [str(p).strip().strip("/") for p in parts if p and str(p).strip().strip("/")]
        if self.prefix and items and items[0] == self.prefix:
            items = items[1:]
        return _join(self.prefix, *items)

    def ensure_dir(self, name: str, pdir_key: str) -> str:
        return _join(pdir_key, name)

    # ---- 上传 ----
    def upload(self, path: str, pdir_key: str, filename: Optional[str] = None,
               max_rounds: int = 40) -> dict:
        if not os.path.exists(path):
            raise CosError("上传文件不存在：%s" % path)
        name = filename or os.path.basename(path)
        key = _join(pdir_key or self.prefix, name)
        try:
            self.client.upload_file(Bucket=self.bucket_name, Key=key,
                                    LocalFilePath=path, PartSize=8, MAXThread=4)
        except Exception as exc:  # noqa: BLE001
            raise CosError("COS 上传失败：%s" % exc) from exc
        return {"file_exist": True, "filename": name, "file_id": key,
                "file_size": os.path.getsize(path)}

    # ---- 下载 ----
    def download_url(self, file_id: str, pdir_key: str = "") -> dict:
        key = file_id or _join(pdir_key, "")
        if not key:
            raise CosError("缺少对象 key")
        try:
            url = self.client.get_presigned_url(Bucket=self.bucket_name, Key=key,
                                                Method="GET", Expired=3600)
        except Exception as exc:  # noqa: BLE001
            raise CosError("COS 生成下载链接失败：%s" % exc) from exc
        return {"file_id": key, "filename": posixpath.basename(key),
                "https_download_url": url, "download_url": url}

    # ---- 删除 ----
    def delete(self, file_id: str, pdir_key: str = "", complete: bool = False) -> dict:
        key = file_id or _join(pdir_key, "")
        if not key:
            raise CosError("缺少对象 key")
        try:
            self.client.delete_object(Bucket=self.bucket_name, Key=key)
        except Exception as exc:  # noqa: BLE001
            raise CosError("COS 删除失败：%s" % exc) from exc
        return {"file_id": key, "deleted": True}

    # ---- 列举 ----
    def list_files(self, pdir_key: str = "", limit: int = 50,
                   offset: int = 0) -> dict:
        prefix = (pdir_key or self.prefix).strip().strip("/")
        base = prefix + "/" if prefix else ""
        files = []
        seen = 0
        marker = ""
        more = False
        # 按 Marker 翻页；按 offset 跳过前 N 个，保证“翻页”语义一致。
        while True:
            kwargs = {"Bucket": self.bucket_name, "Prefix": base, "MaxKeys": 1000}
            if marker:
                kwargs["Marker"] = marker
            try:
                out = self.client.list_objects(**kwargs) or {}
            except Exception as exc:  # noqa: BLE001
                raise CosError("COS 列举失败：%s" % exc) from exc
            for obj in out.get("Contents") or []:
                seen += 1
                if seen <= offset:
                    continue
                if len(files) >= int(limit or 50):
                    more = True
                    break
                key = obj.get("Key") or ""
                name = key[len(base):] if base and key.startswith(base) else key
                files.append({
                    "file_id": key,
                    "filename": name,
                    "file_size": int(obj.get("Size") or 0),
                    "file_mtime": _mtime(obj.get("LastModified")),
                })
            if more or str(out.get("IsTruncated")).lower() != "true":
                break
            marker = out.get("NextMarker") or (files[-1]["file_id"] if files else "")
            if not marker:
                break
        return {"file_list": files, "finish_flag": not more}

    # ---- 自检 ----
    def selftest(self) -> str:
        key = _join(self.prefix, "_后台连接测试.txt")
        try:
            self.client.put_object(Bucket=self.bucket_name, Key=key, Body=b"ok")
            self.client.delete_object(Bucket=self.bucket_name, Key=key)
        except Exception as exc:  # noqa: BLE001
            raise CosError("COS 写入自检失败：%s" % exc) from exc
        return key


def list_bucket_objects(client: CosClient, prefix: str = "", limit: int = 10) -> list[dict]:
    return client.list_files(prefix or client.prefix, limit=limit).get("file_list") or []


def put_selftest(client: CosClient) -> str:
    return client.selftest()
