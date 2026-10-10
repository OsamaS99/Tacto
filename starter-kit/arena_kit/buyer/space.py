"""ContractSpace: what a term sheet may contain in this case - the negotiable fields, the values
each may take (the case's options plus the supplier's own standard value), the fixed terms that
never go into a sheet, and the must-haves a deal has to keep."""

import itertools
import math

from ..cost import closing_terms, must_haves_ok

MAX_COMBOS = 20_000


def _unique(values):
    out = []
    for v in values:
        if v not in out:  # dicts (index clauses) are unhashable: compare by value
            out.append(v)
    return out


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
        n = 1
        for v in values:
            n *= len(v)
        if n > MAX_COMBOS:
            return None
        return [dict(zip(self.levers, combo, strict=True)) for combo in itertools.product(*values)]

    def complete(self, terms, std, cost):
        """A full term sheet: exactly the negotiable fields. Fields `terms` leaves out get the value
        with the lowest `cost(terms)` (coordinate descent, two passes); every value is snapped."""
        out = {f: self.snap(f, terms[f], std) for f in self.levers if f in terms}
        out["unit_price"] = round(float(terms["unit_price"]), 2)
        free = [f for f in self.levers if f not in terms]
        for f in free:  # a start value, so every field is present before the descent
            out[f] = self.options(f, std)[0]

        def key(t):  # a must-have broken costs everything
            return cost(t) if must_haves_ok(self.case, t, std) else math.inf

        for _ in range(2):
            for f in free:
                out[f] = min(self.options(f, std), key=lambda v, f=f: key({**out, f: v}))
        return {f: out[f] for f in self.negotiable}

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
