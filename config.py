"""Typed bench configuration loaded once at session start (Chapters 3 and 4)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Capability(BaseModel):
    model_config = ConfigDict(extra="allow")
    adapter: str

    @property
    def options(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class PowerLimits(BaseModel):
    v_min: float = Field(ge=0)
    v_max: float = Field(gt=0)
    i_max: float = Field(gt=0)
    i_trip: float | None = None  # over-current threshold for the monitor; defaults to i_max

    @model_validator(mode="after")
    def _ordered(self):
        if self.v_min > self.v_max:
            raise ValueError("v_min must not exceed v_max")
        return self


class CameraLimits(BaseModel):
    luma_min: float = 20
    luma_max: float = 235
    min_spread: float = 40
    min_sharpness: float = 35
    marker_position_tolerance_px: float = 12
    baseline: dict[str, Any] = Field(default_factory=dict)


class BenchConfig(BaseModel):
    bench_id: str
    location: str = ""
    owners: list[str] = Field(default_factory=list)
    capabilities: dict[str, Capability]
    power_limits: PowerLimits | None = None
    timeouts: dict[str, float] = Field(default_factory=dict)
    camera_limits: dict[str, CameraLimits] = Field(default_factory=dict)
    camera_markers: dict[str, int] = Field(default_factory=dict)
    tolerated_dtcs: list[str] = Field(default_factory=list)
    hazardous_allowed: bool = False
    lock_dir: str | None = None
    network_db: str | None = None
    signals: dict[str, str] = Field(default_factory=dict)  # signal name -> message name

    def has(self, *caps: str) -> bool:
        return all(c in self.capabilities for c in caps)

    def missing(self, *caps: str) -> list[str]:
        return [c for c in caps if c not in self.capabilities]

    def timeout(self, name: str, default: float) -> float:
        return self.timeouts.get(name, default)


def load_bench(path: str | Path) -> BenchConfig:
    path = Path(path)
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    cfg = BenchConfig.model_validate(data)
    if cfg.network_db and not Path(cfg.network_db).is_absolute():
        cfg.network_db = str((path.parent / cfg.network_db).resolve())
    return cfg
