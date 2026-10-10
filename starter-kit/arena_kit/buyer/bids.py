"""BidGenerator: how much to concede this turn (in our C, on our own package) and the complete
term sheets that cost exactly that. Concessions follow the supplier's moves (reciprocity) and
the clock (an aspiration curve), never pass half the remaining gap, never go backwards - except
that a supplier who moves back gets our last concession back."""

import statistics

from .records import CandidatePackage

MAX_COMBOS = 300  # beyond this: the C-optimal combination and its one-field neighbours


def anchor_price(case, dossier, their_price, sid=None):
    """Our opening price: the lowest defensible figure - never above the current price in a price
    defence or a renewal, never below what the data supports."""
    b = case["buyer"]
    current = b.get("current_unit_price")
    options = [0.9 * their_price]
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


def best_combo(ev, space, sid, price):
    """The term sheet with the lowest C for us at `price`."""
    return space.complete({"unit_price": price}, ev.std.get(sid), lambda t: ev.c(sid, t))


def targets(case, dossier, ev, space, sid, opening_price, c_alt):
    """(C at our anchor, C we never bid beyond) for one supplier: the anchor on our best terms (not
    below the price that squeezes a strategic supplier), and at most its opening price on our best
    terms or the alternative, whichever is cheaper."""
    anchor = anchor_price(case, dossier, opening_price, sid)
    floor = ev.best_price(sid, best_combo(ev, space, sid, anchor))
    c_target = ev.c(sid, best_combo(ev, space, sid, max(anchor, floor)))
    c_res = min(c_alt, ev.c(sid, best_combo(ev, space, sid, opening_price)))
    return c_target, max(c_target, c_res - 0.01 * (c_res - c_target))


def aspiration(c_target, c_res, tau, beta):
    """C we are prepared to reach by time `tau` (0..1): Boulware below beta 1, linear at 1."""
    return c_target + (c_res - c_target) * min(1.0, max(0.0, tau)) ** (1 / beta)


def next_bid(pb, t, a, c_target, c_res, c_their_on_ours, tau, hold=False):
    """Our C this turn. `t`: SupplierThreadState, `a`: SupplierAssessment, `c_their_on_ours`: their
    price on our best terms - we never offer more than half the gap to it at once. `hold`: no
    concession this turn (a thread we only keep alive)."""
    if not t.my_c:
        return c_target
    last = t.my_c[-1]
    if a.regressed:  # it moved back: our last concession goes back too
        return max(c_target, min(last, t.my_c[-2] if len(t.my_c) > 1 else last))
    if hold:
        return last
    step = pb.k * max(0.0, a.concession)  # reciprocity: their move, in our C
    bid = max(aspiration(c_target, c_res, tau, pb.beta), last + step)
    bid = min(bid, last + 0.5 * max(0.0, c_their_on_ours - last), c_res)
    return max(bid, last)  # never backwards


def generate(ev, space, sid, target_c, their_price, standing=None):
    """Complete term sheets that cost us `target_c` (within a cent): the C-optimal one plus
    variants that give the supplier more for the same C. Bounded in C, not in price - a dearer
    price on terms that save us more is fine."""
    std = ev.std.get(sid)
    base = best_combo(ev, space, sid, their_price)
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


def select(evaluated, n=3):
    """Up to `n` diverse sheets: the one closest to our ideal terms (the highest price - simple
    suppliers read the price), the one worth most to the supplier, and one more."""
    if not evaluated:
        return []
    by_cash = sorted(evaluated, key=lambda e: -e.cash_theirs)
    by_price = sorted(evaluated, key=lambda e: -e.terms["unit_price"])
    picks = []
    for e in (by_price[0], by_cash[0], *by_cash[1:]):
        if len(picks) == n:
            break
        if all(_distance(e.terms, p.terms) > 0 for p in picks):
            picks.append(e)
    return picks


def _distance(a, b):
    return sum(a[f] != b[f] for f in a if f != "unit_price")
