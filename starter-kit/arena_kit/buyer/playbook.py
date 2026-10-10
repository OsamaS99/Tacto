"""CasePlaybook: the procurement policy for a case - the mode from the case type, the goal from
the category strategy. Both apply independently: the mode says what to argue and trade, the goal
how hard to hold and how much the relationship weighs. Starting values, tuned in the simulator."""

from dataclasses import replace

from .records import CasePlaybook

# mode -> (levers in trade priority, what the mode argues)
MODES = {
    "price_increase": (
        ("effective_date", "payment_days", "contract_months", "volume_per_year"),
        "Defend the increase: driver indices that fell, notice shorter than the contract allows, "
        "should-cost below the ask. A later effective date is worth money. Close once the increase is "
        "at or below what the cost drivers justify.",
    ),
    "renewal": (
        ("contract_months", "incoterm", "payment_days", "volume_per_year"),
        "Renew without paying more: cover the whole horizon, free delivery, longer payment. Cite "
        "delivery performance below target and cheaper quotes.",
    ),
    "rfq": (
        ("incoterm", "payment_days", "lead_time_days", "contract_months"),
        "Make the bidders compete: every bidder gets an early answer, a cheaper rival quote may be "
        "cited once to each other bidder. Close on the best total cost.",
    ),
    "bundling": (
        ("volume_per_year", "contract_months", "payment_days", "incoterm"),
        "Trade volume and contract length for price across the bundle; few rounds.",
    ),
    "raw_material": (
        ("index_clause", "contract_months", "payment_days", "incoterm"),
        "Choose the index clause on its cost: a falling index wants a high material share and no "
        "deadband, a rising one no clause or a wide deadband. Cite the index moves in our favour.",
    ),
    "capacity": (
        ("volume_per_year", "contract_months", "lead_time_days", "payment_days"),
        "Secure the capacity: commit volume and length for price, lead time within the need. Faster "
        "payment is cheap for us and worth much to a supplier short of cash.",
    ),
    "quality": (
        ("ppm_commitment", "chargeback_eur_per_complaint", "lead_time_days", "payment_days"),
        "Fix quality before price: a lower ppm commitment, a chargeback up to our cost per defect, an "
        "8D report. Cite the scorecard and the complaints.",
    ),
}

# goal -> beta, k, min_rounds_accept, max_rounds (None: round_limit - 1), p_walk, mirror_final, budget
GOALS = {
    "leverage": (0.3, 0.5, 3, 5, 0.25, True, 25.0),
    "stabilise": (0.8, 0.9, 2, 5, 0.40, False, 25.0),
    "secure_supply": (1.0, 1.0, 1, 5, 0.50, False, 20.0),
    "quality_first": (0.5, 0.7, 2, 5, 0.35, True, 25.0),
    "routine": (0.6, 0.8, 1, 5, 0.30, True, 15.0),
}


def playbook(case, adjust=None):
    """The policy for `case`; `adjust`: bounded overrides (e.g. from the preparation call)."""
    mode = case.get("type") if case.get("type") in MODES else "price_increase"
    goal = case["buyer"].get("category_goal") if case["buyer"].get("category_goal") in GOALS else "leverage"
    levers, focus = MODES[mode]
    beta, k, min_acc, max_rounds, p_walk, mirror, budget = GOALS[goal]
    limit = case.get("round_limit", 10)
    rfq = len(case.get("suppliers", [])) > 1
    pb = CasePlaybook(
        mode=mode,
        goal=goal,
        beta=beta,
        k=k,
        min_rounds_accept=min(min_acc, limit - 1),
        max_rounds=min(max_rounds or limit - 1, limit - 1),
        p_walk=p_walk,
        mirror_final=mirror,
        relationship=goal in ("stabilise", "secure_supply"),
        llm_budget_s=budget,
        send_margin_s=15.0 if rfq else 10.0,
        levers=tuple(f for f in levers if f in case["negotiable"]),
        focus=focus,
    )
    return replace(pb, **bounded(pb, adjust or {}))


def bounded(pb, adjust):
    """Only these fields may change, and only within these bounds."""
    limits = {"beta": (0.2, 1.5), "k": (0.3, 1.0), "p_walk": (0.1, 0.7)}
    out = {}
    for name, (lo, hi) in limits.items():
        if isinstance(adjust.get(name), int | float):
            out[name] = min(hi, max(lo, float(adjust[name])))
    return out
