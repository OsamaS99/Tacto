import json
import string
import threading
import time
from dataclasses import replace

import httpx
import pytest

from arena_kit.buyer import model as model_mod
from arena_kit.buyer import replay, writer
from arena_kit.buyer.decisions import Decisions
from arena_kit.buyer.model import Model, model_from_env, openai_chat
from arena_kit.buyer.prompts import PREP, ROLES, SHARED, TURN, check
from arena_kit.buyer.records import Claim, DisclosurePacket, EvidenceDossier

SHEETS = (
    {"unit_price": 210.5, "payment_days": 60, "contract_months": 24},
    {"unit_price": 205.0, "payment_days": 30, "contract_months": 24},
)
CLAIM = Claim("idx:STEEL", ("idx:STEEL",), "Steel fell 4.2 % since January (idx:STEEL).", "idx")
PKT = DisclosurePacket(
    sid="sup:1",
    mode="price_increase",
    goal="leverage",
    rev=4,
    packages=SHEETS,
    claims=(CLAIM,),
    question=None,
    their_last="Our price is final. Please pay faster.",
    allowed_numbers=frozenset({"210.50", "205.00", "60", "30", "24"}),
    subject="Re: Housing price",
    questions=("breakdown",),
    acknowledgments=("neutral", "cash"),
)


def turn(package_ids=("P1",), claim_ids=("C1",), question="breakdown", **extra):
    return json.dumps(
        {
            "package_ids": list(package_ids),
            "claim_ids": list(claim_ids),
            "question": question,
            "acknowledgment": "neutral",
            "trade": None,
            "tone": "professional",
            "rationale": "trade payment for price",
            **extra,
        }
    )


def replies(*texts):
    """A provider that answers with `texts` in turn and counts its calls."""
    calls = []

    def call(system, user, timeout_s):
        calls.append(user)
        return texts[min(len(calls), len(texts)) - 1]

    return call, calls


def test_writer_timeout_falls_back_to_template_within_budget(monkeypatch):
    monkeypatch.setattr(writer, "MIN_CALL_S", 0.3)
    release = threading.Event()
    slow = Model([("slow", lambda s, u, t: release.wait(5) and turn())])
    t0 = time.monotonic()
    msg, source, _ = writer.write_turn(slow, PKT, 1.0)
    took = time.monotonic() - t0
    release.set()
    assert source == "template" and took < 1.0 + 0.5
    assert msg == writer.template(PKT)


def test_writer_invalid_json_uses_template_without_repair():
    call, calls = replies("I think we should push.")
    msg, source, _ = writer.write_turn(Model([("m", call)]), PKT, 10.0)
    assert source == "template" and msg == writer.template(PKT) and len(calls) == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"package_ids": ("P9",)},
        {"claim_ids": ("C7",)},
        {"question": "budget"},
        {"message": {"opening": "We guarantee exclusivity."}},
    ],
)
def test_writer_rejects_ids_and_public_text_it_did_not_offer(bad):
    call, _ = replies(turn(**bad))
    msg, source, _ = writer.write_turn(Model([("m", call)]), PKT, 10.0)
    assert source == "template" and msg == writer.template(PKT)


def test_writer_assembles_the_chosen_portfolio_verbatim_and_keeps_rationale_private():
    call, _ = replies(turn(package_ids=("P1", "P2"), rationale="Our private budget is seventy thousand."))
    msg, source, read = writer.write_turn(Model([("m", call)]), PKT, 10.0)
    assert source == "llm" and "private budget" in read["rationale"]
    assert msg.term_sheets == SHEETS and msg.refs == ("idx:STEEL",)
    assert CLAIM.sentence in msg.body and writer.QUESTIONS["breakdown"] in msg.body
    assert "budget" not in msg.body


def test_opening_selection_is_normalized_to_all_packages_and_one_claim():
    pkt = replace(PKT, rev=5, first_contact=True)
    call, _ = replies(turn(package_ids=("P1",), claim_ids=()))
    msg, source, _ = writer.write_turn(Model([("m", call)]), pkt, 10.0)
    assert source == "llm_normalized"
    assert msg.term_sheets == SHEETS and msg.refs == CLAIM.ids


def test_later_open_selection_is_normalized_to_two_packages():
    call, _ = replies(turn(package_ids=("P1",)))
    msg, source, _ = writer.write_turn(Model([("m", call)]), replace(PKT, rev=3), 10.0)
    assert source == "llm_normalized" and msg.term_sheets == SHEETS


def test_later_single_matched_branch_remains_a_clean_model_selection():
    pkt = replace(PKT, packages=(SHEETS[0],), first_contact=False)
    call, _ = replies(turn(package_ids=("P1",)))
    msg, source, _ = writer.write_turn(Model([("m", call)]), pkt, 10.0)
    assert source == "llm" and msg.term_sheets == (SHEETS[0],)


@pytest.mark.parametrize("bad", [{"package_ids": ("P9",)}, {"claim_ids": ("C7",)}])
def test_normalization_never_repairs_unknown_ids(bad):
    call, _ = replies(turn(**bad))
    pkt = replace(PKT, first_contact=True)
    msg, source, _ = writer.write_turn(Model([("m", call)]), pkt, 10.0)
    assert source == "template" and msg == writer.template(pkt)


def test_normalization_clears_a_trade_missing_from_an_appended_package():
    sheets = (SHEETS[0], {"unit_price": 205.0, "contract_months": 24})
    pkt = replace(PKT, packages=sheets, trades=("payment_days",), first_contact=True)
    call, _ = replies(turn(package_ids=("P1",), trade="payment_days"))
    msg, source, _ = writer.write_turn(Model([("m", call)]), pkt, 10.0)
    assert source == "llm_normalized" and msg.term_sheets == sheets
    assert "payment terms shown" not in msg.body


def test_revision_one_is_not_first_contact_after_a_buyer_message():
    call, _ = replies(turn(package_ids=("P1", "P2"), claim_ids=()))
    msg, source, _ = writer.write_turn(Model([("m", call)]), replace(PKT, rev=1, first_contact=False), 10.0)
    assert source == "llm" and msg.refs == ()


def test_final_offer_still_requires_one_package_and_no_claim():
    pkt = replace(PKT, status="final_offer", acknowledgments=("neutral", "final"))
    call, _ = replies(turn(package_ids=("P1",), claim_ids=(), question=None, acknowledgment="final"))
    msg, source, _ = writer.write_turn(Model([("m", call)]), pkt, 10.0)
    assert source == "llm" and msg.term_sheets == (SHEETS[0],) and msg.refs == ()


def test_invalid_selection_advances_to_fallback_not_a_second_primary_call():
    bad, calls = replies(turn(package_ids=("P9",)))
    good, _ = replies(turn(package_ids=("P1", "P2"), claim_ids=()))
    msg, source, _ = writer.write_turn(Model([("bad", bad), ("good", good)]), PKT, 20.0)
    assert source == "llm" and msg.refs == () and len(calls) == 1


def test_model_uses_the_fallback_when_the_primary_fails():
    def broken(system, user, timeout_s):
        raise httpx.ConnectError("down")

    m = Model([("primary", broken), ("fallback", lambda s, u, t: "{}")])
    assert m.complete("s", "u", 10.0) == "{}" and m.last[0] == "fallback"
    release = threading.Event()
    slow = Model([("primary", lambda s, u, t: release.wait(5) and "{}"), ("fallback", lambda s, u, t: "{}")])
    assert slow.complete("s", "u", 1.0) is None and slow.last[0] is None  # < 4 s left: no fallback
    release.set()


def test_openai_adapter_sends_effort_and_drops_json_mode_on_400():
    bodies = []

    def handle(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": "response_format unsupported"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    call = openai_chat("https://llm.test/v1", "k", "gpt-x", "medium", transport=httpx.MockTransport(handle))
    assert call("s", "u", 10.0) == "{}"
    assert len(bodies) == 2 and bodies[0]["reasoning_effort"] == "medium" and "response_format" not in bodies[1]
    assert call("s", "u", 10.0) == "{}" and len(bodies) == 3  # remembered


def test_model_from_env(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "gpt-x")
    monkeypatch.setenv("LLM_FALLBACK_MODEL", "gpt-y")
    monkeypatch.setenv("LLM_TURN", "1")
    assert [n for n, _ in model_from_env().providers] == ["gpt-x", "gpt-y"]
    monkeypatch.setenv("LLM_TURN", "0")
    assert model_from_env() is None


def decisions(answers=None, status=200, delay=0.0):
    seen = []

    def handle(request):
        seen.append(json.loads(request.content))
        time.sleep(delay)
        return httpx.Response(status, json={"answers": answers or []})

    return Decisions("https://llm.test/v1", "k", timeout_s=0.5, transport=httpx.MockTransport(handle)), seen


def intent(choice, p):
    probabilities = [{"value": choice, "probability": p}]
    return {"type": "choice", "name": "intent", "choice": choice, "probabilities": probabilities}


def test_decisions_labels_only_above_the_threshold():
    d, seen = decisions(
        [
            intent("walk_threat", 0.9),
            {"type": "predicate", "name": "firm_final", "probability": 0.95},
            {"type": "predicate", "name": "cash_pressure", "probability": 0.5},
            {"type": "predicate", "name": "values_payment_days", "probability": 0.8},
        ]
    )
    assert d.classify_reply("This is our final offer.") == {
        "intent": "walk_threat",
        "firm_final": True,
        "values": ["payment_days"],
    }
    assert seen[0]["model"] == "gpt-6-luna" and {q["type"] for q in seen[0]["questions"]} == {"choice", "predicate"}
    low, _ = decisions([intent("hold", 0.55), {"type": "predicate", "name": "firm_final", "probability": 0.6}])
    assert low.classify_reply("Let us think about it.") is None


@pytest.mark.parametrize("kw", [{"status": 500}, {"delay": 1.0}, {"answers": [{"type": "choice"}]}])
def test_decisions_returns_none_on_error_or_timeout(kw):
    d, _ = decisions(**kw)
    t0 = time.monotonic()
    assert d.classify_reply("Our price stands.") is None
    assert time.monotonic() - t0 < 0.5 + 0.3


def test_every_role_has_a_prompt_a_schema_and_a_valid_example():
    names = {"preparation", "counterparty_analyst", "communication_agent", "replay_analyst"}
    assert set(ROLES) == names
    for role in [*ROLES.values(), PREP, TURN]:
        assert role.system.startswith(SHARED) and role.schema["type"] == "object"
        assert role.check(role.example) == [], role.name
        fields = {f for _, f, _, _ in string.Formatter().parse(role.user) if f} - {"schema"}
        system, user = role.render(**{f: 1.0 if f == "budget_s" else "x" for f in fields})
        assert json.dumps(role.schema) in user and role.budget_s > 0 and role.fallback
    assert check(TURN.schema, {"proposal": {}}) != []


def test_prep_returns_a_bounded_brief_or_defaults():
    case = {
        "case_id": "T",
        "type": "renewal",
        "buyer": {"category_goal": "leverage"},
        "negotiable": ["unit_price", "payment_days"],
    }
    dossier = EvidenceDossier("T", text="no data")
    good = {**PREP.example, "playbook": {"beta": 0.5, "k": 0.7, "p_walk": 0.3}}
    call, _ = replies(json.dumps(good))
    brief = writer.prep(Model([("m", call)]), case, dossier)
    assert brief.playbook == good["playbook"] and brief.questions == ("breakdown",)
    bad = {**good, "playbook": {"beta": 9, "k": 0.7, "p_walk": 0.3}}
    call, _ = replies(json.dumps(bad))
    assert writer.prep(Model([("m", call)]), case, dossier).playbook == {}
    invented = {**good, "facts": [{"kind": "fact", "source_ids": ["cost:invented"], "text": "Cheap."}]}
    call, _ = replies(json.dumps(invented))
    assert not writer.prep(Model([("m", call)]), case, dossier).facts


def test_prep_cannot_hide_verified_claims():
    case = {
        "case_id": "T",
        "type": "renewal",
        "buyer": {"category_goal": "leverage"},
        "negotiable": ["unit_price", "payment_days"],
    }
    dossier = EvidenceDossier("T", [CLAIM], text="verified")
    answer = {**PREP.example, "permitted_disclosures": []}
    call, _ = replies(json.dumps(answer))
    assert writer.prep(Model([("m", call)]), case, dossier).permitted_disclosures == (CLAIM.cid,)


def test_replay_summarises_without_a_model(tmp_path):
    lines = [
        {"kind": "send", "writer": "template", "llm_ms": 1200},
        {"kind": "send", "status": 422},
        {"kind": "result", "result": {"deal": False, "score": 0.0}},
    ]
    (tmp_path / "s1.events.jsonl").write_text("\n".join(map(json.dumps, lines)) + "\n{broken")
    out = replay.analyze([tmp_path])
    assert out["source"] == "code" and out["summary"]["http_errors"] == {"422": 1}
    assert {f["category"] for f in out["failures"]} == {"execution"}
    assert out["summary"]["no_deal"] == 1  # choosing a better alternative need not be a policy failure
    assert ROLES["replay_analyst"].check({k: out[k] for k in ROLES["replay_analyst"].schema["required"]}) == []


def test_replay_counts_normalized_writer_output(tmp_path):
    lines = [
        {"kind": "send", "writer": "llm"},
        {"kind": "send", "writer": "llm_normalized"},
        {"kind": "send", "writer": "template"},
    ]
    (tmp_path / "writer.events.jsonl").write_text("\n".join(map(json.dumps, lines)))
    events, sessions = replay.load([tmp_path])
    assert replay.summarize(events, sessions)["writer"] == {
        "llm": 1,
        "llm_normalized": 1,
        "template": 1,
    }


def test_concurrency_is_capped(monkeypatch):
    monkeypatch.setattr(model_mod, "_slots", threading.BoundedSemaphore(1))
    release = threading.Event()
    hold = threading.Thread(target=model_mod.within, args=(lambda: release.wait(5), 2.0))
    hold.start()
    time.sleep(0.05)
    assert model_mod.within(lambda: "x", 0.2) is None  # no slot within the deadline
    release.set()
    hold.join()
    assert model_mod.within(lambda: "x", 0.5) == "x"


def test_a_keep_alive_question_survives_on_a_final_offer():
    from dataclasses import replace

    pkt = replace(PKT, packages=(), claims=(), status="final_offer", question="validity", questions=("validity",))
    assert "until when your offer remains valid" in writer.template(pkt).body


@pytest.mark.parametrize("body", ["The final delivery date is 3 May.", "We ship internationally."])
def test_everyday_words_are_neither_a_final_offer_nor_a_refusal(body):
    from arena_kit.buyer.interpreter import interpret

    obs = interpret("sup:1", {"body": body})
    assert obs.firmness == 1 and not obs.refused
