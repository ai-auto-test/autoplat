"""Expiring, visible bench locks (Chapter 5)."""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from autoplat.timeline import current_timeline


@dataclass
class LockRecord:
    bench_id: str
    owner: str
    purpose: str
    since: float
    expires: float
    pid: int

    def describe(self) -> str:
        age = int(time.time() - self.since)
        return f"{self.owner} ({self.purpose}), held {age}s, pid {self.pid}"


class BenchLock:
    """File-based lock with expiry. Suitable for one host or a shared filesystem."""

    def __init__(self, bench_id: str, owner: str, purpose: str, lock_dir: str | Path | None = None,
                 ttl_s: float = 900.0):
        self.bench_id, self.owner, self.purpose, self.ttl_s = bench_id, owner, purpose, ttl_s
        self.path = Path(lock_dir or Path.home() / ".autoplat" / "locks") / f"{bench_id}.lock"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.held = False

    def _read(self) -> LockRecord | None:
        try:
            return LockRecord(**json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None

    def current_holder(self) -> str | None:
        rec = self._read()
        return rec.describe() if rec else None

    def _try_create(self) -> bool:
        now = time.time()
        rec = LockRecord(self.bench_id, self.owner, self.purpose, now, now + self.ttl_s, os.getpid())
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(rec), f)
        return True

    def acquire(self, timeout_s: float = 600.0, poll_s: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while True:
            if self._try_create():
                self.held = True
                return True
            rec = self._read()
            if rec is not None and rec.expires < time.time():
                # An expired lock means a run ended without cleanup: make that visible.
                tl = current_timeline()
                if tl is not None:
                    tl.record("bench", "lock", "expired_lock_removed", holder=rec.describe())
                print(f"[autoplat] WARNING removing expired lock on {self.bench_id}: {rec.describe()}")
                self.path.unlink(missing_ok=True)
                continue
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll_s)

    def refresh(self) -> None:
        rec = self._read()
        if self.held and rec is not None and rec.pid == os.getpid():
            rec.expires = time.time() + self.ttl_s
            self.path.write_text(json.dumps(asdict(rec)), encoding="utf-8")

    def release(self) -> None:
        rec = self._read()
        if self.held and rec is not None and rec.pid == os.getpid():
            self.path.unlink(missing_ok=True)
        self.held = False

    def __enter__(self):
        if not self.acquire():
            from autoplat.errors import InstrumentBusy
            raise InstrumentBusy(f"bench {self.bench_id} busy: held by {self.current_holder()}")
        return self

    def __exit__(self, *exc):
        self.release()
