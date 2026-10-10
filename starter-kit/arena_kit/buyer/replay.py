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
                    events.append({"file": str(f.resolve()), **json.loads(line)})
                except (json.JSONDecodeError, TypeError):
                    continue  # a line cut by a crash
        elif f.suffix == ".json":
            try:
                sessions.append({"file": str(f.resolve()), **json.loads(f.read_text())})
            except json.JSONDecodeError:
                continue
    return events, sessions


def summarize(events, sessions):
    """What the logs show, in numbers."""
    unique = {}
    for item in [*sessions, *events]:
        result = item.get("result")
        if isinstance(result, dict):
            key = item.get("session_id") or str(Path(item.get("file", "unknown")).with_suffix("")).removesuffix(
                ".events"
            )
            # Local simulator session IDs can repeat in separate case folders.
            key = (str(Path(item.get("file", "")).parent), key)
            unique[key] = result
    results = list(unique.values())
    scores = [r["score"] for r in results if isinstance(r.get("score"), int | float)]
    status = Counter(
        e.get("http_status") or e.get("status")
        for e in events
        if isinstance(e.get("http_status") or e.get("status"), int) and (e.get("http_status") or e.get("status")) >= 400
    )
    llm_ms = sorted(e["llm_ms"] for e in events if isinstance(e.get("llm_ms"), int | float))
    return {
        "sessions": len(
            {
                str(Path(item.get("file", "unknown")).with_suffix("")).removesuffix(".events")
                for item in [*sessions, *events]
            }
        ),
        "results": len(results),
        "deals": sum(bool(r.get("deal")) for r in results),
        "mean_score": round(statistics.mean(scores), 3) if scores else None,
        "events": dict(Counter(e.get("kind") or e.get("event") or e.get("type") or "-" for e in events)),
        "writer": dict(Counter(e["writer"] for e in events if e.get("writer"))),
        "http_errors": {str(k): v for k, v in status.items()},
        "late": sum(
            str(e.get("error_kind") or "").upper() == "LATE"
            or "late" in str(e.get("error") or e.get("detail") or "").lower()
            for e in events
        ),
        "no_deal": sum(not bool(r.get("deal")) for r in results),
        "failed_must_haves": sum(r.get("deal") and r.get("must_haves_ok") is False for r in results),
        "llm_ms_p50": llm_ms[len(llm_ms) // 2] if llm_ms else None,
        "llm_ms_p95": llm_ms[int(0.95 * (len(llm_ms) - 1))] if llm_ms else None,
    }


def deterministic(s):
    """FailureAnalysis from the counts alone: the failures code can name without a model."""
    failures = []
    if s.get("failed_must_haves"):
        failures.append(_f("economic", f"{s['failed_must_haves']} deal(s) broke must-haves", "failed_must_haves"))
    if n := s["http_errors"].get("422"):
        failures.append(_f("execution", f"{n} invalid move(s) refused (422)", "http_errors"))
    if s["late"]:
        failures.append(_f("timing", f"{s['late']} move(s) arrived late (409)", "late"))
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
        found = parse_json(
            model.complete(
                system, user, budget_s, role="critic", validate=lambda text: REPLAY_ANALYST.check(parse_json(text))
            )
        )
        if found is not None and not REPLAY_ANALYST.check(found):
            return {**found, "summary": s, "source": "model"}
    return {**out, "summary": s, "source": "code"}
