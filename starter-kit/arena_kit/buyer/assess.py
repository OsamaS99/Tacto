"""The counterparty, as observed: how far its sheets moved in our C, whether it holds or moved
back. Facts from the thread only; the Decision Selector may add a label, never a figure."""

from .records import SupplierAssessment

FIRM_STEP = 0.002  # a move of less than 0.2 % of its C counts as holding


def assess(t, label=None):
    """`t`: SupplierThreadState. `label`: optional {intent, cash_pressure, values} from a classifier."""
    steps = [a - b for a, b in zip(t.their_c, t.their_c[1:], strict=False)]  # + = in our favour
    last = steps[-1] if steps else 0.0
    firmness = 0
    for s in reversed(steps):
        if s > FIRM_STEP * (t.their_c[0] or 1):
            break
        firmness += 1
    label = label or {}
    walk_risk = 0.6 if t.status == "final_offer" else 0.15 + 0.1 * min(firmness, 3)
    if label.get("intent") == "walk_threat":
        walk_risk = max(walk_risk, 0.6)
    return SupplierAssessment(
        concession=last,
        firmness=min(firmness, 3),
        regressed=last < -FIRM_STEP * (t.their_c[0] or 1),
        walk_risk=walk_risk,
        intent=label.get("intent"),
        cash_pressure=bool(label.get("cash_pressure")),
        values=tuple(label.get("values") or ()),
        source="decisions" if label else "rules",
    )
