"""云存储统一接口。

上层（storage / body_store / chat_sync / restore / media）只依赖本接口的
五个动作，不直接碰任何厂商 SDK：

    ensure_path(parts)         → 目录/前缀标识
    upload(path, pdir, name)   → 写入并返回 file_id
    download_url(file_id)      → 取一个短时下载地址
    delete(file_id, pdir)      → 删除对象
    call("weiyun.list", args)  → 兼容旧代码的列举入口

新增厂商只要实现 CloudBackend，并在 provider.py 注册，业务代码不动。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class CloudError(RuntimeError):
    """统一云存储异常；上层只需要捕获这一种。"""


class CloudBackend(ABC):
    """所有云存储适配器的共同契约。"""

    name: str = ""

    @abstractmethod
    def root_dir_key(self) -> str:
        """返回根前缀/根目录标识。"""

    @abstractmethod
    def ensure_path(self, parts) -> str:
        """按业务层传来的目录片段返回可用的目录/前缀标识。"""

    @abstractmethod
    def ensure_dir(self, name: str, pdir_key: str) -> str:
        """在指定父目录下确保子目录存在（对象存储可无操作）。"""

    @abstractmethod
    def upload(self, path: str, pdir_key: str,
               filename: Optional[str] = None,
               max_rounds: int = 40) -> dict:
        """上传一个本地文件，返回至少含 file_id / file_size 的字典。"""

    @abstractmethod
    def download_url(self, file_id: str, pdir_key: str = "") -> dict:
        """返回短时下载地址，至少含 https_download_url 或 download_url。"""

    @abstractmethod
    def delete(self, file_id: str, pdir_key: str = "",
               complete: bool = False) -> dict:
        """删除一个对象。"""

    @abstractmethod
    def list_files(self, pdir_key: str = "", limit: int = 50,
                   offset: int = 0) -> dict:
        """列举目录，返回 {"file_list": [...], "finish_flag": bool}。

        file_list 每项至少含 file_id / filename / file_size / file_mtime。
        """

    def call(self, tool: str, arguments: dict, timeout: int = 30) -> dict:
        """兼容旧代码的调用入口；默认只支持 weiyun.list。"""
        if tool == "weiyun.list":
            args = arguments or {}
            return self.list_files(
                args.get("dir_key") or args.get("pdir_key") or "",
                limit=min(int(args.get("limit") or 50), 1000),
                offset=int(args.get("offset") or 0),
            )
        raise CloudError("%s 适配层不支持接口：%s" % (self.name or "cloud", tool))

    def selftest(self) -> str:
        """可写性自检；默认不实现，适配器可覆盖。"""
        raise CloudError("%s 未实现写入自检" % (self.name or "cloud"))
