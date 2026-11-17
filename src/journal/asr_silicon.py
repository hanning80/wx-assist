"""硅基流动免费语音识别通道。

只做一件事：给一个 WAV，调用 /audio/transcriptions，拿回文字。
密钥优先取参数，其次取环境变量；日志里不打印密钥。
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_MODEL = "FunAudioLLM/SenseVoiceSmall"
MAX_BYTES = 50 * 1024 * 1024


def _env(key: str) -> str:
    """环境变量优先，其次项目 .env；避免只在 .env 写了 Key 却读不到。"""
    value = os.getenv(key, "").strip()
    if value:
        return value
    path = Path(__file__).resolve().parents[2] / ".env"
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith(key + "="):
                return s.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


class SiliconFlowAsr:
    def __init__(self, api_key: Optional[str] = None,
                 base_url: str = DEFAULT_BASE_URL,
                 model: str = DEFAULT_MODEL,
                 timeout: float = 60.0):
        self.api_key = (api_key
                        or _env("JOURNAL_ASR_SILICON_KEY")
                        or _env("SILICONFLOW_API_KEY")).strip()
        self.base_url = (base_url or _env("JOURNAL_ASR_SILICON_BASE_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.model = (model or _env("JOURNAL_ASR_SILICON_MODEL") or DEFAULT_MODEL).strip()
        self.timeout = timeout

    def transcribe(self, wav_path: str | Path) -> dict:
        if not self.api_key:
            return {"ok": False, "text": "", "latency_ms": 0,
                    "engine": "siliconflow",
                    "error": "缺少硅基流动密钥（JOURNAL_ASR_SILICON_KEY）"}

        source = Path(wav_path)
        if not source.is_file():
            return {"ok": False, "text": "", "latency_ms": 0,
                    "engine": "siliconflow", "error": "音频文件不存在"}

        t0 = time.time()
        fixed_path: Optional[Path] = None
        try:
            from .asr_volc import ensure_16k_mono
            fixed = ensure_16k_mono(source)
            fixed_path = Path(fixed)
            if fixed_path != source:
                pass
            else:
                fixed_path = None

            size = fixed_path.stat().st_size if fixed_path else source.stat().st_size
            if size > MAX_BYTES:
                return {"ok": False, "text": "", "latency_ms": 0,
                        "engine": "siliconflow", "error": "音频超过 50MB"}

            import requests
            with open(fixed_path or source, "rb") as f:
                resp = requests.post(
                    self.base_url + "/audio/transcriptions",
                    headers={"Authorization": "Bearer %s" % self.api_key},
                    files={"file": (source.name, f, "audio/wav")},
                    data={"model": self.model},
                    timeout=self.timeout,
                )
            latency = int((time.time() - t0) * 1000)
            if resp.status_code != 200:
                detail = (resp.text or "").strip()[:200]
                return {"ok": False, "text": "", "latency_ms": latency,
                        "engine": "siliconflow",
                        "error": "接口返回 %s：%s" % (resp.status_code, detail)}
            data = resp.json()
            text = (data.get("text") or "").strip()
            if not text:
                return {"ok": False, "text": "", "latency_ms": latency,
                        "engine": "siliconflow", "error": "识别结果为空"}
            return {"ok": True, "text": text, "latency_ms": latency,
                    "engine": "siliconflow", "error": ""}
        except Exception as e:  # noqa: BLE001
            logger.warning("硅基流动语音识别失败: %s", e)
            return {"ok": False, "text": "", "latency_ms": int((time.time() - t0) * 1000),
                    "engine": "siliconflow", "error": str(e)[:200]}
        finally:
            if fixed_path:
                try:
                    fixed_path.unlink(missing_ok=True)
                except OSError:
                    pass
