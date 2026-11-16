"""Per-test timeline of actions and observations with monotonic timestamps (Chapter 3)."""
from __future__ import annotations

import contextvars
import functools
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

_current: contextvars.ContextVar["Timeline | None"] = contextvars.ContextVar("autoplat_timeline", default=None)


@dataclass
class Entry:
    t: float
    layer: str
    component: str
    action: str
    data: dict[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        extra = " ".join(f"{k}={v}" for k, v in self.data.items())
        return f"+{self.t:.3f}  {self.layer:<9} {self.component:<12} {self.action} {extra}".rstrip()


class Timeline:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self.start = clock()
        self.entries: list[Entry] = []

    def now(self) -> float:
        return self._clock() - self.start

    def record(self, layer: str, component: str, action: str, **data: Any) -> Entry:
        entry = Entry(round(self.now(), 3), layer, component, action, data)
        self.entries.append(entry)
        return entry

    def lines(self) -> list[str]:
        return [e.render() for e in self.entries]

    def between(self, start_s: float, end_s: float) -> list[Entry]:
        return [e for e in self.entries if start_s <= e.t <= end_s]

    def write_jsonl(self, path: Path) -> None:
        with path.open("w", encoding="utf-8") as f:
            for e in self.entries:
                f.write(json.dumps({"t": e.t, "layer": e.layer, "component": e.component,
                                    "action": e.action, **e.data}, default=str) + "\n")

    def activate(self) -> contextvars.Token:
        return _current.set(self)

    @staticmethod
    def deactivate(token: contextvars.Token) -> None:
        _current.reset(token)


def current_timeline() -> Timeline | None:
    return _current.get()


def traced(component: str, layer: str = "adapter"):
    """Record every call of an adapter or service method in the active timeline."""
    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            tl = current_timeline()
            if tl is None:
                return fn(*args, **kwargs)
            shown = {k: v for k, v in kwargs.items() if k != "timeout_s"}
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                tl.record(layer, component, fn.__name__, **shown, error=type(exc).__name__)
                raise
            summary = result if isinstance(result, (str, int, float, bool)) or result is None else type(result).__name__
            tl.record(layer, component, fn.__name__, **shown, result=summary)
            return result
        return wrapper
    return decorate
