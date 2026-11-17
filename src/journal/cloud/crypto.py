"""日记上云前的加密：AES-256-GCM，一个文件一把随机盐，密钥只在本地 .env。

为什么加密：微云是个人网盘，账号一旦被登进去（或分享链接被误发），
里面就是"我今天喂了鸟什么、说了什么"这类私话。加密后网盘里只有密文，
文件名也不带标题，只有日期和内容指纹。
"""

from __future__ import annotations

import os
from typing import Optional

from Crypto.Cipher import AES

KEY_ENV = "JOURNAL_CLOUD_KEY"   # 32 字节，hex 或任意口令，程序内部做派生
MAGIC = b"WXJ1"                 # 文件头标记，便于识别是加密件


def _master_key() -> bytes:
    raw = os.environ.get(KEY_ENV, "").strip()
    if not raw:
        try:
            for line in open(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env"), encoding="utf-8"):
                if line.startswith(KEY_ENV + "="):
                    raw = line.split("=", 1)[1].strip()
                    break
        except OSError:
            raw = ""
    if not raw:
        # 没配就直接报错，不要静默用固定密钥（那种"假加密"最坑）
        raise RuntimeError("缺少 %s，云端加密无法启用" % KEY_ENV)
    if len(raw) == 64:
        try:
            return bytes.fromhex(raw)
        except ValueError:
            pass
    # 口令型：用 SHA256 拉平到 32 字节
    import hashlib
    return hashlib.sha256(raw.encode("utf-8")).digest()


def encrypt_bytes(data: bytes) -> bytes:
    key = _master_key()
    nonce = os.urandom(12)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    ct, tag = cipher.encrypt_and_digest(data)
    return MAGIC + nonce + tag + ct


def decrypt_bytes(blob: bytes) -> bytes:
    if not blob.startswith(MAGIC):
        return blob          # 老文件没加密，原样返回
    body = blob[len(MAGIC):]
    nonce, tag, ct = body[:12], body[12:28], body[28:]
    key = _master_key()
    return AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(ct, tag)


def encrypt_file(src: str, dst: Optional[str] = None) -> str:
    dst = dst or (src + ".enc")
    with open(src, "rb") as f:
        blob = encrypt_bytes(f.read())
    with open(dst, "wb") as f:
        f.write(blob)
    return dst


def decrypt_file(src: str, dst: Optional[str] = None) -> str:
    dst = dst or (src[:-4] if src.endswith(".enc") else src + ".dec")
    with open(src, "rb") as f:
        data = decrypt_bytes(f.read())
    with open(dst, "wb") as f:
        f.write(data)
    return dst
