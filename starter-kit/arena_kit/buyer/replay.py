"""Reading a run afterwards (Replay Analyst). Code first counts what the event logs and session
files show - results, invalid moves, late moves, writer sources, model latency - and turns the
plain failures into findings; the model, if given, explains the rest from that summary and an
excerpt, in the same FailureAnalysis shape. Offline only: never during a case."""

import json
import statistics
from collections import Counter
from pathlib import Path

from .model import parse_json
from .prompts import REPLAY_ANALYST

EXCERPT_CHARS = 20_000


def load(paths):
    """(events, sessions) from `*.events.jsonl` and session `*.json` files or folders of them."""
    files = []
    for p in map(Path, paths):
        files += sorted(p.rglob("*.json*")) if p.is_dir() else [p]
    events, sessions = [], []
    for f in files:
        if f.name.endswith(".events.jsonl"):
            for line in f.read_text().splitlines():
                try:
                    events.append({"file": f.stem.removesuffix(".events"), **json.loads(line)})
                except (json.JSONDecodeError, TypeError):
                    continue  # a line cut by a crash
        elif f.suffix == ".json":
            try:
                sessions.append(json.loads(f.read_text()))
            except json.JSONDecodeError:
                continue
    return events, sessions


def summarize(events, sessions):
    """What the logs show, in numbers."""
    results = [r for e in events if isinstance(r := e.get("result"), dict)]
    results += [s["result"] for s in sessions if isinstance(s.get("result"), dict)]
    scores = [r["score"] for r in results if isinstance(r.get("score"), int | float)]
    status = Counter(e["status"] for e in events if isinstance(e.get("status"), int) and e["status"] >= 400)
    llm_ms = sorted(e["llm_ms"] for e in events if isinstance(e.get("llm_ms"), int | float))
    return {
        "sessions": len({e["file"] for e in events} | {s.get("session_id") for s in sessions} - {None}),
        "results": len(results),
        "deals": sum(bool(r.get("deal")) for r in results),
        "mean_score": round(statistics.mean(scores), 3) if scores else None,
        "events": dict(Counter(e.get("kind") or e.get("event") or e.get("type") or "-" for e in events)),
        "writer": dict(Counter(e["writer"] for e in events if e.get("writer"))),
        "http_errors": {str(k): v for k, v in status.items()},
        "late": sum("late" in str(e.get("error") or e.get("detail") or "") for e in events),
        "llm_ms_p50": llm_ms[len(llm_ms) // 2] if llm_ms else None,
        "llm_ms_p95": llm_ms[int(0.95 * (len(llm_ms) - 1))] if llm_ms else None,
    }


def deterministic(s):
    """FailureAnalysis from the counts alone: the failures code can name without a model."""
    failures = []
    if s["results"] > s["deals"]:
        failures.append(_f("economic", f"{s['results'] - s['deals']} case(s) ended without a deal", "results"))
    if n := s["http_errors"].get("422"):
        failures.append(_f("execution", f"{n} invalid move(s) refused (422)", "http_errors"))
    if s["late"]:
        failures.append(_f("timing", f"{s['late']} move(s) arrived late (409)", "late"))
    if s["writer"].get("template") and s["writer"].get("template", 0) > sum(s["writer"].values()) / 2:
        failures.append(_f("execution", "the template wrote most messages: the model was slow or invalid", "writer"))
    outcome = f"{s['deals']} deal(s) in {s['results']} result(s), mean score {s['mean_score']}"
    return {"outcome": outcome, "failures": failures, "policy_changes": [], "regression_cases": []}


def _f(category, cause, evidence):
    return {"category": category, "cause": cause, "evidence": f"summary.{evidence}", "hypothesis": False}


def analyze(paths, model=None, budget_s=REPLAY_ANALYST.budget_s):
    """FailureAnalysis {outcome, failures, policy_changes, regression_cases, summary, source}."""
    events, sessions = load(paths)
    s = summarize(events, sessions)
    out = deterministic(s)
    if model is not None and events:
        excerpt = "\n".join(json.dumps(e, ensure_ascii=False, default=str)[:400] for e in events)[-EXCERPT_CHARS:]
        system, user = REPLAY_ANALYST.render(
            case_id="run",
            case_type="replay",
            goal="-",
            rev=len(events),
            budget_s=budget_s,
            summary=json.dumps(s),
            events=excerpt,
        )
        found = parse_json(model.complete(system, user, budget_s))
        if found is not None and not REPLAY_ANALYST.check(found):
            return {**found, "summary": s, "source": "model"}
    return {**out, "summary": s, "source": "code"}
