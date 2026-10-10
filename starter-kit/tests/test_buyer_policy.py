"""The buyer's deterministic policy on synthetic states: packages, economics, bids, acceptance."""

import json
import math
from dataclasses import replace
from pathlib import Path

import pytest

from arena_kit.buyer import bids
from arena_kit.buyer.acceptance import TAU_CLOSE, TAU_LATE, decide, epsilon, feasible, floor_c
from arena_kit.buyer.assess import assess
from arena_kit.buyer.economics import EconomicEvaluator, estimate_supplier
from arena_kit.buyer.playbook import playbook
from arena_kit.buyer.records import (
    CandidatePackage,
    CaseState,
    EvidenceDossier,
    SupplierEstimate,
    SupplierObservation,
    SupplierThreadState,
)
from arena_kit.buyer.space import ContractSpace
from arena_kit.cost import must_haves_ok, total_cost

S03 = json.loads((Path(__file__).parents[1] / "arena_kit/sim_cases/S03.json").read_text())
S03_STD = {"payment_days": 14, "contract_months": 12, "incoterm": "EXW"}  # the simulator's standard terms
OPENING = 239.55

CASE = {
    "case_id": "case:T", "type": "renewal", "title": "T", "parts": ["part:T"], "suppliers": ["sup:A"],
    "negotiable": ["unit_price", "payment_days", "contract_months", "incoterm"],
    "fixed_terms": {"lead_time_days": 35, "volume_per_year": 1000, "index_clause": None, "tooling_eur": 0},
    "term_options": {"payment_days": [30, 45, 60, 90], "contract_months": [12, 24], "incoterm": ["EXW", "FCA", "DAP"]},
    "buyer": {"need_per_year": 1000, "horizon_years": 2, "current_unit_price": 100.0, "fallback_unit_price": 120.0,
              "interest_rate": 0.08, "reference_payment_days": 60, "need_lead_time_days": 35,
              "category_goal": "leverage", "fallback_risk_pct": 5, "late_cost_per_day": 200, "cost_per_defect": 50,
              "freight_eur_per_unit": {"EXW": 3.0, "FCA": 2.0, "DAP": 0.0}, "index_paths": {}, "must_haves": [],
              "alternative": {"kind": "none", "ref": None, "terms": {}}},
    "round_limit": 10,
}  # fmt: skip
STD = {"payment_days": 30, "contract_months": 12, "incoterm": "EXW"}


def with_buyer(case=CASE, **buyer):
    return {**case, "buyer": {**case["buyer"], **buyer}}


def thread(ev, sid="sup:A", prices=(110.0,), mine=(), status="open", moves_left=None, standing=None, n=None):
    """A thread whose supplier quoted `prices` (on its standard terms) and we sent sheets costing `mine`."""
    std = ev.std.get(sid, {})
    their = [ev.c(sid, {"unit_price": p}) for p in prices]
    limit = ev.case.get("round_limit", 10)
    return SupplierThreadState(
        sid=sid,
        offer_id=f"offer:{sid[4:]}-{n if n is not None else len(mine)}",
        status=status,
        standing=standing or {"unit_price": prices[-1]},
        std=std,
        missing=[],
        rounds_used=len(mine),
        moves_left=limit - len(mine) if moves_left is None else moves_left,
        their_c=their,
        my_c=list(mine),
    )


def state(case, threads, ev, tau=0.0, pb=None, opening=110.0, market=None, market_c=None):
    """The case state with each supplier's targets; `opening`: its first price (or {sid: price})."""
    space = ContractSpace(case)
    cs = CaseState(case, pb or playbook(case), None, {t.sid: t for t in threads}, c_alt=ev.c_alt(), tau=tau)
    for t in threads:
        first = opening[t.sid] if isinstance(opening, dict) else opening
        cs.targets[t.sid] = bids.targets(case, None, ev, space, t.sid, first, cs.c_alt, market, market_c=market_c)
    return cs, space


def evaluator(case=CASE, std=STD, sids=("sup:A",), estimates=None):
    return EconomicEvaluator(case, {s: dict(std) for s in sids}, estimates)


# -- packages and economics ----------------------------------------------------------------------


def test_s03_opening_stays_in_the_floor_led_band_on_90_24_dap():
    ev = EconomicEvaluator(S03, {"sup:70520": S03_STD})
    t = thread(ev, "sup:70520", prices=(OPENING,))
    cs, space = state(S03, [t], ev, opening=OPENING)
    p = decide(cs, ev, space, ["sup:70520"])
    assert p.kind == "send"  # the opening is never taken in round 1
    best = max(p.packages, key=lambda e: e.terms["unit_price"])
    assert {k: best.terms[k] for k in S03_STD} == {"payment_days": 90, "contract_months": 24, "incoterm": "DAP"}
    assert best.terms["unit_price"] <= round(0.9 * OPENING, 2)
    c_floor = ev.c("sup:70520", {**best.terms, "unit_price": round(0.9 * OPENING, 2)})
    assert all(e.c <= c_floor + 0.01 for e in p.packages)


def test_complete_fills_the_cheapest_values_and_snaps():
    ev, space = evaluator(), ContractSpace(CASE)
    full = space.complete({"unit_price": 99.999, "payment_days": 50}, STD, lambda t: ev.c("sup:A", t))
    assert full == {"unit_price": 100.0, "payment_days": 45, "contract_months": 24, "incoterm": "DAP"}
    assert space.valid(full, STD) == []
    brute = min(space.combos(STD), key=lambda c: ev.c("sup:A", {**c, "unit_price": 100.0}))
    assert bids.best_combo(ev, space, "sup:A", 100.0) == {**brute, "unit_price": 100.0}


def test_price_for_inverts_total_cost_exactly():
    ev = evaluator()
    terms = {"unit_price": 100.0, "payment_days": 90, "contract_months": 24, "incoterm": "DAP"}
    for target in (150_000.0, 201_234.56, 230_000.0):
        p = ev.price_for("sup:A", terms, target)
        assert ev.c("sup:A", {**terms, "unit_price": p}) <= target + 0.01
        assert ev.c("sup:A", {**terms, "unit_price": round(p + 0.01, 2)}) > target + 0.01


def test_price_for_never_goes_below_best_price_for_a_strategic_supplier():
    case = with_buyer(category_goal="stabilise", sustainable_margin_pct=8, relationship_eur_per_point=5000)
    ev = evaluator(case, estimates={"sup:A": SupplierEstimate((90.0, 95.0, 100.0))})
    terms = {"unit_price": 100.0, "payment_days": 60, "contract_months": 24, "incoterm": "DAP"}
    floor = ev.best_price("sup:A", terms)
    assert floor > 95.0  # squeezing below a sustainable margin costs more than it saves
    c_floor = ev.c("sup:A", {**terms, "unit_price": floor})
    assert ev.price_for("sup:A", terms, c_floor - 100) is None  # cheaper than the floor's C: not by squeezing
    p = ev.price_for("sup:A", terms, c_floor + 5_000)
    assert p >= floor and ev.c("sup:A", {**terms, "unit_price": p}) <= c_floor + 5_000.01
    assert evaluator().best_price("sup:A", terms) == 0.0  # no relationship term, no floor


def test_c_trade_offs():
    ev = evaluator()
    base = {"unit_price": 100.0, "payment_days": 60, "contract_months": 24, "incoterm": "FCA"}
    assert ev.c("sup:A", base) < ev.c("sup:A", {**base, "contract_months": 12})  # cover the horizon
    assert ev.c("sup:A", {**base, "incoterm": "DAP"}) < ev.c("sup:A", {**base, "incoterm": "EXW"})
    assert ev.c("sup:A", {**base, "payment_days": 90}) < ev.c("sup:A", {**base, "payment_days": 30})


def test_later_effective_date_and_index_clause_by_their_cost():
    clause = {"series": "S", "material_share": 0.5, "deadband_pct": 0.0, "base_month": "2026-08"}
    case = {
        **CASE,
        "negotiable": ["unit_price", "effective_date", "index_clause"],
        "fixed_terms": {**CASE["fixed_terms"], "contract_months": 24, "payment_days": 60, "incoterm": "DAP"},
        "term_options": {"effective_date": ["2026-11-01", "2027-04-01"], "index_clause": [None, clause]},
    }
    del case["fixed_terms"]["index_clause"]
    space = ContractSpace(case)
    for path, want in ((-0.06, clause), (0.06, None)):
        ev = EconomicEvaluator(with_buyer(case, index_paths={"S": path}))
        assert bids.best_combo(ev, space, "sup:A", 110.0)["index_clause"] == want
    ev = EconomicEvaluator(case)
    assert bids.best_combo(ev, space, "sup:A", 110.0)["effective_date"] == "2027-04-01"  # dearer than today
    assert bids.best_combo(ev, space, "sup:A", 90.0)["effective_date"] == "2026-11-01"  # cheaper: at once


# -- bids ------------------------------------------------------------------------------------------


def test_reciprocity_no_concession_without_movement_and_monotone_bids():
    ev = evaluator()
    pb = playbook(CASE)
    c_target, c_res = 190_000.0, 230_000.0
    t = thread(ev, prices=(110.0, 108.0), mine=(190_000.0,))
    theirs = t.their_c[0] - t.their_c[1]
    step = bids.next_bid(pb, t, assess(t), c_target, c_res, 240_000.0, tau=0.0) - 190_000.0
    assert 0 < step <= pb.k * theirs + 0.01
    held = thread(ev, prices=(110.0, 110.0), mine=(190_000.0,))
    assert bids.next_bid(pb, held, assess(held), c_target, c_res, 240_000.0, tau=0.0) == 190_000.0
    late = bids.next_bid(pb, held, assess(held), c_target, c_res, 240_000.0, tau=0.9)
    assert late > 190_000.0  # only the clock moves us without a move from them
    last, prices = 190_000.0, [110.0]
    for price in (109.0, 108.5, 108.5, 107.0):  # bids never go backwards while they hold or move
        prices.append(price)
        t = thread(ev, prices=tuple(prices), mine=(last,) * (len(prices) - 1))
        nxt = bids.next_bid(pb, t, assess(t), c_target, c_res, ev.c("sup:A", {"unit_price": price}), tau=0.2)
        assert nxt >= last
        last = nxt


def test_a_regression_meets_a_hold_and_is_not_accepted_early():
    ev = evaluator()
    t = thread(ev, prices=(110.0, 105.0, 107.0), mine=(195_000.0, 199_000.0))
    a = assess(t)
    assert a.regressed
    assert bids.next_bid(playbook(CASE), t, a, 190_000.0, 230_000.0, 240_000.0, tau=0.5) == 199_000.0
    full = {"unit_price": 104.0, "payment_days": 90, "contract_months": 24, "incoterm": "DAP"}
    mine = (204_000.0, 205_000.0, 206_500.0)  # its sheet costs 206,613: as good as our next bid
    for prices, kind in (((110.0, 105.0, 104.0), "accept"), ((110.0, 103.0, 104.0), "send")):
        t = thread(ev, prices=prices, mine=mine, standing=full)
        cs, space = state(CASE, [t], ev)
        assert decide(cs, ev, space, ["sup:A"]).kind == kind


def test_generated_packages_cost_the_target_and_keep_the_must_haves():
    case = with_buyer(must_haves=[{"field": "payment_days", "op": ">=", "value": 45}])
    ev, space = evaluator(case), ContractSpace(case)
    out = bids.generate(ev, space, "sup:A", 200_000.0, 110.0, {"unit_price": 110.0})
    assert out
    for e in out:
        assert must_haves_ok(case, e.terms, STD) and space.valid(e.terms, STD) == []
        assert 200_000.0 - 2_100 < e.c <= 200_000.01  # within a cent of price (2 x 1000 units)


# -- acceptance ------------------------------------------------------------------------------------


def test_agreed_is_accepted_by_its_current_id():
    ev = evaluator()
    t = thread(ev, prices=(110.0, 104.0), mine=(190_000.0,), status="agreed", n=7)
    cs, space = state(CASE, [t], ev)
    p = decide(cs, ev, space, ["sup:A"])
    assert (p.kind, p.sid, p.term_sheet_id) == ("accept", "sup:A", "offer:A-7")


def test_a_partial_final_offer_is_mirrored_only_when_it_pays():
    case = with_buyer(alternative={"kind": "quote", "ref": "q", "terms": {"unit_price": 125.0, "incoterm": "DAP"}})
    ev = evaluator(case)
    t = thread(ev, prices=(110.0, 104.0), mine=(190_000.0,), status="final_offer")
    cs, space = state(case, [t], ev)
    p = decide(cs, ev, space, ["sup:A"])
    assert p.kind == "send" and p.packages[0].terms["unit_price"] == 104.0  # its price, our terms
    assert p.packages[0].c < ev.c("sup:A", t.standing)
    off = replace(cs.playbook, mirror_final=False)
    assert decide(replace(cs, playbook=off), ev, space, ["sup:A"]).kind == "accept"
    tight = with_buyer(alternative={"kind": "quote", "ref": "q", "terms": {"unit_price": 104.5, "incoterm": "EXW"}})
    ev2 = evaluator(tight)
    t2 = thread(ev2, prices=(110.0, 104.0), mine=(190_000.0,), status="final_offer")
    cs2, _ = state(tight, [t2], ev2)
    cs2.c_alt = ev2.c("sup:A", t2.standing) + 120_000  # walking costs far more than the mirror saves
    assert decide(cs2, ev2, space, ["sup:A"]).kind == "accept"
    again = thread(ev, prices=(110.0, 104.0), mine=(190_000.0, p.packages[0].c), status="final_offer")
    cs3, _ = state(case, [again], ev)
    assert decide(cs3, ev, space, ["sup:A"]).kind == "accept"  # mirrored once: no second push


def test_without_an_alternative_a_walk_scores_as_the_opening():
    ev = evaluator()
    t = thread(ev, prices=(110.0, 104.0), mine=(190_000.0,), status="final_offer")
    cs, space = state(CASE, [t], ev)
    assert math.isinf(cs.c_alt) and all(math.isfinite(c) for c in cs.targets["sup:A"])
    assert decide(cs, ev, space, ["sup:A"]).kind == "send"  # the gain is large, the opening close
    cs.playbook = replace(cs.playbook, p_walk=0.9)
    assert decide(cs, ev, space, ["sup:A"]).kind == "accept"


@pytest.mark.parametrize("price", [math.inf, -math.inf, math.nan, True, False])
def test_nonfinite_and_boolean_prices_are_infeasible(price):
    ev, space = evaluator(), ContractSpace(CASE)
    terms = {"unit_price": price, "payment_days": 60, "contract_months": 24, "incoterm": "DAP"}
    assert space.valid(terms, STD)
    assert math.isinf(ev.c("sup:A", terms))
    assert ev.price_for("sup:A", terms, math.inf) is None


def test_reservation_never_rises_to_meet_an_unaffordable_aspiration():
    case = with_buyer(
        alternative={
            "kind": "quote",
            "terms": {
                "unit_price": 80.0,
                "payment_days": 90,
                "contract_months": 24,
                "incoterm": "DAP",
            },
        }
    )
    ev = evaluator(case)
    t = thread(ev)
    cs, space = state(case, [t], ev)
    target, reservation = cs.targets[t.sid]
    assert target <= reservation <= cs.c_alt
    p = decide(cs, ev, space, [t.sid])
    assert all(e.c <= cs.c_alt + 0.01 for e in p.packages)


@pytest.mark.parametrize("hold,regressed", [(False, False), (True, False), (False, True)])
def test_reduced_reservation_overrides_previous_bid(hold, regressed):
    ev = evaluator()
    t = thread(ev, prices=(110.0, 108.0), mine=(230_000.0,))
    a = replace(assess(t), regressed=regressed)
    assert bids.next_bid(playbook(CASE), t, a, 190_000, 200_000, 250_000, 0.8, hold) <= 200_000


def test_every_decision_uses_the_mid_scenario_whatever_the_evidence():
    case = with_buyer(category_goal="stabilise", sustainable_margin_pct=8, relationship_eur_per_point=5000)
    dossier = EvidenceDossier(case["case_id"], should_cost={"sup:A": (95.0, 10.0), "*": (90.0, 15.0)})
    specific = estimate_supplier(dossier, "sup:A", 110.0, cap=10.0)
    generic = estimate_supplier(dossier, "sup:B", 110.0)
    assert specific.full_cost[1] == 95.0  # a quote-based cap cannot overwrite dated cost evidence
    ev = evaluator(case, sids=("sup:A", "sup:B"), estimates={"sup:A": specific, "sup:B": generic})
    terms = {"unit_price": 100.0, "payment_days": 60, "contract_months": 24, "incoterm": "DAP"}
    assert ev.evaluate(CandidatePackage("sup:A", terms, "test")).c == ev.c("sup:A", terms, "mid")
    assert ev.c("sup:B", terms) == ev.c("sup:B", terms, "mid")


def test_late_rfq_closes_valid_offer_despite_cheaper_noncompliant_rival():
    case = {
        **with_buyer(must_haves=[{"field": "payment_days", "op": ">=", "value": 45}]),
        "type": "rfq",
        "suppliers": ["sup:A", "sup:B"],
    }
    ev = evaluator(case, sids=("sup:A", "sup:B"))
    full = {"payment_days": 90, "contract_months": 24, "incoterm": "DAP"}
    a = thread(ev, "sup:A", (110.0, 99.0), (180_000.0,) * 8, moves_left=1, standing={**full, "unit_price": 99.0})
    b = thread(ev, "sup:B", (90.0, 85.0), (160_000.0,), standing={**full, "payment_days": 30, "unit_price": 85.0})
    cs, space = state(case, [a, b], ev, opening={"sup:A": 110.0, "sup:B": 90.0})
    p = decide(cs, ev, space, [a.sid, b.sid])
    assert p.kind == "accept" and p.sid == a.sid
    ended = replace(b, moves_left=0)
    assert math.isinf(floor_c(ev, ended))


def test_better_terms_can_pay_a_price_above_current_quote():
    ev, space = evaluator(), ContractSpace(CASE)
    target = ev.c("sup:A", {"unit_price": 104.0}) - 1000
    out = bids.generate(ev, space, "sup:A", target, 104.0)
    selected = bids.select(out)
    assert any(e.terms["unit_price"] > 104.0 for e in selected)
    assert all(e.c <= target + 0.01 for e in selected)


def test_a_stated_dependency_never_blocks_a_deal():
    ev = evaluator()
    t = thread(ev, status="final_offer")
    t.observation = SupplierObservation("m", "f", dependencies=({"condition": "our general terms apply"},))
    cs, space = state(CASE, [t], ev)
    assert feasible(cs, ev, t)
    assert decide(cs, ev, space, [t.sid]).kind == "accept"


def test_observed_cash_and_firmness_affect_assessment():
    t = thread(evaluator())
    t.observation = SupplierObservation("m", "f", firmness=3, cash_pressure=True, values=("payment_days",))
    a = assess(t)
    assert a.walk_risk >= 0.6 and a.cash_pressure and a.values == ("payment_days",)


def test_a_final_offer_that_breaks_a_must_have_is_mirrored_never_accepted():
    case = with_buyer(must_haves=[{"field": "payment_days", "op": ">=", "value": 45}])
    ev = evaluator(case)
    t = thread(ev, prices=(110.0, 104.0), mine=(190_000.0,), status="final_offer")
    cs, space = state(case, [t], ev, pb=replace(playbook(case), mirror_final=False))
    p = decide(cs, ev, space, ["sup:A"])
    assert p.kind == "send" and p.packages[0].terms["payment_days"] >= 45
    gone = thread(ev, prices=(110.0, 104.0), mine=(190_000.0, p.packages[0].c), status="final_offer")
    cs2, _ = state(case, [gone], ev)
    assert decide(cs2, ev, space, ["sup:A"]).kind == "walk"


def test_all_final_takes_the_best_sheet_or_walks_to_the_alternative():
    case = {**with_buyer(alternative={"kind": "quote", "ref": "q", "terms": {"unit_price": 103.0}})}
    case["suppliers"] = ["sup:A", "sup:B"]
    ev = evaluator(case, sids=("sup:A", "sup:B"))
    full = {"payment_days": 30, "contract_months": 24, "incoterm": "DAP"}
    a = thread(ev, "sup:A", (110.0, 100.0), (180_000.0,), "final_offer", standing={**full, "unit_price": 100.0})
    b = thread(ev, "sup:B", (110.0, 101.0), (180_000.0,), "final_offer", standing={**full, "unit_price": 101.0})
    cs, space = state(case, [a, b], ev)
    p = decide(cs, ev, space, ["sup:B", "sup:A"])
    assert (p.kind, p.sid) == ("accept", "sup:A")
    cs.c_alt = ev.c("sup:A", a.standing) - 1  # the alternative beats both
    assert decide(cs, ev, space, ["sup:B", "sup:A"]).kind == "walk"


def test_acnext_and_actime():
    ev = evaluator()
    full = {"payment_days": 90, "contract_months": 24, "incoterm": "DAP"}
    t = thread(ev, prices=(110.0, 104.0), mine=(206_500.0,) * 3, standing={**full, "unit_price": 104.0})
    cs, space = state(CASE, [t], ev)
    assert decide(cs, ev, space, ["sup:A"]).kind == "accept"  # as good as our next bid
    far = thread(ev, prices=(110.0, 108.0), mine=(206_500.0,) * 3, standing={**full, "unit_price": 108.0})
    cs, _ = state(CASE, [far], ev)
    assert decide(cs, ev, space, ["sup:A"]).kind == "send"
    cs.tau = TAU_LATE
    assert decide(cs, ev, space, ["sup:A"]).kind == "accept"  # late in the wave: take what is good enough
    last = replace(far, moves_left=1)
    cs, _ = state(CASE, [last], ev)
    assert decide(cs, ev, space, ["sup:A"]).kind == "accept"
    early = thread(ev, prices=(110.0,), standing={**full, "unit_price": 110.0})
    cs, _ = state(CASE, [early], ev, tau=TAU_CLOSE)
    assert decide(cs, ev, space, ["sup:A"]).kind == "accept"  # the cap: close on the best sheet


def test_rfq_dominance_and_every_bidder_answered():
    case = {**CASE, "type": "rfq", "suppliers": ["sup:A", "sup:B", "sup:C"]}
    ev = evaluator(case, sids=("sup:A", "sup:B", "sup:C"))
    pb = playbook(case)
    first = [thread(ev, s, (p,)) for s, p in (("sup:A", 110.0), ("sup:B", 112.0), ("sup:C", 130.0))]
    openings = {"sup:A": 110.0, "sup:B": 112.0, "sup:C": 130.0}
    cs, space = state(case, first, ev, pb=pb, opening=openings)
    for order in (["sup:C", "sup:A", "sup:B"], ["sup:A", "sup:B", "sup:C"]):
        p = decide(cs, ev, space, order)
        assert (p.kind, p.sid) == ("send", order[0])  # the longest-waiting bidder gets its answer
    # A is good enough on its own, but B still moves fast and may reach below it: not yet
    full = {"payment_days": 90, "contract_months": 24, "incoterm": "DAP"}
    a = thread(ev, "sup:A", (110.0, 99.0), (196_500.0,) * 3, standing={**full, "unit_price": 99.0})
    b = thread(ev, "sup:B", (112.0, 106.0, 100.0), (185_000.0,) * 3, standing={**full, "unit_price": 100.0})
    c = thread(ev, "sup:C", (130.0, 129.0), (185_000.0,) * 3)
    cs, _ = state(case, [a, b, c], ev, pb=pb, opening=openings)
    p = decide(cs, ev, space, ["sup:A", "sup:B", "sup:C"])
    assert p.kind == "send"
    hold = decide(cs, ev, space, ["sup:C", "sup:A", "sup:B"])  # the third bidder: kept alive, not bid up
    assert hold.sid == "sup:C" and all(e.c <= 185_000.01 for e in hold.packages)
    still = replace(b, their_c=b.their_c[:1] + [b.their_c[-1]] * 3)  # B stopped moving: A dominates
    cs, _ = state(case, [a, still, c], ev, pb=pb, opening=openings)
    p = decide(cs, ev, space, ["sup:A", "sup:B", "sup:C"])
    assert (p.kind, p.sid, p.term_sheet_id) == ("accept", "sup:A", a.offer_id)


def test_every_package_keeps_the_must_haves():
    case = with_buyer(must_haves=[{"field": "incoterm", "op": "!=", "value": "EXW"}])
    ev = evaluator(case)
    for prices, mine in (((110.0,), ()), ((110.0, 106.0), (190_000.0,)), ((110.0, 105.0, 103.0), (190_000.0,) * 2)):
        t = thread(ev, prices=prices, mine=mine)
        cs, space = state(case, [t], ev)
        p = decide(cs, ev, space, ["sup:A"])
        if p.kind == "send":
            assert all(e.terms["incoterm"] != "EXW" for e in p.packages)
        else:  # its standing on EXW standard terms breaks the must-have: never taken
            assert p.kind != "accept"


def test_targets_without_an_alternative_stay_below_the_opening():
    ev, space = evaluator(), ContractSpace(CASE)
    c_target, c_res = bids.targets(CASE, None, ev, space, "sup:A", 110.0, math.inf)
    at_opening = ev.c("sup:A", bids.best_combo(ev, space, "sup:A", 110.0))
    assert c_target < c_res < at_opening < ev.c("sup:A", {"unit_price": 110.0})
    assert epsilon(CaseState(CASE, playbook(CASE), None, {}, targets={"sup:A": (c_target, c_res)})) > 0
    assert total_cost(CASE, {"unit_price": 110.0}, STD) == ev.c("sup:A", {"unit_price": 110.0})


def test_an_rfq_final_offer_left_standing_is_kept_alive_once_its_supplier_waited():
    case = {**CASE, "type": "rfq", "suppliers": ["sup:A", "sup:B"]}
    ev = evaluator(case, sids=("sup:A", "sup:B"))
    full = {"payment_days": 90, "contract_months": 24, "incoterm": "DAP"}
    a = thread(ev, "sup:A", (110.0, 104.0), (190_000.0,), "final_offer", standing={**full, "unit_price": 104.0})
    b = thread(ev, "sup:B", (112.0, 106.0, 103.0), (185_000.0,) * 2, standing={**full, "unit_price": 103.0})
    cs, space = state(case, [a, b], ev, opening={"sup:A": 110.0, "sup:B": 112.0})
    assert decide(cs, ev, space, ["sup:A", "sup:B"]).sid == "sup:B"  # B still moves: A's offer stands
    p = decide(cs, ev, space, ["sup:A", "sup:B"], waiting={"sup:A"})
    assert (p.kind, p.sid, p.packages, p.question) == ("send", "sup:A", (), "validity")  # no push, no timeout


# -- the agent: one turn ---------------------------------------------------------------------------


class StubModel:
    def __init__(self):
        self.budgets, self.last = [], (None, 0)
        self.trace = []

    def complete(self, system, user, timeout_s, **kwargs):
        self.budgets.append(timeout_s)
        return None  # no answer: the template writes


def opened(price=110.0, **extra):
    sheet = {"term_sheet_id": "offer:A-0", "terms": {"unit_price": price}}
    return {"sup:A": [{"from": "sales@A", "status": "open", "term_sheets": [sheet], "body": "Our quote.", **extra}]}


def test_the_writer_budget_counts_from_the_suppliers_last_message():
    from arena_kit.buyer import BuyerAgent

    now, model = [0.0], StubModel()
    agent = BuyerAgent(model=model, clock=lambda: now[0])
    agent.decide(CASE, opened())
    assert model.budgets and model.budgets[0] <= playbook(CASE).llm_budget_s
    now[0] += 55  # the same supplier message, 55 s on: no time left for the model
    move = agent.decide(CASE, opened())
    assert len(model.budgets) == 1 and move["message"]["term_sheets"]


def test_the_turn_never_raises(monkeypatch):
    from arena_kit.buyer import BuyerAgent, agent

    note = opened()
    note["sup:A"].append({"from": "arena", "status": "open", "term_sheets": [{"term_sheet_id": "x", "terms": {}}]})
    assert BuyerAgent()._decide(CASE, note)["supplier_id"] == "sup:A"  # a sheet without a price is no crash

    def broken(*args, **kw):
        raise ZeroDivisionError

    monkeypatch.setattr(agent, "decide", broken)
    move = BuyerAgent().decide(CASE, opened())
    assert move["message"]["action"] == {"type": "accept", "term_sheet_id": "offer:A-0"}  # the closing move


def test_a_failing_optional_component_retries_on_rules_instead_of_closing(monkeypatch):
    from arena_kit.buyer import BuyerAgent

    plain = BuyerAgent.label

    def label(self, cs, threads, *, allow_model=True):
        if allow_model:
            raise RuntimeError("interpreter down")
        return plain(self, cs, threads, allow_model=False)

    monkeypatch.setattr(BuyerAgent, "label", label)
    move = BuyerAgent().decide(CASE, opened())
    assert move["message"]["term_sheets"] and "action" not in move["message"]  # a bid, not an accept


def test_a_rough_relationship_estimate_never_turns_a_better_final_offer_into_a_walk():
    # case P01: should-cost above the price made the pessimistic relationship charge exceed the alternative
    alt = {"kind": "quote", "ref": "q", "terms": {"unit_price": 112.0, "payment_days": 30, "incoterm": "DAP"}}
    case = with_buyer(category_goal="stabilise", sustainable_margin_pct=8, relationship_eur_per_point=50_000,
                      alternative=alt)  # fmt: skip
    ev = evaluator(case, estimates={"sup:A": SupplierEstimate((105.0, 110.0, 115.0))})
    full = {"payment_days": 30, "contract_months": 24, "incoterm": "DAP", "unit_price": 108.0}
    t = thread(ev, prices=(112.0, 108.0), mine=(200_000.0,), status="final_offer", standing=full)
    cs, space = state(case, [t], ev, pb=replace(playbook(case), mirror_final=False))
    assert ev.c("sup:A", full, "high") > cs.c_alt > ev.c("sup:A", full, "none")
    p = decide(cs, ev, space, ["sup:A"])
    assert (p.kind, p.term_sheet_id) == ("accept", t.offer_id)


# -- practice-run regressions ----------------------------------------------------------------------


def test_equal_cost_values_go_to_the_supplier_and_dominated_asks_are_not_sent():
    # case P10: lead 35 and 42 cost us the same; we asked 35 and the supplier priced it
    case = {
        **CASE, "type": "capacity",
        "negotiable": ["unit_price", "lead_time_days", "contract_months"],
        "fixed_terms": {"payment_days": 60, "volume_per_year": 1000, "incoterm": "DAP", "index_clause": None},
        "term_options": {"lead_time_days": [35, 42, 56], "contract_months": [12, 24, 36]},
    }  # fmt: skip
    case = with_buyer(case, need_lead_time_days=42, must_haves=[{"field": "lead_time_days", "op": "<=", "value": 42}])
    std = {"lead_time_days": 54, "contract_months": 12, "payment_days": 30, "incoterm": "EXW"}
    ev, space = evaluator(case, std), ContractSpace(case)
    best = bids.best_combo(ev, space, "sup:A", 100.0)
    assert best["lead_time_days"] == 42
    assert ev.c("sup:A", best) == ev.c("sup:A", {**best, "lead_time_days": 35})
    t = thread(ev, prices=(110.0,), standing={"unit_price": 110.0, "lead_time_days": 54, "contract_months": 12})
    cs, _ = state(case, [t], ev)
    p = decide(cs, ev, space, ["sup:A"])
    assert p.kind == "send" and {e.terms["lead_time_days"] for e in p.packages} == {42}
    assert {24, 36} <= {e.terms["contract_months"] for e in p.packages}  # a longer term: no clear ask, kept


def test_no_sheet_shows_a_price_above_the_suppliers_own():
    # cases P02, P18: the supplier answered near the dearest price we showed
    ev, space = evaluator(), ContractSpace(CASE)
    out = bids.generate(ev, space, "sup:A", ev.c("sup:A", bids.best_combo(ev, space, "sup:A", 104.0)), 104.0)
    assert max(e.terms["unit_price"] for e in out) > 103.0 > min(e.terms["unit_price"] for e in out)
    assert all(e.terms["unit_price"] <= 103.0 for e in bids.select(out, ceiling=103.0))
    floor = min(e.terms["unit_price"] for e in out)
    assert [e.terms["unit_price"] for e in bids.select(out, ceiling=floor - 1)] == [floor]  # never none


def test_rfq_uses_comparable_costs_and_holding_bidders_still_close():
    case = {**CASE, "type": "rfq", "suppliers": ["sup:A", "sup:B", "sup:C"]}
    case = with_buyer(case, must_haves=[{"field": "payment_days", "op": ">=", "value": 45}])
    ev = evaluator(case, sids=("sup:A", "sup:B", "sup:C"))
    openings = {"sup:A": 110.0, "sup:B": 96.0, "sup:C": 118.0}  # B's quote on 30 days breaks the must-have
    threads = [thread(ev, s, (p,)) for s, p in openings.items()]
    comparable = ev.c("sup:B", bids.best_combo(ev, ContractSpace(case), "sup:B", 96.0))
    cs, space = state(case, threads, ev, opening=openings, market_c=comparable)
    for sid in openings:
        assert cs.targets[sid][0] <= comparable + 0.01
    p = decide(cs, ev, space, ["sup:C", "sup:A", "sup:B"])
    assert p.sid == "sup:C" and all(e.c <= comparable + 0.01 for e in p.packages)
    single, sp = ContractSpace(CASE), evaluator()
    assert bids.targets(CASE, None, sp, single, "sup:A", 110.0, math.inf, 110.0) == bids.targets(
        CASE, None, sp, single, "sup:A", 110.0, math.inf
    )  # one supplier: its own quote is the market
    full = {"payment_days": 90, "contract_months": 24, "incoterm": "DAP"}
    held = [
        thread(ev, s, (p, p), (150_000.0,) * 8, moves_left=1, standing={**full, "unit_price": p})
        for s, p in (("sup:A", 110.0), ("sup:C", 118.0))
    ]
    cs, _ = state(case, [*held, threads[1]], ev, opening=openings, market=96.0)
    p = decide(cs, ev, space, ["sup:A", "sup:C", "sup:B"])
    assert (p.kind, p.sid) == ("accept", "sup:A")  # however low the anchor: bidders that hold get a deal


def test_cost_prior_is_anchored_to_opening_not_subsequent_concessions():
    from arena_kit.buyer import BuyerAgent

    case = with_buyer(category_goal="stabilise", sustainable_margin_pct=10, relationship_eur_per_point=5000)
    agent = BuyerAgent()
    agent.prepare(case)
    _, ev = agent.observe(case, opened(110.0))
    assert ev.estimates["sup:A"].full_cost[1] == pytest.approx(0.92 * 110.0)
    cheaper = opened(110.0)
    cheaper["sup:A"] += [{"from": "buyer", "term_sheets": []}, {**cheaper["sup:A"][0], "term_sheets": [
        {"term_sheet_id": "offer:A-1", "terms": {"unit_price": 105.0}}]}]  # fmt: skip
    _, ev = agent.observe(case, cheaper)
    assert ev.estimates["sup:A"].full_cost[1] == pytest.approx(0.92 * 110.0)
    _, ev = agent.observe(with_buyer(case, sustainable_margin_pct=5), opened(110.0))
    assert ev.estimates["sup:A"].full_cost[1] == pytest.approx(0.92 * 110.0)  # below the cap: as estimated


def test_its_own_cheaper_alternative_is_sent_back_not_its_dearer_final_offer_accepted():
    """Case P11: a final offer at 49.00 came next to the supplier's own 46.50 alternative - accept
    takes only the standing sheet, so we send the alternative back (it answers 'agreed')."""
    ev = evaluator()
    full = {"unit_price": 109.0, "payment_days": 90, "contract_months": 24, "incoterm": "DAP"}
    alt = {**full, "unit_price": 104.0}
    t = thread(ev, prices=(110.0, 109.0), mine=(195_000.0, 199_000.0), status="final_offer", standing=full)
    t.sheets = [full, alt]
    cs, space = state(CASE, [t], ev)
    p = decide(cs, ev, space, ["sup:A"])
    assert p.kind == "send" and p.packages[0].terms == alt
    t.my_sheets = [alt]  # we sent it already and it answered with its final offer again: take that
    assert decide(cs, ev, space, ["sup:A"]).kind == "accept"
