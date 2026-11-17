"""阿里云 OSS 适配器。

目标不是另起一套上传逻辑，而是把 OSS 伪装成现有 CloudStore 已经在用的
“目录 + 文件”客户端：ensure_path 返回对象前缀，upload 返回对象 key，
download_url 返回签名 URL，delete 直接删对象。

这样 storage.py / body_store.py / chat_sync.py / restore.py 的调用方式
可以保持不变，微云和 OSS 只在下层切换。
"""

from __future__ import annotations

import os
import posixpath
from typing import Optional

import oss2

from .base import CloudBackend, CloudError

DEFAULT_ENDPOINT = "https://oss-cn-beijing.aliyuncs.com"
DEFAULT_REGION = "oss-cn-beijing"
DEFAULT_PREFIX = "摘星日记"


class OssError(RuntimeError):
    pass


def _env(key: str, default: str = "") -> str:
    value = os.environ.get(key, "").strip()
    if value:
        return value
    path = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(key + "="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return default


def _join(*parts: str) -> str:
    clean = [p.strip().strip("/") for p in parts if p and p.strip().strip("/")]
    return "/".join(clean)


class OssClient(CloudBackend):
    """阿里云 OSS 适配器：把对象存储伪装成“目录 + 文件”客户端。"""

    name = "oss"

    def __init__(self, access_key_id: Optional[str] = None,
                 access_key_secret: Optional[str] = None,
                 endpoint: Optional[str] = None,
                 bucket_name: Optional[str] = None,
                 prefix: Optional[str] = None):
        self.access_key_id = (access_key_id or _env("OSS_ACCESS_KEY_ID")).strip()
        self.access_key_secret = (access_key_secret or _env("OSS_ACCESS_KEY_SECRET")).strip()
        self.endpoint = (endpoint or _env("OSS_ENDPOINT", DEFAULT_ENDPOINT)).strip()
        self.bucket_name = (bucket_name or _env("OSS_BUCKET")).strip()
        self.prefix = (prefix or _env("OSS_PREFIX", DEFAULT_PREFIX)).strip().strip("/")
        if not self.access_key_id or not self.access_key_secret:
            raise OssError("缺少 OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET")
        if not self.bucket_name:
            raise OssError("缺少 OSS_BUCKET")
        if not self.endpoint.startswith(("http://", "https://")):
            self.endpoint = "https://" + self.endpoint
        self.bucket = oss2.Bucket(
            oss2.Auth(self.access_key_id, self.access_key_secret),
            self.endpoint,
            self.bucket_name,
        )

    # ---- 目录 -> 前缀 ----
    def root_dir_key(self) -> str:
        return self.prefix

    def ensure_path(self, parts) -> str:
        items = [str(p).strip().strip("/") for p in parts if p and str(p).strip().strip("/")]
        # 旧代码传的是 ["摘星日记", "正文"]；适配器自己已经有根前缀，
        # 这里去掉重复的第一段，避免出现 摘星日记/摘星日记。
        if self.prefix and items and items[0] == self.prefix:
            items = items[1:]
        return _join(self.prefix, *items)

    def ensure_dir(self, name: str, pdir_key: str) -> str:
        # OSS 没有真实目录；前缀本身就是“目录”。
        return _join(pdir_key, name)

    # ---- 上传 ----
    def upload(self, path: str, pdir_key: str, filename: Optional[str] = None,
               max_rounds: int = 40) -> dict:
        if not os.path.exists(path):
            raise OssError("上传文件不存在：%s" % path)
        name = filename or os.path.basename(path)
        key = _join(pdir_key or self.prefix, name)
        try:
            self.bucket.put_object_from_file(key, path)
        except oss2.exceptions.OssError as exc:
            raise OssError("OSS 上传失败：%s" % exc) from exc
        return {
            "file_exist": True,
            "filename": name,
            "file_id": key,
            "file_size": os.path.getsize(path),
        }

    # ---- 下载 ----
    def download_url(self, file_id: str, pdir_key: str = "") -> dict:
        key = file_id or _join(pdir_key, "")
        if not key:
            raise OssError("缺少对象 key")
        try:
            url = self.bucket.sign_url("GET", key, 3600, slash_safe=True)
        except Exception as exc:  # noqa: BLE001
            raise OssError("OSS 生成下载链接失败：%s" % exc) from exc
        return {
            "file_id": key,
            "filename": posixpath.basename(key),
            "https_download_url": url,
            "download_url": url,
        }

    # ---- 删除 ----
    def delete(self, file_id: str, pdir_key: str = "", complete: bool = False) -> dict:
        key = file_id or _join(pdir_key, "")
        if not key:
            raise OssError("缺少对象 key")
        try:
            self.bucket.delete_object(key)
        except oss2.exceptions.OssError as exc:
            raise OssError("OSS 删除失败：%s" % exc) from exc
        return {"file_id": key, "deleted": True}

    # ---- 兼容旧代码里的 weiyun.list ----
    def list_files(self, pdir_key: str = "", limit: int = 50,
                   offset: int = 0) -> dict:
        prefix = (pdir_key or self.prefix).strip().strip("/")
        base = prefix + "/" if prefix else ""
        files = []
        more = False
        try:
            # ObjectIterator 会按 key 顺序翻完整个前缀；这里按 offset 跳过前 N 个，
            # 保证 restore._list_all 的“翻页”语义在 OSS 上真的生效。
            for idx, obj in enumerate(oss2.ObjectIterator(self.bucket, prefix=base)):
                if idx < offset:
                    continue
                if len(files) >= limit:
                    more = True
                    break
                name = obj.key[len(base):] if base and obj.key.startswith(base) else obj.key
                files.append({
                    "file_id": obj.key,
                    "filename": name,
                    "file_size": int(getattr(obj, "size", 0) or 0),
                    "file_mtime": int(getattr(obj, "last_modified", 0) or 0),
                })
        except oss2.exceptions.OssError as exc:
            raise OssError("OSS 列举失败：%s" % exc) from exc
        return {"file_list": files, "finish_flag": not more}

    def _list(self, prefix: str, limit: int = 1000) -> dict:
        return self.list_files(prefix, limit=limit)

    def call(self, tool: str, arguments: dict, timeout: int = 30) -> dict:
        if tool == "weiyun.list":
            prefix = (arguments or {}).get("dir_key") or (arguments or {}).get("pdir_key") or self.prefix
            return self.list_files(prefix, limit=min(int((arguments or {}).get("limit") or 1000), 1000))
        raise OssError("OSS 适配层不支持接口：%s" % tool)

    def selftest(self) -> str:
        return put_selftest(self)



def list_bucket_objects(client: OssClient, prefix: str = "", limit: int = 10) -> list[dict]:
    """给后台“测试连接”用，返回少量对象元数据。"""
    out = client._list(prefix or client.prefix, limit=limit)
    return out.get("file_list") or []


def put_selftest(client: OssClient) -> str:
    """写一个 2 字节小对象再删掉，验证不是只有列举权限。"""
    key = _join(client.prefix, "_后台连接测试.txt")
    client.bucket.put_object(key, b"ok")
    client.bucket.delete_object(key)
    return key
