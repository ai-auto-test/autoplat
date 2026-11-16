"""Guarded AI agents for test operations (Chapter 12).

The agent proposes; a separate executor acts only on approved requests.
"""
from __future__ import annotations

import functools
import inspect
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

import yaml

from autoplat.errors import ToolError


def bounded(data: dict[str, Any], limit: int) -> dict[str, Any]:
    """Trim list values until the JSON size fits; say so explicitly."""
    out = dict(data)
    out["truncated"] = False
    while len(json.dumps(out, default=str)) > limit:
        lists = [k for k, v in out.items() if isinstance(v, list) and v]
        if not lists:
            break
        key = max(lists, key=lambda k: len(json.dumps(out[k], default=str)))
        out[key] = out[key][: max(len(out[key]) // 2, 0)]
        out["truncated"] = True
    return out


@dataclass
class ToolSpec:
    name: str
    fn: Callable[..., Any]
    description: str
    read_only: bool
    proposal_only: bool
    max_output_bytes: int


class ToolRegistry:
    def __init__(self):
        self.tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self.tools[spec.name] = spec

    def describe(self) -> list[dict[str, Any]]:
        return [{"name": t.name, "description": t.description, "signature": str(inspect.signature(t.fn)),
                 "read_only": t.read_only, "proposal_only": t.proposal_only} for t in self.tools.values()]


registry = ToolRegistry()


def agent_tool(*, read_only: bool = False, proposal_only: bool = False, max_output_bytes: int = 200_000,
               into: ToolRegistry | None = None):
    if not (read_only or proposal_only):
        raise ValueError("agent tools must be read_only or proposal_only; agents never act directly")

    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            result = fn(*args, **kwargs)
            if isinstance(result, dict):
                return bounded(result, max_output_bytes)
            return result
        (into or registry).register(ToolSpec(fn.__name__, wrapper, inspect.getdoc(fn) or "", read_only,
                                             proposal_only, max_output_bytes))
        return wrapper
    return decorate


# ---------------------------------------------------------------- job description
@dataclass
class JobDescription:
    agent: str
    purpose: str
    read_only: list[str]
    proposal_only: list[str]
    max_tool_calls: int = 25
    max_runtime_s: float = 300
    owner: str = ""
    version: str = "1"

    @classmethod
    def load(cls, path: str | Path) -> "JobDescription":
        d = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        tools, limits = d.get("tools", {}), d.get("limits", {})
        return cls(agent=d["agent"], purpose=d["purpose"], read_only=tools.get("read_only", []),
                   proposal_only=tools.get("proposal_only", []),
                   max_tool_calls=limits.get("max_tool_calls", 25), max_runtime_s=limits.get("max_runtime_s", 300),
                   owner=d.get("owner", ""), version=str(d.get("version", "1")))

    def allowed(self) -> set[str]:
        return set(self.read_only) | set(self.proposal_only)


@dataclass
class AuditEntry:
    call_id: str
    tool: str
    params: dict[str, Any]
    output: Any = None
    error: str | None = None
    t: float = field(default_factory=time.time)


class LimitReached(RuntimeError):
    pass


class AgentSession:
    """Enforces the job description: allowed tools, call and time limits, full audit."""

    def __init__(self, job: JobDescription, tools: ToolRegistry = registry, trigger: str = ""):
        self.job, self.tools, self.trigger = job, tools, trigger
        self.audit: list[AuditEntry] = []
        self.started = time.monotonic()

    def call(self, tool: str, **params: Any) -> tuple[str, Any]:
        if len(self.audit) >= self.job.max_tool_calls:
            raise LimitReached(f"max_tool_calls {self.job.max_tool_calls} reached")
        if time.monotonic() - self.started > self.job.max_runtime_s:
            raise LimitReached(f"max_runtime_s {self.job.max_runtime_s} reached")
        call_id = f"call-{len(self.audit) + 1}"
        entry = AuditEntry(call_id, tool, params)
        self.audit.append(entry)
        if tool not in self.job.allowed() or tool not in self.tools.tools:
            entry.error = "tool not permitted by job description"
            raise ToolError(f"{tool!r} is not available to agent {self.job.agent}; "
                            f"allowed: {sorted(self.job.allowed())}")
        try:
            entry.output = self.tools.tools[tool].fn(**params)
        except ToolError as exc:
            entry.error = str(exc)
            raise
        return call_id, entry.output

    def output_lines(self, call_id: str) -> list[str]:
        for e in self.audit:
            if e.call_id == call_id:
                text = e.output if isinstance(e.output, str) else json.dumps(e.output, indent=1, default=str)
                return text.splitlines()
        return []

    def write_audit(self, path: str | Path, final: dict[str, Any] | None = None) -> None:
        Path(path).write_text(json.dumps({
            "agent": self.job.agent, "job_version": self.job.version, "trigger": self.trigger,
            "calls": [asdict(e) for e in self.audit], "final": final}, indent=2, default=str), encoding="utf-8")


def verify_agent_output(session: AgentSession, final: dict[str, Any]) -> list[str]:
    """Every finding cites existing call output lines, e.g. 'call-3:2'."""
    problems = []
    for f in final.get("findings", []):
        cites = f.get("citations", [])
        if not cites:
            problems.append(f"uncited finding: {f.get('text', '')[:60]}")
        for c in cites:
            call_id, _, line = c.strip("[]").partition(":")
            lines = session.output_lines(call_id)
            if not lines or not line.isdigit() or not 1 <= int(line) <= len(lines):
                problems.append(f"citation {c} does not match any tool output")
    if final.get("action_taken"):
        problems.append("agent claims to have taken an action; agents may only propose")
    return problems


class AgentModel(Protocol):
    def next_step(self, job: JobDescription, tools: list[dict[str, Any]], transcript: list[dict[str, Any]]
                  ) -> dict[str, Any]:
        """Return {"tool": name, "params": {...}} or {"final": {...}}."""


def run_agent(session: AgentSession, model: AgentModel) -> dict[str, Any]:
    transcript: list[dict[str, Any]] = []
    tools = [t for t in session.tools.describe() if t["name"] in session.job.allowed()]
    while True:
        try:
            step = model.next_step(session.job, tools, transcript)
        except Exception as exc:
            return {"status": "incomplete", "reason": f"model error: {exc}", "findings": []}
        if "final" in step:
            final = dict(step["final"])
            final["verification_problems"] = verify_agent_output(session, final)
            final.setdefault("status", "complete")
            return final
        try:
            call_id, output = session.call(step["tool"], **step.get("params", {}))
            transcript.append({"call_id": call_id, "tool": step["tool"], "output": output})
        except ToolError as exc:
            transcript.append({"tool": step.get("tool"), "error": str(exc)})
        except LimitReached as exc:
            return {"status": "incomplete", "reason": str(exc), "findings": []}


# ------------------------------------------------------------------ approvals
@dataclass
class ApprovalRequest:
    action: str
    params: dict[str, Any]
    reason: str
    expected_effect: str
    risk: str
    rollback: str
    proposed_by: str
    request_id: str = field(default_factory=lambda: f"apr-{uuid.uuid4().hex[:8]}")
    status: str = "pending"        # pending | approved | rejected | executed | failed
    approver: str | None = None
    policy: str = ""
    result: str = ""


@dataclass
class PolicyRule:
    action: str
    condition: str                 # "bench_idle" or "any"
    approval: str                  # "automatic" | "one_owner" | "owner_and_lead" | "forbidden"


DEFAULT_POLICY = [
    PolicyRule("bench.reset_l0", "bench_idle", "automatic"),
    PolicyRule("bench.reset_l1", "bench_idle", "automatic"),
    PolicyRule("bench.reset_l2", "bench_idle", "one_owner"),
    PolicyRule("bench.reset_l3", "bench_idle", "one_owner"),
    PolicyRule("bench.usb_hub_cycle", "bench_idle", "automatic"),
    PolicyRule("bench.reflash", "any", "owner_and_lead"),
    PolicyRule("tests.rerun", "any", "one_owner"),
    PolicyRule("defect.close", "any", "forbidden"),
]


class ApprovalQueue:
    def __init__(self, folder: str | Path, policy: list[PolicyRule] | None = None):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.policy = {r.action: r for r in (policy or DEFAULT_POLICY)}

    def _path(self, rid: str) -> Path:
        return self.folder / f"{rid}.json"

    def save(self, req: ApprovalRequest) -> None:
        self._path(req.request_id).write_text(json.dumps(asdict(req), indent=2), encoding="utf-8")

    def load(self, rid: str) -> ApprovalRequest:
        return ApprovalRequest(**json.loads(self._path(rid).read_text(encoding="utf-8")))

    def submit(self, req: ApprovalRequest) -> ApprovalRequest:
        rule = self.policy.get(req.action)
        if rule is None or rule.approval == "forbidden":
            req.status, req.policy = "rejected", f"action {req.action!r} not permitted for agents"
        elif rule.approval == "automatic":
            req.status, req.approver, req.policy = "approved", "policy:automatic", f"{rule.action}: automatic"
        else:
            req.policy = f"{rule.action}: {rule.approval}"
        self.save(req)
        return req

    def approve(self, rid: str, approver: str, *, is_lead: bool = False) -> ApprovalRequest:
        req = self.load(rid)
        rule = self.policy[req.action]
        if rule.approval == "owner_and_lead" and not is_lead and req.approver is None:
            req.approver = approver  # first of two approvals
        else:
            req.approver = f"{req.approver}+{approver}" if req.approver else approver
            req.status = "approved"
        self.save(req)
        return req

    def reject(self, rid: str, approver: str) -> ApprovalRequest:
        req = self.load(rid)
        req.status, req.approver = "rejected", approver
        self.save(req)
        return req

    def pending(self) -> list[ApprovalRequest]:
        return [r for r in (self.load(p.stem) for p in self.folder.glob("apr-*.json")) if r.status == "pending"]


class Executor:
    """Executes approved requests after re-checking preconditions at execution time."""

    def __init__(self, queue: ApprovalQueue, actions: dict[str, Callable[..., str]],
                 preconditions: dict[str, Callable[[dict[str, Any]], tuple[bool, str]]] | None = None):
        self.queue, self.actions, self.preconditions = queue, actions, preconditions or {}

    def execute(self, rid: str) -> ApprovalRequest:
        req = self.queue.load(rid)
        if req.status != "approved":
            raise PermissionError(f"{rid} is {req.status}, not approved")
        rule = self.queue.policy[req.action]
        check = self.preconditions.get(rule.condition)
        if check is not None:
            ok, why = check(req.params)
            if not ok:
                req.status, req.result = "failed", f"precondition {rule.condition} not met: {why}"
                self.queue.save(req)
                return req
        try:
            req.result = self.actions[req.action](**req.params)
            req.status = "executed"
        except Exception as exc:
            req.status, req.result = "failed", f"{type(exc).__name__}: {exc}"
        self.queue.save(req)
        return req
