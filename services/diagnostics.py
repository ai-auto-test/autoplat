"""Diagnostics service: identity, DTCs as evidence, and bounded fault injection (Chapter 9)."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from autoplat.adapters.base import Hardware
from autoplat.timeline import current_timeline


class DiagnosticsService:
    def __init__(self, hw: Hardware, tolerated: list[str] | None = None):
        self.hw = hw
        self.tolerated = set(tolerated or [])
        self.baseline_codes: set[str] = set()

    def read_identity(self) -> dict[str, Any]:
        return {"sw_version": str(self.hw.diag.read_did(0xF189)).strip(),
                "vin": str(self.hw.diag.read_did(0xF190)).strip()}

    def dtcs(self) -> list[str]:
        return [d["code"] for d in self.hw.diag.read_dtcs()]

    def record_baseline(self) -> list[str]:
        """Record DTCs before a scenario. Recording first preserves evidence that clearing would destroy."""
        codes = self.dtcs()
        self.baseline_codes = set(codes)
        tl = current_timeline()
        if tl is not None:
            tl.record("service", "diag", "dtc_baseline", codes=",".join(codes) or "-")
        return codes

    def new_dtcs(self) -> list[str]:
        return sorted(set(self.dtcs()) - self.baseline_codes)

    def unexpected_dtcs(self, expected: set[str] | None = None) -> list[str]:
        return [c for c in self.new_dtcs() if c not in self.tolerated and c not in (expected or set())]

    def clear_dtcs(self) -> None:
        self.hw.diag.clear_dtcs()
        self.baseline_codes = set()


@contextmanager
def suppress_message(bus, message_name: str, timeline=None):
    timeline = timeline or current_timeline()
    if timeline is not None:
        timeline.record("fault", "can", "fault_inject", kind="suppress", message=message_name)
    bus.pause_periodic(message_name)
    try:
        yield
    finally:
        bus.resume_periodic(message_name)
        if timeline is not None:
            timeline.record("fault", "can", "fault_remove", kind="suppress", message=message_name)
