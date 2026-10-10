"""AcceptancePolicy and the turn decision: close (accept the best valid standing sheet, or walk
away), or continue (which thread, at what C). Everything here is code: the model never decides
to accept, to walk, or what a package costs.

Order: late in the wave we close; an `agreed` sheet is accepted (its current id); a `final_offer`
is accepted, mirrored (its price, our terms for what it left open - only when the expected gain
beats the walk risk) or left standing while a better thread may still move; an `open` sheet is
accepted once it is as good as our next bid, or our rounds or time run out; otherwise we bid. A
sheet is only taken if it keeps the must-haves, beats the alternative on pessimistic C and - in an
RFQ - beats what every other live thread may still reach."""

import math
from functools import partial

from ..cost import must_haves_ok
from . import bids
from .assess import assess, pace
from .records import CandidatePackage, DecisionProposal

LIVE = {"open", "final_offer", "agreed"}
TAU_LATE, TAU_CLOSE = 0.6, 0.85  # share of the wave's time gone: take what is good enough / close now
FLOOR_STEPS = 2  # an open rival may still move this many steps at its recent pace


def live(cs):
    return [t for t in cs.threads.values() if t.status in LIVE]


def c_of(ev, t, scenario="mid"):
    if not t.offer_id or t.standing.get("unit_price") is None:
        return math.inf
    return ev.c(t.sid, t.standing, scenario)


def feasible(cs, ev, t):
    """A sheet we may take: it exists, keeps the must-haves and beats the alternative (without an
    alternative: any priced sheet). A standing sheet is the supplier's own price, so it does not
    squeeze the supplier: our estimate of the relationship charge - rough by nature - must never
    turn a deal that beats the alternative into a walk-away (case P01 walked from a better offer)."""
    c = c_of(ev, t, "none")
    return math.isfinite(c) and must_haves_ok(cs.case, t.standing, t.std) and c <= cs.c_alt


def floor_c(ev, t):
    """The C a thread may still reach: an open sheet less a few more steps at its recent pace."""
    c = c_of(ev, t)
    return c - FLOOR_STEPS * pace(t) if t.status == "open" and t.moves_left > 0 else c


def dominates(ev, t, threads):
    """`t` at its pessimistic C beats every other live thread at its optimistic floor."""
    c = c_of(ev, t, "high")
    return all(c <= floor_c(ev, o) for o in threads if o.sid != t.sid)


def c_open(cs):
    """The cheapest opening quote: without a deal and without an alternative we score as if there."""
    return min((t.their_c[0] for t in cs.threads.values() if t.their_c), default=math.inf)


def fallback(cs, ev, t):
    """What we close on if `t` walks away: the best other valid sheet or the alternative."""
    c = min([cs.c_alt, *(c_of(ev, o, "high") for o in live(cs) if o.sid != t.sid and feasible(cs, ev, o))])
    return c if math.isfinite(c) else c_open(cs)


def closing(cs, ev, reason="close"):
    """The best valid standing sheet across all threads, else walk away (the alternative)."""
    ok = [t for t in live(cs) if feasible(cs, ev, t)]
    if not ok:
        sid = (live(cs) or list(cs.threads.values()))[0].sid
        return DecisionProposal("walk", sid, reason=f"{reason}: no sheet beats the alternative")
    t = min(ok, key=lambda t: c_of(ev, t))
    return accept(t, f"{reason}: best valid sheet")


def accept(t, reason):
    return DecisionProposal("accept", t.sid, term_sheet_id=t.offer_id, reason=reason)


def epsilon(cs):
    """What counts as 'as good as': 1 % of the room between our target and our limit."""
    spans = [res - target for target, res in cs.targets.values() if math.isfinite(res - target)]
    return 0.01 * max(spans or [1.0])


def mirror(cs, ev, space, t, keep_stated):
    """Their price with our terms for every field they left open (`keep_stated`) or for every field.
    None unless it saves us more than epsilon (or repairs a broken must-have), and None once we
    offered as much before - they saw it and answered with this sheet, so asking again only burns
    a round."""
    if t.standing.get("unit_price") is None:
        return None
    broken = not must_haves_ok(cs.case, t.standing, t.std)  # then its stated terms cannot stand either
    keep = {f: v for f, v in t.standing.items() if f in space.levers} if keep_stated and not broken else {}
    terms = space.complete({**keep, "unit_price": t.standing["unit_price"]}, t.std, lambda x: ev.c(t.sid, x))
    eps = epsilon(cs)
    if space.valid(terms, t.std) or not (broken or c_of(ev, t) - ev.c(t.sid, terms) > eps):
        return None
    m = ev.evaluate(CandidatePackage(t.sid, terms, "mirror"))
    if t.my_c and t.my_c[-1] >= m.c - eps:
        return None
    return m


def alternative(cs, ev, space, t, threads):
    """The supplier's own cheaper sheet from its last message, as a package we send back: an
    alternative cannot be accepted by its id (accept takes the standing sheet), but the supplier
    answers its own terms with `agreed` (case P11 closed at 49.00 next to its own 46.50 alternative).
    None unless it keeps the must-haves, beats the alternative and every other thread's reach,
    saves more than epsilon on the standing sheet, and was not our last offer already."""
    if t.moves_left <= 1:
        return None
    best, c_now = None, c_of(ev, t)
    for s in t.sheets[1:]:
        stated = {**{f: v for f, v in t.std.items() if f in space.levers}, **s}
        terms = space.complete(stated, t.std, partial(ev.c, t.sid))
        if space.valid(terms, t.std) or terms in t.my_sheets:
            continue
        p = ev.evaluate(CandidatePackage(t.sid, terms, "alternative"))
        if ev.c(t.sid, terms, "none") > cs.c_alt or any(p.c_high > floor_c(ev, o) for o in threads if o.sid != t.sid):
            continue
        if p.c < c_now - epsilon(cs) and (best is None or p.c < best.c):
            best = p
    return best


def keep_alive(cs, ev, threads):
    """RFQ: the threads we still concede to - the best `rfq_keep` by the C they may reach; the rest
    get a holding bid, so none starves and none is bid up for nothing."""
    quoting = sorted((t for t in threads if c_of(ev, t) < math.inf), key=lambda t: floor_c(ev, t))
    return {t.sid for t in quoting[: cs.playbook.rfq_keep]} | {t.sid for t in threads if c_of(ev, t) == math.inf}


def decide(cs, ev, space, order, urgent=False, waiting=()):
    """The next move. `order`: live supplier IDs, most urgent first. `urgent`: the wave's cap is near.
    `waiting`: suppliers that have waited long for our answer - a final offer we leave standing
    while another thread moves gets a question, or it times out (three in a row end the thread)."""
    pb = cs.playbook
    threads = live(cs)
    if urgent or cs.tau >= TAU_CLOSE or not threads:
        return closing(cs, ev, "no live thread" if not threads else "cap near")
    eps = epsilon(cs)
    if any(t.status == "agreed" and feasible(cs, ev, t) for t in threads):
        best = min((t for t in threads if feasible(cs, ev, t)), key=lambda t: c_of(ev, t))
        return accept(best, "agreed: take the best sheet")
    late = cs.tau >= TAU_LATE
    rivals = keep_alive(cs, ev, threads)
    for sid in order:
        t = cs.threads[sid]
        if t.status not in LIVE:
            continue
        a = assess(t, getattr(t, "label", None))
        c_now = c_of(ev, t)
        takeable = feasible(cs, ev, t) and dominates(ev, t, threads)
        alt = None if late else alternative(cs, ev, space, t, threads)  # late: no round to spare
        if t.status == "final_offer":
            p = max(pb.p_walk, a.walk_risk if a.intent == "walk_threat" else 0.0)
            risk = p * max(0.0, fallback(cs, ev, t) - c_now)  # what a walk would cost us
            if alt and (1 - p) * (c_now - alt.c) > risk:  # any answer but accept may push it to walk
                return DecisionProposal("send", t.sid, (alt,), reason="final offer: its own cheaper alternative")
            m = mirror(cs, ev, space, t, keep_stated=True) if t.moves_left > 0 else None
            if takeable:
                if m and pb.mirror_final and (1 - p) * (c_now - m.c) > risk:
                    return DecisionProposal("send", t.sid, (m,), reason="final offer: its price on our open terms")
                return accept(t, "final offer: best sheet")
            if m and not feasible(cs, ev, t) and m.c_high <= min(cs.c_alt, fallback(cs, ev, t)):
                # its terms break a must-have or lose to the alternative: our terms at its price risk nothing
                return DecisionProposal("send", t.sid, (m,), reason="final offer: not takeable, its price on our terms")
            if any(o.status == "open" for o in threads):  # a better thread may still move: never push it
                if sid in waiting and t.moves_left > 1 and feasible(cs, ev, t):
                    return DecisionProposal("send", sid, question="validity", reason="final offer: kept alive")
                continue
            return closing(cs, ev, "all final")
        if t.moves_left <= 0:
            if takeable:
                return accept(t, "no rounds left")
            continue
        their_price = t.standing.get("unit_price")
        if their_price is None or sid not in cs.targets:
            continue  # no price quoted: nothing to bid against
        c_target, c_res = cs.targets[sid]
        # never bid a supplier up past a sheet another one already offers us
        c_res = min([c_res, *(c_of(ev, o) for o in threads if o.sid != sid and feasible(cs, ev, o))])
        c_best = min(c_now, alt.c) if alt else c_now
        ours_at_theirs = ev.c(sid, bids.best_combo(ev, space, sid, their_price))
        if t.my_c and ours_at_theirs <= t.my_c[-1] + eps:
            # its price on our terms is no better than our own bid: it refused our terms, so the gap
            # is to its real sheet (cases P08, P18: a gap of 0 froze us and drew a final offer)
            ours_at_theirs = c_best
        tau = max(cs.tau, t.rounds_used / max(pb.max_rounds, 1))
        next_c = bids.next_bid(pb, t, a, c_target, c_res, ours_at_theirs, tau, hold=sid not in rivals)
        time_up = late or t.moves_left <= 1 or t.rounds_used >= pb.max_rounds
        ready = late or t.rounds_used >= pb.min_rounds_accept
        if ready and (takeable or alt) and (c_best <= next_c + eps or time_up) and not (a.regressed and not time_up):
            if alt:
                return DecisionProposal("send", t.sid, (alt,), reason="close: its own cheaper alternative")
            m = mirror(cs, ev, space, t, keep_stated=False) if t.moves_left > 0 and not late else None
            if m:
                return DecisionProposal("send", t.sid, (m,), reason="close: its price on our terms")
            return accept(t, "as good as our next bid" if c_now <= next_c + eps else "time is up")
        demand = partial(space.demand, ref={**ev.standard(sid), **t.standing}, std=t.std)
        packages = bids.select(bids.generate(ev, space, sid, next_c, their_price, t.standing), demand, their_price)
        # never the same terms at a lower price than we showed before: that reads as a step back
        packages = [p for p in packages if not any(_cut(p.terms, s) for s in t.my_sheets)] or packages
        if packages:
            return DecisionProposal("send", t.sid, tuple(packages), reason=f"bid at C {next_c:,.0f}")
    return closing(cs, ev, "nothing left to bid")


def _cut(terms, shown):
    """`terms` are a sheet we showed before, at a lower price."""
    same = all(terms[f] == shown.get(f) for f in terms if f != "unit_price")
    return same and terms["unit_price"] < shown.get("unit_price", 0)
