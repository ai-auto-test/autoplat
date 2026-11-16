"""Build a bench's adapters from its capability description."""
from __future__ import annotations

import os

from autoplat.adapters.base import Hardware
from autoplat.adapters.power import SafePowerSupply
from autoplat.config import BenchConfig, PowerLimits

CAN_INTERFACES = {"socketcan", "vector", "pcan", "kvaser", "virtual", "ixxat", "neovi", "slcan"}


def build_hardware(bench: BenchConfig) -> Hardware:
    caps = bench.capabilities
    hw = Hardware()
    if any(c.adapter == "sim" for c in caps.values()):
        from autoplat.adapters import sim

        opts = caps["target"].options if "target" in caps else {}
        hw.sim = sim.SimWorld(boot_time_s=float(opts.get("boot_time_s", 0.4)),
                              build_id=opts.get("build_id", "IVI-24.3.118"),
                              variant=opts.get("variant", "EU_HIGH"),
                              packages=opts.get("packages"))
        for fault in filter(None, os.environ.get("AUTOPLAT_SIM_FAULTS", "").split(",")):
            hw.sim.inject(fault.strip())

    def adapter(name: str) -> tuple[str | None, dict]:
        c = caps.get(name)
        return (c.adapter, c.options) if c else (None, {})

    kind, opts = adapter("power_control")
    if kind == "sim":
        from autoplat.adapters.sim import SimPowerSupply
        raw = SimPowerSupply(hw.sim)
    elif kind == "scpi":
        from autoplat.adapters.power import ScpiPowerSupply
        raw = ScpiPowerSupply(opts["host"], int(opts.get("port", 5025)), opts.get("channel"))
    elif kind is not None:
        raise ValueError(f"unknown power_control adapter {kind!r}")
    if kind is not None:
        limits = bench.power_limits or PowerLimits(v_min=0, v_max=16, i_max=10)
        hw.power = SafePowerSupply(raw, limits)

    kind, opts = adapter("ignition")
    if kind == "sim":
        from autoplat.adapters.sim import SimRelay
        hw.relay = SimRelay(hw.sim)
    elif kind == "scpi_relay":
        from autoplat.adapters.relay import ScpiRelay
        hw.relay = ScpiRelay(opts["host"], opts["channels"], int(opts.get("port", 5025)))

    kind, opts = adapter("can")
    if kind == "sim":
        from autoplat.adapters.sim import SimSignalBus
        hw.bus = SimSignalBus(hw.sim)
    elif kind in CAN_INTERFACES:
        from autoplat.adapters.can_bus import CanSignalBus
        if not bench.network_db:
            raise ValueError("bench file needs 'network_db' (DBC path) for a real CAN adapter")
        hw.bus = CanSignalBus(bench.network_db, interface=kind, channel=str(opts.get("channel", "can0")),
                              bitrate=opts.get("bitrate", 500000))

    kind, opts = adapter("target")
    if kind == "sim":
        from autoplat.adapters.sim import SimOta, SimTarget
        hw.target = SimTarget(hw.sim)
        hw.ota = SimOta(hw.sim)
    elif kind == "ssh":
        from autoplat.adapters.target_ssh import SshTarget
        hw.target = SshTarget(opts["host"], opts.get("commands", {}), port=int(opts.get("port", 22)),
                              identity_file=opts.get("identity_file"))

    kind, opts = adapter("display_capture")
    if kind == "sim":
        from autoplat.adapters.sim import SimCapture
        hw.capture = SimCapture(hw.sim, bench.camera_markers or None)
    elif kind == "opencv":
        from autoplat.adapters.capture import OpenCvCapture
        hw.capture = OpenCvCapture(opts.get("device", 0), opts.get("width"), opts.get("height"))

    kind, opts = adapter("doip")
    if kind == "sim":
        from autoplat.adapters.sim import SimDiagnostics
        hw.diag = SimDiagnostics(hw.sim)
    elif kind == "doip":
        from autoplat.adapters.diag import UdsDoipDiagnostics
        hw.diag = UdsDoipDiagnostics(opts["ip"], int(str(opts["logical_address"]), 0))

    kind, opts = adapter("audio_loopback")
    if kind == "sim":
        from autoplat.adapters.sim import SimAudio
        hw.audio = SimAudio(hw.sim, int(opts.get("sample_rate", 8000)))
    elif kind is not None:
        raise NotImplementedError(
            f"audio adapter {kind!r}: implement autoplat.adapters.base.AudioIO for your interface")
    return hw
