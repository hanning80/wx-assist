"""流式 SHA1：微云要求的分块哈希不是"每块独立算"，而是同一台 SHA1 机器走走停停的内部状态。

标准库 hashlib 只能在结尾取最终摘要，拿不到中途的寄存器值，所以这里自己实现一版
（只做我们需要的：分块状态、整体摘要、尾部校验状态）。
"""

from __future__ import annotations

BLOCK = 524288  # 微云固定 512KB 分块


def _rol(n, b):
    n &= 0xFFFFFFFF
    return ((n << b) | (n >> (32 - b))) & 0xFFFFFFFF


class Sha1Stream:
    """可随时取内部寄存器状态的 SHA1。

    state_hex() 返回 h0..h4 各 4 字节小端拼接后的 20 字节十六进制（40 字符），
    这正是微云 block_sha_list / check_sha 要的东西。
    """

    def __init__(self):
        self.h = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0]
        self.buf = bytearray()
        self.count = 0  # 已喂入的总字节数

    # ---- 内部：压缩一个 64 字节块 ----
    def _compress(self, block: bytes):
        w = [int.from_bytes(block[i:i + 4], "big") for i in range(0, 64, 4)]
        for i in range(16, 80):
            w.append(_rol(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1))
        a, b, c, d, e = self.h
        for i in range(80):
            if i < 20:
                f = (b & c) | ((~b) & d)
                k = 0x5A827999
            elif i < 40:
                f = b ^ c ^ d
                k = 0x6ED9EBA1
            elif i < 60:
                f = (b & c) | (b & d) | (c & d)
                k = 0x8F1BBCDC
            else:
                f = b ^ c ^ d
                k = 0xCA62C1D6
            t = (_rol(a, 5) + (f & 0xFFFFFFFF) + e + k + w[i]) & 0xFFFFFFFF
            e, d, c, b, a = d, c, _rol(b, 30), a, t
        self.h = [(x + y) & 0xFFFFFFFF for x, y in
                  zip(self.h, [a, b, c, d, e])]

    # ---- 对外 ----
    def update(self, data: bytes):
        self.count += len(data)
        self.buf.extend(data)
        while len(self.buf) >= 64:
            self._compress(bytes(self.buf[:64]))
            del self.buf[:64]

    def state_hex(self) -> str:
        """取当前寄存器状态（小端序），要求此时缓冲区里没有未压缩的整块。"""
        return b"".join(x.to_bytes(4, "little") for x in self.h).hex()

    def digest_hex(self) -> str:
        """按标准 SHA1 收尾，返回 40 字符摘要（大端序）。"""
        h = list(self.h)
        buf = bytearray(self.buf)
        buf.append(0x80)
        while len(buf) % 64 != 56:
            buf.append(0)
        buf.extend((self.count * 8 & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "big"))
        saved = self.h
        self.h = h
        self.buf = bytearray()
        for i in range(0, len(buf), 64):
            self._compress(bytes(buf[i:i + 64]))
        out = b"".join(x.to_bytes(4, "big") for x in self.h).hex()
        self.h = saved
        return out


def block_info(path: str) -> dict:
    """算出微云预上传要的一整套参数：分块哈希、文件摘要、尾部校验。"""
    import base64
    import hashlib

    s = Sha1Stream()
    md5 = hashlib.md5()
    size = 0
    blocks = []
    last_block = b""
    with open(path, "rb") as f:
        while True:
            chunk = f.read(BLOCK)
            if not chunk:
                break
            size += len(chunk)
            md5.update(chunk)
            if last_block:
                # 上一块确认不是最后一块 → 落定它的状态
                blocks.append(last_block_state)
            s.update(chunk)
            last_block = chunk
            last_block_state = s.state_hex()

    if not last_block:
        raise ValueError("空文件没有分块")

    last_size = len(last_block)
    check_size = last_size % 128 or 128
    file_sha = s.digest_hex()
    blocks.append(file_sha)  # 最后一块用整文件摘要

    # check_sha：重来一遍，走到"最后块去掉尾部 check_size"的位置取状态
    s2 = Sha1Stream()
    with open(path, "rb") as f:
        fed = 0
        while fed < size - last_size:
            n = min(BLOCK, size - last_size - fed)
            s2.update(f.read(n))
            fed += n
    rest = last_size - check_size
    if rest:
        with open(path, "rb") as f:
            f.seek(size - last_size)
            s2.update(f.read(rest))

    return {
        "size": size,
        "md5": md5.hexdigest(),
        "block_sha_list": blocks,
        "file_sha": file_sha,
        "check_sha": s2.state_hex(),
        "check_data": base64.b64encode(last_block[rest:]).decode(),
    }
