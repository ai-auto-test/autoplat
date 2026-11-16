"""OTA update journey: phases, interruption and consistency checks (Chapter 10)."""
from __future__ import annotations

from typing import Any

from autoplat.adapters.base import Hardware
from autoplat.waiting import wait_until

# Interrupt points as (phase, minimum progress percent).
INTERRUPT_POINTS: dict[str, tuple[str, float]] = {
    "download_25pct": ("download", 25), "download_90pct": ("download", 90),
    "verify": ("verify", 0), "install_10pct": ("install", 10), "install_60pct": ("install", 60),
    "first_boot_early": ("first_boot", 0), "done": ("done", 0),
}
PHASE_ORDER = ["idle", "download", "verify", "install", "first_boot", "done"]


def is_consistent(identity: dict[str, Any]) -> bool:
    """All components report the same release as the top-level build."""
    return all(v == identity["build_id"] for v in identity.get("components", {}).values())


class OtaService:
    def __init__(self, hw: Hardware):
        self.hw = hw

    def start_update(self, package: str = "candidate") -> None:
        self.hw.ota.start_update(package)

    def package_build_id(self, package: str = "candidate") -> str:
        return self.hw.ota.package_build_id(package)

    def wait_for_phase(self, point: str, timeout_s: float = 1800) -> dict[str, Any]:
        phase, pct = INTERRUPT_POINTS[point]

        def reached():
            st = self.hw.ota.status()
            order = PHASE_ORDER.index(st["phase"]) if st["phase"] in PHASE_ORDER else -1
            target = PHASE_ORDER.index(phase)
            return order > target or (order == target and st.get("progress", 0) >= pct), st

        return wait_until(reached, timeout_s=timeout_s, interval_s=0.01, describe=f"OTA point {point}")
