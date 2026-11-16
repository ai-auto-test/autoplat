"""Programmable power supply adapters with enforced safety limits (Chapter 5)."""
from __future__ import annotations

import socket
import threading
import time

from autoplat.config import PowerLimits
from autoplat.errors import BenchSafetyError, InstrumentBusy
from autoplat.timeline import current_timeline, traced


class ScpiPowerSupply:
    """Generic SCPI supply over a raw TCP socket (port 5025 on most LAN instruments)."""

    def __init__(self, host: str, port: int = 5025, channel: int | None = None, connect_timeout_s: float = 5.0):
        self.host, self.port, self.channel = host, port, channel
        try:
            self._sock = socket.create_connection((host, port), timeout=connect_timeout_s)
        except OSError as exc:
            raise InstrumentBusy(f"power supply {host}:{port} not reachable: {exc}") from exc
        self._lock = threading.Lock()
        if channel is not None:
            self._write(f"INST:NSEL {channel}", 5.0)

    def _write(self, cmd: str, timeout_s: float) -> None:
        with self._lock:
            self._sock.settimeout(timeout_s)
            self._sock.sendall((cmd + "\n").encode("ascii"))

    def _query(self, cmd: str, timeout_s: float) -> str:
        with self._lock:
            self._sock.settimeout(timeout_s)
            self._sock.sendall((cmd + "\n").encode("ascii"))
            data = b""
            while not data.endswith(b"\n"):
                chunk = self._sock.recv(4096)
                if not chunk:
                    raise ConnectionResetError("instrument closed the connection")
                data += chunk
            return data.decode("ascii").strip()

    @traced("psu")
    def set_output(self, enabled: bool, *, timeout_s: float = 5.0) -> None:
        self._write(f"OUTP {'ON' if enabled else 'OFF'}", timeout_s)

    def output_enabled(self, *, timeout_s: float = 2.0) -> bool:
        return self._query("OUTP?", timeout_s) in ("1", "ON")

    @traced("psu")
    def set_voltage(self, volts: float, *, timeout_s: float = 5.0) -> None:
        self._write(f"VOLT {volts:.3f}", timeout_s)

    @traced("psu")
    def set_current_limit(self, amps: float, *, timeout_s: float = 5.0) -> None:
        self._write(f"CURR {amps:.3f}", timeout_s)

    def measure_current(self, *, timeout_s: float = 2.0) -> float:
        return float(self._query("MEAS:CURR?", timeout_s))

    def identity(self) -> str:
        return self._query("*IDN?", 2.0)

    def close(self) -> None:
        self._sock.close()


class SafePowerSupply:
    """Wraps any PowerSupply and refuses requests outside the bench's configured limits."""

    def __init__(self, psu, limits: PowerLimits):
        self._psu, self._limits = psu, limits
        self._monitor: threading.Thread | None = None
        self._stop = threading.Event()
        self.tripped: str | None = None

    def set_voltage(self, volts: float, *, timeout_s: float = 5.0) -> None:
        if not self._limits.v_min <= volts <= self._limits.v_max:
            raise BenchSafetyError(
                f"Requested {volts} V outside bench limits "
                f"[{self._limits.v_min}, {self._limits.v_max}] V")
        self._psu.set_current_limit(self._limits.i_max, timeout_s=timeout_s)
        self._psu.set_voltage(volts, timeout_s=timeout_s)

    def set_output(self, enabled: bool, *, timeout_s: float = 5.0) -> None:
        if enabled:
            # Current limit is always configured before the output can be enabled.
            self._psu.set_current_limit(self._limits.i_max, timeout_s=timeout_s)
            self.tripped = None
        self._psu.set_output(enabled, timeout_s=timeout_s)

    def output_enabled(self, *, timeout_s: float = 2.0) -> bool:
        return self._psu.output_enabled(timeout_s=timeout_s)

    def set_current_limit(self, amps: float, *, timeout_s: float = 5.0) -> None:
        if amps > self._limits.i_max:
            raise BenchSafetyError(f"Requested current limit {amps} A exceeds bench maximum {self._limits.i_max} A")
        self._psu.set_current_limit(amps, timeout_s=timeout_s)

    def measure_current(self, *, timeout_s: float = 2.0) -> float:
        return self._psu.measure_current(timeout_s=timeout_s)

    def identity(self) -> str:
        return self._psu.identity()

    # over-current monitor ---------------------------------------------------
    def start_monitor(self, period_s: float = 0.2, window: int = 3) -> None:
        trip = self._limits.i_trip or self._limits.i_max
        if self._monitor is not None:
            return
        self._stop.clear()

        def run():
            over = 0
            while not self._stop.wait(period_s):
                try:
                    amps = self._psu.measure_current(timeout_s=1.0)
                except Exception:
                    continue
                over = over + 1 if amps > trip else 0
                if over >= window:
                    self._psu.set_output(False, timeout_s=2.0)
                    self.tripped = f"over-current {amps:.2f} A > {trip:.2f} A at {time.monotonic():.1f}"
                    tl = current_timeline()
                    if tl is not None:
                        tl.record("bench", "psu", "overcurrent_trip", amps=amps)
                    over = 0

        self._monitor = threading.Thread(target=run, name="psu-monitor", daemon=True)
        self._monitor.start()

    def stop_monitor(self) -> None:
        self._stop.set()
        if self._monitor is not None:
            self._monitor.join(timeout=2.0)
        self._monitor = None
