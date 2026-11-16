"""In-process vehicle simulator so the whole framework runs without a bench.

One SimWorld models power, ignition, boot, services, vehicle signals, cameras, display,
audio routing, DTCs, and an A/B OTA agent. The Sim* adapters expose it through the same
interfaces as real equipment. Faults are injected with `world.inject(name)`:

  compositor_fail, backlight_off, link_down, capture_no_signal, camera_black,
  camera_mirror, camera_stale, duck_wrong, restore_wrong, ota_mixed, ota_data_loss,
  boot_slow
"""
from __future__ import annotations

import hashlib
import math
import threading
import time
import uuid
from typing import Any, Callable

import cv2
import numpy as np

from autoplat.adapters.base import Frame
from autoplat.errors import CaptureNoSignal, TargetUnreachable
from autoplat.timeline import traced

SERVICES = ["compositor", "hmi", "camera-service", "audio-manager", "nav"]
VIEW_MARKERS = {"rear": 7, "front": 8, "left": 9, "right": 10}
W, H = 640, 360

OTA_PHASES = [  # (name, start_s, end_s) relative to update start
    ("download", 0.0, 0.8),
    ("verify", 0.8, 0.9),
    ("install", 0.9, 1.7),
    ("first_boot", 1.7, 2.1),
    ("done", 2.1, math.inf),
]


class SimWorld:
    def __init__(self, *, boot_time_s: float = 0.4, build_id: str = "IVI-24.3.118", variant: str = "EU_HIGH",
                 packages: dict[str, str] | None = None, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.boot_time_s = boot_time_s
        self.variant = variant
        self.lock = threading.RLock()
        self.psu_output = False
        self.voltage = 0.0
        self.current_limit = 0.0
        self.relays: dict[str, bool] = {"KL15": False}
        self.powered_since: float | None = None
        self.boot_id = ""
        self.faults: set[str] = set()
        self.signals: dict[str, Any] = {"TransmissionGear": "P", "VehicleSpeed": 0.0}
        self.signal_time: dict[str, float] = {}
        self.periodic: dict[str, dict[str, Any]] = {}
        self.paused: dict[str, float] = {}
        self.resumed_at: dict[str, float] = {}
        self.dtcs: dict[str, int] = {}
        self.requested_view: str | None = None
        self.view_since = 0.0
        self.audio_sources: dict[str, tuple[float, float, float]] = {}
        self.dialogs = 0
        self.log: list[tuple[float, str]] = []
        self.slots = {"A": build_id, "B": build_id}
        self.active_slot = "A"
        self.mixed_component: str | None = None
        self.user_data = {"profiles": ["driver1", "driver2"], "paired_phones": 2, "camera_calibration": "C7"}
        self.packages = packages or {"candidate": "IVI-24.3.119"}
        self.ota: dict[str, Any] | None = None
        self.session = "default"

    # ------------------------------------------------------------------ faults
    def inject(self, fault: str) -> None:
        with self.lock:
            self.faults.add(fault)
            self._log(f"SIM fault injected {fault}")

    def clear(self, fault: str | None = None) -> None:
        with self.lock:
            if fault is None:
                self.faults.clear()
            else:
                self.faults.discard(fault)

    # ------------------------------------------------------------------- power
    def now(self) -> float:
        return self.clock()

    def _log(self, line: str) -> None:
        t = self.now() - self.powered_since if self.powered_since else 0.0
        self.log.append((self.now(), f"[+{t:.2f}] {line}"))

    def supply_ok(self) -> bool:
        return self.psu_output and 9.0 <= self.voltage <= 16.5

    def update(self) -> None:
        with self.lock:
            on = self.supply_ok() and self.relays.get("KL15", False)
            if on and self.powered_since is None:
                self.powered_since = self.now()
                self.boot_id = uuid.uuid4().hex[:8]
                self._log(f"BOOT-001 boot started boot_id={self.boot_id} build={self.build_id}")
            elif not on and self.powered_since is not None:
                self._power_lost()
            self._tick_signals()
            self._tick_ota()

    def _power_lost(self) -> None:
        self.powered_since = None
        self.requested_view = None
        self.audio_sources.clear()
        if self.ota is not None and self.ota["phase"] != "done":
            phase = self.ota["phase"]
            if phase == "first_boot":
                self.active_slot = self.ota["from_slot"]  # boot not confirmed: A/B fallback
            if "ota_mixed" in self.faults and phase == "install":
                self.mixed_component = self.ota["build"]
            if "ota_data_loss" in self.faults:
                self.user_data = {"profiles": [], "paired_phones": 0, "camera_calibration": None}
            self.ota = None

    @property
    def build_id(self) -> str:
        return self.slots[self.active_slot]

    def ready(self) -> bool:
        self.update()
        if self.powered_since is None:
            return False
        boot = self.boot_time_s * (4 if "boot_slow" in self.faults else 1)
        return self.now() - self.powered_since >= boot

    def require_link(self) -> None:
        self.update()
        if self.powered_since is None or self.now() - self.powered_since < self.boot_time_s * 0.5:
            raise TargetUnreachable("simulated target not reachable (powered off or booting)")

    # ----------------------------------------------------------------- signals
    def set_signals(self, values: dict[str, Any]) -> None:
        with self.lock:
            for k, v in values.items():
                if self.signals.get(k) != v:
                    self.signal_time[k] = self.now()
                self.signals[k] = v

    def _tick_signals(self) -> None:
        if not self.ready_quick():
            return
        for message, since in list(self.paused.items()):
            if self.now() - since > 0.3 and "U0401" not in self.dtcs:
                self.dtcs["U0401"] = 0x09
                self._log(f"COM-031 timeout of {message}; DTC U0401 set")

    def ready_quick(self) -> bool:
        return self.powered_since is not None and self.now() - self.powered_since >= self.boot_time_s

    def indicators(self) -> set[str]:
        self.update()
        out = set()
        for message, since in self.paused.items():
            if self.now() - since > 0.3:
                out.add("speed_unavailable")
        for message, t in self.resumed_at.items():
            if self.now() - t < 0.2 and message not in self.paused:
                out.add("speed_unavailable")
        return out

    # ------------------------------------------------------------------ camera
    def active_view(self) -> tuple[str | None, float]:
        self.update()
        if not self.ready():
            return None, 0.0
        if self.signals.get("TransmissionGear") == "R":
            since = self.signal_time.get("TransmissionGear", 0.0) + 0.05
            return ("rear", since) if self.now() >= since else (None, 0.0)
        if self.requested_view:
            return self.requested_view, self.view_since
        return None, 0.0

    # ------------------------------------------------------------------- audio
    def audio_level_db(self, source: str, t: float) -> float | None:
        """Return the gain applied to `source` at time t, or None if inaudible."""
        active = {s for s, (_, start, end) in self.audio_sources.items() if start <= t < end}
        if source not in active:
            return None
        if source == "media":
            if "phone" in active:
                return None
            if "navigation" in active:
                return -6.0 if "duck_wrong" in self.faults else -12.0
            media_start = self.audio_sources["media"][1]
            interrupted = any(s in ("navigation", "phone") and media_start < end <= t
                              for s, (_, _start, end) in self.audio_sources.items())
            if interrupted and "restore_wrong" in self.faults:
                return -4.2
            return 0.0
        if source == "phone" and "navigation" in active:
            return -6.0
        return 0.0

    # --------------------------------------------------------------------- OTA
    def start_update(self, package: str) -> None:
        with self.lock:
            self.require_link()
            build = self.packages[package]
            self.ota = {"package": package, "build": build, "t0": self.now(), "phase": "download",
                        "from_slot": self.active_slot}
            self._log(f"OTA-001 update started package={package} build={build}")

    def _tick_ota(self) -> None:
        if self.ota is None or self.powered_since is None:
            return
        elapsed = self.now() - self.ota["t0"]
        for name, start, end in OTA_PHASES:
            if start <= elapsed < end:
                if name != self.ota["phase"]:
                    self._enter_ota_phase(name)
                self.ota["progress"] = 100.0 if math.isinf(end) else round(100 * (elapsed - start) / (end - start), 1)
                break

    def _enter_ota_phase(self, name: str) -> None:
        inactive = "B" if self.ota["from_slot"] == "A" else "A"
        if name == "first_boot":
            self.slots[inactive] = self.ota["build"]
            self.active_slot = inactive
        self.ota["phase"] = name
        self._log(f"OTA-010 phase {name}")
        if name == "done":
            self._log(f"OTA-099 update confirmed build={self.build_id}")

    def ota_status(self) -> dict[str, Any]:
        self.update()
        if self.ota is None:
            return {"phase": "idle", "progress": 0.0}
        return {"phase": self.ota["phase"], "progress": self.ota.get("progress", 0.0),
                "package": self.ota["package"]}


# ---------------------------------------------------------------------- adapters
class SimPowerSupply:
    def __init__(self, world: SimWorld):
        self.w = world

    @traced("psu")
    def set_output(self, enabled: bool, *, timeout_s: float = 5.0) -> None:
        with self.w.lock:
            self.w.psu_output = enabled
        self.w.update()

    def output_enabled(self, *, timeout_s: float = 2.0) -> bool:
        return self.w.psu_output

    @traced("psu")
    def set_voltage(self, volts: float, *, timeout_s: float = 5.0) -> None:
        with self.w.lock:
            self.w.voltage = volts
        self.w.update()

    def set_current_limit(self, amps: float, *, timeout_s: float = 5.0) -> None:
        self.w.current_limit = amps

    def measure_current(self, *, timeout_s: float = 2.0) -> float:
        self.w.update()
        if not self.w.supply_ok():
            return 0.0
        return 1.35 if self.w.powered_since is not None else 0.003

    def identity(self) -> str:
        return "AUTOPLAT,SIM-PSU,0001,1.0"


class SimRelay:
    def __init__(self, world: SimWorld):
        self.w = world

    def read(self, channel: str) -> bool:
        return self.w.relays.get(channel, False)

    @traced("relay")
    def set(self, channel: str, state: bool) -> None:
        with self.w.lock:
            self.w.relays[channel] = state
        self.w.update()


class SimSignalBus:
    def __init__(self, world: SimWorld):
        self.w = world

    @traced("can")
    def send_signal(self, message: str, **signals: Any) -> None:
        self.w.set_signals(signals)
        self.w.update()

    @traced("can")
    def start_periodic(self, message: str, period_s: float, **signals: Any) -> None:
        self.w.periodic[message] = {"period": period_s, "signals": dict(signals)}
        self.w.paused.pop(message, None)
        self.w.set_signals(signals)

    def update_periodic(self, message: str, **signals: Any) -> None:
        self.w.periodic[message]["signals"].update(signals)
        self.w.set_signals(signals)

    @traced("can")
    def pause_periodic(self, message: str) -> None:
        self.w.paused[message] = self.w.now()

    @traced("can")
    def resume_periodic(self, message: str) -> None:
        if self.w.paused.pop(message, None) is not None:
            self.w.resumed_at[message] = self.w.now()

    def read_signal(self, message: str, signal: str, *, timeout_s: float = 1.0) -> Any:
        return self.w.signals.get(signal)

    def close(self) -> None:
        pass


class SimTarget:
    def __init__(self, world: SimWorld):
        self.w = world

    def health(self, services: list[str] | None = None, *, timeout_s: float = 5.0) -> dict[str, str]:
        self.w.require_link()
        state = "ready" if self.w.ready() else "starting"
        out = {s: state for s in SERVICES}
        if "compositor_fail" in self.w.faults and state == "ready":
            out["compositor"] = "failed"
        return {k: v for k, v in out.items() if services is None or k in services}

    def identity(self, *, timeout_s: float = 5.0) -> dict[str, Any]:
        self.w.require_link()
        build = self.w.build_id
        components = {"ivi": build, "hmi": self.w.mixed_component or build, "camera-service": build}
        return {"build_id": build, "variant": self.w.variant, "bootloader": "BL-7.2.4", "hw_rev": "SIM",
                "boot_id": self.w.boot_id, "slot": self.w.active_slot, "components": components}

    def configuration(self, *, timeout_s: float = 5.0) -> dict[str, Any]:
        self.w.require_link()
        return {"build_id": self.w.build_id, "variant": self.w.variant,
                "display": {"primary": "12.3in_1920x720", "secondary": None},
                "cameras": list(VIEW_MARKERS), "audio": {"amplifier": "sim", "channels": 4},
                "features": ["nav", "surround_view"]}

    def view_state(self) -> dict[str, Any]:
        view, since = self.w.active_view()
        return {"view": view, "state": f"{view.upper()}_ACTIVE" if view else "INACTIVE", "since": since}

    @traced("target")
    def request_view(self, view: str) -> None:
        self.w.require_link()
        with self.w.lock:
            self.w.requested_view = None if view in (None, "none") else view
            self.w.view_since = self.w.now() + 0.05
            if self.w.requested_view:
                self.w._log(f"CAM-014 stream started view={view}")
                if "camera_black" in self.w.faults:
                    self.w._log("CAM-077 overlay buffer allocation failed size=8294400")

    def display_status(self) -> dict[str, Any]:
        self.w.require_link()
        view, _ = self.w.active_view()
        return {
            "compositor": self.health(["compositor"])["compositor"],
            "top_surface": f"camera_{view}" if view else "hmi_home",
            "link_locked": "link_down" not in self.w.faults,
            "backlight_pwm": 0.0 if "backlight_off" in self.w.faults else 0.8,
            "protected_content": False,
        }

    def indicators(self) -> set[str]:
        return self.w.indicators()

    def logs(self, since_s: float | None = None) -> list[str]:
        cutoff = self.w.now() - since_s if since_s else -math.inf
        return [line for t, line in self.w.log if t >= cutoff]

    @traced("target")
    def restart_service(self, name: str, *, timeout_s: float = 15.0) -> None:
        self.w.require_link()
        self.w.clear(f"{name}_fail")
        self.w._log(f"SYS-020 service {name} restarted")

    @traced("target")
    def reboot(self, *, timeout_s: float = 10.0) -> None:
        self.w.require_link()
        with self.w.lock:
            self.w._power_lost()
        self.w.update()

    def go_home(self) -> None:
        self.w.require_link()
        self.w.requested_view = None

    def dismiss_dialogs(self) -> None:
        self.w.dialogs = 0

    def user_data_digest(self) -> str:
        self.w.require_link()
        return hashlib.sha256(repr(sorted(self.w.user_data.items())).encode()).hexdigest()[:16]


class SimCapture:
    """Renders what a capture card would see on the display output."""

    def __init__(self, world: SimWorld, markers: dict[str, int] | None = None):
        self.w = world
        self.markers = markers or VIEW_MARKERS
        self._counter = 0
        self._last: Frame | None = None
        self._dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        self._rng = np.random.default_rng(1)

    def _scene(self, view: str) -> np.ndarray:
        img = np.full((H, W, 3), 90, np.uint8)
        yy, xx = np.mgrid[0:H, 0:W]
        checker = (((xx // 40) + (yy // 40)) % 2 * 120 + 60).astype(np.uint8)
        img[:, :, 0] = checker
        img[:, :, 1] = checker
        img[:, :, 2] = np.clip(checker.astype(int) + 20, 0, 255).astype(np.uint8)
        marker = cv2.aruco.generateImageMarker(self._dict, self.markers[view], 140)
        m = np.full((180, 180), 255, np.uint8)
        m[20:160, 20:160] = marker
        y0, x0 = (H - 180) // 2, (W - 180) // 2
        img[y0:y0 + 180, x0:x0 + 180] = cv2.cvtColor(m, cv2.COLOR_GRAY2BGR)
        noise = self._rng.integers(-4, 5, img.shape, dtype=np.int16)
        img = np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)
        if (view == "rear") != ("camera_mirror" in self.w.faults):
            img = cv2.flip(img, 1)  # rear view is displayed mirrored; the fault inverts that
        return img

    def _home(self) -> np.ndarray:
        img = np.full((H, W, 3), (40, 32, 28), np.uint8)
        for i in range(4):
            x = 40 + i * 150
            cv2.rectangle(img, (x, 120), (x + 120, 240), (150, 120, 90), -1)
        cv2.rectangle(img, (20, 20), (80, 60), (255, 255, 255), -1)  # home icon region
        cv2.rectangle(img, (35, 30), (65, 50), (40, 32, 28), -1)
        return img

    def grab(self, *, timeout_s: float = 2.0) -> Frame:
        if "capture_no_signal" in self.w.faults:
            raise CaptureNoSignal("simulated capture device reports no input signal")
        if "camera_stale" in self.w.faults and self._last is not None:
            return self._last
        self._counter += 1
        self.w.update()
        status_ok = self.w.ready() and "link_down" not in self.w.faults and "backlight_off" not in self.w.faults
        if not status_ok or "compositor_fail" in self.w.faults:
            image = np.zeros((H, W, 3), np.uint8)
        else:
            view, _ = self.w.active_view()
            if view is None:
                image = self._home()
            elif "camera_black" in self.w.faults:
                image = np.full((H, W, 3), 3, np.uint8)
            else:
                image = self._scene(view)
        self._last = Frame(image=image, timestamp=self.w.now(), counter=self._counter)
        return self._last

    def signal_info(self) -> dict[str, Any]:
        ok = "capture_no_signal" not in self.w.faults
        return {"opened": True, "signal": ok, "width": W if ok else 0, "height": H if ok else 0, "fps": 30.0}

    def close(self) -> None:
        pass


class SimDiagnostics:
    def __init__(self, world: SimWorld):
        self.w = world

    def change_session(self, session: str, *, timeout_s: float = 5.0) -> None:
        self.w.require_link()
        self.w.session = session

    def read_did(self, did: int, *, timeout_s: float = 5.0) -> Any:
        self.w.require_link()
        values = {0xF189: self.w.build_id, 0xF190: "WSIM0000000000001", 0xF191: "SIM"}
        if did not in values:
            from autoplat.adapters.diag import DiagnosticNegativeResponse
            raise DiagnosticNegativeResponse(f"ReadDataByIdentifier 0x{did:04X}", 0x31)
        return values[did]

    def read_dtcs(self, *, status_mask: int = 0xFF, timeout_s: float = 5.0) -> list[dict[str, Any]]:
        self.w.require_link()
        self.w.update()
        return [{"code": c, "status": s} for c, s in sorted(self.w.dtcs.items()) if s & status_mask]

    def clear_dtcs(self, *, timeout_s: float = 5.0) -> None:
        self.w.require_link()
        self.w.dtcs.clear()

    def tester_present(self, *, timeout_s: float = 2.0) -> None:
        self.w.require_link()

    def close(self) -> None:
        pass


class SimAudio:
    """Digital loopback of the mixer output."""

    def __init__(self, world: SimWorld, sample_rate: int = 8000):
        self.w = world
        self.sample_rate = sample_rate

    def play_tone(self, source: str, freq_hz: float, duration_s: float | None = None) -> None:
        self.w.require_link()
        now = self.w.now()
        end = now + duration_s if duration_s else math.inf
        self.w.audio_sources[source] = (freq_hz, now, end)

    def stop(self, source: str) -> None:
        if source in self.w.audio_sources:
            freq, start, _ = self.w.audio_sources[source]
            self.w.audio_sources[source] = (freq, start, self.w.now())

    def record(self, duration_s: float) -> np.ndarray:
        t0 = self.w.now()
        n = int(duration_s * self.sample_rate)
        t = np.arange(n) / self.sample_rate
        out = np.zeros(n)
        for source, (freq, _start, _end) in self.w.audio_sources.items():
            gain_db = self.w.audio_level_db(source, t0)
            if gain_db is None:
                continue
            out += 0.25 * 10 ** (gain_db / 20) * np.sin(2 * np.pi * freq * t)
        return out


class SimOta:
    def __init__(self, world: SimWorld):
        self.w = world

    @traced("ota")
    def start_update(self, package: str) -> None:
        self.w.start_update(package)

    def status(self) -> dict[str, Any]:
        return self.w.ota_status()

    def package_build_id(self, package: str) -> str:
        return self.w.packages[package]
