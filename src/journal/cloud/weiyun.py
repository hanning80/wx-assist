"""微云（腾讯微云）客户端：直连它的 MCP HTTP 端点，不走任何中间代理。

端点 https://www.weiyun.com/api/v3/mcpserver，认证靠请求头 WyHeader: mcp_token=...
只用得上几件事：列目录、建目录、上传、拿下载链接、删除。

上传是两阶段：预上传（带一堆 SHA1 分块状态）→ 若没秒传，再按通道分片上传。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from typing import Optional

import requests

from .base import CloudBackend
from .sha1stream import BLOCK, block_info

logger = logging.getLogger(__name__)

DEFAULT_URL = "https://www.weiyun.com/api/v3/mcpserver"
ROOT_DIR_NAME = "摘星日记"          # 网盘根的目录名
RETRY_CODES = {50000, 117406, -89016}   # 服务繁忙 / 后端临时失败 / 上传繁忙


class WeiyunError(RuntimeError):
    pass


def load_token() -> str:
    tok = os.environ.get("WEIYUN_MCP_TOKEN", "").strip()
    if tok:
        return tok
    path = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env")
    try:
        for line in open(path, encoding="utf-8"):
            if line.startswith("WEIYUN_MCP_TOKEN="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


class Weiyun(CloudBackend):
    name = "weiyun"

    def __init__(self, token: Optional[str] = None, url: str = DEFAULT_URL):
        self.token = token or load_token()
        if not self.token:
            raise WeiyunError("缺少 WEIYUN_MCP_TOKEN")
        self.url = url
        self.sess = requests.Session()
        self.sess.headers.update({
            "Content-Type": "application/json",
            "WyHeader": "mcp_token=%s" % self.token,
            "User-Agent": "wx-assist-journal/1.0",
        })

    # ---- 底层 ----
    def rpc(self, method: str, params: dict, timeout: int = 30) -> dict:
        body = {"jsonrpc": "2.0", "id": int(time.time() * 1000) % 2147483647,
                "method": method, "params": params}
        r = self.sess.post(self.url, data=json.dumps(body), timeout=timeout)
        if r.status_code != 200:
            raise WeiyunError("HTTP %s %s" % (r.status_code, r.text[:200]))
        data = r.json()
        if "error" in data:
            raise WeiyunError(str(data["error"]))
        return data.get("result", {})

    def call(self, tool: str, arguments: dict, timeout: int = 30) -> dict:
        res = self.rpc("tools/call", {"name": tool, "arguments": arguments}, timeout=timeout)
        if res.get("isError"):
            raise WeiyunError(str(res)[:300])
        txt = ""
        for item in res.get("content", []):
            if item.get("type") == "text":
                txt += item.get("text", "")
        try:
            out = json.loads(txt) if txt else {}
        except json.JSONDecodeError:
            out = {"raw": txt}
        if out.get("error"):
            raise WeiyunError(str(out["error"]))
        return out

    # ---- 目录 ----
    def root_dir_key(self) -> str:
        out = self.call("weiyun.list", {"limit": 50, "get_type": 0})
        return out.get("pdir_key", "")

    def find_dir(self, name: str, pdir_key: str) -> Optional[str]:
        out = self.call("weiyun.list", {"limit": 50, "get_type": 1, "dir_key": pdir_key,
                                        "pdir_key": pdir_key})
        for d in out.get("dir_list", []):
            if d.get("dir_name") == name:
                return d.get("dir_key")
        return None

    def ensure_dir(self, name: str, pdir_key: str) -> str:
        key = self.find_dir(name, pdir_key)
        if key:
            return key
        out = self.call("weiyun.create_dir", {"dir_name": name, "pdir_key": pdir_key})
        key = out.get("dir_key", "")
        if not key:
            raise WeiyunError("建目录失败：%s" % out)
        logger.info("微云建目录 %s -> %s", name, key)
        return key

    def ensure_path(self, parts) -> str:
        pdir = self.root_dir_key()
        if not pdir:
            raise WeiyunError("拿不到网盘根目录 key")
        for p in parts:
            pdir = self.ensure_dir(p, pdir)
        return pdir

    # ---- 上传 ----
    def upload(self, path: str, pdir_key: str, filename: Optional[str] = None,
               max_rounds: int = 40) -> dict:
        info = block_info(path)
        name = filename or os.path.basename(path)
        base_args = {
            "filename": name,
            "file_size": info["size"],
            "file_sha": info["file_sha"],
            "file_md5": info["md5"],
            "block_sha_list": info["block_sha_list"],
            "check_sha": info["check_sha"],
            "check_data": info["check_data"],
            "pdir_key": pdir_key,
        }
        # 微云大文件一次预上传只返回当前一批 512KB 通道（实测每次 4 个）；
        # 传完这一批后，用同样的参数再预上传一次，服务端会返回下一批通道。
        # 旧实现每轮只传第一个通道，且把 block_sha_list 裁掉后再预上传，
        # 导致 5MB 以上的视频从第二批开始就报 -89016/-89004。
        with open(path, "rb") as f:
            for rnd in range(max_rounds):
                pre = self._retry(lambda: self.call("weiyun.upload", base_args, timeout=90))
                if pre.get("file_exist"):
                    return {"file_exist": True, "filename": name,
                            "file_id": pre.get("file_id", ""), "file_size": info["size"]}

                upload_key = pre.get("upload_key") or ""
                channels = pre.get("channel_list") or []
                ex = pre.get("ex") or ""
                if not upload_key or not channels:
                    raise WeiyunError("预上传没给 upload_key/通道：%s" % str(pre)[:240])

                for idx, ch in enumerate(channels):
                    f.seek(int(ch.get("offset", 0)))
                    chunk = f.read(int(ch.get("len", BLOCK)))
                    part = {
                        "filename": name,
                        "upload_key": upload_key,
                        "channel_list": channels,
                        "channel_id": ch.get("id"),
                        "ex": ex,
                        "file_data": base64.b64encode(chunk).decode(),
                        "file_size": info["size"],
                        "file_sha": info["file_sha"],
                        "block_sha_list": info["block_sha_list"],
                        "check_sha": info["check_sha"],
                        "check_data": info["check_data"],
                        "pdir_key": pdir_key,
                    }
                    st = self._retry(lambda: self.call("weiyun.upload", part, timeout=120))
                    if st.get("file_exist") or (st.get("upload_state") == 2):
                        file_id = st.get("file_id") or ""
                        # 分片完成响应经常不带 file_id，但紧接着同名预上传
                        # 会返回 file_exist=True 和真正的 file_id。
                        if not file_id:
                            try:
                                fin = self.call("weiyun.upload", base_args, timeout=90)
                                if fin.get("file_exist"):
                                    file_id = fin.get("file_id", "")
                            except WeiyunError:
                                pass
                        return {"file_exist": True, "filename": name,
                                "file_id": file_id,
                                "file_size": info["size"]}
                    upload_key = st.get("upload_key") or upload_key
                    ex = st.get("ex") or ex
                    if st.get("error"):
                        raise WeiyunError("分片 %d 上传失败：%s" % (idx, st["error"]))
                # 这一批通道都传完仍未完成，下一轮预上传取下一批。

        raise WeiyunError("分片上传未完成：%s" % name)

        raise WeiyunError("分片上传未完成：%s" % name)

    def _retry(self, fn, times: int = 3):
        delay = 2
        for i in range(times):
            try:
                return fn()
            except WeiyunError as e:
                code = 0
                m = re.search(r'(?:retcode|code)\s*[=:]\s*(-?\d+)', str(e))
                if m:
                    try:
                        code = int(m.group(1))
                    except ValueError:
                        code = 0
                if code in RETRY_CODES and i < times - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise
        raise WeiyunError("重试后仍失败")

    # ---- 下载 / 删除 ----
    def download_url(self, file_id: str, pdir_key: str) -> dict:
        out = self.call("weiyun.download", {"items": [{"file_id": file_id, "pdir_key": pdir_key}]})
        items = out.get("items") or out.get("file_list") or []
        return items[0] if items else out

    def delete(self, file_id: str, pdir_key: str, complete: bool = False) -> dict:
        return self.call("weiyun.delete", {"file_list": [{"file_id": file_id, "pdir_key": pdir_key}],
                                           "delete_completely": complete})

    def list_files(self, pdir_key: str = "", limit: int = 50,
                   offset: int = 0) -> dict:
        args = {"limit": min(int(limit), 1000), "get_type": 2,
                "dir_key": pdir_key, "pdir_key": pdir_key, "offset": int(offset)}
        return self.call("weiyun.list", args)

    def selftest(self) -> str:
        """只验证 token 能列根目录，不写任何东西。"""
        key = self.root_dir_key()
        if not key:
            raise WeiyunError("微云根目录不可读")
        return key
