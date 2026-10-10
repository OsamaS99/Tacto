"""AcceptancePolicy and the turn decision: close (accept the best valid standing sheet, or walk
away), or continue (which thread, at what C). Everything here is code: the model never decides
to accept, to walk, or what a package costs.

Order: a near cap closes; an `agreed` sheet is accepted; a `final_offer` is accepted, mirrored
(its price, our terms - only when the expected gain beats the walk risk) or left for a better
thread; an `open` sheet is accepted once it is as good as our next bid or our rounds are used up
(after a mirror if its open terms cost us); otherwise we bid."""

import math

from ..cost import must_haves_ok
from . import bids
from .assess import assess
from .records import CandidatePackage, DecisionProposal

LIVE = {"open", "final_offer", "agreed"}


def live(cs):
    return [t for t in cs.threads.values() if t.status in LIVE]


def c_of(ev, t, scenario="mid"):
    if not t.offer_id or t.standing.get("unit_price") is None:
        return math.inf
    return ev.c(t.sid, t.standing, scenario)


def feasible(cs, ev, t):
    """A sheet we may take: it exists, keeps the must-haves and beats the alternative (pessimistic C)."""
    return c_of(ev, t) < math.inf and must_haves_ok(cs.case, t.standing, t.std) and c_of(ev, t, "high") <= cs.c_alt


def closing(cs, ev, reason="close"):
    """The best valid standing sheet across all threads, else walk away (the alternative)."""
    ok = [t for t in live(cs) if feasible(cs, ev, t)]
    if not ok:
        sid = (live(cs) or list(cs.threads.values()))[0].sid
        return DecisionProposal("walk", sid, reason=f"{reason}: no sheet beats the alternative")
    t = min(ok, key=lambda t: c_of(ev, t))
    return DecisionProposal("accept", t.sid, term_sheet_id=t.offer_id, reason=f"{reason}: best valid sheet")


def epsilon(cs):
    """What counts as 'as good as': 1 % of the room between our target and our limit."""
    spans = [res - target for target, res in cs.targets.values() if math.isfinite(res - target)]
    return 0.01 * max(spans or [1.0])


def mirror(cs, ev, space, t, keep_stated):
    """Their price with our terms for every field they left open (`keep_stated`) or for every field;
    None unless it saves us more than epsilon."""
    if t.standing.get("unit_price") is None:
        return None
    keep = {f: v for f, v in t.standing.items() if f in space.levers} if keep_stated else {}
    terms = space.complete({**keep, "unit_price": t.standing["unit_price"]}, t.std, lambda x: ev.c(t.sid, x))
    if space.valid(terms, t.std) or c_of(ev, t) - ev.c(t.sid, terms) <= epsilon(cs):
        return None
    return ev.evaluate(CandidatePackage(t.sid, terms, "mirror"))


def decide(cs, ev, space, order, urgent=False):
    """The next move. `order`: live supplier IDs, most urgent first. `urgent`: the wave's cap is near."""
    pb = cs.playbook
    threads = live(cs)
    if urgent or not threads:
        return closing(cs, ev, "cap near" if urgent else "no live thread")
    eps = epsilon(cs)
    agreed = [t for t in threads if t.status == "agreed" and feasible(cs, ev, t)]
    if agreed:
        best = min((t for t in threads if feasible(cs, ev, t)), key=lambda t: c_of(ev, t))
        return DecisionProposal("accept", best.sid, term_sheet_id=best.offer_id, reason="agreed: take the best sheet")
    for sid in order:
        t = cs.threads[sid]
        if t.status not in LIVE:
            continue
        others = [o for o in threads if o.sid != t.sid]
        c_now = c_of(ev, t)
        dominates = all(c_now <= c_of(ev, o) for o in others)
        if t.status == "final_offer":
            if feasible(cs, ev, t) and dominates:
                m = mirror(cs, ev, space, t, keep_stated=True) if pb.mirror_final and t.moves_left > 0 else None
                if m and (1 - pb.p_walk) * (c_now - m.c) > pb.p_walk * max(0.0, cs.c_alt - c_now):
                    return DecisionProposal("send", t.sid, (m,), reason="final offer: its price on our open terms")
                return DecisionProposal("accept", t.sid, term_sheet_id=t.offer_id, reason="final offer: best sheet")
            if any(o.status != "final_offer" for o in others):
                continue  # a better thread may still move: never push a final offer
            return closing(cs, ev, "all final")
        if t.moves_left <= 0:
            if feasible(cs, ev, t) and dominates:
                return DecisionProposal("accept", t.sid, term_sheet_id=t.offer_id, reason="no rounds left")
            continue
        a = assess(t)
        c_target, c_res = cs.targets[t.sid]
        their_price = t.standing.get("unit_price")
        if their_price is None:
            continue  # no price quoted: nothing to bid against
        ours_at_theirs = ev.c(t.sid, bids.best_combo(ev, space, t.sid, their_price))
        tau = max(cs.tau, t.rounds_used / max(pb.max_rounds, 1))
        next_c = bids.next_bid(pb, t, a, c_target, c_res, ours_at_theirs, tau)
        time_up = t.moves_left <= 1 or t.rounds_used >= pb.max_rounds
        can_accept = t.rounds_used >= pb.min_rounds_accept and feasible(cs, ev, t) and dominates
        if can_accept and not (a.regressed and not time_up) and (c_now <= next_c + eps or time_up):
            m = mirror(cs, ev, space, t, keep_stated=False)
            if m and m.c < next_c + eps:
                return DecisionProposal("send", t.sid, (m,), reason="close: its price on our terms")
            return DecisionProposal("accept", t.sid, term_sheet_id=t.offer_id, reason="as good as our next bid")
        packages = bids.select(bids.generate(ev, space, t.sid, next_c, their_price, t.standing))
        if packages:
            return DecisionProposal("send", t.sid, tuple(packages), reason=f"bid at C {next_c:,.0f}")
    return closing(cs, ev, "nothing left to bid")
