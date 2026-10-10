import json

from arena_kit.agents import StrategicAgent
from arena_kit.cli import agent_factory
from arena_kit.client import ArenaClient
from arena_kit.cost import total_cost
from arena_kit.play import RemoteTable, play

CASE = {
    "case_id": "case:STRATEGY",
    "set": "practice",
    "type": "rfq",
    "title": "Strategic sourcing test",
    "briefing": "Compare two supplier proposals.",
    "trigger_doc": None,
    "parts": ["part:T1"],
    "suppliers": ["sup:A", "sup:B"],
    "negotiable": ["unit_price", "payment_days"],
    "fixed_terms": {
        "lead_time_days": 30,
        "volume_per_year": 1_000,
        "contract_months": 12,
        "incoterm": "DAP",
        "tooling_eur": 0,
        "index_clause": None,
    },
    "term_options": {"payment_days": [14, 30, 60, 90]},
    "buyer": {
        "need_per_year": 1_000,
        "horizon_years": 1,
        "current_unit_price": 40.0,
        "fallback_unit_price": 48.0,
        "interest_rate": 0.08,
        "reference_payment_days": 60,
        "need_lead_time_days": 30,
        "category_goal": "leverage",
        "fallback_risk_pct": 3,
        "index_risk_pct": 1,
        "late_cost_per_day": 100,
        "cost_per_defect": 50,
        "freight_eur_per_unit": {"EXW": 1.0, "FCA": 0.5, "DAP": 0.0},
        "index_paths": {},
        "must_haves": [],
        "alternative": {"kind": "none", "ref": None, "terms": {}},
    },
    "round_limit": 10,
    "refs": [],
    "facts": {},
}


def supplier_message(supplier, price, payment=30, status="open", *, omit_payment=False):
    terms = {"unit_price": price}
    if not omit_payment:
        terms["payment_days"] = payment
    quote = (
        f"Our standard terms\nPayment terms (days net) {payment}\nTerms fixed by your enquiry\nLead time (days) 30\n"
    )
    return {
        "from": f"sales@{supplier}",
        "status": status,
        "body": "Please find our proposal.",
        "term_sheets": [{"term_sheet_id": f"offer:{supplier}-0", "terms": terms}],
        "attachments": [{"name": "quote.pdf", "text": quote}],
    }


def test_strategic_agent_compares_all_suppliers_with_standard_terms():
    threads = {
        "sup:A": [supplier_message("A", 40.0, payment=14, omit_payment=True)],
        "sup:B": [supplier_message("B", 40.0, payment=60)],
    }

    decision = StrategicAgent().decide(CASE, threads)

    # Equal unit prices are not equal deals: supplier B's longer payment term is cheaper.
    assert decision["supplier_id"] == "sup:B"
    assert "action" not in decision["message"]


def test_strategic_agent_includes_supplier_health_when_ranking_strategic_offers():
    class Package:
        def lookup(self, ref):
            return None

        def query(self, sql, params=()):
            if "should_cost" in sql:
                return [
                    {
                        "supplier_id": "sup:A",
                        "unit_cost": 45.0,
                        "price_unit": 1,
                        "confidence_pct": 0,
                    },
                    {
                        "supplier_id": "sup:B",
                        "unit_cost": 30.0,
                        "price_unit": 1,
                        "confidence_pct": 0,
                    },
                ]
            return []

    case = {
        **CASE,
        "buyer": {
            **CASE["buyer"],
            "category_goal": "stabilise",
            "sustainable_margin_pct": 5,
            "relationship_eur_per_point": 10_000,
        },
    }
    threads = {
        "sup:A": [supplier_message("A", 40.0, payment=60)],
        "sup:B": [supplier_message("B", 40.0, payment=60)],
    }

    decision = StrategicAgent(package=Package()).decide(case, threads)

    assert decision["supplier_id"] == "sup:B"


def test_strategic_agent_accounts_for_supplier_financing_pressure():
    class Package:
        def lookup(self, ref):
            return None

        def query(self, sql, params=()):
            if "should_cost" in sql:
                return [
                    {
                        "supplier_id": "sup:A",
                        "unit_cost": 38.0,
                        "price_unit": 1,
                        "confidence_pct": 0,
                    },
                    {
                        "supplier_id": "sup:B",
                        "unit_cost": 38.0,
                        "price_unit": 1,
                        "confidence_pct": 0,
                    },
                ]
            if "supplier_intel" in sql:
                return [{"credit_index": 280 if params[0] == "sup:A" else 120}]
            return []

    case = {
        **CASE,
        "buyer": {
            **CASE["buyer"],
            "category_goal": "stabilise",
            "sustainable_margin_pct": 5,
            "relationship_eur_per_point": 10_000,
        },
    }
    threads = {
        "sup:A": [supplier_message("A", 40.0, payment=90)],
        "sup:B": [supplier_message("B", 40.0, payment=90)],
    }
    for events in threads.values():
        events[0]["attachments"][0]["text"] = events[0]["attachments"][0]["text"].replace(
            "Payment terms (days net) 90",
            "Payment terms (days net) 30",
        )

    decision = StrategicAgent(package=Package()).decide(case, threads)

    assert decision["supplier_id"] == "sup:B"


def test_strategic_agent_does_not_trade_price_for_a_larger_relationship_penalty():
    class Package:
        def lookup(self, ref):
            return None

        def query(self, sql, params=()):
            if "should_cost" in sql:
                return [
                    {
                        "supplier_id": "sup:A",
                        "unit_cost": 45.0,
                        "price_unit": 1,
                        "confidence_pct": 0,
                    }
                ]
            return []

    case = {
        **CASE,
        "buyer": {
            **CASE["buyer"],
            "category_goal": "stabilise",
            "sustainable_margin_pct": 5,
            "relationship_eur_per_point": 10_000,
        },
    }
    opening = supplier_message("A", 40.0, payment=60)
    supplier = {
        "full_cost": 45.0,
        "interest_rate": 0.1,
        "standard": {"payment_days": 60, "lead_time_days": 30},
    }

    message = StrategicAgent(package=Package()).decide(case, {"sup:A": [opening]})["message"]

    opening_cost = total_cost(case, {"unit_price": 40.0, "payment_days": 60}, supplier["standard"], supplier)
    assert message["term_sheets"]
    assert all(
        total_cost(case, sheet["terms"], supplier["standard"], supplier) <= opening_cost
        for sheet in message["term_sheets"]
    )


def test_strategic_agent_keeps_a_material_opening_anchor_with_supplier_health():
    case = {
        **CASE,
        "buyer": {
            **CASE["buyer"],
            "category_goal": "stabilise",
            "sustainable_margin_pct": 5,
            "relationship_eur_per_point": 10_000,
        },
        "refs": ["cost:T1"],
        "facts": {
            "cost:T1": {
                "part": "part:T1",
                "unit_cost": 37.0,
                "price_unit": 1,
                "confidence_pct": 0,
            }
        },
    }

    message = StrategicAgent().decide(case, {"sup:A": [supplier_message("A", 40.0, payment=60)]})["message"]

    assert message["term_sheets"]
    assert all(sheet["terms"]["unit_price"] <= 39.60 for sheet in message["term_sheets"])


def test_strategic_agent_does_not_mix_uncapped_and_anchored_opening_packages():
    class Package:
        def lookup(self, ref):
            return None

        def query(self, sql, params=()):
            if "should_cost" in sql:
                return [
                    {
                        "supplier_id": "sup:A",
                        "unit_cost": 36.3,
                        "price_unit": 1,
                        "confidence_pct": 0,
                    }
                ]
            if "supplier_intel" in sql:
                return [{"credit_index": 260}]
            return []

    case = {
        **CASE,
        "buyer": {
            **CASE["buyer"],
            "category_goal": "stabilise",
            "sustainable_margin_pct": 5,
            "relationship_eur_per_point": 1_000,
        },
    }

    message = StrategicAgent(package=Package()).decide(
        case,
        {"sup:A": [supplier_message("A", 40.0, payment=14)]},
    )["message"]

    assert message["term_sheets"]
    assert all(sheet["terms"]["unit_price"] <= 39.60 for sheet in message["term_sheets"])


def test_strategic_agent_uses_case_supplied_should_cost_without_a_data_package():
    case = {
        **CASE,
        "suppliers": ["sup:A"],
        "refs": ["cost:T1"],
        "facts": {
            "cost:T1": {
                "part": "part:T1",
                "unit_cost": 45.0,
                "price_unit": 1,
                "confidence_pct": 0,
            }
        },
        "buyer": {
            **CASE["buyer"],
            "category_goal": "stabilise",
            "sustainable_margin_pct": 5,
            "relationship_eur_per_point": 10_000,
        },
    }
    opening = supplier_message("A", 40.0, payment=60)
    supplier = {
        "full_cost": 45.0,
        "interest_rate": 0.1,
        "standard": {"payment_days": 60, "lead_time_days": 30},
    }

    message = StrategicAgent().decide(case, {"sup:A": [opening]})["message"]

    opening_cost = total_cost(case, {"unit_price": 40.0, "payment_days": 60}, supplier["standard"], supplier)
    assert message["term_sheets"]
    assert all(
        total_cost(case, sheet["terms"], supplier["standard"], supplier) <= opening_cost
        for sheet in message["term_sheets"]
    )


def test_strategic_agent_does_not_reuse_ambiguous_should_cost_across_suppliers():
    case = {
        **CASE,
        "refs": ["cost:T1"],
        "facts": {
            "cost:T1": {
                "part": "part:T1",
                "unit_cost": 38.0,
                "price_unit": 1,
                "confidence_pct": 5,
            }
        },
        "buyer": {
            **CASE["buyer"],
            "category_goal": "stabilise",
            "sustainable_margin_pct": 5,
            "relationship_eur_per_point": 10_000,
        },
    }
    threads = {
        "sup:A": [supplier_message("A", 40.0, payment=60)],
        "sup:B": [supplier_message("B", 40.0, payment=60)],
    }

    agent = StrategicAgent()
    agent.decide(case, threads)
    offers = agent.evaluate_offers(case, threads)

    assert all(offer.supplier is None for offer in offers)


def test_strategic_agent_uses_the_midpoint_to_the_lower_should_cost_bound():
    class Package:
        def lookup(self, ref):
            return None

        def query(self, sql, params=()):
            if "should_cost" in sql:
                return [
                    {
                        "supplier_id": "sup:A",
                        "unit_cost": 40.0,
                        "price_unit": 1,
                        "confidence_pct": 10,
                    }
                ]
            return []

    case = {
        **CASE,
        "suppliers": ["sup:A"],
        "buyer": {
            **CASE["buyer"],
            "category_goal": "stabilise",
            "sustainable_margin_pct": 5,
            "relationship_eur_per_point": 10_000,
        },
    }

    offer = StrategicAgent(package=Package()).evaluate_offers(
        case,
        {"sup:A": [supplier_message("A", 44.0, payment=60)]},
    )[0]

    assert offer.supplier["full_cost"] == 38.0


def test_strategic_agent_estimates_supplier_health_when_standard_payment_is_missing():
    case = {
        **CASE,
        "suppliers": ["sup:A"],
        "refs": ["cost:T1"],
        "facts": {
            "cost:T1": {
                "part": "part:T1",
                "unit_cost": 38.0,
                "price_unit": 1,
                "confidence_pct": 5,
            }
        },
        "buyer": {
            **CASE["buyer"],
            "category_goal": "stabilise",
            "sustainable_margin_pct": 5,
            "relationship_eur_per_point": 10_000,
        },
    }
    opening = supplier_message("A", 44.0, omit_payment=True)
    opening["attachments"][0]["text"] = "Our standard terms\nTerms fixed by your enquiry\nLead time (days) 30\n"

    message = StrategicAgent().decide(case, {"sup:A": [opening]})["message"]

    assert message["term_sheets"]


def test_strategic_agent_tries_every_supplier_until_packages_are_possible():
    case = {
        **CASE,
        "suppliers": ["sup:A", "sup:B", "sup:C"],
        "fixed_terms": {key: value for key, value in CASE["fixed_terms"].items() if key != "lead_time_days"},
        "buyer": {
            **CASE["buyer"],
            "alternative": {
                "kind": "quote",
                "ref": "quote:ALT",
                "terms": {"unit_price": 5.0, "payment_days": 60},
            },
        },
    }

    def message_with_lead(supplier, price, lead):
        message = supplier_message(supplier, price, payment=60)
        message["attachments"][0]["text"] = (
            message["attachments"][0]["text"]
            .replace("Terms fixed by your enquiry\n", "")
            .replace("Lead time (days) 30", f"Lead time (days) {lead}")
        )
        return message

    threads = {
        "sup:A": [message_with_lead("A", 40.0, 100)],
        "sup:B": [message_with_lead("B", 41.0, 100)],
        "sup:C": [message_with_lead("C", 100.0, 30)],
    }

    decision = StrategicAgent().decide(case, threads)

    assert decision["supplier_id"] == "sup:C"
    assert decision["message"]["term_sheets"]


def test_strategic_agent_generates_complete_diverse_packages():
    threads = {"sup:A": [supplier_message("A", 44.0, payment=30)]}

    message = StrategicAgent().decide(CASE, threads)["message"]

    sheets = [sheet["terms"] for sheet in message["term_sheets"]]
    assert 1 < len(sheets) <= 3
    assert all(set(sheet) == set(CASE["negotiable"]) for sheet in sheets)
    assert len({sheet["payment_days"] for sheet in sheets}) == len(sheets)
    assert all(sheet["unit_price"] < 44.0 for sheet in sheets)


def test_strategic_agent_keeps_the_best_payment_extreme_on_a_plain_opening():
    message = StrategicAgent().decide(
        CASE,
        {"sup:A": [supplier_message("A", 44.0, payment=30)]},
    )["message"]

    assert 90 in {sheet["terms"]["payment_days"] for sheet in message["term_sheets"]}


def test_strategic_agent_tests_a_compensated_supplier_preferred_index_package():
    clause = {
        "series": "METAL",
        "material_share": 0.45,
        "deadband_pct": 3,
        "base_month": "2026-08",
    }
    case = {
        **CASE,
        "type": "raw_material",
        "negotiable": ["unit_price", "index_clause", "payment_days"],
        "fixed_terms": {key: value for key, value in CASE["fixed_terms"].items() if key != "index_clause"},
        "term_options": {
            "index_clause": [None, clause],
            "payment_days": [14, 60, 90],
        },
        "buyer": {
            **CASE["buyer"],
            "index_paths": {"METAL": 0.06},
        },
    }

    opening = supplier_message("A", 44.0, payment=14)
    opening["term_sheets"][0]["terms"]["index_clause"] = clause

    sheets = StrategicAgent().decide(case, {"sup:A": [opening]})["message"]["term_sheets"]

    assert sheets
    assert any(sheet["terms"]["index_clause"] is None for sheet in sheets)
    assert any(sheet["terms"]["index_clause"] == clause and sheet["terms"]["payment_days"] == 14 for sheet in sheets)


def test_strategic_agent_uses_cautious_anchors_for_relationship_and_raw_material_cases():
    agent = StrategicAgent()
    stabilise = {**CASE, "buyer": {**CASE["buyer"], "category_goal": "stabilise"}}
    raw_material = {**CASE, "type": "raw_material"}

    assert agent._opening_anchor(stabilise) == 0.02
    assert agent._opening_anchor(raw_material) == 0.06


def test_strategic_agent_anchors_leverage_packages_below_the_current_price():
    threads = {"sup:A": [supplier_message("A", 100.0, payment=30)]}

    message = StrategicAgent().decide(CASE, threads)["message"]

    assert message["term_sheets"]
    assert all(sheet["terms"]["unit_price"] < CASE["buyer"]["current_unit_price"] for sheet in message["term_sheets"])


def test_strategic_agent_covers_the_full_horizon_when_contract_options_allow_it():
    case = {
        **CASE,
        "negotiable": ["unit_price", "payment_days", "contract_months"],
        "fixed_terms": {key: value for key, value in CASE["fixed_terms"].items() if key != "contract_months"},
        "term_options": {
            "payment_days": [30, 60, 90],
            "contract_months": [12, 24, 36],
        },
        "buyer": {**CASE["buyer"], "horizon_years": 2},
    }

    message = StrategicAgent().decide(case, {"sup:A": [supplier_message("A", 44.0)]})["message"]

    assert message["term_sheets"]
    assert all(sheet["terms"]["contract_months"] >= 24 for sheet in message["term_sheets"])


def test_strategic_agent_anchors_from_a_package_that_covers_the_need_and_horizon():
    class Package:
        def lookup(self, ref):
            return None

        def query(self, sql, params=()):
            if "should_cost" in sql:
                return [
                    {
                        "supplier_id": "sup:A",
                        "unit_cost": 20.0,
                        "price_unit": 1,
                        "confidence_pct": 0,
                    }
                ]
            return []

    case = {
        **CASE,
        "negotiable": [
            "unit_price",
            "payment_days",
            "volume_per_year",
            "contract_months",
        ],
        "fixed_terms": {
            key: value
            for key, value in CASE["fixed_terms"].items()
            if key not in {"volume_per_year", "contract_months"}
        },
        "term_options": {
            "payment_days": [30, 60, 90],
            "volume_per_year": [800, 1_000, 1_200],
            "contract_months": [12, 24, 36],
        },
        "buyer": {
            **CASE["buyer"],
            "horizon_years": 2,
            "fallback_unit_price": 120.0,
            "category_goal": "stabilise",
            "sustainable_margin_pct": 5,
            "relationship_eur_per_point": 10_000,
        },
    }
    opening = supplier_message("A", 100.0)
    opening["term_sheets"][0]["terms"].update({"volume_per_year": 800, "contract_months": 12})

    message = StrategicAgent(package=Package()).decide(case, {"sup:A": [opening]})["message"]

    assert message["term_sheets"]
    assert all(sheet["terms"]["unit_price"] <= 99.0 for sheet in message["term_sheets"])
    assert all(sheet["terms"]["volume_per_year"] >= 1_000 for sheet in message["term_sheets"])
    assert all(sheet["terms"]["contract_months"] >= 24 for sheet in message["term_sheets"])


def test_strategic_agent_can_limit_itself_to_one_package():
    threads = {"sup:A": [supplier_message("A", 44.0, payment=30)]}

    message = StrategicAgent(max_sheets=1).decide(CASE, threads)["message"]

    assert len(message["term_sheets"]) == 1


def test_strategic_agent_never_moves_its_counteroffer_backward():
    agent = StrategicAgent()
    events = [supplier_message("A", 44.0, payment=30)]

    first = agent.decide(CASE, {"sup:A": events})["message"]
    events.extend([{**first, "from": "buyer"}, supplier_message("A", 43.0, payment=30)])
    second = agent.decide(CASE, {"sup:A": events})["message"]
    events.extend([{**second, "from": "buyer"}, supplier_message("A", 42.0, payment=30)])
    third = agent.decide(CASE, {"sup:A": events})["message"]

    def prices_by_payment(message):
        return {sheet["terms"]["payment_days"]: sheet["terms"]["unit_price"] for sheet in message["term_sheets"]}

    second_prices = prices_by_payment(second)
    third_prices = prices_by_payment(third)
    shared_options = second_prices.keys() & third_prices.keys()
    assert shared_options
    assert all(third_prices[option] >= second_prices[option] for option in shared_options)


def test_strategic_agent_rewards_a_larger_supplier_move_with_a_larger_concession():
    def next_counter(previous_supplier_price):
        events = [supplier_message("A", previous_supplier_price, payment=30)]
        events.append(
            {
                "from": "buyer",
                "term_sheets": [{"terms": {"unit_price": 35.0, "payment_days": 60}}],
            }
        )
        events.append(supplier_message("A", 43.0, payment=30))
        message = StrategicAgent().decide(CASE, {"sup:A": events})["message"]
        return max(sheet["terms"]["unit_price"] for sheet in message["term_sheets"])

    small_move = next_counter(44.0)
    large_move = next_counter(50.0)

    assert large_move > small_move


def test_strategic_agent_measures_supplier_movement_from_its_best_alternative():
    def next_counter(current_message):
        events = [supplier_message("A", 44.0, payment=30)]
        events.append(
            {
                "from": "buyer",
                "term_sheets": [{"terms": {"unit_price": 35.0, "payment_days": 60}}],
            }
        )
        events.append(current_message)
        return StrategicAgent().decide(CASE, {"sup:A": events})["message"]["term_sheets"]

    direct = supplier_message("A", 43.0, payment=60)
    alternative = supplier_message("A", 43.5, payment=30)
    alternative["term_sheets"].append(
        {
            "term_sheet_id": "offer:A-0-a",
            "terms": {"unit_price": 43.0, "payment_days": 60},
        }
    )

    assert next_counter(alternative) == next_counter(direct)


def test_strategic_agent_does_not_repeat_packages_when_the_supplier_holds():
    agent = StrategicAgent()
    events = [supplier_message("A", 44.0, payment=30)]

    first = agent.decide(CASE, {"sup:A": events})["message"]
    events.extend([{**first, "from": "buyer"}, supplier_message("A", 43.0, payment=30)])
    second = agent.decide(CASE, {"sup:A": events})["message"]
    events.extend([{**second, "from": "buyer"}, supplier_message("A", 43.0, payment=30)])
    third = agent.decide(CASE, {"sup:A": events})["message"]

    assert third["term_sheets"] != second["term_sheets"]


def test_strategic_agent_rotates_packages_at_the_alternative_ceiling():
    case = {
        **CASE,
        "buyer": {
            **CASE["buyer"],
            "alternative": {
                "kind": "quote",
                "ref": "quote:ALT",
                "terms": {"unit_price": 30.0, "payment_days": 60},
            },
        },
    }
    agent = StrategicAgent()
    events = [supplier_message("A", 44.0, payment=30)]

    first = agent.decide(case, {"sup:A": events})["message"]
    events.extend([{**first, "from": "buyer"}, supplier_message("A", 44.0, payment=30)])
    second = agent.decide(case, {"sup:A": events})["message"]

    first_packages = {json.dumps(sheet["terms"], sort_keys=True) for sheet in first["term_sheets"]}
    second_packages = {json.dumps(sheet["terms"], sort_keys=True) for sheet in second["term_sheets"]}
    assert second_packages
    assert first_packages.isdisjoint(second_packages)


def test_strategic_agent_accepts_only_a_valid_final_offer_that_beats_the_alternative():
    case = {
        **CASE,
        "buyer": {
            **CASE["buyer"],
            "alternative": {
                "kind": "quote",
                "ref": "quote:ALT",
                "terms": {"unit_price": 40.0, "payment_days": 60},
            },
        },
    }

    expensive = {"sup:A": [supplier_message("A", 42.0, payment=60, status="final_offer")]}
    cheap = {"sup:A": [supplier_message("A", 39.0, payment=60, status="final_offer")]}

    rejected = StrategicAgent().decide(case, expensive)["message"]["action"]
    accepted = StrategicAgent().decide(case, cheap)["message"]["action"]

    assert rejected == {"type": "walk_away"}
    assert accepted == {"type": "accept", "term_sheet_id": "offer:A-0"}


def test_strategic_agent_accepts_the_cheapest_final_alternative():
    final = supplier_message("A", 44.0, payment=14, status="final_offer")
    final["term_sheets"].append(
        {
            "term_sheet_id": "offer:A-0-a",
            "terms": {"unit_price": 42.0, "payment_days": 60},
        }
    )

    action = StrategicAgent().decide(CASE, {"sup:A": [final]})["message"]["action"]

    assert action == {"type": "accept", "term_sheet_id": "offer:A-0-a"}


def test_strategic_agent_never_accepts_an_unpriced_final_offer():
    message = supplier_message("A", 42.0, status="final_offer")
    message["term_sheets"][0]["terms"] = {}

    action = StrategicAgent().decide(CASE, {"sup:A": [message]})["message"]["action"]

    assert action == {"type": "walk_away"}


def test_strategic_agent_never_proposes_a_package_worse_than_the_alternative():
    case = {
        **CASE,
        "buyer": {
            **CASE["buyer"],
            "alternative": {
                "kind": "quote",
                "ref": "quote:ALT",
                "terms": {"unit_price": 30.0, "payment_days": 60},
            },
        },
    }
    threads = {"sup:A": [supplier_message("A", 44.0, payment=30)]}

    message = StrategicAgent().decide(case, threads)["message"]

    alternative_cost = total_cost(case, case["buyer"]["alternative"]["terms"])
    assert message["term_sheets"]
    assert all(total_cost(case, sheet["terms"]) <= alternative_cost for sheet in message["term_sheets"])


def test_strategic_agent_filters_required_options_before_bounding_the_search():
    case = {
        **CASE,
        "negotiable": ["unit_price", "payment_days", "lead_time_days", "contract_months"],
        "fixed_terms": {
            "volume_per_year": 1_000,
            "incoterm": "DAP",
            "tooling_eur": 0,
            "index_clause": None,
        },
        "term_options": {
            "payment_days": list(range(1, 21)),
            "lead_time_days": list(range(1, 21)),
            "contract_months": list(range(1, 21)),
        },
        "buyer": {
            **CASE["buyer"],
            "must_haves": [{"field": "payment_days", "op": ">=", "value": 20}],
        },
    }
    threads = {"sup:A": [supplier_message("A", 44.0, payment=1)]}

    message = StrategicAgent().decide(case, threads)["message"]

    assert message["term_sheets"]
    assert all(sheet["terms"]["payment_days"] == 20 for sheet in message["term_sheets"])


def test_strategic_agent_can_reuse_a_valid_supplier_standard_option():
    case = {
        **CASE,
        "term_options": {"payment_days": [14, 30]},
        "buyer": {
            **CASE["buyer"],
            "must_haves": [{"field": "payment_days", "op": ">=", "value": 60}],
        },
    }
    threads = {
        "sup:A": [supplier_message("A", 44.0, payment=60, omit_payment=True)],
    }

    message = StrategicAgent().decide(case, threads)["message"]

    assert message["term_sheets"]
    assert all(sheet["terms"]["payment_days"] == 60 for sheet in message["term_sheets"])


def test_strategic_agent_can_reuse_none_as_a_supplier_standard_option():
    clause = {
        "series": "PPI-X",
        "material_share": 0.5,
        "deadband_pct": 2,
        "base_month": "2026-01",
    }
    case = {
        **CASE,
        "negotiable": ["unit_price", "index_clause"],
        "fixed_terms": {key: value for key, value in CASE["fixed_terms"].items() if key != "index_clause"},
        "term_options": {"index_clause": [clause]},
        "buyer": {
            **CASE["buyer"],
            "must_haves": [{"field": "index_clause", "op": "==", "value": None}],
        },
    }
    opening = supplier_message("A", 44.0, payment=30)
    opening["term_sheets"][0]["terms"] = {"unit_price": 44.0}
    opening["attachments"][0]["text"] = (
        "Our standard terms\nIndex clause none\nTerms fixed by your enquiry\nLead time (days) 30\n"
    )

    message = StrategicAgent().decide(case, {"sup:A": [opening]})["message"]

    assert message["term_sheets"]
    assert all(sheet["terms"]["index_clause"] is None for sheet in message["term_sheets"])


def test_strategic_agent_keeps_working_when_the_llm_writer_fails():
    def broken_writer(system, user):
        raise RuntimeError("provider unavailable")

    threads = {"sup:A": [supplier_message("A", 44.0, payment=30)]}

    message = StrategicAgent(complete=broken_writer).decide(CASE, threads)["message"]

    assert message["subject"] == CASE["title"]
    assert message["body"]
    assert message["term_sheets"]


def test_strategic_agent_cites_a_short_notice_against_the_contract():
    case = {
        **CASE,
        "refs": ["doc:NOTICE", "ctr:AGREEMENT"],
        "facts": {
            "doc:NOTICE": {"supplier": "sup:A", "days": 51},
            "ctr:AGREEMENT": {"supplier": "sup:A", "days": 90},
        },
    }

    message = StrategicAgent().decide(case, {"sup:A": [supplier_message("A", 44.0, payment=30)]})["message"]

    assert "51 days" in message["body"]
    assert "90 days" in message["body"]
    assert message["refs"] == ["doc:NOTICE", "ctr:AGREEMENT"]


def test_strategic_agent_does_not_repeat_an_evidence_argument():
    case = {
        **CASE,
        "refs": ["doc:NOTICE", "ctr:AGREEMENT"],
        "facts": {
            "doc:NOTICE": {"supplier": "sup:A", "days": 51},
            "ctr:AGREEMENT": {"supplier": "sup:A", "days": 90},
        },
    }
    agent = StrategicAgent()
    events = [supplier_message("A", 44.0, payment=30)]

    first = agent.decide(case, {"sup:A": events})["message"]
    events.extend([{**first, "from": "buyer"}, supplier_message("A", 43.0, payment=30)])
    second = agent.decide(case, {"sup:A": events})["message"]

    assert first["refs"] == ["doc:NOTICE", "ctr:AGREEMENT"]
    assert second["refs"] == []


def test_strategic_agent_cites_supplier_delivery_performance():
    case = {
        **CASE,
        "refs": ["sc:A@2026Q3"],
        "facts": {
            "sc:A@2026Q3": {
                "supplier": "sup:A",
                "otd_pct": 85.2,
                "ppm": 712,
            }
        },
    }

    message = StrategicAgent().decide(case, {"sup:A": [supplier_message("A", 44.0, payment=30)]})["message"]

    assert "85.2%" in message["body"]
    assert message["refs"] == ["sc:A@2026Q3"]


def test_strategic_agent_discovers_recent_purchase_prices_for_the_supplier():
    class Package:
        facts = {
            "po:RECENT": {
                "supplier": "sup:A",
                "part": "part:T1",
                "date": "2026-09-30",
                "unit_price": 39.0,
                "price_unit": 1,
            },
            "po:PREVIOUS": {
                "supplier": "sup:A",
                "part": "part:T1",
                "date": "2026-09-15",
                "unit_price": 39.0,
                "price_unit": 1,
            },
        }

        def lookup(self, ref):
            return self.facts.get(ref)

        def query(self, sql, params=()):
            if "FROM po_lines" in sql:
                return [
                    {"id": "po:RECENT", "order_date": "2026-09-30"},
                    {"id": "po:PREVIOUS", "order_date": "2026-09-15"},
                ]
            return []

    message = StrategicAgent(package=Package()).decide(
        CASE,
        {"sup:A": [supplier_message("A", 44.0)]},
    )["message"]

    assert "39.00" in message["body"]
    assert message["refs"] == ["po:RECENT", "po:PREVIOUS"]


def test_strategic_agent_opens_with_the_three_strongest_raw_material_facts():
    clause = {
        "series": "AS-14404",
        "material_share": 0.45,
        "deadband_pct": 3,
        "base_month": "2026-08",
    }
    case = {
        **CASE,
        "type": "raw_material",
        "suppliers": ["sup:A"],
        "negotiable": ["unit_price", "index_clause", "payment_days"],
        "fixed_terms": {key: value for key, value in CASE["fixed_terms"].items() if key != "index_clause"},
        "term_options": {
            "index_clause": [None, clause],
            "payment_days": [14, 60, 90],
        },
        "buyer": {
            **CASE["buyer"],
            "index_paths": {"AS-14404": 0.06},
        },
        "refs": ["ctr:AGREEMENT"],
        "facts": {"ctr:AGREEMENT": {"supplier": "sup:A", "days": 60}},
    }

    class Package:
        facts = {
            "po:TARGET": {
                "supplier": "sup:A",
                "part": "part:T1",
                "date": "2026-09-18",
                "unit_price": 5.84,
                "price_unit": 1,
            },
            "po:COMPETITOR": {
                "supplier": "sup:B",
                "part": "part:T1",
                "date": "2026-09-24",
                "unit_price": 5.45,
                "price_unit": 1,
            },
            "fc:T1@2026-09": {
                "part": "part:T1",
                "snapshot_month": "2026-09",
                "change_pct": 11.9,
                "volume_per_year": 1_119,
            },
        }

        def lookup(self, ref):
            return self.facts.get(ref)

        def document(self, ref):
            assert ref == "ctr:AGREEMENT"
            return "The price follows index PPI-C2431 with an 80% material share."

        def query(self, sql, params=()):
            if "FROM po_lines" in sql:
                rows = [
                    {"id": "po:COMPETITOR", "supplier_id": "sup:B", "order_date": "2026-09-24"},
                    {"id": "po:TARGET", "supplier_id": "sup:A", "order_date": "2026-09-18"},
                ]
                return [row for row in rows if len(params) == 1 or row["supplier_id"] == params[1]]
            if "FROM forecasts" in sql:
                return [{"id": "fc:T1@2026-09"}]
            return []

    message = StrategicAgent(package=Package()).decide(
        case,
        {"sup:A": [supplier_message("A", 6.10)]},
    )["message"]

    assert message["refs"] == [
        "ctr:AGREEMENT",
        "po:COMPETITOR",
        "fc:T1@2026-09",
    ]
    assert "PPI-C2431" in message["body"]
    assert "5.45" in message["body"]
    assert "+11.9%" in message["body"]


def test_strategic_agent_discovers_volume_growth_from_the_latest_forecast():
    class Package:
        def lookup(self, ref):
            if ref == "fc:T1@2026-09":
                return {
                    "part": "part:T1",
                    "change_pct": 11.9,
                    "volume_per_year": 1_119,
                }
            return None

        def query(self, sql, params=()):
            if "FROM forecasts" in sql:
                return [{"id": "fc:T1@2026-09"}]
            return []

    message = StrategicAgent(package=Package()).decide(
        CASE,
        {"sup:A": [supplier_message("A", 44.0)]},
    )["message"]

    assert "+11.9%" in message["body"]
    assert message["refs"] == ["fc:T1@2026-09"]


def test_strategic_agent_only_calls_the_newest_forecast_latest():
    case = {
        **CASE,
        "refs": ["fc:T1@2026-03", "fc:T1@2026-09"],
        "facts": {
            "fc:T1@2026-03": {
                "part": "part:T1",
                "change_pct": 5.0,
                "volume_per_year": 1_050,
            },
            "fc:T1@2026-09": {
                "part": "part:T1",
                "change_pct": 11.9,
                "volume_per_year": 1_119,
            },
        },
    }

    message = StrategicAgent().decide(
        case,
        {"sup:A": [supplier_message("A", 44.0)]},
    )["message"]

    assert "+11.9%" in message["body"]
    assert message["refs"] == ["fc:T1@2026-09"]


def test_strategic_agent_mines_a_falling_cost_index():
    class Package:
        rows = {
            "PPI-ENERGY": [
                {"id": "idx:PPI-ENERGY@2026-08", "month": "2026-08", "value": 96.3},
                {"id": "idx:PPI-ENERGY@2025-08", "month": "2025-08", "value": 100.0},
            ]
        }

        def lookup(self, ref):
            return next(
                (row for rows in self.rows.values() for row in rows if row["id"] == ref),
                None,
            )

        def query(self, sql, params=()):
            if "cost_drivers" in sql:
                return [{"series_id": "PPI-ENERGY", "weight": 0.2}]
            if "index_values" in sql:
                return self.rows[params[0]]
            return []

    message = StrategicAgent(package=Package()).decide(CASE, {"sup:A": [supplier_message("A", 44.0, payment=30)]})[
        "message"
    ]

    assert "-3.7%" in message["body"]
    assert message["refs"] == [
        "idx:PPI-ENERGY@2025-08",
        "idx:PPI-ENERGY@2026-08",
    ]


def test_strategic_agent_does_not_repeat_evidence_across_suppliers():
    class Package:
        def lookup(self, ref):
            return {"id": ref, "month": ref[-7:], "value": 100.0}

        def query(self, sql, params=()):
            if "cost_drivers" in sql:
                return [{"series_id": "PPI-ENERGY", "weight": 0.2}]
            if "index_values" in sql:
                return [
                    {"id": "idx:PPI-ENERGY@2026-08", "month": "2026-08", "value": 96.3},
                    {"id": "idx:PPI-ENERGY@2025-08", "month": "2025-08", "value": 100.0},
                ]
            return []

    case = {**CASE, "suppliers": ["sup:A", "sup:B"]}
    threads = {
        "sup:A": [supplier_message("A", 44.0, payment=30)],
        "sup:B": [supplier_message("B", 44.0, payment=30)],
    }
    agent = StrategicAgent(package=Package())

    first = agent.decide(case, threads)
    first_events = threads[first["supplier_id"]]
    first_events.extend(
        [
            {**first["message"], "from": "buyer"},
            supplier_message(first["supplier_id"].removeprefix("sup:"), 43.0, payment=30),
        ]
    )
    second = agent.decide(case, threads)

    assert first["message"]["refs"]
    assert second["supplier_id"] != first["supplier_id"]
    assert second["message"]["refs"] == []


def test_strategic_agent_keeps_the_exact_evidence_when_the_writer_paraphrases():
    case = {
        **CASE,
        "refs": ["doc:NOTICE", "ctr:AGREEMENT"],
        "facts": {
            "doc:NOTICE": {"supplier": "sup:A", "days": 51},
            "ctr:AGREEMENT": {"supplier": "sup:A", "days": 90},
        },
    }

    def writer(system, user):
        return json.dumps(
            {
                "subject": "Notice period",
                "body": (
                    "The notice is 10 days under 20 days (doc:NOTICE, ctr:AGREEMENT). "
                    "The announced change provides 51 days of notice, below the 90 days "
                    "required by our contract (doc:NOTICE, ctr:AGREEMENT)."
                ),
            }
        )

    message = StrategicAgent(complete=writer).decide(case, {"sup:A": [supplier_message("A", 44.0, payment=30)]})[
        "message"
    ]

    assert "51 days" in message["body"]
    assert "90 days" in message["body"]
    assert "10 days" not in message["body"]


def test_strategic_agent_uses_llm_only_for_email_copy():
    def writer(system, user):
        request = json.loads(user)
        assert request["action"] == "counter"
        assert request["packages"]
        return json.dumps({"subject": "Three options", "body": "Please review these packages."})

    threads = {"sup:A": [supplier_message("A", 44.0, payment=30)]}

    message = StrategicAgent(complete=writer).decide(CASE, threads)["message"]

    assert message["subject"] == "Three options"
    assert message["body"] == "Please review these packages."
    assert message["term_sheets"]  # Packages still come from deterministic code.


def test_agent_factory_builds_the_strategic_agent(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "test-model")

    agent = agent_factory("strategic", None)()

    assert isinstance(agent, StrategicAgent)


def test_agent_factory_builds_strategic_without_llm_configuration(monkeypatch):
    for name in ("LLM_MODEL", "LLM_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    agent = agent_factory("strategic", None)()

    assert isinstance(agent, StrategicAgent)
    assert agent.complete is None


def test_strategic_agent_plays_a_complete_case_through_the_api(sim):
    url, _, _ = sim
    anonymous = ArenaClient(url)
    key = anonymous.register("strategic-test", "sim")["api_key"]
    client = ArenaClient(url, key, rate=1_000)

    result = play(RemoteTable(client), "case:S01", StrategicAgent())

    assert result["ended"]
    assert result["deal"]
    assert 0 <= result["score"] <= 1
