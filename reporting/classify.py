"""Automatic first classification of non-passing results (Chapter 13)."""
from __future__ import annotations

from autoplat.errors import AutoplatError

CATEGORIES = ("product", "test", "bench", "infrastructure", "unknown")

# Errors that usually mean the test code itself is wrong.
_TEST_BUGS = (TypeError, AttributeError, NameError, KeyError, IndexError, ImportError, NotImplementedError)


def classify(exc: BaseException | None, phase: str, bench_healthy: bool | None = None) -> str:
    """Rules: adapter exception category first; then phase and exception type; else unknown.

    bench_healthy: result of a post-failure bench health check (None if not checked).
    """
    if exc is None:
        return "unknown"
    if isinstance(exc, AutoplatError):
        return exc.category
    if isinstance(exc, AssertionError):
        if phase != "call":
            return "bench" if bench_healthy is False else "unknown"
        return "product" if bench_healthy is not False else "unknown"
    if isinstance(exc, TimeoutError):
        return "bench" if bench_healthy is False else "unknown"
    if isinstance(exc, _TEST_BUGS):
        return "test"
    if isinstance(exc, (ConnectionError, OSError)):
        return "infrastructure"
    return "unknown"
