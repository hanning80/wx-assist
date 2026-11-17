"""语音识别通道选择：默认只用硅基流动免费模型；火山只在明确选择时启用。"""

from __future__ import annotations

import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_shared = None
_shared_lock = threading.Lock()


class FallbackAsr:
    """先走主通道，失败再走备用通道；两边都失败时把错误合并返回。"""

    def __init__(self, primary, secondary=None):
        self.primary = primary
        self.secondary = secondary

    def transcribe(self, wav_path) -> dict:
        first = None
        if self.primary is not None:
            first = self.primary.transcribe(wav_path)
            if first.get("ok") and (first.get("text") or "").strip():
                return first
            logger.warning("主语音通道失败，尝试备用通道: %s",
                           str(first.get("error") or "")[:160])
        if self.secondary is not None:
            second = self.secondary.transcribe(wav_path)
            if second.get("ok") and (second.get("text") or "").strip():
                second["fallback_from"] = (first or {}).get("engine", "")
                return second
            first_error = (first or {}).get("error") or ""
            second_error = second.get("error") or ""
            return {
                "ok": False, "text": "", "latency_ms": 0,
                "engine": second.get("engine", ""),
                "error": ("主通道：%s；备用通道：%s" % (first_error, second_error))[:260],
            }
        return first or {"ok": False, "text": "", "latency_ms": 0,
                         "engine": "", "error": "没有可用的语音识别通道"}


def build_asr(provider: Optional[str] = None):
    """按配置构建识别器。

    默认 siliconflow：只走免费模型，不自动切到付费火山；
    只有显式配置 volc 时才启用火山，避免静默产生费用。
    """
    from src.config import load_config
    from .asr_silicon import SiliconFlowAsr
    from .asr_volc import VolcAsr

    cfg = load_config()
    mode = (provider or cfg.journal_asr_provider or "siliconflow").strip().lower()

    sf = None
    if cfg.journal_asr_silicon_key:
        sf = SiliconFlowAsr(
            api_key=cfg.journal_asr_silicon_key,
            base_url=cfg.journal_asr_silicon_base_url,
            model=cfg.journal_asr_silicon_model,
        )

    if mode == "volc":
        return FallbackAsr(VolcAsr())

    # 兼容旧配置 auto：只有显式写 auto 才保留火山备用；
    # 当前正式配置 siliconflow 不静默转付费。
    if mode == "auto":
        return FallbackAsr(sf, VolcAsr())

    # 默认和显式 siliconflow 都只用免费通道；失败由队列重试。
    return FallbackAsr(sf)


def get_shared_asr():
    """全进程共用一个识别器，避免每条语音重新读配置。"""
    global _shared
    if _shared is None:
        with _shared_lock:
            if _shared is None:
                _shared = build_asr()
    return _shared
