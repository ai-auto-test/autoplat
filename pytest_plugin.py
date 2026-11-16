"""pytest plugin: bench selection, capability filtering, fixtures, evidence, classification.

Enable in a root conftest.py:  pytest_plugins = ["autoplat.pytest_plugin"]
"""
from __future__ import annotations

import os
import platform
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from autoplat.config import BenchConfig, load_bench
from autoplat.evidence import RunIdentity, RunStore, TestEvidence, git_revision, utc_now
from autoplat.redaction import redactor
from autoplat.reporting.classify import classify
from autoplat.reporting.packet import build_failure_packet, packet_to_yaml, select_log_lines
from autoplat.reporting.quarantine import load_quarantine
from autoplat.reporting.signature import Failure, KnownIssueStore, assertion_kind, signature
from autoplat.reporting.summary import summarize

MARKERS = [
    "smoke: fast, high-signal checks run after every flash",
    "functional: feature behavior checks",
    "slow: runtime longer than five minutes",
    "gate(name): scenario is part of the named release gate",
    "requires(*caps): bench capabilities the scenario needs",
    "req(*ids): requirement IDs covered by the scenario",
    "risk(level): high, medium or low",
    "hazardous: flashing, fault injection on safety-relevant networks, power interruption",
    "observation(level): echo, internal, downstream or physical",
    "camera: camera feature domain",
    "display: display feature domain",
    "audio: audio feature domain",
    "network: vehicle network and diagnostics domain",
    "ota: OTA and release domain",
    "unstable: known unstable; excluded from gates",
]
DOMAINS = ("camera", "display", "audio", "network", "ota")
NA = "not applicable: "
REPORTS = pytest.StashKey[dict]()


def pytest_addoption(parser):
    g = parser.getgroup("autoplat")
    g.addoption("--bench", default=None, help="bench YAML (default: ini autoplat_bench)")
    g.addoption("--run-root", default=None, help="folder for run evidence (default: ini autoplat_run_root)")
    g.addoption("--suite", default="adhoc", help="suite name recorded in the run identity")
    g.addoption("--gate", default=None, help="run only scenarios marked gate(<name>)")
    g.addoption("--expect-build", default=None, help="stop the run if the target reports another build")
    g.addoption("--allow-hazardous", action="store_true", help="permit @hazardous tests on benches that allow them")
    g.addoption("--owner", default=os.environ.get("USERNAME") or os.environ.get("USER") or "pipeline")
    parser.addini("autoplat_bench", "default bench YAML", default="")
    parser.addini("autoplat_run_root", "folder for run evidence", default="runs")
    parser.addini("autoplat_quarantine", "quarantine YAML", default="quarantine.yaml")
    parser.addini("autoplat_known_issues", "known issues YAML", default="known_issues.yaml")


class State:
    def __init__(self, config: pytest.Config):
        self.config = config
        root = Path(config.rootpath)
        bench = config.getoption("--bench") or config.getini("autoplat_bench")
        self.bench_path = (root / bench) if bench and not Path(bench).is_absolute() else (Path(bench) if bench else None)
        self.run_root = root / (config.getoption("--run-root") or config.getini("autoplat_run_root"))
        self.quarantine = load_quarantine(root / config.getini("autoplat_quarantine"))
        self.known = KnownIssueStore.load(root / config.getini("autoplat_known_issues"))
        self._bench: BenchConfig | None = None
        self.run_store: RunStore | None = None
        self.target_identity: dict[str, Any] = {}

    @property
    def bench(self) -> BenchConfig | None:
        if self._bench is None and self.bench_path is not None and self.bench_path.exists():
            self._bench = load_bench(self.bench_path)
        return self._bench

    def store(self) -> RunStore:
        if self.run_store is None:
            bench = self.bench
            self.run_store = RunStore(self.run_root, bench.bench_id if bench else "nobench",
                                      self.config.getoption("--suite"))
            rev, dirty = git_revision(Path(self.config.rootpath))
            self.run_store.write_identity(RunIdentity(
                run_id=self.run_store.run_id,
                bench={"id": bench.bench_id if bench else None, "file": str(self.bench_path)},
                platform={"tests_rev": rev, "tests_dirty": dirty, "python": sys.version.split()[0],
                          "host": platform.node()},
                suite=self.config.getoption("--suite"), started=utc_now(),
                owner=self.config.getoption("--owner")))
        return self.run_store


def pytest_configure(config):
    for m in MARKERS:
        config.addinivalue_line("markers", m)
    config.stash[STATE] = State(config)


STATE = pytest.StashKey[State]()


def _state(config) -> State:
    return config.stash[STATE]


def pytest_collection_modifyitems(config, items):
    state = _state(config)
    bench = state.bench
    gate = config.getoption("--gate")
    selected, deselected = [], []
    for item in items:
        if gate:
            marks = [m.args[0] for m in item.iter_markers("gate") if m.args]
            if gate not in marks or item.get_closest_marker("unstable"):
                deselected.append(item)
                continue
        selected.append(item)
        req = item.get_closest_marker("requires")
        if req:
            if bench is None:
                item.add_marker(pytest.mark.skip(reason=NA + "no bench selected (--bench)"))
            elif not bench.has(*req.args):
                item.add_marker(pytest.mark.skip(reason=NA + f"bench lacks {', '.join(bench.missing(*req.args))}"))
        if item.get_closest_marker("hazardous"):
            if not (bench and bench.hazardous_allowed and config.getoption("--allow-hazardous")):
                item.add_marker(pytest.mark.skip(reason=NA + "hazardous tests need a permitting bench "
                                                 "and --allow-hazardous"))
        q = state.quarantine.get(item.nodeid)
        if q is not None:
            if q.expired():
                item.warn(pytest.PytestWarning(f"quarantine for {item.nodeid} expired {q.expires} ({q.ticket})"))
            else:
                item.add_marker(pytest.mark.xfail(reason=f"quarantined until {q.expires}: {q.reason} [{q.ticket}]",
                                                  strict=False))
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = selected


# ---------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="session")
def bench(pytestconfig) -> BenchConfig:
    b = _state(pytestconfig).bench
    if b is None:
        pytest.skip(NA + "no bench selected (--bench)")
    return b


@pytest.fixture(scope="session")
def bench_lock(bench, pytestconfig):
    from autoplat.locks import BenchLock

    state = _state(pytestconfig)
    lock = BenchLock(bench.bench_id, owner=pytestconfig.getoption("--owner"),
                     purpose=f"pytest suite {pytestconfig.getoption('--suite')}",
                     lock_dir=bench.lock_dir or state.run_root / ".locks")
    if not lock.acquire(timeout_s=bench.timeout("lock", 600)):
        pytest.exit(f"Bench {bench.bench_id} busy: held by {lock.current_holder()}", returncode=3)
    try:
        yield lock
    finally:
        lock.release()


@pytest.fixture(scope="session")
def hardware(bench, bench_lock):
    from autoplat.adapters import build_hardware
    from autoplat.bench import safe_state

    hw = build_hardware(bench)
    try:
        yield hw
    finally:
        for line in safe_state(hw, bench.signals.get("TransmissionGear", "PT_Transmission")):
            if line.startswith("FAILED"):
                print(f"[autoplat] safe-state {line}")
        hw.close()


@pytest.fixture(scope="session")
def vehicle_service(hardware, bench, pytestconfig):
    from autoplat.services.vehicle import VehicleService

    state = _state(pytestconfig)
    vs = VehicleService(hardware, bench)
    if hardware.power is not None and hasattr(hardware.power, "start_monitor"):
        hardware.power.start_monitor()
    vs.ensure_ready()
    identity = vs.read_identity()
    expected = pytestconfig.getoption("--expect-build")
    identity["verified"] = expected is None or identity.get("build_id") == expected
    store = state.store()
    store.identity.target = identity
    store.write_identity(store.identity)
    state.target_identity = identity
    if not identity["verified"]:
        pytest.exit(f"infrastructure: target reports {identity.get('build_id')}, expected {expected}; "
                    "run stopped before testing the wrong build", returncode=4)
    return vs


@pytest.fixture
def evidence(request, hardware, pytestconfig):
    state = _state(pytestconfig)
    folder = state.store().test_dir(request.node.nodeid)
    ev = TestEvidence(folder, request.node.nodeid, redactor)
    t0 = time.monotonic()
    _register_collectors(ev, hardware, t0)
    request.node._autoplat_evidence = ev
    with ev:
        yield ev


def _register_collectors(ev: TestEvidence, hw, t0: float) -> None:
    def logs(e):
        lines = hw.target.logs(since_s=time.monotonic() - t0 + 1.0)
        e.notes["log_lines"] = lines
        e.attach_text("logs/target.txt", "\n".join(lines))

    def dtcs(e):
        codes = hw.diag.read_dtcs()
        e.notes["dtcs"] = codes
        e.attach_json("dtc_snapshot.json", codes)

    def frame(e):
        e.attach_image("frames/failure.png", hw.capture.grab(timeout_s=2.0).image)

    def bench_health(e):
        healthy = True
        if hw.capture is not None:
            info = hw.capture.signal_info()
            healthy &= info.get("signal", True) is not False
        e.notes["bench_healthy"] = healthy

    ev.add_collector("bench_health", bench_health)
    if hw.target is not None:
        ev.add_collector("target_logs", logs)
    if hw.diag is not None:
        ev.add_collector("dtc_snapshot", dtcs)
    if hw.capture is not None:
        ev.add_collector("display_frame", frame)


@pytest.fixture
def timeline(evidence):
    return evidence.timeline


@pytest.fixture
def vehicle(vehicle_service, evidence):
    """Guarantees: supply on, ignition on, critical services ready."""
    vehicle_service.ensure_ready()
    evidence.mark("precondition: vehicle ready")
    yield vehicle_service
    try:
        vehicle_service.ensure_park()
    except Exception:
        pass


@pytest.fixture
def ivi_home(vehicle, hardware, evidence):
    """Guarantees: vehicle ready and home screen shown."""
    from autoplat.services.display import DisplayService

    hardware.target.go_home()
    DisplayService(hardware, evidence).wait_home(timeout_s=10)
    evidence.mark("precondition: ivi_home")
    yield vehicle
    hardware.target.dismiss_dialogs()


@pytest.fixture
def camera(vehicle, hardware, bench, evidence):
    from autoplat.services.camera import CameraService

    cam = CameraService(hardware, bench, vehicle, evidence)
    yield cam
    try:
        cam.release_view()
    except Exception:
        pass


@pytest.fixture
def display(vehicle, hardware, evidence):
    from autoplat.services.display import DisplayService
    return DisplayService(hardware, evidence)


@pytest.fixture
def hmi(vehicle, hardware):
    from autoplat.services.display import HmiService
    return HmiService(hardware)


@pytest.fixture
def audio(vehicle, hardware, evidence):
    from autoplat.services.audio import AudioService

    svc = AudioService(hardware, evidence)
    svc.stop_all()
    yield svc
    svc.stop_all()


@pytest.fixture
def diag(vehicle, hardware, bench, evidence):
    from autoplat.services.diagnostics import DiagnosticsService

    svc = DiagnosticsService(hardware, bench.tolerated_dtcs)
    svc.record_baseline()
    yield svc
    try:
        unexpected = svc.unexpected_dtcs(set(evidence.notes.get("expected_dtcs", [])))
    except Exception:
        return
    if unexpected:
        evidence.notes["unexpected_dtcs"] = unexpected
        evidence.timeline.record("service", "diag", "unexpected_dtcs", codes=",".join(unexpected))


@pytest.fixture
def ota(vehicle, hardware):
    from autoplat.services.ota import OtaService
    if hardware.ota is None:
        pytest.skip(NA + "bench has no OTA agent")
    return OtaService(hardware)


@pytest.fixture
def power(hardware):
    return hardware.power


@pytest.fixture
def sim(hardware, vehicle_service):
    """The simulator world, for fault injection. Faults are cleared and health restored afterwards."""
    if hardware.sim is None:
        pytest.skip(NA + "requires the simulated bench")
    yield hardware.sim
    hardware.sim.clear()
    try:
        vehicle_service.ensure_ready()
    except Exception:
        vehicle_service.recover()


# ------------------------------------------------------------------------- reporting
@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    item.stash.setdefault(REPORTS, {})[report.when] = (report, call.excinfo)
    ev: TestEvidence | None = getattr(item, "_autoplat_evidence", None)
    if report.failed and report.when in ("setup", "call") and ev is not None:
        problems = ev.collect_on_failure()
        if problems:
            ev.notes["collector_problems"] = problems
        exc = call.excinfo.value if call.excinfo else None
        category = classify(exc, report.when, ev.notes.get("bench_healthy"))
        report.user_properties.append(("classification", category))
    if report.when == "teardown":
        _finalize(item)
    return report


def _finalize(item) -> None:
    state = _state(item.config)
    reports = item.stash.get(REPORTS, {})
    ev: TestEvidence | None = getattr(item, "_autoplat_evidence", None)
    setup, call, teardown = (reports.get(k, (None, None)) for k in ("setup", "call", "teardown"))
    outcome, phase, excinfo, reason = "passed", "call", None, ""
    rep_call = call[0]
    if setup[0] is not None and setup[0].failed:
        outcome, phase, excinfo = "error", "setup", setup[1]
    elif setup[0] is not None and setup[0].skipped:
        outcome, phase = "skipped", "setup"
        reason = setup[0].longrepr[2] if isinstance(setup[0].longrepr, tuple) else str(setup[0].longrepr)
    elif rep_call is not None and hasattr(rep_call, "wasxfail"):
        outcome, reason = ("xfailed" if rep_call.skipped else "xpassed"), rep_call.wasxfail
    elif rep_call is not None and rep_call.failed:
        outcome, excinfo = "failed", call[1]
    elif rep_call is not None and rep_call.skipped:
        outcome = "skipped"
        reason = rep_call.longrepr[2] if isinstance(rep_call.longrepr, tuple) else str(rep_call.longrepr)
    elif teardown[0] is not None and teardown[0].failed:
        outcome, phase, excinfo = "error", "teardown", teardown[1]
    reason = str(reason).removeprefix("Skipped: ")

    is_bench_test = ev is not None or item.get_closest_marker("requires") is not None
    if not is_bench_test or state.bench is None:
        return
    exc = excinfo.value if excinfo else None
    message = str(exc) if exc is not None else ""
    healthy = ev.notes.get("bench_healthy") if ev else None
    category = classify(exc, phase, healthy) if outcome in ("failed", "error") else None
    domain = next((d for d in DOMAINS if item.get_closest_marker(d)), "other")
    target = state.target_identity
    result = {
        "nodeid": item.nodeid, "outcome": outcome, "phase": phase, "classification": category,
        "reason": reason, "message": message[:500], "domain": domain,
        "gates": [m.args[0] for m in item.iter_markers("gate") if m.args],
        "requirements": [a for m in item.iter_markers("req") for a in m.args],
        "build": target.get("build_id", ""), "bench": state.bench.bench_id,
        "quarantined": outcome in ("xfailed", "xpassed") and str(reason).startswith("quarantined"),
        "duration_s": round(sum(r[0].duration for r in reports.values() if r[0] is not None), 3),
    }
    if ev is not None:
        result["retries"] = sum(e.layer == "retry" for e in ev.timeline.entries)
        result["unexpected_dtcs"] = ev.notes.get("unexpected_dtcs", [])
    if outcome in ("failed", "error") and ev is not None:
        log_lines = select_log_lines(ev.notes.get("log_lines", []), components=[domain])
        fail = Failure(item.nodeid, assertion_kind(message), log_lines, category or "unknown")
        sig = signature(fail)
        result["signature"] = sig
        matches = [{"issue": i.id, "title": i.title, "similarity": round(s, 2)} for i, s in state.known.match(sig)]
        obs = item.get_closest_marker("observation")
        packet = build_failure_packet(
            nodeid=item.nodeid, intent=(item.function.__doc__ or item.name).strip().splitlines()[0]
            if hasattr(item, "function") else item.name,
            outcome=outcome, phase=phase, assertion=message, classification_hint=category or "unknown",
            identity={k: target.get(k) for k in ("build_id", "variant", "components")} | {"bench": state.bench.bench_id},
            timeline_lines=ev.timeline.lines(), log_lines=log_lines,
            observation_level=obs.args[0] if obs and obs.args else "unspecified",
            requirements=result["requirements"],
            retries=[e.render() for e in ev.timeline.entries if e.layer == "retry"],
            dtc_delta=ev.notes.get("unexpected_dtcs", []), metrics=ev.metrics,
            artifacts=[a.path for a in ev.artifacts], known_issue_matches=matches,
            unknowns=ev.notes.get("collector_problems"), redactor=redactor)
        (ev.folder / "packet.yaml").write_text(packet_to_yaml(packet), encoding="utf-8")
        result["packet"] = str(ev.folder / "packet.yaml")
    if ev is not None:
        ev.write_result(result)
    state.store().add_result(result)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    state = _state(config)
    if state.run_store is None or not state.run_store.results:
        return
    store = state.run_store
    text = summarize(store.results, {"run_id": store.run_id, "target": state.target_identity,
                                     "bench": {"id": state.bench.bench_id if state.bench else None}})
    (store.path / "summary.txt").write_text(text + "\n", encoding="utf-8")
    terminalreporter.write_sep("=", "autoplat run summary")
    terminalreporter.write_line(text)
    terminalreporter.write_line(f"Evidence: {store.path}")
