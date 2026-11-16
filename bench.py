"""Bench-level operations shared by the pytest plugin and the CLI."""
from __future__ import annotations

from autoplat.adapters.base import Hardware
from autoplat.errors import TargetUnreachable


def safe_state(hw: Hardware, gear_message: str = "PT_Transmission") -> list[str]:
    """Return the bench to a defined safe state. Every step is attempted; failures are reported."""
    done, problems = [], []

    def step(name, fn):
        try:
            fn()
            done.append(name)
        except Exception as exc:
            problems.append(f"{name}: {type(exc).__name__}: {exc}")

    if hw.sim is not None:
        step("clear simulated faults", hw.sim.clear)
    if hw.diag is not None:
        def close_session():
            try:
                hw.diag.change_session("default")
            except TargetUnreachable:
                pass  # target off: no session can be open
        step("diagnostic session default", close_session)
    if hw.bus is not None:
        for message in list(getattr(hw.sim, "paused", {}) or {}):
            step(f"resume {message}", lambda m=message: hw.bus.resume_periodic(m))
        step("gear P", lambda: hw.bus.send_signal(gear_message, TransmissionGear="P"))
    if hw.audio is not None:
        for source in ("media", "navigation", "phone", "chime"):
            step(f"stop {source}", lambda s=source: hw.audio.stop(s))
    if hw.relay is not None:
        step("ignition off", lambda: hw.relay.set("KL15", False))
    if hw.power is not None:
        stop = getattr(hw.power, "stop_monitor", None)
        if stop:
            step("stop current monitor", stop)
        step("supply output off", lambda: hw.power.set_output(False))
    return done + [f"FAILED {p}" for p in problems]
