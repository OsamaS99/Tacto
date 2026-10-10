"""EconomicEvaluator: the one place the buyer turns terms into euros. Normal moves, fallbacks and
acceptance all price through it, on the published cost function (`cost.total_cost`) with each
supplier's own standard terms. Supplier economics are estimates: the relationship term is priced
in three scenarios (low / mid / high C), never as a fact."""

import math

from ..cost import RELATIONSHIP_GOALS, closing_terms, supplier_value, total_cost
from .records import CandidatePackage, EvaluatedPackage, SupplierEstimate

SCENARIO = {"low": 0, "mid": 1, "high": 2}  # index into SupplierEstimate.full_cost; high = dearest C


def estimate_supplier(dossier, sid, price, cash_pressure=False):
    """Full cost from the supplier's should-cost row, else the generic one, else 92 % of its price
    (the should-cost error of 5-15 % sets the spread). Cash pressure raises its cost of money."""
    found = dossier.should_cost.get(sid) or dossier.should_cost.get("*") if dossier else None
    if found:
        cost, conf = found
        spread, source = max(conf, 5.0) / 100, "should-cost"
    else:
        cost, spread, source = 0.92 * price, 0.07, "default"
    return SupplierEstimate(
        full_cost=(cost * (1 - spread), cost, cost * (1 + spread)),
        interest_rate=0.12 if cash_pressure else 0.08,
        source=source,
    )


class EconomicEvaluator:
    def __init__(self, case, std=None, estimates=None):
        self.case = case
        self.std = std or {}  # sid -> standard terms from its quote PDFs
        self.estimates = estimates or {}  # sid -> SupplierEstimate
        self.relationship = case["buyer"].get("category_goal") in RELATIONSHIP_GOALS and bool(self.estimates)

    def standard(self, sid):
        """The supplier's standard terms, completed from the case, as `cost.supplier_value` reads them."""
        return closing_terms(self.case, {"unit_price": 0.0}, self.std.get(sid))

    def supplier(self, sid, scenario="mid"):
        est = self.estimates.get(sid)
        if not self.relationship or est is None:
            return None
        return {
            "standard": self.standard(sid),
            "full_cost": est.full_cost[SCENARIO[scenario]],
            "interest_rate": est.interest_rate,
        }

    def c(self, sid, terms, scenario="mid"):
        return total_cost(self.case, terms, self.std.get(sid), self.supplier(sid, scenario))

    def c_alt(self):
        alt = self.case["buyer"].get("alternative") or {}
        return total_cost(self.case, alt["terms"]) if alt.get("terms", {}).get("unit_price") else math.inf

    def cash_theirs(self, sid, terms):
        est = self.estimates.get(sid)
        supplier = {"standard": self.standard(sid), "interest_rate": est.interest_rate if est else 0.08}
        return supplier_value(self.case, terms, supplier)

    def evaluate(self, pkg: CandidatePackage) -> EvaluatedPackage:
        return EvaluatedPackage(
            pkg=pkg,
            c=self.c(pkg.sid, pkg.terms),
            c_high=self.c(pkg.sid, pkg.terms, "high"),
            cash_theirs=self.cash_theirs(pkg.sid, pkg.terms),
        )

    def best_price(self, sid, terms):
        """The price below which C rises again (relationship goals: squeezing costs); 0 without."""
        if not self.relationship:
            return 0.0
        lo, hi = 0.3 * terms["unit_price"], 1.5 * terms["unit_price"]
        for _ in range(60):  # golden section: C is convex in the price
            a, b = lo + (hi - lo) * 0.382, lo + (hi - lo) * 0.618
            if self.c(sid, {**terms, "unit_price": a}) <= self.c(sid, {**terms, "unit_price": b}):
                hi = b
            else:
                lo = a
        return round((lo + hi) / 2, 2)

    def price_for(self, sid, terms, target_c):
        """The highest price (cents, rounded down) at which `terms` costs at most `target_c`, not
        below `best_price`; None if even that price costs more."""
        floor = self.best_price(sid, terms)
        p0 = max(terms["unit_price"], floor + 1.0)
        c0, c1 = self.c(sid, {**terms, "unit_price": p0}), self.c(sid, {**terms, "unit_price": p0 + 1})
        if c1 - c0 <= 0:
            return None
        if not self.relationship:  # C is linear in the price
            p = p0 + (target_c - c0) / (c1 - c0)
        else:  # increasing above best_price: bisect
            lo, hi = floor, p0
            while self.c(sid, {**terms, "unit_price": hi}) < target_c:
                hi *= 1.5
            for _ in range(60):
                mid = (lo + hi) / 2
                lo, hi = (mid, hi) if self.c(sid, {**terms, "unit_price": mid}) <= target_c else (lo, mid)
            p = lo
        p = math.floor(p * 100) / 100
        if p <= 0 or p < floor or self.c(sid, {**terms, "unit_price": p}) > target_c + 0.01:
            return None
        return p
