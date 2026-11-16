"""Compact, structured failure packets (Chapter 11, Appendix B)."""
from __future__ import annotations

import re
from typing import Any

import yaml

from autoplat.redaction import Redactor

LOG_SEVERITY = re.compile(r"(?i)\b(error|fail|failed|timeout|fatal|warn|warning|dropped|lost)\b|-0[5-9]\d\b")


def select_log_lines(lines: list[str], components: list[str] | None = None, limit: int = 40) -> list[str]:
    """Rule-based selection: severity keywords, message IDs >= 050, or lines from named components."""
    comps = [c.lower() for c in components or []]
    picked = [line for line in lines if LOG_SEVERITY.search(line)
              or any(c in line.lower() for c in comps)]
    return picked[-limit:]


def build_failure_packet(*, nodeid: str, intent: str, outcome: str, phase: str, assertion: str,
                         classification_hint: str, identity: dict[str, Any], timeline_lines: list[str],
                         log_lines: list[str], observation_level: str = "unspecified",
                         requirements: list[str] | None = None, retries: list[str] | None = None,
                         baseline: dict[str, Any] | None = None, dtc_delta: list[str] | None = None,
                         metrics: dict[str, Any] | None = None, artifacts: list[str] | None = None,
                         known_issue_matches: list[dict[str, Any]] | None = None,
                         unknowns: list[str] | None = None, redactor: Redactor | None = None,
                         destination: str = "external", max_timeline: int = 40) -> dict[str, Any]:
    redactor = redactor or Redactor()
    unknowns = list(unknowns or [])
    if baseline is None:
        unknowns.append("No baseline (last passing run on this bench) available")
    if not log_lines:
        unknowns.append("No target log lines selected for the test window")
    packet = {
        "packet_version": 3,
        "scenario": {"id": nodeid, "intent": intent, "observation_level": observation_level,
                     "requirements": requirements or []},
        "result": {"outcome": outcome, "phase": phase, "assertion": assertion.strip().splitlines()[0][:300]
                   if assertion.strip() else "", "classification_hint": classification_hint,
                   "retries": retries or []},
        "identity": identity,
        "baseline": baseline or "not available",
        "timeline_excerpt": "\n".join(timeline_lines[-max_timeline:]),
        "logs_excerpt": "\n".join(log_lines),
        "diagnostics": {"dtc_delta": dtc_delta or []},
        "evidence": {"metrics": metrics or {}, "artifacts": artifacts or []},
        "known_issue_matches": known_issue_matches or [],
        "unknowns": unknowns or ["none stated"],
        "sanitization": {"policy": destination, "destination": destination},
    }
    return _redact(packet, redactor, destination)


def _redact(value: Any, redactor: Redactor, profile: str) -> Any:
    if isinstance(value, str):
        return redactor.redact(value, profile=profile)
    if isinstance(value, dict):
        return {k: _redact(v, redactor, profile) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, redactor, profile) for v in value]
    return value


class _PacketDumper(yaml.SafeDumper):
    pass


def _str(dumper, value: str):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_PacketDumper.add_representer(str, _str)


def packet_to_yaml(packet: dict[str, Any]) -> str:
    return yaml.dump(packet, Dumper=_PacketDumper, sort_keys=False, allow_unicode=True, width=100)
