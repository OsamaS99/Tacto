"""ContractSpace: what a term sheet may contain in this case - the negotiable fields, the values
each may take (the case's options plus the supplier's own standard value), the fixed terms that
never go into a sheet, and the must-haves a deal has to keep."""

import itertools
import math

from ..cost import closing_terms, must_haves_ok

MAX_COMBOS = 20_000
EXACT = 600  # free combinations up to this are enumerated; beyond, coordinate descent
# levers that ask more of the supplier the higher (+1) or the lower (-1) the value
DIRECTION = {"payment_days": 1, "chargeback_eur_per_complaint": 1, "lead_time_days": -1, "ppm_commitment": -1}


def _unique(values):
    out = []
    for v in values:
        if v not in out:  # dicts (index clauses) are unhashable: compare by value
            out.append(v)
    return out


def _product(values):
    n = 1
    for v in values:
        n *= len(v)
    return n


class ContractSpace:
    def __init__(self, case):
        self.case = case
        self.negotiable = list(case["negotiable"])
        self.levers = [f for f in self.negotiable if f != "unit_price"]
        self.fixed = case.get("fixed_terms", {})

    def options(self, field, std=None):
        """Values `field` may take: the case's options, the supplier's standard value, else the
        buyer's plan value (a field with no options and no standard has exactly one value)."""
        opts = list(self.case.get("term_options", {}).get(field) or [])
        if std and std.get(field) is not None:
            opts.append(std[field])
        if not opts:
            opts.append(closing_terms(self.case, {"unit_price": 0.0}, std).get(field))
        return _unique(opts)

    def combos(self, std=None):
        """Every combination of the non-price fields (dicts without unit_price); None if too many."""
        values = [self.options(f, std) for f in self.levers]
        if _product(values) > MAX_COMBOS:
            return None
        return [dict(zip(self.levers, combo, strict=True)) for combo in itertools.product(*values)]

    def complete(self, terms, std, cost, prefer=None):
        """A full term sheet: exactly the negotiable fields. Fields `terms` leaves out get the values
        with the lowest `cost(terms)` that keep the must-haves (exact when the combinations are few,
        else coordinate descent); every value is snapped. Among values that cost the same, the one
        nearest the supplier's own terms (`prefer`, else its standard terms): a give that costs us nothing
        (case P10 asked a faster delivery worth 0 to us, and paid for it in the price)."""
        out = {f: self.snap(f, terms[f], std) for f in self.levers if f in terms}
        out["unit_price"] = round(float(terms["unit_price"]), 2)
        free = [f for f in self.levers if f not in terms]
        values = [self.options(f, std) for f in free]
        ref = {**closing_terms(self.case, {"unit_price": 0.0}, std), **(prefer or {})}

        def key(t):  # a must-have broken costs everything
            c = round(cost(t), 2) if must_haves_ok(self.case, t, std) else math.inf
            return c, sum(self.demand(t, ref, std).values())

        if _product(values) <= EXACT:
            fills = (dict(zip(free, combo, strict=True)) for combo in itertools.product(*values))
            out.update(min(fills, key=lambda fill: key({**out, **fill})))
        else:
            out.update({f: v[0] for f, v in zip(free, values, strict=True)})
            for _ in range(2):
                for f, opts in zip(free, values, strict=True):
                    out[f] = min(opts, key=lambda v, f=f: key({**out, f: v}))
        return {f: out[f] for f in self.negotiable}

    def demand(self, terms, ref, std=None):
        """Per lever, what `terms` asks of the supplier beyond its own terms `ref`, 0..1: how far past
        them in the direction that costs it (longer payment, faster delivery, ...; where `ref` is
        silent: past its easiest option); 1 for any change of a lever without a clear direction
        (contract length, volume, a clause), 0 where `ref` is silent on it."""
        out = {}
        for f in self.levers:
            v, r = terms.get(f), ref.get(f)
            if f in DIRECTION and isinstance(v, int | float):
                opts = [o for o in self.options(f, std) if isinstance(o, int | float)]
                r = r if isinstance(r, int | float) else (min if DIRECTION[f] > 0 else max)(opts)
                out[f] = max(0.0, DIRECTION[f] * (v - r)) / ((max(opts + [r]) - min(opts + [r])) or 1)
            else:
                out[f] = 0.0 if r is None or v == r else 1.0
        return out

    def snap(self, field, value, std=None):
        opts = self.options(field, std)
        if value in opts:
            return value
        if isinstance(value, int | float) and all(isinstance(o, int | float) for o in opts):
            return min(opts, key=lambda o: (abs(o - value), o))
        return opts[0]

    def valid(self, terms, std=None):
        """Why a term sheet would be refused or must not be sent ([] if fine)."""
        errors = []
        if set(terms) != set(self.negotiable):
            errors.append(f"fields {sorted(terms)} != negotiable {sorted(self.negotiable)}")
        price = terms.get("unit_price")
        if not isinstance(price, int | float) or price <= 0 or round(price, 2) != price:
            errors.append(f"unit_price {price!r} must be > 0 with 2 decimals")
        for f in self.levers:
            if f in terms and terms[f] not in self.options(f, std):
                errors.append(f"{f}={terms[f]!r} is not an allowed value")
        if not errors and not must_haves_ok(self.case, terms, std):
            errors.append("breaks a must-have")
        return errors
