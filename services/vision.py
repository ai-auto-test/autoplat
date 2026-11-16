"""Image measurements for display and camera checks (Chapters 6 and 7)."""
from __future__ import annotations

from typing import Any

import cv2
import numpy as np

_DICTIONARY = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
detector = cv2.aruco.ArucoDetector(_DICTIONARY)


def image_metrics(frame_bgr: np.ndarray) -> dict[str, float]:
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    return {
        "mean_luma": float(gray.mean()),
        "luma_p5": float(np.percentile(gray, 5)),
        "luma_p95": float(np.percentile(gray, 95)),
        "sharpness": float(cv2.Laplacian(gray, cv2.CV_64F).var()),
    }


def image_is_healthy(m: dict[str, float], limits) -> bool:
    return (limits.luma_min < m["mean_luma"] < limits.luma_max
            and m["luma_p95"] - m["luma_p5"] > limits.min_spread
            and m["sharpness"] > limits.min_sharpness)


def health_problems(m: dict[str, float], limits) -> list[str]:
    problems = []
    if not limits.luma_min < m["mean_luma"] < limits.luma_max:
        problems.append(f"mean_luma {m['mean_luma']:.1f} outside ({limits.luma_min}, {limits.luma_max})")
    if m["luma_p95"] - m["luma_p5"] <= limits.min_spread:
        problems.append(f"luma spread {m['luma_p95'] - m['luma_p5']:.1f} <= {limits.min_spread}")
    if m["sharpness"] <= limits.min_sharpness:
        problems.append(f"sharpness {m['sharpness']:.1f} <= {limits.min_sharpness}")
    return problems


def detect_marker(frame_bgr: np.ndarray) -> dict[int, np.ndarray] | None:
    corners, ids, _ = detector.detectMarkers(frame_bgr)
    if ids is None:
        return None
    return {int(i): c.reshape(4, 2) for i, c in zip(ids.flatten(), corners)}


def marker_center(corners: np.ndarray) -> tuple[float, float]:
    c = corners.mean(axis=0)
    return float(c[0]), float(c[1])


def icon_present(frame_bgr: np.ndarray, template_bgr: np.ndarray, roi: tuple[int, int, int, int],
                 threshold: float = 0.85) -> tuple[bool, float, tuple[int, int]]:
    x, y, w, h = roi
    region = frame_bgr[y:y + h, x:x + w]
    result = cv2.matchTemplate(region, template_bgr, cv2.TM_CCOEFF_NORMED)
    _, score, _, location = cv2.minMaxLoc(result)
    return score >= threshold, float(score), location


def frames_differ(a: np.ndarray, b: np.ndarray, min_mean_abs_diff: float = 0.5) -> bool:
    """Live streams carry sensor noise; an identical frame suggests a frozen source."""
    return float(np.mean(cv2.absdiff(a, b))) >= min_mean_abs_diff


def summarize_distribution(samples: list[float]) -> dict[str, Any]:
    arr = np.asarray(samples, dtype=float)
    return {"n": int(arr.size), "median": float(np.median(arr)), "p95": float(np.percentile(arr, 95)),
            "max": float(arr.max()), "min": float(arr.min())}
