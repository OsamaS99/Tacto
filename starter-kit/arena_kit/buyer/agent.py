"""BuyerAgent: one case, one writer. Each turn it rebuilds the canonical state from the arena's
threads, lets code decide (close, or which thread at what C with which packages and claims), lets
the model phrase it (or the template, when the model is slow, absent or wrong), and passes every
move through the guard. Plugs into `play()` like any agent: `decide(case, threads)`."""

import logging
import math
import time

from ..docs import standard_terms
from ..play import MOVE_LIMIT_S, closing_move, latest, moves_left
from . import bids
from .acceptance import decide, live
from .economics import EconomicEvaluator, estimate_supplier
from .guard import GuardReject, check_message, pick_claims
from .playbook import playbook
from .records import CaseState, SupplierThreadState
from .space import ContractSpace
from .writer import disclosure, template, write_turn

log = logging.getLogger("arena")
ENDED = {"walked_away", "no_bid"}
KEEP_ALIVE_S = 30.0  # an RFQ final offer we leave standing gets a message once its supplier waited this long


class BuyerAgent:
    def __init__(self, package=None, model=None, decisions=None, store=None, clock=time.monotonic):
        self.package, self.model, self.decisions, self.store, self.clock = package, model, decisions, store, clock
        self.case_id = None
        self.deadline = None  # set by play(): the wave's cap (time.time()), or a function returning it
        self.seen, self.since = {}, {}  # sid -> supplier messages seen / monotonic time the last arrived
        self.labels = {}  # sid -> (supplier messages seen, Decision Selector label of the last)
        self.started_at = None  # time.time() when the case was prepared
        self.event = {}  # what the last decision was and why, for the table's event line

    # -- preparation (before the clock matters) -------------------------------------------------
    def prepare(self, case):
        from .dossier import build_dossier  # the data layer is optional: no package, no dossier

        self.case_id, self.started_at = case["case_id"], time.time()
        self.dossier = build_dossier(self.package, case) if self.package else None
        adjust = {}
        if self.model is not None and self.dossier is not None:
            from .writer import prep

            adjust = prep(self.model, case, self.dossier) or {}
            log.info("%s prep %s: %s", case["case_id"], self.model.last[0] or "none", adjust or "defaults")
        self.playbook = playbook(case, adjust)
        self.space = ContractSpace(case)
        self.seen, self.since, self.labels = {}, {}, {}

    # -- canonical state ----------------------------------------------------------------------
    def observe(self, case, threads, now=None):
        now = self.clock() if now is None else now
        states = {}
        std = {sid: standard_terms(ev) for sid, ev in threads.items()}
        estimates = {}
        if self.playbook.relationship:
            for sid, ev in threads.items():
                price = (latest(ev).get("term_sheets") or [{}])[0].get("terms", {}).get("unit_price")
                if price:
                    cash = bool(self.dossier and self.dossier.cash_pressure.get(sid))
                    estimates[sid] = estimate_supplier(self.dossier, sid, price, cash)
        econ = EconomicEvaluator(case, std, estimates)
        for sid, ev in threads.items():
            last = latest(ev)
            n = sum(m.get("from") != "buyer" for m in ev)
            if self.seen.get(sid) != n:
                self.seen[sid], self.since[sid] = n, now
            sheets = last.get("term_sheets") or []
            theirs = [m for m in ev if m.get("from") != "buyer" and m.get("term_sheets")]
            mine = [m for m in ev if m.get("from") == "buyer" and m.get("term_sheets")]
            states[sid] = SupplierThreadState(
                sid=sid,
                offer_id=sheets[0]["term_sheet_id"] if sheets else None,
                status=last.get("status") or "open",
                standing=sheets[0]["terms"] if sheets else {},
                std=std[sid],
                missing=last.get("missing_fields") or [],
                rounds_used=sum(m.get("from") == "buyer" for m in ev),
                moves_left=moves_left(case, ev),
                their_c=[_c(econ, sid, m["term_sheets"][0].get("terms") or {}) for m in theirs],
                my_c=[max(econ.c(sid, s["terms"]) for s in m["term_sheets"]) for m in mine],
                claim_results=last.get("claim_results") or [],
                last_body=last.get("body") or "",
                rev=len(ev),
            )
        cs = CaseState(case, self.playbook, self.dossier, states, c_alt=econ.c_alt())
        cited, cited_to = set(), {}
        for sid, ev in threads.items():
            for m in ev:
                if m.get("from") == "buyer":
                    for ref in m.get("refs") or []:
                        (cited_to.setdefault(sid, set()) if ref.startswith(("quote:", "offer:")) else cited).add(ref)
        cs.cited, cs.cited_to = cited, cited_to
        for sid, ev in threads.items():
            first = next((m for m in ev if m.get("from") != "buyer" and m.get("term_sheets")), None)
            opening = first and first["term_sheets"][0]["terms"].get("unit_price")
            if not opening:
                continue
            cs.targets[sid] = bids.targets(case, self.dossier, econ, self.space, sid, opening, cs.c_alt)
        cs.tau = self.time_used()
        return cs, econ

    def time_used(self):
        """0..1: how much of the wave's time has gone since we opened (0 without a cap)."""
        cap = self.deadline() if callable(self.deadline) else self.deadline
        if not cap or self.started_at is None:
            return 0.0
        return min(1.0, max(0.0, (time.time() - self.started_at) / max(cap - self.started_at, 1.0)))

    # -- one turn -------------------------------------------------------------------------------
    def decide(self, case, threads):
        """The next move; never raises - a failure closes the case on the best valid sheet instead."""
        try:
            return self._decide(case, threads)
        except Exception:
            log.exception("%s: the buyer failed - closing move instead", case.get("case_id"))
            self.event = {"reason": "buyer failed: closing move", "source": "fallback"}
            return closing_move(case, threads)

    def _decide(self, case, threads):
        t0 = self.clock()
        if case["case_id"] != self.case_id:
            self.prepare(case)
        cs, econ = self.observe(case, threads, t0)
        self.label(cs)
        order = sorted((t.sid for t in live(cs)), key=lambda s: self.since.get(s, 0.0))
        waiting = {s for s in order if self.clock() - self.since.get(s, t0) >= KEEP_ALIVE_S}
        proposal = decide(cs, econ, self.space, order, waiting=waiting)
        log.info("%s %s %s: %s", case["case_id"], proposal.kind, proposal.sid, proposal.reason)
        self.event = {"reason": proposal.reason, "source": proposal.kind}
        if proposal.kind == "accept":
            return {
                "supplier_id": proposal.sid,
                "message": {"action": {"type": "accept", "term_sheet_id": proposal.term_sheet_id}},
            }
        if proposal.kind == "walk":
            return {"supplier_id": proposal.sid, "message": {"action": {"type": "walk_away"}}}
        t = cs.threads[proposal.sid]
        claims = pick_claims(cs, proposal.sid) if proposal.packages else ()  # a keep-alive argues nothing
        packet = disclosure(cs, t, proposal, claims)
        w0 = self.clock()
        try:
            msg, source, read = write_turn(
                self.model, packet, self.budget(proposal.sid), case["case_id"], self.playbook.focus
            )
        except Exception:  # the model's reply is untrusted input: a surprise in it costs the prose only
            log.exception("%s writer failed - template instead", case["case_id"])
            msg, source, read = template(packet), "template", None
        try:
            msg = check_message(cs, econ, self.space, proposal, packet, msg)
        except GuardReject as e:  # the model's text failed the guard: the template always passes
            log.warning("%s guard: %s - template instead", case["case_id"], e)
            msg, source = check_message(cs, econ, self.space, proposal, packet, template(packet)), "template"
        llm = self.model.last if self.model else (None, 0)
        log.info(
            "%s send %s: writer %s (%s) %d ms; read %s; label %s",
            case["case_id"],
            proposal.sid,
            source,
            llm[0] or "-",
            1000 * (self.clock() - w0),
            read,
            t.label,
        )
        self.event.update(
            writer=source,
            model=llm[0],
            llm_ms=llm[1] if source != "template" else None,
            label=t.label,
            packages=[{"terms": p.terms, "c": round(p.c, 2)} for p in proposal.packages],
        )
        return {"supplier_id": proposal.sid, "message": msg.payload()}

    def label(self, cs):
        """Decision Selector labels for each new supplier message (3 s each), while its thread
        keeps more than 15 s of its 60."""
        if self.decisions is None:
            return
        for t in live(cs):
            n = self.seen.get(t.sid)
            if self.labels.get(t.sid, (None,))[0] != n and self.left([t.sid]) > 15:
                try:
                    found = self.decisions.classify_reply(t.last_body)
                except Exception:  # an odd answer: no label, the rules decide
                    log.exception("%s decisions failed", cs.case["case_id"])
                    found = None
                self.labels[t.sid] = (n, found)
            t.label = self.labels.get(t.sid, (None, None))[1]

    def left(self, sids):
        """Seconds until the most urgent of these threads is due (60 s from its supplier's last
        message), less the send margin."""
        now = self.clock()
        due = min((self.since.get(s, now) for s in sids), default=now) + MOVE_LIMIT_S
        return due - self.playbook.send_margin_s - now

    def budget(self, sid):
        """Seconds the writer may take: the playbook's budget within what is left of this thread's 60 s."""
        return min(self.playbook.llm_budget_s, self.left([sid]))


def _c(econ, sid, terms):
    """C of a supplier's sheet; infinite for one without a price (e.g. an arena note)."""
    return econ.c(sid, terms) if terms.get("unit_price") is not None else math.inf
