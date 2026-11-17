"""火山流式识别（单流攒段）。

对外只暴露一个动作：给一个 WAV，拿回文字。
内部按 v3 协议帧收发，实测过的三个坑都写在注释里。

实测要点（2026-11-15）：
  1. 整段一次性推完（FLAG_LAST）比按 100 毫秒切片快得多：18 秒音频 1.69 秒出全文；
  2. 必须用 websocket-client，websockets 新版解析响应头会崩；
  3. 音频要求 PCM 16k / 16bit / 单声道，采样率不对要先转。
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import struct
import subprocess
import sys
import time
import uuid
import wave
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream"
DEFAULT_RESOURCE = "volc.seedasr.sauc.duration"


def _find_ffmpeg() -> str:
    """优先用随程序打包的 ffmpeg；其次才看 PATH。

    微信语音经常是 24k 单声道，火山识别要 16k，必须转一道。打包后
    进程的 PATH 里通常没有 ffmpeg，不能只写 "ffmpeg"。
    """
    env = os.environ.get("WX_ASSIST_FFMPEG", "").strip()
    if env and Path(env).is_file():
        return env
    candidates = []
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        candidates.append(Path(meipass) / "tools" / "ffmpeg" / "ffmpeg.exe")
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent
                          / "tools" / "ffmpeg" / "ffmpeg.exe")
    candidates.append(Path(__file__).resolve().parents[2]
                      / "tools" / "ffmpeg" / "ffmpeg.exe")
    for p in candidates:
        if p.is_file():
            return str(p)
    return shutil.which("ffmpeg") or "ffmpeg"


MT_FULL_CLIENT = 1
MT_AUDIO_ONLY = 2
MT_ERROR = 15
FLAG_NONE = 0
FLAG_LAST = 2

MAX_SECONDS = 60  # 超过就截断，避免一包过大


def make_frame(mtype: int, flags: int, ser: int, payload: bytes) -> bytes:
    """拼一帧：4 字节头 + 4 字节大端长度 + 载荷。"""
    return (
        bytes([(1 << 4) | 1, (mtype << 4) | flags, (ser << 4) | 0, 0x00])
        + struct.pack(">I", len(payload))
        + payload
    )


def parse_frame(data: bytes) -> dict:
    """解一帧。服务端可能返回错误帧、JSON 帧或裸文本。"""
    if len(data) < 4:
        return {"_type": "raw", "flag": 0}
    h = data[:4]
    mtype = h[1] >> 4
    flags = h[1] & 0x0F
    ser = h[2] >> 4
    comp = h[2] & 0x0F
    pos = 4
    if flags in (1, 3):
        pos += 4
    if mtype == MT_ERROR:
        code = struct.unpack(">I", data[pos:pos + 4])[0]
        pos += 4
        size = struct.unpack(">I", data[pos:pos + 4])[0]
        pos += 4
        return {"_type": "error", "code": code,
                "message": data[pos:pos + size].decode("utf-8", "ignore")}
    if pos + 4 > len(data):
        return {"_type": "raw", "flag": flags}
    size = struct.unpack(">I", data[pos:pos + 4])[0]
    pos += 4
    payload = data[pos:pos + size]
    if comp == 1:
        try:
            payload = gzip.decompress(payload)
        except OSError:
            pass
    if ser == 1:
        try:
            return json.loads(payload)
        except ValueError:
            return {"_type": "text", "text": payload.decode("utf-8", "ignore")}
    return {"_type": "raw", "flag": flags}


def read_pcm(path: str | Path) -> tuple[bytes, int, int]:
    """读 WAV，返回 (PCM 数据, 采样率, 通道数)。"""
    with wave.open(str(path), "rb") as w:
        ch = w.getnchannels()
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())
    return frames, rate, ch


def ensure_16k_mono(src: str | Path, dst: Optional[str | Path] = None) -> str:
    """不是 16k/单声道就转一份，返回可直接送识别的路径。"""
    frames, rate, ch = read_pcm(src)
    if rate == 16000 and ch == 1:
        return str(src)
    dst = Path(dst) if dst else Path(src).with_name(Path(src).stem + "_16k.wav")
    ffmpeg = os.environ.get("FFMPEG_BIN", "").strip() or _find_ffmpeg()
    cmd = [ffmpeg, "-y", "-i", str(src), "-vn", "-acodec", "pcm_s16le",
           "-ar", "16000", "-ac", "1", str(dst)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=30)
    return str(dst)


def _truncate(frames: bytes, rate: int, ch: int, max_seconds: int = MAX_SECONDS) -> bytes:
    limit = int(rate * max_seconds) * 2 * ch
    return frames[:limit]


class VolcAsr:
    """火山识别客户端。密钥优先取参数，其次环境变量。"""

    def __init__(self, api_key: Optional[str] = None,
                 resource_id: str = DEFAULT_RESOURCE,
                 url: str = DEFAULT_URL, timeout: float = 20.0):
        self.api_key = api_key or os.environ.get("JOURNAL_ASR_KEY", "") or os.environ.get("VOLC_KEY", "")
        self.resource_id = resource_id
        self.url = url
        self.timeout = timeout

    def transcribe(self, wav_path: str | Path) -> dict:
        """给一个 WAV，返回 {ok, text, latency_ms, error}。"""
        if not self.api_key:
            return {"ok": False, "text": "", "latency_ms": 0,
                    "error": "缺少识别密钥（JOURNAL_ASR_KEY）"}
        try:
            fixed = ensure_16k_mono(wav_path)
            frames, rate, ch = read_pcm(fixed)
            frames = _truncate(frames, rate, ch)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "text": "", "latency_ms": 0, "error": f"音频处理失败: {e}"}

        t0 = time.time()
        try:
            text = self._run(frames, rate, ch)
            if not (text or "").strip():
                return {"ok": False, "text": "", "latency_ms": int((time.time() - t0) * 1000),
                        "error": "识别结果为空"}
            return {"ok": True, "text": text, "latency_ms": int((time.time() - t0) * 1000),
                    "error": ""}
        except Exception as e:  # noqa: BLE001
            logger.warning("火山识别失败: %s", e)
            return {"ok": False, "text": "", "latency_ms": int((time.time() - t0) * 1000),
                    "error": str(e)}

    def _run(self, frames: bytes, rate: int, ch: int) -> str:
        import websocket  # 必须用 websocket-client

        headers = [
            "X-Api-Key: %s" % self.api_key,
            "X-Api-Resource-Id: %s" % self.resource_id,
            "X-Api-Connect-Id: %s" % uuid.uuid4(),
            "X-Api-Request-Id: %s" % uuid.uuid4(),
        ]
        ws = websocket.create_connection(self.url, header=headers, timeout=self.timeout)
        try:
            req = {
                "user": {"uid": "wx_journal"},
                "audio": {"format": "pcm", "rate": rate, "bits": 16, "channel": ch},
                "request": {"model_name": "bigmodel", "enable_itn": True,
                            "enable_punc": True, "enable_nonstream": True,
                            "show_utterances": True},
            }
            ws.send(make_frame(MT_FULL_CLIENT, FLAG_NONE, 1, json.dumps(req).encode()))
            ws.send(make_frame(MT_AUDIO_ONLY, FLAG_LAST, 0, frames))

            texts: list[str] = []
            final = ""
            deadline = time.time() + 30
            while time.time() < deadline:
                try:
                    ws.settimeout(3)
                    raw = ws.recv()
                except Exception:
                    break
                if not raw:
                    break
                obj = parse_frame(raw)
                if obj.get("_type") == "error":
                    raise RuntimeError("识别服务报错 code=%s %s" % (
                        obj.get("code"), str(obj.get("message"))[:160]))
                res = obj.get("result")
                if not res:
                    continue
                if res.get("text"):
                    texts.append(res["text"])
                for u in res.get("utterances") or []:
                    if u.get("definite") and u.get("text"):
                        final = (final + u["text"]) if final else u["text"]
                if res.get("text") and not final:
                    final = res["text"]
            return (final or (texts[-1] if texts else "")).strip()
        finally:
            try:
                ws.close()
            except Exception:
                pass
