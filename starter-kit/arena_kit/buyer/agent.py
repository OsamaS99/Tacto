"""BuyerAgent: one case, one writer. Each turn it rebuilds the canonical state from the arena's
threads, lets code decide (close, or which thread at what C with which packages and claims), lets
the model phrase it (or the template, when the model is slow, absent or wrong), and passes every
move through the guard. Plugs into `play()` like any agent: `decide(case, threads)`."""

import hashlib
import json
import logging
import math
import time
from dataclasses import asdict, replace

from ..cost import must_haves_ok
from ..docs import standard_terms
from ..play import MOVE_LIMIT_S, closing_move, latest, moves_left
from . import bids
from .acceptance import closing, decide, feasible, live
from .economics import EconomicEvaluator, estimate_supplier
from .guard import GuardReject, check_action, check_message, pick_claims
from .interpreter import identity, interpret
from .model import SEND_MARGIN_S
from .playbook import playbook
from .records import CaseState, DecisionProposal, NegotiationBrief, SupplierObservation, SupplierThreadState
from .space import ContractSpace
from .writer import QUESTIONS, disclosure, template, write_turn

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
        self.session_id, self.turn_context = None, None
        self.observations, self.questions = {}, {}
        self._restored_session = None
        self.started_at = None  # time.time() when the case was prepared
        self.event = {}  # what the last decision was and why, for the table's event line

    # -- preparation (before the clock matters) -------------------------------------------------
    def prepare(self, case, *, allow_model=True):
        from .dossier import build_dossier
        from .writer import prep

        self.case_id, self.started_at = case["case_id"], time.time()
        self.dossier = build_dossier(self.package, case) if self.package else None
        semantic_case = {
            k: v
            for k, v in case.items()
            if k not in {"seconds_left", "open", "wave", "session_id", "status", "started_at", "ends_at", "deadline"}
        }
        self.brief_hash = hashlib.sha256(
            json.dumps(
                {
                    "case": semantic_case,
                    "dossier": asdict(self.dossier) if self.dossier else None,
                    "prompt_version": "v3",
                },
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        cached = self.store.case_memo(self.case_id) if self.store else {}
        if (
            cached.get("brief_hash") == self.brief_hash
            and cached.get("brief")
            and (not allow_model or not self.model or cached.get("model_attempted"))
        ):
            self.brief = NegotiationBrief(
                **{k: tuple(v) if isinstance(v, list) else v for k, v in cached["brief"].items()}
            )
        else:
            self.brief = prep(self.model if allow_model else None, case, self.dossier)
            if self.store:
                self.store.case_memo(
                    self.case_id,
                    brief_hash=self.brief_hash,
                    brief=asdict(self.brief),
                    model_attempted=bool(self.model and allow_model),
                )
        self.playbook = self._playbook(case)
        self.space = ContractSpace(case)
        self.seen, self.since, self.labels = {}, {}, {}
        self.observations, self.questions = {}, {}
        self._restored_session = None

    def _playbook(self, case):
        """The case's playbook with the brief's adjustments and lever order (negotiable levers only)."""
        base = playbook(case, self.brief.playbook)
        priority = tuple(f for f in self.brief.lever_priority if f in case["negotiable"] and f != "unit_price")
        return replace(base, levers=tuple(dict.fromkeys((*priority, *base.levers))), send_margin_s=SEND_MARGIN_S)

    def set_turn_context(self, context):
        self.turn_context = context
        if context.session_id:
            self.session_id = context.session_id
        for sid, due in context.due_at.items():
            self.since[sid] = due - MOVE_LIMIT_S

    def _restore(self):
        if not self.store or not self.session_id or self._restored_session == self.session_id:
            return
        saved = self.store.memo(self.session_id)
        if saved.get("brief_hash") == self.brief_hash and saved.get("brief"):
            value = saved["brief"]
            self.brief = NegotiationBrief(**{k: tuple(v) if isinstance(v, list) else v for k, v in value.items()})
            self.playbook = self._playbook(self.case)
            for key, value in saved.get("observations", {}).items():
                self.observations[key] = SupplierObservation(
                    **{k: tuple(v) if isinstance(v, list) else v for k, v in value.items()}
                )
            self.questions = {sid: set(q) for sid, q in saved.get("questions", {}).items()}
        self._restored_session = self.session_id

    def _persist(self):
        if self.store and self.session_id:
            self.store.memo(
                self.session_id,
                brief_hash=self.brief_hash,
                brief=asdict(self.brief),
                observations={k: asdict(v) for k, v in self.observations.items()},
                questions={sid: sorted(q) for sid, q in self.questions.items()},
            )

    # -- canonical state ----------------------------------------------------------------------
    def observe(self, case, threads, now=None):
        now = self.clock() if now is None else now
        self.case = case
        self._restore()
        states = {}
        std = {sid: standard_terms(ev) for sid, ev in threads.items()}
        estimates = {}
        if self.playbook.relationship:
            for sid, ev in threads.items():
                first = next((m for m in ev if m.get("from") != "buyer" and m.get("term_sheets")), {})
                price = (first.get("term_sheets") or [{}])[0].get("terms", {}).get("unit_price")
                if (
                    isinstance(price, int | float)
                    and not isinstance(price, bool)
                    and math.isfinite(price)
                    and price > 0
                ):
                    cash = bool(self.dossier and self.dossier.cash_pressure.get(sid))
                    estimates[sid] = estimate_supplier(self.dossier, sid, price, cash)
        econ = EconomicEvaluator(case, std, estimates, {sid: _quoted(ev, std[sid]) for sid, ev in threads.items()})
        for sid, ev in threads.items():
            last = latest(ev)
            n = sum(m.get("from") != "buyer" for m in ev)
            if self.seen.get(sid) != n:
                self.seen[sid] = n
                due = self.turn_context.due_at.get(sid) if self.turn_context else None
                self.since[sid] = due - MOVE_LIMIT_S if due is not None else now
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
                their_c=[_c_msg(econ, case, sid, m, std[sid]) for m in theirs],
                my_c=[max(econ.c(sid, s["terms"]) for s in m["term_sheets"]) for m in mine],
                claim_results=last.get("claim_results") or [],
                last_body=last.get("body") or "",
                rev=len(ev),
                observation=self.observations.get(identity(sid, last)[1]),
                sheets=[s["terms"] for s in sheets if (s.get("terms") or {}).get("unit_price") is not None],
                my_sheets=[s["terms"] for s in mine[-1]["term_sheets"]] if mine else [],
            )
        cs = CaseState(case, self.playbook, self.dossier, states, c_alt=econ.c_alt(), brief=self.brief)
        cited, cited_to = set(), {}
        for sid, ev in threads.items():
            for m in ev:
                if m.get("from") == "buyer":
                    self.questions.setdefault(sid, set()).update(
                        q for q, sentence in QUESTIONS.items() if sentence in (m.get("body") or "")
                    )
                    for ref in m.get("refs") or []:
                        (cited_to.setdefault(sid, set()) if ref.startswith(("quote:", "offer:")) else cited).add(ref)
        cs.cited, cs.cited_to = cited, cited_to
        openings = {}
        for sid, ev in threads.items():
            first = next((m for m in ev if m.get("from") != "buyer" and m.get("term_sheets")), None)
            if opening := first and first["term_sheets"][0]["terms"].get("unit_price"):
                openings[sid] = opening
        market_costs = []
        for sid in openings:
            first = next(m for m in threads[sid] if m.get("from") != "buyer" and m.get("term_sheets"))
            terms = first["term_sheets"][0]["terms"]
            if must_haves_ok(case, terms, std[sid]):
                market_costs.append(econ.c(sid, terms))
        market_c = min(market_costs, default=None)
        for sid, opening in openings.items():
            cs.targets[sid] = bids.targets(
                case, self.dossier, econ, self.space, sid, opening, cs.c_alt, market_c=market_c
            )
        cs.tau = self.time_used()
        return cs, econ

    def time_used(self):
        """0..1: how much of the wave's time has gone since we opened (0 without a cap)."""
        cap = self.deadline() if callable(self.deadline) else self.deadline
        if not cap or self.started_at is None:
            return 0.0
        return min(1.0, max(0.0, (time.time() - self.started_at) / max(cap - self.started_at, 1.0)))

    # -- one turn -------------------------------------------------------------------------------
    def decide(self, case, threads, turn_context=None):
        """The next move. A failure retries once on rules and templates alone (no model, no
        interpreter): a broken optional component must not cost the negotiation. If that fails too,
        the case closes through the same financial guard, and `closing_move` is the last resort."""
        if turn_context is not None:
            self.set_turn_context(turn_context)
        for degraded in (False, True):
            try:
                return self._decide(case, threads, degraded)
            except Exception:
                log.exception("%s: the buyer failed%s", case.get("case_id"), " again" if degraded else " - retrying")
        self.event = {"reason": "buyer failed: closing move", "source": "fallback"}
        try:
            return self.close_decision(case, threads, reason="buyer failure")
        except Exception:
            log.exception("%s: the closure failed - plain closing move", case.get("case_id"))
            return closing_move(case, threads)

    def _decide(self, case, threads, degraded=False):
        t0 = self.clock()
        if case["case_id"] != self.case_id:
            self.prepare(case, allow_model=False)
        cs, econ = self.observe(case, threads, t0)
        self.label(cs, threads, allow_model=not degraded)
        pending = set(self.turn_context.in_flight) if self.turn_context else set()
        order = sorted((t.sid for t in live(cs) if t.sid not in pending), key=lambda s: self.since.get(s, 0.0))
        waiting = {s for s in order if self.clock() - self.since.get(s, t0) >= KEEP_ALIVE_S}
        proposal = decide(
            cs, econ, self.space, order, urgent=bool(self.turn_context and self.turn_context.closing), waiting=waiting
        )
        check_action(cs, econ, self.space, proposal)
        self._persist()
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
        bid = min((p.terms["unit_price"] for p in proposal.packages), default=None)
        claims = pick_claims(cs, proposal.sid, bid=bid) if proposal.packages else ()  # a keep-alive argues nothing
        claims = tuple(c for c in claims if c.cid in self.brief.permitted_disclosures or c.per_supplier)
        observation = t.observation
        blocked = set(self.questions.get(t.sid, ()))
        if observation:
            blocked.update((*observation.answered, *observation.refused))
        packet = disclosure(cs, t, proposal, claims, blocked_questions=blocked)
        w0 = self.clock()
        try:
            msg, source, read = write_turn(
                None if degraded else self.model,
                packet,
                self.budget(proposal.sid),
                case["case_id"],
                self.playbook.focus,
                deadline=self.turn_context.decision_deadline if self.turn_context else None,
                observation=asdict(observation) if observation else None,
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
            observation=asdict(observation) if observation else None,
            model_trace=self.model.trace if self.model else [],
            prompt_version="v3",
            packages=[{"terms": p.terms, "c": round(p.c, 2)} for p in proposal.packages],
        )
        return {"supplier_id": proposal.sid, "message": msg.payload()}

    def label(self, cs, threads, *, allow_model=True):
        """Interpret once per reply before package generation, using one shared deadline."""
        ordered = sorted(live(cs), key=lambda t: self.since.get(t.sid, 0))
        pending = set(self.turn_context.in_flight) if self.turn_context else set()
        for t in ordered:
            if t.sid in pending:
                continue  # a recovered send may replace this reply; do not cache a budgetless interpretation
            message = latest(threads[t.sid])
            _, fingerprint = identity(t.sid, message)
            if fingerprint not in self.observations:
                deadline = self.turn_context.decision_deadline if self.turn_context else None
                budget = self.budget(t.sid)
                obs = interpret(
                    t.sid,
                    message,
                    model=self.model if allow_model else None,
                    decisions=self.decisions if allow_model else None,
                    budget_s=budget,
                    deadline=deadline,
                    brief=self.brief,
                    asked=self.questions.get(t.sid, ()),
                )
                self.observations[fingerprint] = obs
            obs = self.observations[fingerprint]
            t.observation, t.label = obs, obs.labels()
            if not t.label["values"]:
                interests = [i for i in self.brief.interests if i["supplier_id"] == t.sid]
                t.label["values"] = list(dict.fromkeys(f for i in interests for f in i["fields"]))
            self.labels[t.sid] = (self.seen.get(t.sid), t.label)

    def close_decision(self, case, threads, reason="closing"):
        """Every emergency closure uses the same relationship economics and financial guard."""
        if case["case_id"] != self.case_id:
            self.prepare(case, allow_model=False)
        cs, econ = self.observe(case, threads)
        self.label(cs, threads, allow_model=False)
        try:
            proposal = closing(cs, econ, reason)
            check_action(cs, econ, self.space, proposal)
        except Exception:
            log.exception("%s closure validation failed", case.get("case_id"))
            options = sorted((t for t in live(cs) if feasible(cs, econ, t)), key=lambda t: econ.c(t.sid, t.standing))
            if options:
                t = options[0]
                proposal = DecisionProposal("accept", t.sid, term_sheet_id=t.offer_id, reason="validated recovery")
            else:
                sid = next(iter(cs.threads))
                proposal = DecisionProposal("walk", sid, reason="validated recovery: no feasible offer")
            check_action(cs, econ, self.space, proposal)
        self.event = {"reason": proposal.reason, "source": "closure"}
        action = (
            {"type": "accept", "term_sheet_id": proposal.term_sheet_id}
            if proposal.kind == "accept"
            else {"type": "walk_away"}
        )
        self._persist()
        return {"supplier_id": proposal.sid, "message": {"action": action}}

    def left(self, sids):
        """Seconds until the most urgent of these threads is due (60 s from its supplier's last
        message), less the send margin."""
        now = self.clock()
        due = min((self.since.get(s, now) + MOVE_LIMIT_S for s in sids), default=now)
        end = due - SEND_MARGIN_S
        if self.turn_context and self.turn_context.decision_deadline is not None:
            end = min(end, self.turn_context.decision_deadline)
        return max(0.0, end - now)

    def budget(self, sid):
        """Seconds the writer may take: the playbook's budget within what is left of this thread's 60 s."""
        return min(self.playbook.llm_budget_s, self.left([sid]))


def _c(econ, sid, terms):
    """C of a supplier's sheet; infinite for one without a price (e.g. an arena note)."""
    return econ.c(sid, terms) if terms.get("unit_price") is not None else math.inf


def _quoted(ev, std):
    """The defect rate and delivery the supplier quoted itself: its first sheet's ppm (or its
    standard), its latest sheet's lead time (or its standard)."""
    sheets = [m["term_sheets"][0].get("terms") or {} for m in ev if m.get("from") != "buyer" and m.get("term_sheets")]
    first, last = (sheets[0], sheets[-1]) if sheets else ({}, {})
    return {
        "ppm": first.get("ppm_commitment") or std.get("ppm_commitment"),
        "lead": last.get("lead_time_days") or std.get("lead_time_days"),
    }


def _c_msg(econ, case, sid, m, std):
    """C of a supplier message: its best sheet that keeps the must-haves, else its standing sheet -
    a dearer standing sheet next to a cheaper alternative is no step back (case P11: its
    alternatives priced our new ask, and we read the standing sheet as a regression)."""
    sheets = [s.get("terms") or {} for s in m["term_sheets"]]
    ok = [_c(econ, sid, s) for s in sheets if must_haves_ok(case, s, std)]
    return min(ok, default=_c(econ, sid, sheets[0]))
