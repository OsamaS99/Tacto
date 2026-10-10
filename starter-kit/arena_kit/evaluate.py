"""`arena eval`: play agents against an in-process arena-sim on the same cases and compare - score,
completion, timeouts, invalid actions (422), late moves (409 late), decision latency. The
simulator's supplier is a plain script that reads only the price: this tests validity, timing and
packaging, not persuasion.

    uv run arena eval --agents naive,buyer --cases eval_cases --repeat 1 [--clock] [--parallel 3]

With --clock the 60-second rule is on and each case gets a wave's cap (`cap_s`) as its deadline,
as in the Live Final; without it nothing is timed, so no deadline is passed either.
"""

import socket
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .client import ArenaClient, ArenaError
from .play import RemoteTable, play

WAVE_CAP_S = 480  # a Live Final wave: 8 minutes


def serve(cases_dir, facts=None, clock=True):
    """arena-sim on a free local port in this process: (base_url, Sim)."""
    import uvicorn

    from .sim import Sim, create_app, load_cases

    sim = Sim(load_cases(cases_dir), facts or {}, no_clock=not clock)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(sim), port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.01)
    return f"http://127.0.0.1:{port}", sim


class Timed:
    """Wraps an agent: decision latency per move."""

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


class Counting(RemoteTable):
    """A table that counts invalid (422) and late (409) moves."""

    def __init__(self, client):
        super().__init__(client)
        self.invalid = self.late = 0

    def send(self, supplier_id, message):
        try:
            return super().send(supplier_id, message)
        except ArenaError as e:
            self.invalid += e.status == 422
            self.late += e.status == 409 and "late" in str(e.detail)
            raise


def evaluate(factories, cases_dir, repeat=1, clock=True, parallel=3, facts=None, cap_s=WAVE_CAP_S):
    """{agent: {case: [run, ...]}}, run = {score, deal, completed, timeout, invalid, late, moves,
    mean_ms, p95_ms, error}."""
    url, sim = serve(cases_dir, facts, clock)
    client = ArenaClient(url, ArenaClient(url).register(f"eval-{time.time_ns()}", "sim")["api_key"])
    case_ids = sorted(sim.cases)
    jobs = [(name, cid) for name in factories for cid in case_ids for _ in range(repeat)]

    def one(job):
        name, cid = job
        table, agent = Counting(client), Timed(factories[name]())
        deadline = time.time() + cap_s if clock else None  # play() hands it to the agent: paced on a real clock only
        run = {"error": None, "end_reason": None}
        try:
            r = play(table, cid, agent, deadline)
            run.update(score=r.get("score") or 0.0, deal=bool(r.get("deal")), c_deal=r.get("c_deal"))
            run["end_reason"] = client.session(table.session_id).get("end_reason")
        except Exception as e:  # report, do not stop the others
            run.update(score=0.0, deal=False, error=f"{type(e).__name__}: {e}")
        ms = sorted(agent.ms) or [0.0]
        run.update(
            completed=run["error"] is None and run["end_reason"] is not None,
            timeout=table.late > 0 or run["end_reason"] in ("timeout", "wave_cap"),
            invalid=table.invalid,
            late=table.late,
            moves=len(agent.ms),
            mean_ms=statistics.mean(ms),
            p95_ms=ms[min(len(ms) - 1, int(0.95 * len(ms)))],
        )
        return name, cid, run

    out = {name: {cid: [] for cid in case_ids} for name in factories}
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        for name, cid, run in pool.map(one, jobs):
            out[name][cid].append(run)
    return out


def _rate(runs, key):
    return sum(bool(r[key]) for r in runs) / len(runs)


def report(out):
    """A plain-text table: one row per agent and case, then each agent's means."""
    head = (
        f"{'agent':8} {'case':10} {'score':>6} {'min':>6} {'deals':>5} {'done':>5} {'tmo':>5} {'422':>4} "
        f"{'late':>4} {'mean ms':>8} {'p95 ms':>8}  error"
    )
    lines = [head]
    for name, cases in out.items():
        for cid, runs in cases.items():
            scores = [r["score"] for r in runs]
            err = next((r["error"] for r in runs if r["error"]), "")
            lines.append(
                f"{name:8} {cid:10} {statistics.mean(scores):6.3f} {min(scores):6.3f} "
                f"{sum(r['deal'] for r in runs):5d} {_rate(runs, 'completed'):5.0%} {_rate(runs, 'timeout'):5.0%} "
                f"{sum(r['invalid'] for r in runs):4d} {sum(r['late'] for r in runs):4d} "
                f"{statistics.mean(r['mean_ms'] for r in runs):8.0f} {max(r['p95_ms'] for r in runs):8.0f}  {err[:60]}"
            )
        every = [r for runs in cases.values() for r in runs]
        means = [statistics.mean(r["score"] for r in runs) for runs in cases.values()]
        lines.append(
            f"{name:8} {'MEAN':10} {statistics.mean(means):6.3f} {'':6} {'':5} {_rate(every, 'completed'):5.0%} "
            f"{_rate(every, 'timeout'):5.0%} {'':4} {'':4} {statistics.mean(r['mean_ms'] for r in every):8.0f}"
        )
    return "\n".join(lines)


def cases_folder(path):
    p = Path(path)
    if not any(p.glob("*.json")):
        raise SystemExit(f"no case files in {p}")
    return p
