"""CAN signal access via cantools (DBC) and python-can (Chapter 9)."""
from __future__ import annotations

import time
from typing import Any, Callable

from autoplat.errors import BenchError
from autoplat.timeline import current_timeline
from autoplat.waiting import WaitTimeout

# protect(message_name, data: bytearray, counter) mutates data in place (alive counter, checksum).
Protector = Callable[[str, bytearray, int], None]


class CanSignalBus:
    """Named signals over CAN. Periodic messages keep their cycle time, counter and checksum."""

    def __init__(self, dbc: str, *, interface: str = "socketcan", channel: str = "can0",
                 bitrate: int | None = 500000, protect: Protector | None = None, **bus_kwargs: Any):
        import can
        import cantools

        self._can = can
        self.db = cantools.database.load_file(dbc)
        kwargs = dict(bus_kwargs)
        if bitrate is not None and interface not in ("virtual",):
            kwargs["bitrate"] = bitrate
        try:
            self.bus = can.Bus(interface=interface, channel=channel, **kwargs)
        except Exception as exc:
            raise BenchError(f"CAN interface {interface}:{channel} unavailable: {exc}") from exc
        self.protect = protect
        self._periodic: dict[str, dict[str, Any]] = {}

    def _message(self, name: str):
        return self.db.get_message_by_name(name)

    def _frame(self, name: str, signals: dict[str, Any]):
        msg = self._message(name)
        full = {s.name: 0 for s in msg.signals}
        full.update(signals)
        return msg, self._can.Message(arbitration_id=msg.frame_id, data=msg.encode(full),
                                      is_extended_id=msg.is_extended_frame)

    def send_signal(self, message: str, **signals: Any) -> None:
        msg, frame = self._frame(message, signals)
        if self.protect:
            data = bytearray(frame.data)
            self.protect(message, data, 0)
            frame.data = data
        self.bus.send(frame)
        _record("send", message, signals)

    def start_periodic(self, message: str, period_s: float, **signals: Any) -> None:
        self.stop_periodic(message)
        _, frame = self._frame(message, signals)
        state = {"period": period_s, "signals": dict(signals), "counter": 0, "task": None}
        modifier = None
        if self.protect:
            def modifier(m, _name=message, _state=state):
                _state["counter"] = (_state["counter"] + 1) & 0xFF
                data = bytearray(m.data)
                self.protect(_name, data, _state["counter"])
                m.data = data
        state["task"] = self.bus.send_periodic(frame, period_s, modifier_callback=modifier) \
            if modifier else self.bus.send_periodic(frame, period_s)
        self._periodic[message] = state
        _record("start_periodic", message, signals, period_s=period_s)

    def update_periodic(self, message: str, **signals: Any) -> None:
        state = self._periodic[message]
        state["signals"].update(signals)
        _, frame = self._frame(message, state["signals"])
        if state["task"] is not None:
            state["task"].modify_data(frame)
        _record("update_periodic", message, signals)

    def pause_periodic(self, message: str) -> None:
        state = self._periodic[message]
        if state["task"] is not None:
            state["task"].stop()
            state["task"] = None
        _record("pause_periodic", message, {})

    def resume_periodic(self, message: str) -> None:
        state = self._periodic[message]
        if state["task"] is None:
            self.start_periodic(message, state["period"], **state["signals"])

    def stop_periodic(self, message: str) -> None:
        state = self._periodic.pop(message, None)
        if state and state["task"] is not None:
            state["task"].stop()

    def read_signal(self, message: str, signal: str, *, timeout_s: float = 1.0) -> Any:
        return self.wait_for_signal(message, signal, lambda v: True, timeout_s=timeout_s)

    def wait_for_signal(self, message: str, signal: str, predicate: Callable[[Any], bool],
                        *, timeout_s: float) -> Any:
        msg = self._message(message)
        deadline = time.monotonic() + timeout_s
        last = None
        while time.monotonic() < deadline:
            frame = self.bus.recv(timeout=0.1)
            if frame is None or frame.arbitration_id != msg.frame_id:
                continue
            last = msg.decode(frame.data)[signal]
            if predicate(last):
                return last
        raise WaitTimeout(f"{message}.{signal} not satisfied within {timeout_s}s; last={last!r}", last)

    def close(self) -> None:
        for name in list(self._periodic):
            self.stop_periodic(name)
        self.bus.shutdown()


def _record(action: str, message: str, signals: dict[str, Any], **extra: Any) -> None:
    tl = current_timeline()
    if tl is not None:
        shown = " ".join(f"{k}={v}" for k, v in signals.items())
        tl.record("adapter", "can", action, message=message, signals=shown, **extra)
