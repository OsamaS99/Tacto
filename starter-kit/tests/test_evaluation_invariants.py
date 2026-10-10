"""Generic local scoring/evidence/reporting invariants, without network or model calls."""

from copy import deepcopy

import pytest

from arena_kit.buyer.replay import deterministic, summarize
from arena_kit.cost import must_haves_ok, total_cost
from arena_kit.persona import PersonaSupplier
from arena_kit.sim import Sim

CASE = {
    "case_id": "case:opaque",
    "set": "practice",
    "title": "Part",
    "type": "renewal",
    "parts": ["part:x"],
    "suppliers": ["sup:x"],
    "negotiable": ["payment_days", "unit_price"],
    "fixed_terms": {"lead_time_days": 20, "volume_per_year": 1000, "contract_months": 12, "incoterm": "FCA"},
    "term_options": {"payment_days": [30, 60, 90]},
    "round_limit": 10,
    "buyer": {
        "need_per_year": 1000,
        "horizon_years": 1,
        "current_unit_price": 100,
        "fallback_unit_price": 120,
        "interest_rate": 0.08,
        "reference_payment_days": 60,
        "need_lead_time_days": 20,
        "freight_eur_per_unit": {"FCA": 0},
        "category_goal": "leverage",
        "must_haves": [],
        "alternative": {"kind": "none", "terms": {}},
    },
    "facts": {"sc:opaque": {"supplier": "sup:x", "part": "part:x", "otd_pct": 84.6, "ppm": 500}},
}


def opened(case, persona=False):
    sim = Sim({case["case_id"]: case}, no_clock=True, supplier=PersonaSupplier(seed=0) if persona else None)
    sid = sim.open("team", case["case_id"])["session_id"]
    return sim, sid, sim.sessions[sid]


def test_best_cost_includes_alternative_and_requires_feasible_opening():
    case = deepcopy(CASE)
    case["buyer"]["alternative"] = {"kind": "quote", "terms": {"unit_price": 20, "payment_days": 60}}
    sim, sid, state = opened(case)
    sim.message("team", sid, "sup:x", {"action": {"type": "walk_away"}})
    assert state["result"]["c_best"] == total_cost(case, case["buyer"]["alternative"]["terms"])
    assert state["result"]["score"] == 1
    case["buyer"]["must_haves"] = [{"field": "payment_days", "op": "==", "value": 90}]
    sim, sid, state = opened(case)
    sim.message("team", sid, "sup:x", {"action": {"type": "walk_away"}})
    assert state["result"]["c_open"] is None and state["result"]["score"] is None
    assert not state["result"]["scoring_valid"]


@pytest.mark.parametrize("persona", [False, True])
def test_best_supplier_package_obeys_must_haves(persona):
    case = deepcopy(CASE)
    case["buyer"]["must_haves"] = [{"field": "payment_days", "op": "<=", "value": 30}]
    sim, sid, state = opened(case, persona)
    th = state["threads"]["sup:x"]
    sim.message("team", sid, "sup:x", {"action": {"type": "walk_away"}})
    p = th.get("persona")
    expected = []
    for days in case["term_options"]["payment_days"]:
        price = p.price_for(case, {"payment_days": days}, p.floor) if p else round(th["opening"] * 0.9, 2)
        terms = {"unit_price": price, "payment_days": days}
        if must_haves_ok(case, terms, th["standard"]):
            expected.append(total_cost(case, terms, th["standard"], p.economics(case, terms) if p else None))
    assert state["result"]["c_best"] == min(expected)


def test_semantic_evidence_rejects_wrong_numbers_and_groups_index_pairs():
    case = deepcopy(CASE)
    case["facts"].update(
        {
            "idx:S@2025-09": {"month": "2025-09", "series": "S", "value": 100},
            "idx:S@2026-09": {"month": "2026-09", "series": "S", "value": 90},
        }
    )
    sim, sid, _ = opened(case)
    msg = {"body": "OTD is 99.0% (sc:opaque). Prices changed -10.0% (idx:S@2025-09, idx:S@2026-09)."}
    reply = sim.message("team", sid, "sup:x", msg)
    assert [r["result"] for r in reply["claim_results"]] == ["contradicted", "verified"]
    reply = sim.message("team", sid, "sup:x", {"body": "Prices changed -10.0% (idx:S@2025-09, idx:S@2026-09)."})
    assert reply["claim_results"] == [{"index": 0, "result": "duplicate"}]


def test_replay_does_not_diagnose_good_walkaways_or_intentional_templates_as_failures():
    result = {"score": 1.0, "deal": False}
    events = [
        {
            "file": "/run/caseA/sim1.events.jsonl",
            "session_id": "sim1",
            "result": result,
            "writer": "template",
            "error_kind": "LATE",
            "http_status": 409,
        },
        {"file": "/run/caseB/sim1.events.jsonl", "session_id": "sim1", "result": result},
    ]
    sessions = [{"file": "/run/caseA/sim1.json", "session_id": "sim1", "result": result}]
    summary = summarize(events, sessions)
    assert summary["sessions"] == summary["results"] == 2
    assert summary["late"] == 1
    assert [f["category"] for f in deterministic(summary)["failures"]] == ["timing"]
