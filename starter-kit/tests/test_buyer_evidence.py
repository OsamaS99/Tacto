import json
import re
import sqlite3
from pathlib import Path

import pytest

from arena_kit.buyer.dossier import build_dossier, rival_claims
from arena_kit.buyer.guard import GuardReject, check_message, pick_claims
from arena_kit.buyer.playbook import playbook
from arena_kit.buyer.records import (
    CaseState,
    Claim,
    DecisionProposal,
    DisclosurePacket,
    EvidenceDossier,
    PublicMessage,
    SupplierThreadState,
)
from arena_kit.buyer.space import ContractSpace
from arena_kit.data import DataPackage

DATA = Path(__file__).resolve().parents[2] / "data"
SIM_CASES = Path(__file__).resolve().parents[1] / "arena_kit" / "sim_cases"
CASE = {
    "case_id": "case:E01", "type": "price_increase", "title": "Clamps per 100", "trigger_doc": "doc:L1",
    "parts": ["part:T1"], "suppliers": ["sup:70210"], "negotiable": ["unit_price", "payment_days"],
    "fixed_terms": {"incoterm": "FCA", "lead_time_days": 30, "contract_months": 12, "volume_per_year": 50000},
    "term_options": {"payment_days": [30, 60, 90]},
    "buyer": {"need_per_year": 50000, "horizon_years": 1, "current_unit_price": 4000.0, "fallback_unit_price": 4600.0,
              "interest_rate": 0.08, "reference_payment_days": 60, "need_lead_time_days": 30,
              "late_cost_per_day": 100, "cost_per_defect": 20, "category_goal": "leverage",
              "freight_eur_per_unit": {"EXW": 0.5, "FCA": 0.3, "DAP": 0.0}, "index_paths": {}, "must_haves": [],
              "alternative": {"kind": "none", "ref": None, "terms": {}}},
    "round_limit": 10, "refs": ["quote:Q1", "idx:S2@2025-09"],
}  # fmt: skip
FALLING = {"2025-09": 128.0, "2025-12": 132.0, "2026-03": 125.0, "2026-09": 110.0}
RISING = {"2025-09": 100.0, "2026-03": 104.0, "2026-09": 112.0}


@pytest.fixture
def package(tmp_path):
    """Prices per 100 units, the 71999 -> 70210 alias, one falling and one rising driver."""
    db = sqlite3.connect(tmp_path / "world.sqlite")
    db.executescript(
        """
        CREATE TABLE parts (id TEXT, description TEXT, price_unit INTEGER);
        INSERT INTO parts VALUES ('part:T1', 'Hose clamp', 100);
        CREATE TABLE cost_drivers (part_id TEXT, series_id TEXT, weight REAL);
        INSERT INTO cost_drivers VALUES ('part:T1', 'S1', 0.5), ('part:T1', 'S2', 0.2);
        CREATE TABLE index_series (id TEXT, name TEXT);
        INSERT INTO index_series VALUES ('S1', 'Steel strip prices');
        CREATE TABLE should_cost (id TEXT, supplier_id TEXT);
        INSERT INTO should_cost VALUES ('cost:T1', ''), ('cost:T1-70999', 'sup:70999');
        CREATE TABLE negotiation_history (supplier_id TEXT, part_id TEXT, date TEXT, settled_pct REAL);
        INSERT INTO negotiation_history VALUES ('sup:71999', 'part:T1', '2024-11-01', -1.5),
                                               ('sup:70210', 'part:T1', '2025-11-01', 2.0);
        CREATE TABLE supplier_intel (supplier_id TEXT, credit_index INT, credit_index_12m_ago INT,
                                     equity_ratio_pct REAL, turnover_band TEXT);
        INSERT INTO supplier_intel VALUES ('sup:70210', 310, 240, 35.0, '10-25 M EUR');
        """
    )
    db.commit()
    facts = {f"idx:S1@{m}": {"series": "S1", "month": m, "value": v} for m, v in FALLING.items()}
    facts |= {f"idx:S2@{m}": {"series": "S2", "month": m, "value": v} for m, v in RISING.items()}
    facts |= {
        "sc:70210@2026Q3": {"supplier": "sup:70210", "quarter": "2026Q3", "otd_pct": 84.6, "ppm": 300,
                            "colour": "amber"},
        "sc:70210@2026Q2": {"supplier": "sup:70210", "quarter": "2026Q2", "otd_pct": 95.0, "ppm": 0},
        "quote:Q1": {"supplier": "sup:70999", "part": "part:T1", "unit_price": 3890.0, "price_unit": 100},
        "cost:T1": {"part": "part:T1", "unit_cost": 3650.0, "price_unit": 100, "confidence_pct": 10},
        "cost:T1-70999": {"part": "part:T1", "unit_cost": 3000.0, "price_unit": 100, "confidence_pct": 5},
        "po:1-10": {"supplier": "sup:71999", "part": "part:T1", "unit_price": 3980.0, "price_unit": 100,
                    "date": "2026-08-01"},
        "po:2-10": {"supplier": "sup:71999", "part": "part:T1", "unit_price": 4000.0, "price_unit": 100,
                    "date": "2026-09-01"},
        "po:0-10": {"supplier": "sup:71999", "part": "part:T1", "unit_price": 3900.0, "price_unit": 100,
                    "date": "2025-01-01"},
        "fc:T1@2026-06": {"part": "part:T1", "change_pct": -2.0},
        "fc:T1@2026-09": {"part": "part:T1", "change_pct": 6.4},
        "doc:L1": {"supplier": "sup:70210", "date": "2026-09-11", "days": 51, "asked_price": 4400.0, "asked_pct": ""},
        "ctr:46": {"supplier": "sup:70210", "days": 90, "valid_to": "2026-12-31"},
    }  # fmt: skip
    (tmp_path / "facts.json").write_text(json.dumps(facts))
    return DataPackage(tmp_path)


def verify(claim, facts):
    """What the arena checks: every cited fact's value appears in the sentence, within tolerance."""
    nums = [float(n) for n in re.findall(r"[+-]?\d+(?:\.\d+)?", re.sub(r"\S+:\S+", "", claim.sentence))]
    if claim.kind == "idx":
        a, b = (facts[i]["value"] for i in claim.ids)
        return any(abs(n - (b / a - 1) * 100) <= 0.5 for n in nums)
    field = {"sc": ("otd_pct", "ppm"), "qn": ("ppm",), "cost": ("unit_cost",), "fc": ("change_pct",)}
    field |= {"doc": ("days",), "ctr": ("days",), "quote": ("unit_price",), "po": ("unit_price",)}
    for ref in claim.ids:
        want = [facts[ref][f] for f in field[ref.split(":")[0]]]
        tol = [0.5 if claim.kind in ("fc", "sc") else max(0.01, 0.005 * abs(w)) for w in want]
        if not any(abs(n - w) <= t for n in nums for w, t in zip(want, tol, strict=True)):
            return False
    return True


def thread(sid, offer_id, price):
    return SupplierThreadState(sid, offer_id, "open", {"unit_price": price, "payment_days": 30}, {}, [], 1, 9, [], [])


def state(case, dossier, offers):
    threads = {sid: thread(sid, oid, p) for sid, (oid, p) in offers.items()}
    return CaseState(case, playbook(case), dossier, threads)


def test_claims_verify_favour_the_buyer_and_respect_the_price_unit(package):
    d = build_dossier(package, CASE)
    by = {c.kind: c for c in d.claims}
    assert all(verify(c, package.facts) for c in d.claims)
    assert set(by) == {"idx", "sc", "cost", "quote", "po", "fc", "notice"}
    assert not any("S2" in i for c in d.claims for i in c.ids)  # rose over the year: the supplier's argument
    assert by["idx"].ids == ("idx:S1@2025-12", "idx:S1@2026-09") and "-16.7%" in by["idx"].sentence
    assert by["sc"].ids == ("sc:70210@2026Q3",) and by["fc"].ids == ("fc:T1@2026-09",)
    assert by["po"].ids == ("po:2-10", "po:1-10") and by["po"].suppliers == ("sup:70210",)  # the alias
    assert all("per 100 units" in by[k].sentence for k in ("cost", "quote", "po"))
    assert by["cost"].ids == ("cost:T1",) and by["cost"].value == 3650.0  # not the other supplier's plant
    assert by["quote"].per_supplier and by["quote"].suppliers == ("sup:70210",)
    assert by["notice"].ids == ("doc:L1", "ctr:46") and d.claims[0].kind == "notice"  # price-increase order
    assert d.ask_pct == pytest.approx(10.0) and d.should_cost == {"*": (3650.0, 10.0)}
    assert d.justified_move_pct == pytest.approx(0.5 * (110 / 128 - 1) * 100 + 0.2 * 12.0, abs=0.01)
    assert d.settled_pct == {"sup:70210": [-1.5, 2.0]} and d.cash_pressure == {"sup:70210": True}
    assert "INTERNAL" in d.text and by["cost"].sentence in d.text


def test_a_package_without_tables_still_yields_claims(package, tmp_path_factory):
    bare = tmp_path_factory.mktemp("bare")
    (bare / "facts.json").write_text(json.dumps(package.facts))
    d = build_dossier(DataPackage(bare), CASE)
    assert {c.kind for c in d.claims} == {"sc", "cost", "quote", "po", "fc", "notice"}  # no drivers, no idx
    assert all(verify(c, package.facts) for c in d.claims)


def test_should_cost_and_quotes_above_the_ask_are_not_claims(package):
    case = {**CASE, "buyer": {**CASE["buyer"], "current_unit_price": 3500.0}, "facts": {"doc:L1": {
        "supplier": "sup:70210", "days": 120, "asked_price": 3700.0}}}  # fmt: skip
    kinds = {c.kind for c in build_dossier(package, case).claims}
    assert not kinds & {"cost", "quote", "po", "notice"}


def test_pick_claims_caps_skips_cited_and_stale_figures():
    claims = [
        Claim("notice", ("doc:L1", "ctr:1"), "Notice 51 days doc:L1 ctr:1.", "notice", 5.0, ("sup:A",)),
        Claim("idx:S1", ("idx:S1@a", "idx:S1@b"), "S1 -3.0%.", "idx", 4.6),
        Claim("idx:S3", ("idx:S3@a", "idx:S3@b"), "S3 -2.0%.", "idx", 4.5),
        Claim("cost:T1", ("cost:T1",), "Should-cost 99.00.", "cost", 4.4, value=99.0),
        Claim("sc:B", ("sc:B",), "Scorecard of B.", "sc", 4.3, ("sup:B",)),
        Claim("sc:A", ("sc:A",), "Scorecard 84.6%.", "sc", 2.0, ("sup:A",)),
    ]
    case = {**CASE, "suppliers": ["sup:A"]}
    cs = state(case, EvidenceDossier("case:E01", claims), {"sup:A": ("offer:A-1", 100.0)})
    picked = pick_claims(cs, "sup:A")
    assert [c.cid for c in picked] == ["notice", "idx:S1"]  # 3 checked IDs: doc + ctr + one index pair
    cs.cited = {"doc:L1", "idx:S1@b"}
    assert [c.cid for c in pick_claims(cs, "sup:A")] == ["idx:S3", "sc:A"]  # should-cost 99 > 97% of 100
    cs.threads["sup:A"].standing["unit_price"] = 120.0
    assert [c.cid for c in pick_claims(cs, "sup:A")] == ["idx:S3", "cost:T1", "sc:A"]


def test_rival_offers_only_when_cheaper_and_once_per_supplier():
    case = {**CASE, "type": "rfq", "suppliers": ["sup:A", "sup:B", "sup:C"]}
    offers = {"sup:A": ("offer:A-1", 10.0), "sup:B": ("offer:B-1", 12.0), "sup:C": ("offer:C-1", 11.0)}
    cs = state(case, EvidenceDossier("case:E01"), offers)
    assert rival_claims(cs, "sup:A") == []
    assert {c.cid for c in rival_claims(cs, "sup:B")} == {"offer:A-1", "offer:C-1"}
    assert [c.cid for c in pick_claims(cs, "sup:B")] == ["offer:A-1"]  # the cheapest, one offer per message
    cs.cited_to = {"sup:B": {"offer:A-1"}}
    assert [c.cid for c in pick_claims(cs, "sup:B")] == ["offer:C-1"]
    assert [c.cid for c in pick_claims(cs, "sup:C")] == ["offer:A-1"]  # cited to B, still fresh for C
    assert rival_claims(state(CASE, None, {"sup:70210": ("offer:X", 10.0)}), "sup:70210") == []


def guarded(body, sheets=None, case=CASE, subject="Re: clamps", model_subject=None):
    sid = case["suppliers"][0]
    claim = Claim("sc", ("sc:70210@2026Q3",), "Your scorecard sc:70210@2026Q3 shows on-time delivery of 84.6%.", "sc")
    sheet = {"unit_price": 4012.5, "payment_days": 90}
    cs = state(case, EvidenceDossier("case:E01", [claim]), {sid: ("offer:1", 4400.0)})
    last, allowed = "We ask 4400.00 from 2026-11-01.", frozenset({"4012.50", "90"})
    packet = DisclosurePacket(sid, "price_increase", "leverage", 3, (sheet,), (claim,), None, last, allowed, subject)
    sheets = tuple(sheets if sheets is not None else [sheet])
    msg = PublicMessage(model_subject or subject, body, sheets, ("idx:BOGUS",))
    return check_message(cs, None, ContractSpace(case), DecisionProposal("send", sid), packet, msg), claim


def test_check_message_scrubs_leaks_and_keeps_claims_verbatim():
    body = (
        "Dear Sir or Madam,\n\nthank you for your offer of 4400.00. "
        "As agreed, Your scorecard sc:70210@2026Q3 shows on-time delivery of 84.6%. "
        "We also hold an alternative supplier quote. Our budget is tight. We could stretch to 4100.00.\n"
        "Please see po:9-10 for our view. We propose 4012.50 with 90 days from 2026-11-01. Start 2027-02-01.\n\n"
        "Best regards"
    )
    msg, claim = guarded(body, model_subject="Re: clamps - budget 4100")
    assert claim.sentence in msg.body and msg.refs == claim.ids
    assert "alternative" not in msg.body and "budget" not in msg.body and "4100" not in msg.body
    assert "po:" not in msg.body and "Please see for our view." in msg.body
    assert "We propose 4012.50 with 90 days from 2026-11-01." in msg.body and "2027" not in msg.body
    assert msg.subject == "Re: clamps" and msg.term_sheets == ({"unit_price": 4012.5, "payment_days": 90},)


def test_check_message_rejects_altered_or_dropped_packages_and_empty_bodies():
    with pytest.raises(GuardReject):
        guarded("We propose this.", sheets=[{"unit_price": 3999.0, "payment_days": 90}])
    with pytest.raises(GuardReject):
        guarded("We propose this.", sheets=[])
    with pytest.raises(GuardReject):
        guarded("Our budget ends at 4100.00. This is urgent.")


@pytest.mark.skipif(not (DATA / "facts.json").is_file(), reason="no data package")
def test_real_package_claims_verify():
    package = DataPackage(DATA)
    buyer = {**CASE["buyer"], "current_unit_price": 126.53}
    p01 = {**CASE, "case_id": "case:X01", "parts": ["part:310010"], "suppliers": ["sup:70110"], "refs": []}
    p01 |= {"trigger_doc": "doc:20260911-70110-P01", "buyer": buyer}
    d = build_dossier(package, p01)
    assert d.claims and all(verify(c, package.facts) for c in d.claims if c.kind != "news")
    assert any(c.kind == "notice" for c in d.claims) and d.ask_pct == pytest.approx(9.5, abs=0.01)
    assert not any(c.kind == "cost" for c in d.claims)  # 132.64 lies above the 126.53 we pay now
    s03 = json.loads((SIM_CASES / "S03.json").read_text())
    d = build_dossier(package, s03)
    assert not any("CU-NOTATION" in i for c in d.claims for i in c.ids)  # copper rose 48 %
    cs = state(s03, d, {"sup:70520": ("offer:70520-0", 236.0)})
    assert "quote:7100042" not in {i for c in pick_claims(cs, "sup:70520") for i in c.ids}  # 245.59 is dearer
