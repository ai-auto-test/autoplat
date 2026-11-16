"""Command-line tools: safe-state, lock status, run summary, comparison, failure packets."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from autoplat.config import load_bench


def cmd_safe_state(args) -> int:
    from autoplat.adapters import build_hardware
    from autoplat.bench import safe_state

    bench = load_bench(args.bench)
    hw = build_hardware(bench)
    lines = safe_state(hw, bench.signals.get("TransmissionGear", "PT_Transmission"))
    hw.close()
    for line in lines:
        print(("  " if not line.startswith("FAILED") else "! ") + line)
    return 1 if any(line.startswith("FAILED") for line in lines) else 0


def cmd_lock_status(args) -> int:
    from autoplat.locks import BenchLock

    bench = load_bench(args.bench)
    lock = BenchLock(bench.bench_id, "cli", "status", bench.lock_dir or Path(args.run_root) / ".locks")
    holder = lock.current_holder()
    print(f"{bench.bench_id}: {'held by ' + holder if holder else 'free'}")
    return 0


def _latest(run_root: Path) -> Path:
    runs = sorted(p for p in run_root.iterdir() if p.is_dir() and not p.name.startswith("."))
    if not runs:
        raise SystemExit(f"no runs under {run_root}")
    return runs[-1]


def cmd_summary(args) -> int:
    from autoplat.reporting.summary import load_identity, load_results, summarize

    run = Path(args.run) if args.run else _latest(Path(args.run_root))
    print(summarize(load_results(run), load_identity(run)))
    return 0


def cmd_compare(args) -> int:
    from autoplat.reporting.summary import compare, load_results

    print(json.dumps(compare(load_results(args.current), load_results(args.baseline)), indent=2))
    return 0


def cmd_packets(args) -> int:
    run = Path(args.run) if args.run else _latest(Path(args.run_root))
    packets = sorted(run.glob("tests/*/packet.yaml"))
    for p in packets:
        print(f"--- {p.parent.name}")
        print(p.read_text(encoding="utf-8") if args.show else p)
    if not packets:
        print("no failure packets in", run)
    return 0


def cmd_prompt(args) -> int:
    import yaml

    from autoplat.reporting.triage import build_prompt

    print(build_prompt(yaml.safe_load(Path(args.packet).read_text(encoding="utf-8"))))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="autoplat")
    ap.add_argument("--run-root", default="runs")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("safe-state", help="return a bench to its safe state")
    p.add_argument("--bench", required=True)
    p.set_defaults(fn=cmd_safe_state)
    p = sub.add_parser("lock-status", help="show who holds a bench")
    p.add_argument("--bench", required=True)
    p.set_defaults(fn=cmd_lock_status)
    p = sub.add_parser("summary", help="one-screen summary of a run (default: latest)")
    p.add_argument("run", nargs="?")
    p.set_defaults(fn=cmd_summary)
    p = sub.add_parser("compare", help="new/fixed/still-failing against a baseline run")
    p.add_argument("current")
    p.add_argument("baseline")
    p.set_defaults(fn=cmd_compare)
    p = sub.add_parser("packets", help="list failure packets of a run")
    p.add_argument("run", nargs="?")
    p.add_argument("--show", action="store_true")
    p.set_defaults(fn=cmd_packets)
    p = sub.add_parser("prompt", help="print the constrained triage prompt for a packet")
    p.add_argument("packet")
    p.set_defaults(fn=cmd_prompt)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
