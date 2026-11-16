"""Camera views, fresh frames and measured image health (Chapter 7)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

from autoplat.adapters.base import Hardware
from autoplat.config import BenchConfig, CameraLimits
from autoplat.services import vision
from autoplat.services.vehicle import VehicleService
from autoplat.waiting import wait_until

DEFAULT_MARKERS = {"rear": 7, "front": 8, "left": 9, "right": 10}
MIRRORED_VIEWS = {"rear"}


@dataclass
class CameraFrame:
    image: np.ndarray
    age_ms: float
    counter: int
    view: str
    marker_detected: bool
    marker_ids: list[int]
    marker_center: tuple[float, float] | None
    metrics: dict[str, float] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return self.marker_detected and not self.problems


class CameraService:
    def __init__(self, hw: Hardware, bench: BenchConfig, vehicle: VehicleService, evidence=None):
        self.hw, self.bench, self.vehicle, self.evidence = hw, bench, vehicle, evidence
        self.markers = bench.camera_markers or DEFAULT_MARKERS

    def limits(self, view: str) -> CameraLimits:
        return self.bench.camera_limits.get(view) or CameraLimits()

    def request_view(self, view: str) -> None:
        if view == "rear":
            self.vehicle.ensure_reverse_selected()
        else:
            self.vehicle.ensure_park()
            self.hw.target.request_view(view)

    def release_view(self) -> None:
        self.vehicle.ensure_park()
        self.hw.target.request_view("none")

    def wait_for_view(self, view: str, timeout_s: float = 3.0) -> dict[str, Any]:
        expected = f"{view.upper()}_ACTIVE"

        def active():
            state = self.hw.target.view_state()
            return state.get("state") == expected, state

        return wait_until(active, timeout_s=timeout_s, interval_s=0.02, describe=f"camera view {expected}")

    def analyze(self, image: np.ndarray, view: str) -> tuple[list[int], tuple[float, float] | None, dict, list]:
        # Mirrored views are un-mirrored before decoding, so a wrong mirror setting fails detection.
        probe = cv2.flip(image, 1) if view in MIRRORED_VIEWS else image
        found = vision.detect_marker(probe) or {}
        expected = self.markers[view]
        center = vision.marker_center(found[expected]) if expected in found else None
        metrics = vision.image_metrics(image)
        return sorted(found), center, metrics, vision.health_problems(metrics, self.limits(view))

    def wait_for_frame(self, view: str = "rear", *, max_age_ms: float = 250, timeout_s: float = 8.0) -> CameraFrame:
        """Return the first fresh frame showing the expected marker, or the last frame seen at timeout.

        Freshness needs two grabs: a frame counts only if the device counter advanced since the previous one.
        """
        capture = self.hw.capture
        deadline = time.monotonic() + timeout_s
        last: CameraFrame | None = None
        prev_counter = capture.grab(timeout_s=2.0).counter
        while True:
            time.sleep(0.03)
            frame = capture.grab(timeout_s=2.0)
            now = self.hw.sim.now() if self.hw.sim is not None else time.monotonic()
            ids, center, metrics, problems = self.analyze(frame.image, view)
            if frame.counter == prev_counter:
                problems = problems + ["frame counter did not advance (stale stream)"]
            prev_counter = frame.counter
            last = CameraFrame(frame.image, frame.age_ms(now), frame.counter, view,
                               self.markers[view] in ids, ids, center, metrics, problems)
            if last.marker_detected and last.age_ms <= max_age_ms and not problems:
                break
            if time.monotonic() >= deadline:
                break
        if self.evidence is not None:
            for k, v in last.metrics.items():
                self.evidence.metric(f"camera.{view}.{k}", round(v, 2))
            self.evidence.metric(f"camera.{view}.marker_ids", last.marker_ids, self.markers[view])
        return last

    def activation_latency(self, view: str = "rear", timeout_s: float = 5.0) -> float:
        """Seconds from the request to the first frame with the expected marker."""
        self.release_view()
        wait_until(lambda: self.hw.target.view_state()["state"] == "INACTIVE", timeout_s=2.0, interval_s=0.02,
                   describe="camera inactive")
        clock = self.hw.sim.now if self.hw.sim is not None else time.monotonic
        t0 = clock()
        self.request_view(view)
        frame = self.wait_for_frame(view, max_age_ms=1e9, timeout_s=timeout_s)
        if not frame.marker_detected:
            raise AssertionError(f"{view} marker not seen within {timeout_s}s; ids={frame.marker_ids}")
        return clock() - t0
