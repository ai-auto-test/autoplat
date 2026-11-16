"""Vehicle-level control with idempotent `ensure_*` operations (Chapters 2 and 5)."""
from __future__ import annotations

from typing import Any

from autoplat.adapters.base import Hardware
from autoplat.config import BenchConfig
from autoplat.timeline import current_timeline
from autoplat.waiting import WaitTimeout, wait_until

CRITICAL_SERVICES = ["compositor", "hmi", "camera-service", "audio-manager"]


def ensure_ignition(relay, on: bool, *, timeout_s: float = 3.0, channel: str = "KL15") -> None:
    if relay.read(channel) != on:
        relay.set(channel, on)
    wait_until(lambda: relay.read(channel) == on, timeout_s=timeout_s, interval_s=0.05,
               describe=f"{channel} relay state")


class VehicleService:
    def __init__(self, hw: Hardware, bench: BenchConfig, *, nominal_voltage: float = 13.5):
        self.hw, self.bench = hw, bench
        self.nominal_voltage = nominal_voltage

    # power and ignition ------------------------------------------------------
    def ensure_power(self, on: bool = True) -> None:
        psu = self.hw.power
        if on:
            psu.set_voltage(self.nominal_voltage)
            if not psu.output_enabled():
                psu.set_output(True)
        elif psu.output_enabled():
            psu.set_output(False)

    def ensure_ignition(self, on: bool = True) -> None:
        ensure_ignition(self.hw.relay, on, timeout_s=self.bench.timeout("relay", 3.0))

    def power_off(self, *, quiescent_ma: float = 5.0) -> None:
        self.ensure_ignition(False)
        self.ensure_power(False)
        wait_until(lambda: self.hw.power.measure_current() * 1000 < quiescent_ma,
                   timeout_s=self.bench.timeout("power_down", 10.0), interval_s=0.1,
                   describe=f"supply current below {quiescent_ma} mA")

    def power_cycle(self, *, hold_s: float = 1.0) -> None:
        import time

        self.ensure_ignition(False)
        self.ensure_power(False)
        time.sleep(hold_s)
        self.ensure_power(True)
        self.ensure_ignition(True)

    # readiness ------------------------------------------------------------------
    def readiness(self, services: list[str] | None = None) -> tuple[bool, dict[str, str]]:
        try:
            health = self.hw.target.health(services or CRITICAL_SERVICES)
        except Exception as exc:  # booting targets refuse connections; report that as state
            return False, {"link": type(exc).__name__}
        return all(v == "ready" for v in health.values()), health

    def wait_ready(self, timeout_s: float | None = None, services: list[str] | None = None) -> dict[str, str]:
        return wait_until(lambda: self.readiness(services),
                          timeout_s=timeout_s or self.bench.timeout("boot_ready", 90.0),
                          interval_s=0.1, describe="critical services ready")

    def ensure_ready(self) -> dict[str, str]:
        self.ensure_power(True)
        self.ensure_ignition(True)
        self._start_periodic_defaults()
        return self.wait_ready()

    def _start_periodic_defaults(self) -> None:
        bus = self.hw.bus
        if bus is None or getattr(self, "_periodic_started", False):
            return
        self._periodic_started = True
        for message, signals in ((self.message_for("VehicleSpeed"), {"VehicleSpeed": 0.0}),):
            try:
                bus.start_periodic(message, 0.1, **signals)
            except KeyError:
                pass

    # signals -------------------------------------------------------------------
    def message_for(self, signal: str) -> str:
        return self.bench.signals.get(signal, signal)

    def set_signal(self, signal: str, value: Any) -> None:
        self.hw.bus.send_signal(self.message_for(signal), **{signal: value})

    def ensure_gear(self, gear: str) -> None:
        self.set_signal("TransmissionGear", gear)

    def ensure_reverse_selected(self) -> None:
        self.ensure_gear("R")

    def ensure_park(self) -> None:
        self.ensure_gear("P")

    def set_speed(self, kph: float) -> None:
        bus = self.hw.bus
        msg = self.message_for("VehicleSpeed")
        try:
            bus.update_periodic(msg, VehicleSpeed=kph)
        except KeyError:
            bus.start_periodic(msg, 0.1, VehicleSpeed=kph)

    def wait_for_signal_valid(self, signal: str, timeout_s: float = 2.0) -> None:
        wait_until(lambda: "speed_unavailable" not in self.hw.target.indicators(), timeout_s=timeout_s,
                   interval_s=0.05, describe=f"{signal} valid")

    # identity --------------------------------------------------------------------
    def read_identity(self) -> dict[str, Any]:
        ident = dict(self.hw.target.identity())
        if self.hw.diag is not None:
            try:
                ident["diag_sw_version"] = str(self.hw.diag.read_did(0xF189)).strip()
            except Exception as exc:
                ident["diag_sw_version"] = f"unavailable: {type(exc).__name__}"
        return ident

    # recovery ------------------------------------------------------------------------
    def recover(self, max_level: int = 3) -> int:
        """Escalating reset L1..L3 (Chapter 2). Returns the level that restored health."""
        tl = current_timeline()
        steps = [
            (1, lambda: [self.hw.target.restart_service(s) for s, v in self.readiness()[1].items()
                         if v not in ("ready",) and s != "link"]),
            (2, lambda: self.hw.target.reboot()),
            (3, lambda: self.power_cycle()),
        ]
        for level, action in steps:
            if level > max_level:
                break
            try:
                action()
                self.wait_ready(timeout_s=self.bench.timeout("boot_ready", 90.0))
            except Exception as exc:
                if tl is not None:
                    tl.record("bench", "recovery", f"L{level}", outcome="failed", error=type(exc).__name__)
                continue
            if tl is not None:
                tl.record("bench", "recovery", f"L{level}", outcome="restored")
            return level
        raise WaitTimeout(f"recovery up to L{max_level} did not restore health", self.readiness()[1])
