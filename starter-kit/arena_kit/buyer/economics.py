"""EconomicEvaluator: the one place the buyer turns terms into euros. Normal moves, fallbacks and
acceptance all price through it, on the published cost function (`cost.total_cost`) with each
supplier's own standard terms. Supplier economics are estimates: the relationship term is priced
in three scenarios (low / mid / high C), never as a fact."""

import math

from ..cost import RELATIONSHIP_GOALS, closing_terms, supplier_value, total_cost
from .records import CandidatePackage, EvaluatedPackage, SupplierEstimate
from .space import finite_number

SCENARIO = {"low": 0, "mid": 1, "high": 2}  # index into SupplierEstimate.full_cost; high = dearest C
LEAD_DAY = 0.003  # share of its price a supplier charges per day of delivery faster than it quoted


def estimate_supplier(dossier, sid, price, cash_pressure=False, cap=None):
    """Dated cost evidence, or a weak prior from the opening price. The legacy `cap` is ignored:
    an offer does not establish manufacturing cost or a sustainable margin."""
    if not finite_number(price) or price <= 0:
        raise ValueError("opening price must be positive and finite")
    found = (dossier.should_cost.get(sid) or dossier.should_cost.get("*")) if dossier else None
    if found:
        cost, conf = found
        if not finite_number(cost) or cost <= 0 or not finite_number(conf) or not 0 <= conf < 100:
            raise ValueError("invalid should-cost or error range")
        spread = max(conf, 5.0) / 100
        source = "supplier-should-cost" if sid in dossier.should_cost else "generic-should-cost"
    else:
        cost, spread, source = 0.92 * price, 0.07, "default"
    return SupplierEstimate(
        full_cost=(cost * (1 - spread), cost, cost * (1 + spread)),
        interest_rate=0.12 if cash_pressure else 0.08,
        source=source,
    )


class EconomicEvaluator:
    def __init__(self, case, std=None, estimates=None, quoted=None):
        self.case = case
        self.std = std or {}  # sid -> standard terms from its quote PDFs
        self.estimates = estimates or {}  # sid -> SupplierEstimate
        self.quoted = quoted or {}  # sid -> {ppm, lead}: the defect rate and delivery it quoted itself
        self.relationship = case["buyer"].get("category_goal") in RELATIONSHIP_GOALS and bool(self.estimates)

    def standard(self, sid):
        """The supplier's standard terms, completed from the case, as `cost.supplier_value` reads them."""
        return closing_terms(self.case, {"unit_price": 0.0}, self.std.get(sid))

    def risk_scenario(self, sid):
        """Use the upper cost bound for supplier-specific evidence; midpoint for weak priors."""
        est = self.estimates.get(sid)
        return "high" if est is not None and est.source == "supplier-should-cost" else "mid"

    def supplier(self, sid, scenario=None):
        """Our estimate of the supplier for the relationship term; scenario "none" leaves it out."""
        est = self.estimates.get(sid)
        if not self.relationship or est is None or scenario == "none":
            return None
        scenario = scenario or self.risk_scenario(sid)
        return {
            "standard": self.standard(sid),
            "full_cost": est.full_cost[SCENARIO[scenario]],
            "interest_rate": est.interest_rate,
        }

    def c(self, sid, terms, scenario=None):
        """One consistent policy scenario. Malformed prices or non-finite costs are infeasible."""
        if not finite_number(terms.get("unit_price")) or terms["unit_price"] <= 0:
            return math.inf
        try:
            c = total_cost(self.case, terms, self.std.get(sid), self.supplier(sid, scenario))
        except (ArithmeticError, TypeError, ValueError, KeyError):
            return math.inf
        return c if finite_number(c) else math.inf

    def c_alt(self):
        """C of the alternative; infinite without one (`kind: none`, or terms without a price)."""
        alt = self.case["buyer"].get("alternative") or {}
        terms = alt.get("terms") or {}
        if not finite_number(terms.get("unit_price")) or terms["unit_price"] <= 0:
            return math.inf
        try:
            c = total_cost(self.case, terms)
        except (ArithmeticError, TypeError, ValueError, KeyError):
            return math.inf
        return c if finite_number(c) else math.inf

    def cash_theirs(self, sid, terms):
        """What a sheet is worth to the supplier per unit. A chargeback costs it at the defect rate it
        quoted (case P11: standard terms without a ppm made a 500 EUR chargeback look free, and it
        charged 2.50 EUR a unit for it); a delivery faster than it quoted costs it LEAD_DAY of the
        price per day (case P10: it charged about 0.16 EUR a unit per day)."""
        est = self.estimates.get(sid)
        supplier = {"standard": self.standard(sid), "interest_rate": est.interest_rate if est else 0.08}
        q = self.quoted.get(sid) or {}
        if q.get("ppm") and q["ppm"] > (supplier["standard"].get("ppm_commitment") or 0):
            supplier["ppm"] = q["ppm"]
        if q.get("lead") and terms.get("lead_time_days") is not None:
            supplier["extra"] = -LEAD_DAY * terms["unit_price"] * max(0, q["lead"] - terms["lead_time_days"])
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
        if self.supplier(sid) is None:
            return 0.0
        lo, hi = 0.01, max(1.5 * terms["unit_price"], 1.0)
        for _ in range(30):
            if self.c(sid, {**terms, "unit_price": hi}) <= self.c(sid, {**terms, "unit_price": hi * 1.5}):
                break
            hi *= 1.5
        for _ in range(60):  # golden section: C is convex in the price
            a, b = lo + (hi - lo) * 0.382, lo + (hi - lo) * 0.618
            if self.c(sid, {**terms, "unit_price": a}) <= self.c(sid, {**terms, "unit_price": b}):
                hi = b
            else:
                lo = a
        return math.ceil((lo + hi) / 2 * 100) / 100

    def price_for(self, sid, terms, target_c):
        """The highest price in cents at which `terms` costs at most `target_c` (+1 cent: C is rounded),
        not below `best_price`; None if even that price costs more."""
        if not finite_number(target_c) or not finite_number(terms.get("unit_price")):
            return None
        floor = self.best_price(sid, terms)
        p0 = max(terms["unit_price"], floor + 1.0)
        c0, c1 = self.c(sid, {**terms, "unit_price": p0}), self.c(sid, {**terms, "unit_price": p0 + 1})
        if not finite_number(c0) or not finite_number(c1) or c1 - c0 <= 0:
            return None
        if not floor:  # no relationship term: C is linear in the price
            p = p0 + (target_c - c0) / (c1 - c0)
        else:  # increasing above best_price: bisect
            lo, hi = floor, p0
            for _ in range(60):
                if self.c(sid, {**terms, "unit_price": hi}) >= target_c:
                    break
                hi *= 1.5
            else:
                return None
            for _ in range(60):
                mid = (lo + hi) / 2
                lo, hi = (mid, hi) if self.c(sid, {**terms, "unit_price": mid}) <= target_c else (lo, mid)
            p = lo
        p = round(p, 2)
        for _ in range(3):  # the nearest cent, else the cents below it, until C fits
            if self.c(sid, {**terms, "unit_price": p}) <= target_c + 0.01:
                return p if p > 0 and p >= floor else None
            p = round(p - 0.01, 2)
        return None
