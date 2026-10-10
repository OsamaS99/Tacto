"""BuyerAgent: one case, one writer. Each turn it rebuilds the canonical state from the arena's
threads, lets code decide (close, or which thread at what C with which packages and claims), lets
the model phrase it (or the template, when the model is slow, absent or wrong), and passes every
move through the guard. Plugs into `play()` like any agent: `decide(case, threads)`."""

import logging
import math
import time

from ..docs import standard_terms
from ..play import latest, moves_left
from . import bids
from .acceptance import decide, live
from .economics import EconomicEvaluator, estimate_supplier
from .guard import GuardReject, check_message, pick_claims
from .playbook import playbook
from .records import CaseState, SupplierThreadState
from .space import ContractSpace
from .writer import disclosure, template, write

log = logging.getLogger("arena")
ENDED = {"walked_away", "no_bid"}


class BuyerAgent:
    def __init__(self, package=None, model=None, decisions=None, store=None, clock=time.monotonic):
        self.package, self.model, self.decisions, self.store, self.clock = package, model, decisions, store, clock
        self.case_id = None
        self.deadline = None  # set by play(): the wave's cap (time.time()), or a function returning it
        self.seen, self.since = {}, {}  # sid -> supplier messages seen / monotonic time the last arrived
        self.started_at = None  # time.time() when the case was prepared

    # -- preparation (before the clock matters) -------------------------------------------------
    def prepare(self, case):
        from .dossier import build_dossier  # the data layer is optional: no package, no dossier

        self.case_id, self.started_at = case["case_id"], time.time()
        self.dossier = build_dossier(self.package, case) if self.package else None
        adjust = {}
        if self.model is not None and self.dossier is not None:
            from .writer import prep

            adjust = prep(self.model, case, self.dossier) or {}
        self.playbook = playbook(case, adjust)
        self.space = ContractSpace(case)
        self.seen, self.since = {}, {}

    # -- canonical state ----------------------------------------------------------------------
    def observe(self, case, threads):
        now = self.clock()
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
                their_c=[econ.c(sid, m["term_sheets"][0]["terms"]) for m in theirs],
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
            anchor = bids.anchor_price(case, self.dossier, opening)
            c_target = econ.c(sid, bids.best_combo(econ, self.space, sid, anchor))
            c_res = min(cs.c_alt, econ.c(sid, first["term_sheets"][0]["terms"]))  # never worse than its opening
            if not math.isfinite(c_res):
                c_res = econ.c(sid, bids.best_combo(econ, self.space, sid, opening))
            cs.targets[sid] = (c_target, max(c_target, c_res - 0.01 * (c_res - c_target)))
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
        t0 = self.clock()
        if case["case_id"] != self.case_id:
            self.prepare(case)
        cs, econ = self.observe(case, threads)
        order = sorted((t.sid for t in live(cs)), key=lambda s: self.since.get(s, 0.0))
        proposal = decide(cs, econ, self.space, order)
        log.info("%s %s %s: %s", case["case_id"], proposal.kind, proposal.sid, proposal.reason)
        if proposal.kind == "accept":
            return {
                "supplier_id": proposal.sid,
                "message": {"action": {"type": "accept", "term_sheet_id": proposal.term_sheet_id}},
            }
        if proposal.kind == "walk":
            return {"supplier_id": proposal.sid, "message": {"action": {"type": "walk_away"}}}
        t = cs.threads[proposal.sid]
        claims = pick_claims(cs, proposal.sid)
        packet = disclosure(cs, t, proposal, claims)
        budget = self.budget(t0)
        msg = write(self.model, packet, budget) if self.model is not None and budget > 3 else template(packet)
        try:
            msg = check_message(cs, econ, self.space, proposal, packet, msg)
        except GuardReject as e:  # the model's text failed the guard: the template always passes
            log.warning("%s guard: %s - template instead", case["case_id"], e)
            msg = check_message(cs, econ, self.space, proposal, packet, template(packet))
        return {"supplier_id": proposal.sid, "message": msg.payload()}

    def budget(self, t0):
        """Seconds the writer may take: the playbook's budget within the 60 s per message."""
        spent = self.clock() - t0
        return min(self.playbook.llm_budget_s, 60.0 - self.playbook.send_margin_s - spent)
