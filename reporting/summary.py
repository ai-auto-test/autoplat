"""Run summaries with a trust indicator, run comparison, and flake rates (Chapters 13 and 14)."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable


def load_results(run_dir: str | Path) -> list[dict[str, Any]]:
    path = Path(run_dir) / "results.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_identity(run_dir: str | Path) -> dict[str, Any]:
    path = Path(run_dir) / "run.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def trust_level(results: list[dict[str, Any]], identity_verified: bool) -> tuple[str, list[str]]:
    reasons = []
    total = len(results) or 1
    env = sum(r.get("classification") in ("bench", "infrastructure") for r in results)
    unknown = sum(r.get("classification") == "unknown" for r in results)
    if not identity_verified:
        reasons.append("build identity not verified")
    if env / total > 0.05:
        reasons.append(f"{env} bench/infrastructure failures")
    nonpass = sum(r["outcome"] in ("failed", "error") for r in results) or 1
    if unknown / nonpass > 0.2 and unknown > 0:
        reasons.append(f"{unknown} unknown classifications")
    if not identity_verified:
        return "POOR", reasons
    if reasons:
        return "FAIR", reasons
    return "GOOD", ["identity verified, environment healthy"]


def summarize(results: list[dict[str, Any]], identity: dict[str, Any]) -> str:
    target = identity.get("target", {})
    outcomes = Counter(r["outcome"] for r in results)
    classes = Counter(r.get("classification") for r in results if r["outcome"] in ("failed", "error"))
    na = [r for r in results if r["outcome"] == "skipped" and str(r.get("reason", "")).startswith("not applicable")]
    quarantined = sum(r.get("quarantined", False) for r in results)
    executed = outcomes["passed"] + outcomes["failed"] + outcomes["error"]
    rate = f"{100 * outcomes['passed'] / executed:.1f}%" if executed else "n/a"
    by_domain = Counter(r.get("domain", "other") for r in results if r.get("classification") == "product"
                        and r["outcome"] in ("failed", "error"))
    trust, why = trust_level(results, bool(target.get("verified")))
    lines = [
        f"Run {identity.get('run_id', '?')}  build {target.get('build_id', '?')} "
        f"({target.get('variant', '?')})  bench {identity.get('bench', {}).get('id', '?')}",
        f"Executed {executed}: {outcomes['passed']} passed ({rate}), {outcomes['failed']} failed, "
        f"{outcomes['error']} errors; quarantined {quarantined}",
        "Product failures: " + (str(classes["product"]) + "  (" + ", ".join(f"{d} {n}" for d, n in by_domain.items())
                                + ")" if classes["product"] else "0"),
        f"Bench/infrastructure: {classes['bench'] + classes['infrastructure']}   Test: {classes['test']}   "
        f"Unknown: {classes['unknown']}",
        f"Not applicable on this bench: {len(na)}",
        f"Trust: {trust} - {'; '.join(why)}",
    ]
    return "\n".join(lines)


def compare(current: list[dict[str, Any]], baseline: list[dict[str, Any]]) -> dict[str, list[str]]:
    """New, fixed, still failing, and changed-signature results against a baseline run."""
    def index(results):
        return {r["nodeid"]: r for r in results}
    cur, base = index(current), index(baseline)
    out: dict[str, list[str]] = defaultdict(list)
    for nodeid, r in cur.items():
        b = base.get(nodeid)
        failed = r["outcome"] in ("failed", "error")
        b_failed = b is not None and b["outcome"] in ("failed", "error")
        if failed and (b is None or b["outcome"] == "passed"):
            out["new"].append(nodeid)
        elif not failed and b_failed and r["outcome"] == "passed":
            out["fixed"].append(nodeid)
        elif failed and b_failed:
            key = "still_failing" if r.get("signature") == b.get("signature") else "changed_signature"
            out[key].append(nodeid)
    return dict(out)


def flake_rates(runs: Iterable[list[dict[str, Any]]]) -> dict[str, float]:
    """Fraction of executions whose outcome differs from the majority for the same build and bench."""
    groups: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for results in runs:
        for r in results:
            if r["outcome"] in ("passed", "failed", "error"):
                key = (r["nodeid"], r.get("build", ""), r.get("bench", ""))
                groups[key].append("pass" if r["outcome"] == "passed" else "fail")
    per_test: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for (nodeid, _, _), outcomes in groups.items():
        majority, count = Counter(outcomes).most_common(1)[0]
        per_test[nodeid][0] += len(outcomes) - count
        per_test[nodeid][1] += len(outcomes)
    return {k: v[0] / v[1] for k, v in per_test.items() if v[1]}
