"""Run and test evidence folders, labeled artifacts, and failure-time collection (Chapter 3)."""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from autoplat.redaction import Redactor
from autoplat.timeline import Timeline

_UNSAFE = re.compile(r"[^A-Za-z0-9_.\-\[\]]+")


def safe_name(nodeid: str) -> str:
    return _UNSAFE.sub("_", nodeid.replace("::", "__")).strip("_")[:150]


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def git_revision(path: Path) -> tuple[str, bool]:
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=path, capture_output=True,
                             text=True, timeout=5).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=path, capture_output=True,
                                    text=True, timeout=5).stdout.strip())
        return rev or "unknown", dirty
    except (OSError, subprocess.SubprocessError):
        return "unknown", False


@dataclass
class RunIdentity:
    run_id: str
    bench: dict[str, Any]
    platform: dict[str, Any]
    suite: str
    started: str
    target: dict[str, Any] = field(default_factory=dict)
    owner: str = "pipeline"
    purpose: str = "test run"


@dataclass
class Artifact:
    name: str
    kind: str
    path: str
    t_start: float | None = None
    t_end: float | None = None


class RunStore:
    """One folder per run; one sub-folder per test."""

    def __init__(self, root: Path, bench_id: str, suite: str = "adhoc"):
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        self.run_id = f"{stamp}_{bench_id}"
        self.path = Path(root) / self.run_id
        n = 1
        while self.path.exists():
            n += 1
            self.path = Path(root) / f"{self.run_id}_{n}"
        (self.path / "tests").mkdir(parents=True)
        self.suite = suite
        self.identity: RunIdentity | None = None
        self.results: list[dict[str, Any]] = []

    def write_identity(self, identity: RunIdentity) -> None:
        self.identity = identity
        (self.path / "run.json").write_text(json.dumps(asdict(identity), indent=2), encoding="utf-8")

    def test_dir(self, nodeid: str) -> Path:
        d = self.path / "tests" / safe_name(nodeid)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def add_result(self, result: dict[str, Any]) -> None:
        self.results.append(result)
        with (self.path / "results.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(result, default=str) + "\n")


class TestEvidence:
    """Evidence for one test: timeline, labeled artifacts, and failure-time collectors."""

    __test__ = False  # not a pytest test class

    def __init__(self, folder: Path, nodeid: str, redactor: Redactor | None = None):
        self.folder = folder
        self.nodeid = nodeid
        self.timeline = Timeline()
        self.redactor = redactor or Redactor()
        self.artifacts: list[Artifact] = []
        self.metrics: dict[str, Any] = {}
        self.notes: dict[str, Any] = {}
        self._collectors: list[tuple[str, Callable[["TestEvidence"], None]]] = []
        self._token = None

    # lifecycle -------------------------------------------------------------
    def __enter__(self):
        self._token = self.timeline.activate()
        return self

    def __exit__(self, *exc):
        Timeline.deactivate(self._token)
        self.timeline.write_jsonl(self.folder / "timeline.jsonl")
        (self.folder / "timeline.txt").write_text("\n".join(self.timeline.lines()), encoding="utf-8")

    # attaching -------------------------------------------------------------
    def _register(self, name: str, kind: str, path: Path, window: tuple[float, float] | None) -> Path:
        t0, t1 = window if window else (None, self.timeline.now())
        self.artifacts.append(Artifact(name, kind, path.relative_to(self.folder).as_posix(), t0, t1))
        self.timeline.record("evidence", "store", "attach", name=name, kind=kind)
        return path

    def attach_text(self, name: str, text: str, kind: str = "log", window=None) -> Path:
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.redactor.redact(text), encoding="utf-8")
        return self._register(name, kind, path, window)

    def attach_json(self, name: str, data: Any, kind: str = "data") -> Path:
        return self.attach_text(name, json.dumps(data, indent=2, default=str), kind=kind)

    def attach_bytes(self, name: str, data: bytes, kind: str = "binary", window=None) -> Path:
        path = self.folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return self._register(name, kind, path, window)

    def attach_image(self, name: str, image) -> Path:
        import cv2

        ok, buf = cv2.imencode(Path(name).suffix or ".png", image)
        if not ok:
            raise ValueError(f"could not encode image {name}")
        return self.attach_bytes(name, buf.tobytes(), kind="image")

    def metric(self, name: str, value: Any, limit: Any = None) -> None:
        self.metrics[name] = {"value": value, "limit": limit}
        self.timeline.record("evidence", "metric", name, value=value, limit=limit)

    def mark(self, text: str) -> None:
        self.timeline.record("scenario", "mark", text)

    # failure-time collection ---------------------------------------------------
    def add_collector(self, name: str, fn: Callable[["TestEvidence"], None]) -> None:
        self._collectors.append((name, fn))

    def collect_on_failure(self) -> list[str]:
        problems = []
        for name, fn in self._collectors:
            t0 = time.monotonic()
            try:
                fn(self)
            except Exception as exc:  # collection must never hide the original failure
                problems.append(f"{name}: {type(exc).__name__}: {exc}")
                self.timeline.record("evidence", "collector", name, error=repr(exc))
            else:
                self.timeline.record("evidence", "collector", name, took_s=round(time.monotonic() - t0, 3))
        return problems

    def write_result(self, result: dict[str, Any]) -> None:
        payload = {**result, "artifacts": [asdict(a) for a in self.artifacts], "metrics": self.metrics,
                   "notes": self.notes}
        (self.folder / "result.json").write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
