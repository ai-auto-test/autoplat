"""Audio routing and priority measurements (Chapter 8)."""
from __future__ import annotations

import time

import numpy as np

from autoplat.adapters.base import Hardware
from autoplat.services.audio_metrics import tone_level_dbfs
from autoplat.timeline import current_timeline


class AudioService:
    def __init__(self, hw: Hardware, evidence=None):
        self.hw, self.evidence = hw, evidence

    @property
    def fs(self) -> int:
        return self.hw.audio.sample_rate

    def play_tone(self, source: str, freq_hz: float, duration_s: float | None = None) -> None:
        self.hw.audio.play_tone(source, freq_hz, duration_s)
        tl = current_timeline()
        if tl is not None:
            tl.record("service", "audio", "play_tone", source=source, freq_hz=freq_hz, duration_s=duration_s)

    def stop_all(self) -> None:
        for source in ("media", "navigation", "phone", "chime"):
            self.hw.audio.stop(source)

    def record(self, duration_s: float) -> np.ndarray:
        return self.hw.audio.record(duration_s)

    def measure_level(self, freq_hz: float, duration_s: float = 0.5, settle_s: float = 0.0) -> float:
        if settle_s:
            time.sleep(settle_s)
        level = tone_level_dbfs(self.record(duration_s), self.fs, freq_hz)
        level = max(level, -120.0)
        tl = current_timeline()
        if tl is not None:
            tl.record("service", "audio", "measure_level", freq_hz=freq_hz, dbfs=round(level, 2))
        return level
