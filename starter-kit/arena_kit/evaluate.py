"""Local evaluation through the production durable runner; no hosted endpoints are used.

Scripted opponents exercise the protocol. Persona opponents exercise a synthetic economic
model, not the undisclosed hosted supplier. Seeds and manifests make comparisons reproducible.
"""

import copy
import hashlib
import json
import os
import socket
import statistics
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from .buyer.store import Store
from .buyer.transport import DurableTable
from .client import ArenaClient
from .play import play

WAVE_CAP_S = 480


@contextmanager
def serve(cases_dir, facts=None, clock=True, supplier=None, startup_s=10):
    """A bounded, automatically stopped local arena server; holds the socket until startup."""
    import uvicorn

    from .sim import Sim, create_app, load_cases

    sim = Sim(load_cases(cases_dir), facts or {}, no_clock=not clock, supplier=supplier)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(sim), host="127.0.0.1", port=port, log_level="warning"))
    failures = []

    def start():
        try:
            server.run(sockets=[sock])
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=start, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + startup_s
        while not server.started:
            if failures or not thread.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError(f"local arena failed to start: {failures or 'startup deadline'}")
            time.sleep(0.01)
        yield f"http://127.0.0.1:{port}", sim
    finally:
        server.should_exit = True
        thread.join(timeout=3)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=1)
        sock.close()


class Timed:
    """Decision-only latency, deliberately distinct from transport dispatch latency."""

    def __init__(self, agent):
        self.agent, self.ms = agent, []

    def __getattr__(self, name):
        return getattr(self.agent, name)

    def __setattr__(self, name, value):
        if name in ("agent", "ms"):
            object.__setattr__(self, name, value)
        else:
            setattr(self.agent, name, value)

    def decide(self, case, threads):
        t0 = time.monotonic()
        try:
            return self.agent.decide(case, threads)
        finally:
            self.ms.append(1000 * (time.monotonic() - t0))


class Counting(DurableTable):
    """Production transport including close/recovery; HTTP hooks count failed POST attempts."""

    def __init__(self, client, case, store):
        super().__init__(client, case, store)
        self.invalid = self.late = 0
        self.http_errors = {}
        self.metrics_lock = threading.Lock()
        self.owner = threading.get_ident()
        self.client.http.event_hooks["response"].append(self.observe_response)

    def observe_response(self, response):
        if response.request.method != "POST" or response.status_code < 400:
            return
        opening = response.request.url.path.endswith("/sessions") and threading.get_ident() == self.owner
        if not opening and (not self.session_id or f"/sessions/{self.session_id}/" not in response.request.url.path):
            return
        response.read()
        with self.metrics_lock:
            status = str(response.status_code)
            self.http_errors[status] = self.http_errors.get(status, 0) + 1
            self.invalid += response.status_code == 422
            self.late += response.status_code == 409 and "late" in response.text.lower()

    def dispose(self):
        self.client.http.event_hooks["response"].remove(self.observe_response)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def manifest(cases_dir, facts, metadata=None):
    """Only allowlisted non-secret configuration; hashes describe the actual dirty source."""
    root = Path(__file__).resolve().parents[2]
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, stderr=subprocess.DEVNULL
        ).strip()
        dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True))
    except (OSError, subprocess.CalledProcessError):
        head, dirty = None, None
    source = {
        str(p.relative_to(Path(__file__).parent)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(Path(__file__).parent.rglob("*.py"))
    }
    cases = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(cases_dir).glob("*.json"))}
    config = {
        k: os.environ.get(k)
        for k in (
            "LLM_MODEL",
            "LLM_EFFORT",
            "LLM_FALLBACK_MODEL",
            "DECISIONS_MODEL",
            "DECISIONS_ENABLED",
            "LLM_TURN",
            "BUYER_CONCURRENT",
        )
    }
    return {
        "git_head": head,
        "dirty": dirty,
        "source_hash": _digest(source),
        "case_hashes": cases,
        "facts_hash": _digest(facts or {}),
        "config": config,
        **(metadata or {}),
    }


def evaluate(
    factories,
    cases_dir,
    repeat=1,
    clock=True,
    parallel=3,
    facts=None,
    cap_s=WAVE_CAP_S,
    supplier=None,
    seeds=None,
    metadata=None,
):
    """Existing agent/case/run mapping, with per-run immutable input and opponent metadata."""
    if repeat < 1 or parallel < 1:
        raise ValueError("repeat and parallel must be positive")
    seeds = list(seeds) if seeds is not None else [getattr(supplier, "seed", 0)]
    if not seeds:
        raise ValueError("at least one seed is required")
    if supplier is None and len(seeds) > 1:
        raise ValueError("scripted opponents are deterministic; use persona for multiple seeds")
    snapshot = manifest(cases_dir, facts, metadata)
    out = None
    for seed in seeds:
        opponent = copy.copy(supplier)
        if opponent is not None:
            opponent.seed = seed
        with (
            tempfile.TemporaryDirectory(prefix="arena-eval-") as folder,
            serve(cases_dir, facts, clock, opponent) as (url, sim),
        ):
            registration = ArenaClient(url)
            try:
                key = registration.register(f"eval-{time.time_ns()}", "sim")["api_key"]
            finally:
                registration.http.close()
            client = ArenaClient(url, key)
            store = Store(folder)
            case_ids = sorted(sim.cases)
            if out is None:
                out = {name: {cid: [] for cid in case_ids} for name in factories}
            jobs = [(name, cid, repetition) for name in factories for cid in case_ids for repetition in range(repeat)]

            def one(job, client=client, store=store, opponent=opponent, seed=seed, sim=sim):
                name, cid, repetition = job
                table, agent = Counting(client, sim.cases[cid], store), Timed(factories[name]())
                deadline = time.time() + cap_s if clock else None
                run = {
                    "error": None,
                    "end_reason": None,
                    "metadata": {
                        **snapshot,
                        "agent": name,
                        "opponent": type(opponent).__name__ if opponent else "script",
                        "seed": seed,
                        "repeat": repetition,
                        "clock": clock,
                        "parallel": parallel,
                        "cap_s": cap_s,
                    },
                }
                try:
                    result = play(table, cid, agent, deadline)
                    run.update(
                        score=result.get("score"),
                        deal=bool(result.get("deal")),
                        c_deal=result.get("c_deal"),
                        scoring_valid=result.get("scoring_valid", True),
                        end_reason=client.session(table.session_id).get("end_reason"),
                    )
                except Exception as error:
                    run.update(score=0.0, deal=False, scoring_valid=False, error=f"{type(error).__name__}: {error}")
                finally:
                    table.dispose()
                ms = sorted(agent.ms) or [0.0]
                events = []
                for path in Path(folder).glob(f"*/*/{table.session_id}.events.jsonl"):
                    events += [json.loads(line) for line in path.read_text().splitlines() if line]
                dispatch = [e["decision_to_dispatch_ms"] for e in events if "decision_to_dispatch_ms" in e]
                run.update(
                    completed=run["error"] is None and run["end_reason"] is not None,
                    timeout=table.late > 0 or run["end_reason"] in ("timeout", "wave_cap"),
                    invalid=table.invalid,
                    late=table.late,
                    http_errors=dict(table.http_errors),
                    moves=len(agent.ms),
                    mean_ms=statistics.mean(ms),
                    p95_ms=ms[min(len(ms) - 1, int(0.95 * len(ms)))],
                    dispatch_p95_ms=sorted(dispatch)[int(0.95 * (len(dispatch) - 1))] if dispatch else None,
                    dispatch_max_ms=max(dispatch) if dispatch else None,
                )
                return name, cid, run

            try:
                with ThreadPoolExecutor(max_workers=parallel) as pool:
                    for name, cid, run in pool.map(one, jobs):
                        out[name][cid].append(run)
            finally:
                client.http.close()
    return out


def _rate(runs, key):
    return sum(bool(r[key]) for r in runs) / len(runs)


def report(out):
    lines = [
        f"{'agent':8} {'case':10} {'score':>6} {'min':>6} {'deals':>5} {'done':>5} {'tmo':>5} {'422':>4} "
        f"{'late':>4} {'mean ms':>8} {'p95 ms':>8} {'unscored':>8}  error"
    ]
    for name, cases in out.items():
        means = []
        every = []
        for cid, runs in cases.items():
            scores = [
                r["score"] for r in runs if r.get("scoring_valid", True) and isinstance(r.get("score"), int | float)
            ]
            mean = statistics.mean(scores) if scores else None
            if mean is not None:
                means.append(mean)
            every += runs
            text_mean = f"{mean:6.3f}" if mean is not None else "   n/a"
            text_min = f"{min(scores):6.3f}" if scores else "   n/a"
            error = next((r["error"] for r in runs if r["error"]), "")
            lines.append(
                f"{name:8} {cid:10} {text_mean} {text_min} {sum(r['deal'] for r in runs):5d} "
                f"{_rate(runs, 'completed'):5.0%} {_rate(runs, 'timeout'):5.0%} "
                f"{sum(r['invalid'] for r in runs):4d} {sum(r['late'] for r in runs):4d} "
                f"{statistics.mean(r['mean_ms'] for r in runs):8.0f} {max(r['p95_ms'] for r in runs):8.0f} "
                f"{len(runs) - len(scores):8d}  {error[:60]}"
            )
        mean = f"{statistics.mean(means):.3f}" if means else "n/a"
        lines.append(
            f"{name:8} {'MEAN':10} {mean:>6}  done {_rate(every, 'completed'):.0%}; "
            f"unscored {sum(not r.get('scoring_valid', True) for r in every)}"
        )
    return "\n".join(lines)


def cases_folder(path):
    p = Path(path)
    if not any(p.glob("*.json")):
        raise SystemExit(f"no case files in {p}")
    return p
