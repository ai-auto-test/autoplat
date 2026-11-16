"""Objective audio measurements on float samples in [-1, 1] (Chapter 8)."""
from __future__ import annotations

import numpy as np


def rms_dbfs(x: np.ndarray) -> float:
    rms = np.sqrt(np.mean(np.square(x, dtype=np.float64))) if len(x) else 0.0
    return 20 * np.log10(max(rms, 1e-12))


def dominant_frequencies(x: np.ndarray, fs: int, n: int = 3) -> list[tuple[float, float]]:
    window = np.hanning(len(x))
    spectrum = np.abs(np.fft.rfft(x * window))
    freqs = np.fft.rfftfreq(len(x), 1 / fs)
    # Local maxima only, so one windowed tone does not occupy several neighbouring bins.
    peaks = np.flatnonzero((spectrum[1:-1] > spectrum[:-2]) & (spectrum[1:-1] >= spectrum[2:])) + 1
    peaks = peaks[np.argsort(spectrum[peaks])[-n:][::-1]]
    return [(float(freqs[i]), float(spectrum[i])) for i in peaks]


def tone_level_dbfs(x: np.ndarray, fs: int, freq_hz: float, bandwidth_hz: float = 20.0) -> float:
    """RMS level (dBFS) of the component near `freq_hz`; -inf-like floor when absent."""
    if len(x) == 0:
        return -240.0
    window = np.hanning(len(x))
    spectrum = np.fft.rfft(x * window)
    freqs = np.fft.rfftfreq(len(x), 1 / fs)
    band = np.abs(freqs - freq_hz) <= bandwidth_hz / 2
    # Parseval over the band, corrected for the window's energy.
    energy = np.sum(np.abs(spectrum[band]) ** 2) * 2 / (len(x) * np.sum(window ** 2))
    return 10 * np.log10(max(energy, 1e-24))


def clipping_ratio(x: np.ndarray, threshold: float = 0.999) -> float:
    return float(np.mean(np.abs(x) >= threshold)) if len(x) else 0.0


def silent_gaps(x: np.ndarray, fs: int, threshold_dbfs: float = -60, min_gap_s: float = 0.05
                ) -> list[tuple[float, float]]:
    frame = int(fs * 0.01)
    levels = [rms_dbfs(x[i:i + frame])
              for i in range(0, len(x) - frame, frame)]
    gaps, start = [], None
    for k, level in enumerate(levels):
        if level < threshold_dbfs and start is None:
            start = k
        elif level >= threshold_dbfs and start is not None:
            if (k - start) * 0.01 >= min_gap_s:
                gaps.append((start * 0.01, k * 0.01))
            start = None
    if start is not None and (len(levels) - start) * 0.01 >= min_gap_s:
        gaps.append((start * 0.01, len(levels) * 0.01))
    return gaps


def transient_score(x: np.ndarray) -> float:
    """Largest sample-to-sample step relative to the median step; clicks score far above 1."""
    d = np.abs(np.diff(x))
    if len(d) == 0:
        return 0.0
    return float(d.max() / max(np.median(d), 1e-9))


def estimate_delay_s(reference: np.ndarray, captured: np.ndarray, fs: int) -> float:
    n = len(reference) + len(captured) - 1
    size = 1 << (n - 1).bit_length()
    corr = np.fft.irfft(np.fft.rfft(captured, size) * np.conj(np.fft.rfft(reference, size)), size)
    lag = int(np.argmax(corr))
    if lag > size // 2:
        lag -= size
    return lag / fs


def tone(freq_hz: float, duration_s: float, fs: int, amplitude: float = 0.25) -> np.ndarray:
    t = np.arange(int(duration_s * fs)) / fs
    return amplitude * np.sin(2 * np.pi * freq_hz * t)
