"""The counterparty, as observed: how far its sheets moved in our C, whether it holds or moved
back. Facts from the thread only; the Decision Selector may add a label, never a figure."""

import math

from .records import SupplierAssessment

FIRM_STEP = 0.002  # a move of less than 0.2 % of its C counts as holding


def assess(t, label=None):
    """`t`: SupplierThreadState. `label`: optional {intent, cash_pressure, values} from a classifier."""
    cs = [c for c in t.their_c if math.isfinite(c)]  # a sheet without a price has no C
    steps = [a - b for a, b in zip(cs, cs[1:], strict=False)]  # + = in our favour
    last = steps[-1] if steps else 0.0
    unit = FIRM_STEP * (cs[0] if cs else 1.0)
    firmness = 0
    for s in reversed(steps):
        if s > unit:
            break
        firmness += 1
    observation = getattr(t, "observation", None)
    label = {**(observation.labels() if observation is not None else {}), **(label or {})}
    walk_risk = 0.6 if t.status == "final_offer" else 0.15 + 0.1 * min(firmness, 3)
    if label.get("intent") == "walk_threat" or label.get("firm_final"):
        walk_risk = max(walk_risk, 0.6)
    return SupplierAssessment(
        concession=last,
        firmness=min(firmness, 3),
        regressed=last < -unit,
        walk_risk=walk_risk,
        intent=label.get("intent"),
        cash_pressure=bool(label.get("cash_pressure")),
        values=tuple(label.get("values") or ()),
        source="decisions" if label else "rules",
    )


def pace(t, n=2):
    """Its mean step in our C over the last `n` moves (>= 0): what it may still give per round."""
    cs = [c for c in t.their_c if math.isfinite(c)]
    steps = [a - b for a, b in zip(cs, cs[1:], strict=False)][-n:]
    return max(0.0, sum(steps) / len(steps)) if steps else 0.0
