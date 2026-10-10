"""Generic cross-component invariants: durable briefs, grounded observations and safe messages."""

import json
import time

from test_buyer_policy import CASE, STD, evaluator, state, thread

from arena_kit.buyer.agent import BuyerAgent
from arena_kit.buyer.guard import check_message
from arena_kit.buyer.model import Model
from arena_kit.buyer.prompts import INTERPRET, PREP, TURN
from arena_kit.buyer.records import Claim, DisclosurePacket, PublicMessage, TurnContext
from arena_kit.buyer.store import Store
from arena_kit.buyer.writer import disclosure


def test_preparation_and_interpretation_are_persisted_and_cached_across_restart(tmp_path):
    calls = []

    def answer(system, user, timeout):
        role = (
            "preparation"
            if "Build a source-backed" in system
            else ("interpretation" if "Extract interests" in system else "communication")
        )
        calls.append(role)
        return json.dumps(
            {"preparation": PREP.example, "interpretation": INTERPRET.example, "communication": TURN.example}[role]
        )

    model, store = Model([("mock", answer)]), Store(tmp_path)
    store.begin("session", CASE["case_id"], "test")
    agent = BuyerAgent(model=model, store=store)
    agent.prepare(CASE)
    assert agent.brief.playbook and calls == ["preparation"]
    events = {
        "sup:A": [
            {
                "from": "supplier",
                "message_id": "message",
                "status": "open",
                "body": "Lower price if you commit longer.",
                "term_sheets": [{"term_sheet_id": "offer:A", "terms": {"unit_price": 110.0, **STD}}],
            }
        ]
    }
    now = time.monotonic()
    context = TurnContext("session", now, {"sup:A": now + 60}, {"sup:A": 1}, decision_deadline=now + 55)
    agent.decide(CASE, events, context)
    agent.decide(CASE, events, context)
    assert calls.count("interpretation") == 1
    assert store.memo("session")["observations"]
    restarted = BuyerAgent(model=model, store=store)
    restarted.prepare(CASE, allow_model=False)
    restarted.decide(CASE, events, context)
    assert calls.count("interpretation") == 1 and restarted.brief.playbook == PREP.example["playbook"]


def test_runner_receipt_deadline_is_not_reset_by_observation():
    agent = BuyerAgent(clock=lambda: 1000.0)
    agent.prepare(CASE, allow_model=False)
    agent.set_turn_context(TurnContext("s", 950.0, {"sup:A": 1010.0}, {"sup:A": 1}, decision_deadline=1005.0))
    events = {
        "sup:A": [
            {
                "from": "supplier",
                "body": "Standing offer.",
                "status": "open",
                "term_sheets": [{"term_sheet_id": "offer:A", "terms": {"unit_price": 110.0}}],
            }
        ]
    }
    agent.observe(CASE, events)
    assert agent.since["sup:A"] == 950.0 and agent.budget("sup:A") == 5.0


def test_preparation_survives_changed_wave_metadata_before_session_open(tmp_path):
    calls = []

    def prepare(system, user, timeout):
        calls.append(system)
        return json.dumps(PREP.example)

    store, model = Store(tmp_path), Model([("mock", prepare)])
    first = BuyerAgent(model=model, store=store)
    first.prepare({**CASE, "seconds_left": 480, "open": True, "wave": 1})
    assert store.case_memo(CASE["case_id"])["brief_hash"] == first.brief_hash
    resumed = BuyerAgent(model=model, store=store)
    resumed.prepare({**CASE, "seconds_left": 200, "open": True, "wave": 2}, allow_model=False)
    assert len(calls) == 1 and resumed.brief == first.brief and resumed.brief_hash == first.brief_hash


def test_emergency_closure_recovers_valid_offer_without_unchecked_walk(monkeypatch):
    agent = BuyerAgent()
    agent.prepare(CASE, allow_model=False)
    events = {
        "sup:A": [
            {
                "from": "supplier",
                "body": "Final offer.",
                "status": "final_offer",
                "term_sheets": [{"term_sheet_id": "offer:A", "terms": {"unit_price": 100.0, **STD}}],
            }
        ]
    }

    def broken(*args):
        raise ValueError("synthetic policy failure")

    monkeypatch.setattr("arena_kit.buyer.agent.closing", broken)
    result = agent.close_decision(CASE, events)
    assert result["message"]["action"] == {"type": "accept", "term_sheet_id": "offer:A"}


def test_in_flight_reply_is_not_permanently_cached_without_interpretation():
    agent = BuyerAgent()
    agent.prepare(CASE, allow_model=False)
    events = {
        "sup:A": [
            {
                "from": "supplier",
                "body": "Conditional price if approval is pending.",
                "status": "open",
                "term_sheets": [{"term_sheet_id": "offer:A", "terms": {"unit_price": 110.0, **STD}}],
            }
        ]
    }
    now = time.monotonic()
    agent.set_turn_context(TurnContext("session", now, {"sup:A": now + 60}, in_flight=("sup:A",)))
    cs, _ = agent.observe(CASE, events)
    agent.label(cs, events)
    assert not agent.observations


def test_questions_already_asked_or_refused_are_not_offered_again():
    ev = evaluator()
    t = thread(ev)
    cs, _ = state(CASE, [t], ev)
    proposal = __import__("arena_kit.buyer.records", fromlist=["DecisionProposal"]).DecisionProposal(
        "send", "sup:A", question="breakdown"
    )
    packet = disclosure(cs, t, proposal, (), blocked_questions=("breakdown",))
    assert "breakdown" not in packet.questions and packet.question is None


def test_financial_guard_and_semantics_apply_to_every_template_or_selected_message():
    from arena_kit.buyer.records import CandidatePackage, DecisionProposal

    ev = evaluator()
    t = thread(ev)
    cs, space = state(CASE, [t], ev)
    terms = {"unit_price": 100.0, "payment_days": 90, "contract_months": 24, "incoterm": "DAP"}
    claim = Claim("sc:A", ("sc:A",), "Recorded delivery performance is 84.6% (sc:A).", "sc")
    pkg = ev.evaluate(CandidatePackage("sup:A", terms, "test"))
    proposal = DecisionProposal("send", "sup:A", (pkg,))
    packet = DisclosurePacket(
        "sup:A", "renewal", "leverage", 1, (terms,), (claim,), None, "", frozenset({"90"}), "Re: T"
    )
    msg = PublicMessage(
        "Re: T",
        claim.sentence + "\nOur delivery performance is 90%.\nOur budget is seventy thousand euros.\n"
        "We guarantee exclusivity.\nPlease confirm the attached package.",
        (terms,),
    )
    checked = check_message(cs, ev, space, proposal, packet, msg)
    assert claim.sentence in checked.body and "90%" not in checked.body and "budget" not in checked.body
    assert "exclusivity" not in checked.body and checked.refs == ("sc:A",)
