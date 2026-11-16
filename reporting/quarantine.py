"""Quarantine with owner, reason and expiry (Chapter 13)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml


@dataclass
class QuarantineEntry:
    test: str
    owner: str
    reason: str
    since: date
    expires: date
    ticket: str

    def expired(self, today: date | None = None) -> bool:
        return (today or date.today()) > self.expires


def load_quarantine(path: str | Path) -> dict[str, QuarantineEntry]:
    p = Path(path)
    if not p.exists():
        return {}
    entries = {}
    for raw in yaml.safe_load(p.read_text(encoding="utf-8")) or []:
        missing = {"test", "owner", "reason", "since", "expires", "ticket"} - set(raw)
        if missing:
            raise ValueError(f"quarantine entry {raw.get('test', '?')} missing {sorted(missing)}")
        entry = QuarantineEntry(**raw)
        entries[entry.test] = entry
    return entries
