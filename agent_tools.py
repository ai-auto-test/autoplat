"""Concrete read-only and proposal-only tools over run evidence, for the triage agent (Chapter 12)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from autoplat.agents import ApprovalQueue, ApprovalRequest, ToolRegistry, agent_tool
from autoplat.errors import ToolError
from autoplat.evidence import safe_name
from autoplat.reporting.signature import KnownIssueStore

tools = ToolRegistry()


@dataclass
class ToolContext:
    run_dir: Path
    queue: ApprovalQueue
    known: KnownIssueStore
    bench_health: Callable[[str], dict[str, Any]]
    agent_name: str = "agent:nightly-triage"


_ctx: ToolContext | None = None


def configure(ctx: ToolContext) -> None:
    global _ctx
    _ctx = ctx


def _context() -> ToolContext:
    if _ctx is None:
        raise ToolError("tools not configured; call autoplat.agent_tools.configure() first")
    return _ctx


def _test_dir(test: str) -> Path:
    d = _context().run_dir / "tests" / safe_name(test)
    if not d.is_dir():
        raise ToolError(f"no evidence for test {test!r} in run {_context().run_dir.name}")
    return d


@agent_tool(read_only=True, max_output_bytes=20_000, into=tools)
def get_test_result(test: str) -> dict:
    """Return the recorded result (outcome, classification, message, metrics) of one test in the run."""
    data = json.loads((_test_dir(test) / "result.json").read_text(encoding="utf-8"))
    return {k: data.get(k) for k in ("outcome", "phase", "classification", "message", "metrics", "notes")}


@agent_tool(read_only=True, max_output_bytes=200_000, into=tools)
def search_logs(test: str, pattern: str | None = None, start_s: float = 0.0, end_s: float = 120.0) -> dict:
    """Return target log lines captured for a failed test, optionally filtered by regex.

    start_s/end_s are seconds relative to the first captured line; window limited to 120 s.
    Output: {"lines": [...], "truncated": bool}.
    """
    if end_s - start_s > 120:
        raise ToolError("window too large; maximum 120 s")
    path = _test_dir(test) / "logs" / "target.txt"
    if not path.exists():
        raise ToolError(f"no target log captured for {test!r}; request bench evidence instead")
    lines = path.read_text(encoding="utf-8").splitlines()
    if pattern:
        rx = re.compile(pattern)
        lines = [line for line in lines if rx.search(line)]
    return {"lines": lines}


@agent_tool(read_only=True, max_output_bytes=10_000, into=tools)
def get_bench_health(bench: str) -> dict:
    """Return current bench health: capture signal, target link, lock holder."""
    return _context().bench_health(bench)


@agent_tool(read_only=True, max_output_bytes=20_000, into=tools)
def search_known_issues(signature: str) -> dict:
    """Return known issues whose failure signature is similar (>= 0.85) to the given signature."""
    return {"matches": [{"id": i.id, "title": i.title, "similarity": round(s, 2)}
                        for i, s in _context().known.match(signature)]}


@agent_tool(proposal_only=True, max_output_bytes=5_000, into=tools)
def propose_recovery(action: str, params: dict, reason: str, expected_effect: str, risk: str,
                     rollback: str) -> dict:
    """Create an approval request for a recovery action. Nothing is executed by this tool."""
    ctx = _context()
    req = ctx.queue.submit(ApprovalRequest(action, params, reason, expected_effect, risk, rollback, ctx.agent_name))
    return {"request_id": req.request_id, "status": req.status, "policy": req.policy}
