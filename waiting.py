"""Deadline-based waiting and visible retries (Chapter 5)."""
from __future__ import annotations

import time
from typing import Any, Callable

from autoplat.timeline import current_timeline


class WaitTimeout(TimeoutError):
    def __init__(self, message: str, last: Any = None):
        super().__init__(message)
        self.last = last


def _unpack(value: Any) -> tuple[bool, Any]:
    # Predicates may return (ready, detail) so timeouts can report why the state was not reached.
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], bool):
        return value[0], value[1]
    return bool(value), value


def wait_until(predicate: Callable[[], Any], timeout_s: float, *, interval_s: float = 0.25,
               describe: str = "condition", clock: Callable[[], float] = time.monotonic,
               sleep: Callable[[float], None] = time.sleep) -> Any:
    """Poll `predicate` until truthy or the monotonic deadline passes.

    Returns the predicate's value (or the detail of a `(ready, detail)` tuple).
    Raises WaitTimeout naming the condition, timeout, poll count, and last observed value.
    """
    start = clock()
    deadline = start + timeout_s
    attempts = 0
    while True:
        attempts += 1
        ready, last_value = _unpack(predicate())
        if ready:
            tl = current_timeline()
            if tl is not None:
                tl.record("wait", "wait_until", describe, waited_s=round(clock() - start, 3))
            return last_value
        if clock() >= deadline:
            raise WaitTimeout(
                f"{describe} not reached after {timeout_s:.1f}s "
                f"({attempts} polls); last observed={last_value!r}", last_value)
        sleep(interval_s)


def with_retry(op: Callable[[], Any], *, attempts: int = 2,
               retry_on: tuple[type[BaseException], ...] = (ConnectionResetError,),
               timeline=None) -> Any:
    """Retry an *operation* on known-transient errors; every retry is recorded."""
    timeline = timeline or current_timeline()
    for i in range(1, attempts + 1):
        try:
            return op()
        except retry_on as exc:
            if timeline is not None:
                timeline.record("retry", "with_retry", getattr(op, "__name__", "op"),
                                attempt=i, error=repr(exc))
            if i == attempts:
                raise
    raise AssertionError("unreachable")
