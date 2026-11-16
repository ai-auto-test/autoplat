"""Display checks and black-screen classification (Chapter 6)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from autoplat.adapters.base import Hardware
from autoplat.errors import CaptureNoSignal
from autoplat.services import vision
from autoplat.waiting import wait_until

HOME_ICON_ROI = (10, 10, 90, 60)


@dataclass
class DisplayDiagnosis:
    failure_class: str          # "ok" or a taxonomy class
    evidence: dict[str, Any]
    recovery: str

    def sentence(self) -> str:
        parts = "; ".join(f"{k} {v}" for k, v in self.evidence.items())
        return f"{parts}; classification: {self.failure_class}"


TAXONOMY_RECOVERY = {
    "ok": "none",
    "capture_no_signal": "bench: check capture device and cabling",
    "compositor_unhealthy": "restart compositor (L1)",
    "link_down": "power cycle (L3)",
    "backlight_off": "check power mode, then power cycle (L3)",
    "wrong_layer": "re-request the view",
    "protected_content": "none: expected behavior",
    "app_not_rendering": "restart application",
    "unknown": "collect more evidence",
}


class DisplayService:
    def __init__(self, hw: Hardware, evidence=None):
        self.hw, self.evidence = hw, evidence

    def capture_metrics(self) -> dict[str, float]:
        return vision.image_metrics(self.hw.capture.grab().image)

    def classify(self, expected_surface: str | None = None) -> DisplayDiagnosis:
        """Query each observation point before deciding what kind of 'black screen' this is."""
        ev: dict[str, Any] = {}
        try:
            frame = self.hw.capture.grab(timeout_s=2.0)
        except CaptureNoSignal as exc:
            return self._done("capture_no_signal", {"capture": str(exc)})
        m = vision.image_metrics(frame.image)
        ev["mean_luma"] = round(m["mean_luma"], 1)
        try:
            status = self.hw.target.display_status()
        except Exception as exc:
            ev["target"] = type(exc).__name__
            return self._done("unknown", ev)
        ev.update(compositor=status["compositor"], top_surface=status["top_surface"],
                  link_locked=status["link_locked"], backlight_pwm=status["backlight_pwm"])
        if status.get("protected_content"):
            return self._done("protected_content", ev)
        if status["compositor"] != "ready":
            return self._done("compositor_unhealthy", ev)
        if not status["link_locked"]:
            return self._done("link_down", ev)
        if status["backlight_pwm"] <= 0.01:
            return self._done("backlight_off", ev)
        if expected_surface and status["top_surface"] != expected_surface:
            return self._done("wrong_layer", ev)
        if m["mean_luma"] < 8:
            return self._done("app_not_rendering", ev)
        return self._done("ok", ev)

    def _done(self, cls: str, ev: dict[str, Any]) -> DisplayDiagnosis:
        diag = DisplayDiagnosis(cls, ev, TAXONOMY_RECOVERY[cls])
        if self.evidence is not None:
            self.evidence.notes["display_diagnosis"] = diag.sentence()
        return diag

    def wait_home(self, timeout_s: float = 10.0) -> dict[str, Any]:
        def home():
            status = self.hw.target.display_status()
            frame = self.hw.capture.grab()
            return status["top_surface"] == "hmi_home" and vision.image_metrics(frame.image)["mean_luma"] > 20, status

        return wait_until(home, timeout_s=timeout_s, interval_s=0.05, describe="home screen visible")


class HmiService:
    def __init__(self, hw: Hardware):
        self.hw = hw

    def go_home(self) -> None:
        self.hw.target.go_home()

    def dismiss_dialogs(self) -> None:
        self.hw.target.dismiss_dialogs()

    def wait_for_indicator(self, name: str, timeout_s: float = 3.0) -> None:
        wait_until(lambda: (name in self.hw.target.indicators(), sorted(self.hw.target.indicators())),
                   timeout_s=timeout_s, interval_s=0.05, describe=f"indicator {name} shown")

    def wait_for_indicator_cleared(self, name: str, timeout_s: float = 5.0) -> None:
        wait_until(lambda: (name not in self.hw.target.indicators(), sorted(self.hw.target.indicators())),
                   timeout_s=timeout_s, interval_s=0.05, describe=f"indicator {name} cleared")
