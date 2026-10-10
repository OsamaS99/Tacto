"""BidGenerator: how much to concede this turn (in our C, on our own package) and the complete
term sheets that cost exactly that. Concessions follow the supplier's moves (reciprocity) and
the clock (an aspiration curve), never pass half the remaining gap and never go backwards - a
supplier who moves back meets a hold."""

import math
import statistics

from .records import CandidatePackage

MAX_COMBOS = 300  # beyond this: the C-optimal combination and its one-field neighbours


def anchor_price(case, dossier, their_price, sid=None):
    """Our opening price: the lowest defensible figure - never above the current price in a price
    defence or a renewal, never below what the data supports."""
    b = case["buyer"]
    current = b.get("current_unit_price")
    options = [0.85 * their_price]
    if current:
        justified = dossier.justified_move_pct if dossier and dossier.justified_move_pct is not None else 0.0
        options.append(current * (1 + max(0.0, justified) / 100))
        settled = [x for v in (dossier.settled_pct.values() if dossier else []) for x in v]
        if settled:
            options.append(current * (1 + max(0.0, statistics.median(settled)) / 100))
    sc = dossier and (dossier.should_cost.get(sid) or dossier.should_cost.get("*"))
    if sc and sc[0] < their_price:
        options.append(sc[0])
    price = min(options)
    if current and case.get("type") in ("price_increase", "renewal"):
        price = min(price, current)
    return round(max(price, 0.5 * their_price), 2)


def best_combo(ev, space, sid, price, prefer=None):
    """The term sheet with the lowest C for us at `price` (ties: nearest the supplier's `prefer`)."""
    return space.complete({"unit_price": price}, ev.std.get(sid), lambda t: ev.c(sid, t), prefer)


def targets(case, dossier, ev, space, sid, opening_price, c_alt, market=None, *, market_c=None):
    """(C at our anchor, C we never bid beyond) for one supplier: the anchor on our best terms (not
    below the price that squeezes a strategic supplier), and at most its opening price on our best
    terms or the alternative, whichever is cheaper. `market_c` is comparable feasible opening
    total cost. Legacy `market` is ignored because raw prices are not comparable packages."""
    anchor = anchor_price(case, dossier, opening_price, sid)
    floor = ev.best_price(sid, best_combo(ev, space, sid, anchor))
    c_target = ev.c(sid, best_combo(ev, space, sid, max(anchor, floor)))
    c_res = min(c_alt, ev.c(sid, best_combo(ev, space, sid, opening_price)))
    if market_c is not None and math.isfinite(market_c):
        c_target = min(c_target, market_c)
    c_target = min(c_target, c_res)
    return c_target, c_res - 0.01 * max(0.0, c_res - c_target)


def aspiration(c_target, c_res, tau, beta):
    """C we are prepared to reach by time `tau` (0..1): Boulware below beta 1, linear at 1."""
    return c_target + (c_res - c_target) * min(1.0, max(0.0, tau)) ** (1 / beta)


def next_bid(pb, t, a, c_target, c_res, c_their_on_ours, tau, hold=False):
    """Our C this turn. `t`: SupplierThreadState, `a`: SupplierAssessment, `c_their_on_ours`: their
    price on our best terms - we never offer more than half the gap to it at once. `hold`: no
    concession this turn (a thread we only keep alive)."""
    if not t.my_c:
        return min(c_target, c_res)
    last = t.my_c[-1]
    if a.regressed or hold:  # it moved back: we hold - taking a concession back reads as going
        return min(last, c_res)
    step = pb.k * max(0.0, a.concession)  # reciprocity: their move, in our C
    bid = max(aspiration(c_target, c_res, tau, pb.beta), last + step)
    bid = min(bid, last + 0.5 * max(0.0, c_their_on_ours - last), c_res)
    return min(c_res, max(bid, last))  # current economic limits outrank reciprocity


def generate(ev, space, sid, target_c, their_price, standing=None):
    """Complete term sheets that cost us `target_c` (within a cent): the C-optimal one plus
    variants that give the supplier more for the same C. Bounded in C, not in price - a dearer
    price on terms that save us more is fine."""
    if not math.isfinite(target_c):
        return []
    std = ev.std.get(sid)
    base = best_combo(ev, space, sid, their_price, standing)
    combos = space.combos(std)
    if combos is None or len(combos) > MAX_COMBOS:
        combos = [base] + [{**base, f: v} for f in space.levers for v in space.options(f, std) if v != base[f]]
    if standing:  # the supplier's own terms with our price: easy for it to say yes to
        combos.append({**base, **{f: v for f, v in standing.items() if f in space.levers}})
    out = []
    for combo in combos:
        terms = {**combo, "unit_price": their_price}
        terms = {f: terms[f] for f in space.negotiable}
        if space.valid(terms, std):
            continue
        price = ev.price_for(sid, terms, target_c)
        if price is None:
            continue
        out.append(ev.evaluate(CandidatePackage(sid, {**terms, "unit_price": price}, "terms")))
    return out


def select(
    evaluated,
    demand=None,
    ceiling=None,
    n=3,
    *,
    values=(),
    conditions=(),
    supplier_floor=None,
    reference=None,
):
    """Up to `n` diverse sheets: the one closest to our ideal terms (the highest price - simple
    suppliers read the price), the one worth most to the supplier, and one more. `demand(terms)`:
    per lever, how much a sheet asks of the supplier - a sheet that asks more than another worth no
    more to the supplier is never sent, ties go to the smaller ask. `ceiling`: the supplier's own price -
    no sheet shows a dearer one while a sheet at or below it exists (cases P02, P18: it answered
    near our dearest shown price)."""
    evaluated = [e for e in evaluated if math.isfinite(e.c) and math.isfinite(e.cash_theirs)]
    if not evaluated:
        return []
    if ceiling is not None:
        below = [e for e in evaluated if e.terms["unit_price"] <= ceiling]
        evaluated = below or [min(evaluated, key=lambda e: e.terms["unit_price"])]
    pairs = [(e, demand(e.terms) if demand else {}) for e in evaluated]

    def condition_rank(e):
        rank = []
        for fields in conditions:
            relevant = [f for f in fields if f != "unit_price" and f in e.terms and f in (reference or {})]
            matched = sum(e.terms[f] == reference[f] for f in relevant)
            rank.extend((bool(relevant) and matched == len(relevant), matched))
        return tuple(rank)

    def preference(e):
        if not reference:
            return 0
        score = 0
        for field in values:
            value, live = e.terms.get(field), reference.get(field)
            if isinstance(value, int | float) and isinstance(live, int | float):
                if field == "payment_days":
                    score += live - value
                elif field in ("volume_per_year", "contract_months", "lead_time_days"):
                    score += value - live
            elif field in e.terms and field in reference:
                score += int(value == live)
        return score

    max_condition = max((condition_rank(e) for e, _ in pairs), default=())
    pairs = [
        (e, d)
        for e, d in pairs
        if (conditions and condition_rank(e) == max_condition) or not demand or not _dominated(e, d, pairs)
    ]
    by_cash = [e for e, _ in sorted(pairs, key=lambda p: (-preference(p[0]), -p[0].cash_theirs, sum(p[1].values())))]
    by_price = [e for e, _ in sorted(pairs, key=lambda p: (-p[0].terms["unit_price"], sum(p[1].values())))]
    by_condition = [
        e
        for e, _ in sorted(
            pairs,
            key=lambda p: (condition_rank(p[0]), preference(p[0]), p[0].cash_theirs, -p[0].c),
            reverse=True,
        )
    ]
    by_friendly = [
        e
        for e, _ in sorted(pairs, key=lambda p: (-p[0].cash_theirs, p[0].c))
        if supplier_floor is not None and e.cash_theirs >= supplier_floor - 0.005
    ]
    by_relief = [e for e, d in sorted(pairs, key=lambda p: (sum(p[1].values()), -p[0].cash_theirs, p[0].c))]
    picks = []
    ordered = [
        *(by_condition[:1] if conditions else []),
        *by_friendly[:1],
        *by_relief[:1],
        by_price[0],
        by_cash[0],
        *by_condition,
        *by_cash,
    ]
    for e in ordered:
        if len(picks) == n:
            break
        if all(_distance(e.terms, p.terms) > 0 for p in picks):
            picks.append(e)
    return picks


def _dominated(e, d, pairs):
    """Another sheet at no lower price and no less cash for the supplier asks no more on any lever,
    and less somewhere (or pays more)."""
    price = e.terms["unit_price"]
    return any(
        o.terms["unit_price"] >= price
        and o.cash_theirs >= e.cash_theirs - 0.005
        and all(od[f] <= d[f] for f in d)
        and (o.terms["unit_price"] > price or od != d)
        for o, od in pairs
        if o is not e
    )


def _distance(a, b):
    return sum(a[f] != b[f] for f in a if f != "unit_price")
