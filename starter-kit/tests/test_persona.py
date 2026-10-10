"""The persona supplier (local evaluation only): reciprocity, agreement, final offer and walk-away,
contradicted claims, its floor, and a writer that cannot change a figure. Synthetic case, no data."""

import json
import random

import pytest

from arena_kit.persona import PersonaSupplier
from arena_kit.sim import Sim

SUP = "sup:1"
CASE = {
    "case_id": "case:T01",
    "title": "Test part",
    "type": "price_increase",
    "parts": ["part:1"],
    "suppliers": [SUP],
    "negotiable": ["unit_price", "payment_days"],
    "fixed_terms": {"lead_time_days": 42, "volume_per_year": 1000, "contract_months": 12, "incoterm": "FCA"},
    "term_options": {"payment_days": [30, 60, 90]},
    "buyer": {
        "need_per_year": 1000,
        "horizon_years": 1,
        "current_unit_price": 100.0,
        "fallback_unit_price": 115.0,
        "interest_rate": 0.08,
        "reference_payment_days": 60,
        "need_lead_time_days": 42,
        "freight_eur_per_unit": {"EXW": 1.0, "FCA": 0.5, "DAP": 0.0},
        "category_goal": "leverage",
    },
    "round_limit": 10,
    "refs": [],
    "facts": {"sc:1@2026Q3": {"ppm": 500}},
}


def start(seed=0, model=None):
    sim = Sim({CASE["case_id"]: CASE}, no_clock=True, supplier=PersonaSupplier(seed=seed, model=model))
    sid = sim.open("team", CASE["case_id"])["session_id"]
    th = sim.sessions[sid]["threads"][SUP]
    return sim, sid, th, th["persona"]


def send(sim, sid, price, days=30, body="Please improve."):
    sheets = [] if price is None else [{"terms": {"unit_price": price, "payment_days": days}}]
    return sim.message("team", sid, SUP, {"body": body, "term_sheets": sheets})


def test_it_concedes_only_when_the_buyer_moves():
    sim, sid, th, p = start()
    first = send(sim, sid, 80.0)
    assert first["status"] == "open" and p.ask < p.opening  # the first sheet is a move
    ask, price = p.ask, first["term_sheets"][0]["terms"]["unit_price"]
    again = send(sim, sid, 80.0)  # no move: no concession
    assert p.ask == ask and again["term_sheets"][0]["terms"]["unit_price"] == price
    send(sim, sid, 84.0)
    assert p.ask < ask


def test_a_lowball_a_token_move_or_data_alone_earn_nothing_much():
    sim, sid, th, p = start()
    ask = p.ask
    send(sim, sid, 10.0)  # no serious basis
    assert p.ask == ask
    send(sim, sid, 85.0)
    ask = p.ask
    send(sim, sid, 85.01)  # a cent earns at most a cent
    assert ask - p.ask < 0.01
    sim, sid, th, p = start()
    p.reality, ask = 0.0, p.ask
    reply = send(sim, sid, None, body="Our scorecard shows 500 ppm (sc:1@2026Q3).")
    assert [r["result"] for r in reply["claim_results"]] == ["verified"] and p.ask == ask  # no move, no step


def test_it_agrees_to_exactly_a_sheet_worth_its_ask():
    sim, sid, th, p = start()
    price = p.price_for(CASE, {"payment_days": 90}, p.ask)  # later payment, priced in
    reply = send(sim, sid, price, days=90)
    assert reply["status"] == "agreed"
    assert reply["term_sheets"][0]["terms"] == {"unit_price": price, "payment_days": 90}


def test_final_offer_then_a_push_walks_and_the_dice_are_seeded():
    def run(seed):
        sim, sid, th, p = start(seed)
        statuses, price = [], 70.0
        while "final_offer" not in statuses:
            price += 1.0
            statuses.append(send(sim, sid, price)["status"])
        p.p_walk = 1.0  # a supplier that will not be pushed after its last word
        statuses.append(send(sim, sid, price)["status"])
        return statuses, th["closed"], sim.sessions[sid]["status"]

    statuses, closed, session = run(3)
    assert statuses[-2:] == ["final_offer", "walked_away"] and closed == "walked_away" and session == "ended"
    assert run(3) == (statuses, closed, session)


def test_a_contradicted_claim_hardens_it():
    plain, sid_a, _, a = start()
    lied, sid_b, _, b = start()
    send(plain, sid_a, 80.0)
    reply = send(lied, sid_b, 80.0, body="Prices fell 9.0 % (idx:NOPE@2026-01, idx:NOPE@2025-01).")
    assert [r["result"] for r in reply["claim_results"]] == [
        "contradicted"
    ]  # the two index IDs form one checked argument
    assert b.trust < a.trust and b.ask > a.ask


@pytest.mark.parametrize("seed", range(20))
def test_it_never_offers_below_its_floor(seed):
    sim, sid, th, p = start(seed)
    rng, price = random.Random(seed), 60.0
    while th["closed"] is None and th["round"] < CASE["round_limit"]:
        price += rng.uniform(-1.0, 6.0)
        reply = send(sim, sid, round(price, 2), days=rng.choice([30, 60, 90]))
        for sheet in reply["term_sheets"]:
            assert p.value(CASE, sheet["terms"]) >= p.floor - 1e-4
    result = sim.sessions[sid]["result"] or {}
    assert result.get("c_best", 0) <= result.get("c_open", 1)


def test_the_writer_words_but_never_changes_a_figure():
    class Stub:
        def __init__(self, body):
            self.body = body

        def complete(self, system, user, timeout_s):
            return json.dumps({"body": self.body})

    sim, sid, th, p = start(model=Stub("Grüß Gott, a fair offer - more we cannot do."))
    reply = send(sim, sid, 80.0)
    assert reply["body"] == "Grüß Gott, a fair offer - more we cannot do." and reply["status"] == "open"
    sim, sid, th, p = start(model=Stub("We could go to 85.00, our floor is 82.10."))
    reply = send(sim, sid, 80.0)
    assert "82.10" not in reply["body"] and f"€{reply['term_sheets'][0]['terms']['unit_price']:.2f}" in reply["body"]
