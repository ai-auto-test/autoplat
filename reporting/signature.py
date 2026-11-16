"""Failure signatures and known-issue matching (Chapters 11 and 13)."""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def normalize(line: str) -> str:
    line = re.sub(r"^\[\+?[\d.]+\]\s*", "", line)  # leading timestamps
    line = re.sub(r"0x[0-9a-fA-F]+", "<hex>", line)
    line = re.sub(r"\b[0-9a-f]{8,}\b", "<id>", line)
    line = re.sub(r"\d+(\.\d+)?", "<n>", line)
    return line.strip().lower()


@dataclass
class Failure:
    scenario: str
    assertion_kind: str
    key_log_lines: list[str] = field(default_factory=list)
    classification: str = "unknown"


def assertion_kind(message: str) -> str:
    """Strip numbers and quoted values so 'Stale frame: 312 ms' and 'Stale frame: 290 ms' group."""
    first = message.strip().splitlines()[0] if message.strip() else ""
    first = re.sub(r"'[^']*'|\"[^\"]*\"", "<v>", first)
    return normalize(first)[:160]


def signature(failure: Failure) -> str:
    keys = [normalize(line) for line in failure.key_log_lines[:5]]
    return "|".join([failure.scenario, failure.assertion_kind, *keys])


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


@dataclass
class KnownIssue:
    id: str
    signature: str
    title: str = ""
    owner: str = ""
    waiver_expires: str | None = None


class KnownIssueStore:
    def __init__(self, issues: list[KnownIssue] | None = None):
        self.issues = issues or []

    @classmethod
    def load(cls, path: str | Path) -> "KnownIssueStore":
        p = Path(path)
        if not p.exists():
            return cls()
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or []
        return cls([KnownIssue(**d) for d in data])

    def match(self, sig: str, threshold: float = 0.85) -> list[tuple[KnownIssue, float]]:
        scored = [(i, similarity(sig, i.signature)) for i in self.issues]
        return sorted([s for s in scored if s[1] >= threshold], key=lambda s: -s[1])
