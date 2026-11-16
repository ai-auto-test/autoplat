"""Relay/switch units for ignition (KL15), KL30 and similar lines."""
from __future__ import annotations

from autoplat.adapters.power import ScpiPowerSupply
from autoplat.timeline import traced


class ScpiRelay(ScpiPowerSupply):
    """SCPI switch unit using ROUTe:CLOSe/OPEN with a channel map, e.g. {"KL15": "@1001"}."""

    def __init__(self, host: str, channels: dict[str, str], port: int = 5025):
        super().__init__(host, port)
        self.channels = channels

    def read(self, channel: str) -> bool:
        return self._query(f"ROUT:CLOS? ({self.channels[channel]})", 2.0) == "1"

    @traced("relay")
    def set(self, channel: str, state: bool) -> None:
        self._write(f"ROUT:{'CLOS' if state else 'OPEN'} ({self.channels[channel]})", 2.0)
