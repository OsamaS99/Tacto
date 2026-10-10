"""A persona supplier for local evaluation (`arena eval --supplier persona`) - never used by the buyer.

Each supplier gets hidden economics from the data package and the case: a floor (should-cost plus a
3-8 % margin, at least 85 % of its opening), its cost of money (dearer when short of cash), its real
ppm, how tight its capacity is. A deterministic core decides price and status: it values every term
sheet in cash (`cost.supplier_value` plus lead-time, volume, contract, index and quality extras),
concedes a bounded step only when the buyer moved, lets verified data move it a little and
contradicted data harden it, agrees when a sheet is worth its current ask, goes final near its floor
or after round 3-5 and may walk when pushed after that. An optional language model only words the
e-mail; a reply that changes a figure falls back to the template. Seeded: the same seed and the same
buyer moves give the same decisions.
"""

import itertools
import json
import math
import os
import random
import re
from dataclasses import dataclass, field

from .cost import closing_terms, supplier_value, total_cost
from .sim import NAMES, _fmt

AS_OF, RECENT = "2026-09-30", "2025-10-01"  # the data's date; notes older than a year are stale
QUOTE_SINCE = "2026-04-01"  # a quote younger than six months is its opening
BASE_RATE, CASH_RATE = 0.08, 0.12
LEAD_COST = 0.0015  # share of the price per day of lead time below its standard
VOLUME_GAIN, CONTRACT_GAIN, INDEX_GAIN, QUALITY_COST = 0.04, 0.01, 0.02, 0.03  # shares of the price
CAPACITY_LEAD = {1: 2.0, 0: 1.0, -1: 0.5}  # tight, normal, spare: what a faster delivery costs it
CAPACITY_VOLUME = {1: -0.5, 0: 1.0, -1: 1.5}  # and what more volume is worth to it
TOL, MAX_HARD, MIN_TRUST = 1e-4, 2.0, 0.2
TOKEN = 0.1  # a buyer move below this share of its step is a token and earns only a token answer
WRITE_S = 15.0
CASH_NEWS = {"credit_watch", "credit_downgrade", "restructuring", "short_time_work"}
TIGHT_NEWS, SPARE_NEWS = {"full_order_book", "allocation"}, {"free_capacity", "short_time_work"}
NOTES = {
    "cash": r"pay (?:earlier|our invoices within)|bank is watching",
    "firm": r"cannot sign anything that lowers our margin",
    "overdue": r"invoices are overdue",
    "tight": r"backlog|fully booked|no free capacity|allocat",
    "spare": r"additional parts at short notice|free (?:machine )?capacity",
}
SIGNALS = {  # what the buyer's e-mail says, as a person would read it
    "urgency": r"urgent|asap|as soon as possible|immediately|no alternative|only (?:supplier|source)|depend on you"
    r"|line stop|dringend",
    "threat": r"otherwise we|we will (?:switch|resource|re-source|delist|look elsewhere|take our business)"
    r"|move (?:our|the) (?:business|volume)|last chance|terminate",
    "promise": r"we will (?:send|share|provide|forward)\b[^.]{0,40}\b(?:data|figures|numbers|forecast|document|file)",
    "breakdown": r"breakdown|cost structure|kalkulation",
    "polite": r"thank|appreciate|danke",
}
LINES = {
    "moved": "You moved, so do we.",
    "held": "We see no movement on your side; our offer stands.",
    "lowball": "Your figure is far from any realistic basis; we keep our offer.",
    "credited": "We checked the data you cited; it holds and we took it into account.",
    "reality": "We checked the data you cited. It is correct - but that's not our reality, other costs weigh more.",
    "contradicted": "The figures you cite do not match what we know. That does not build Vertrauen.",
    "urgency": "We understand it is urgent for you; our price follows our costs, not your timetable.",
    "threat": "Pressure does not change our costs.",
    "unkept": "You promised us the data last time - we are still waiting for it.",
    "breakdown_no": "As a {owner} business we do not share our Kalkulation; please understand.",
    "breakdown": "Our cost breakdown is attached.",
    "cash": "Faster payment would help us; we reflect it in the price.",
    "tight": "Our capacity is well booked this year.",
    "spare": "We have capacity for more volume.",
}
HEAD = {
    "open": "Our Angebot:",
    "final_offer": "Das ist unser letztes Angebot - further we cannot go:",
    "agreed": "Einverstanden - we confirm your terms exactly as you sent them:",
    "walked_away": "Under these conditions we see no basis. We end the negotiation here.",
}
WHY = {
    "agreed": "The terms are worth our ask: we agree.",
    "walked_away": "Pushed after our last word, or misled once too often: we end it.",
    "final_offer": "Near our limit, or enough rounds: our last word.",
}
WRITER = """You write one e-mail as the {role} of {name}, a {owner} supplier, to a buyer. Mix in a few
German words as a German Mittelstand supplier would (Angebot, Zahlungsziel, Grüße). React to what the
buyer wrote. The decision is made: status {status}, terms exactly as in the draft. Never change, add or
hint at any figure (no costs, margins, limits or other prices); never promise more. Keep it short.
Reply as JSON: {{"body": "..."}}"""
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


@dataclass
class Persona:
    """One supplier's hidden economics and its state in one thread."""

    name: str
    owner: str
    role: str
    cash: bool
    capacity: int  # 1 tight, 0 normal, -1 spare
    ppm: float  # the defect rate it really achieves
    open_book: bool  # shares a cost breakdown when asked
    rate: float
    opening: float
    floor: float  # in value: EUR per unit at its standard terms
    low: float  # below this a sheet is no serious basis: its floor less its range (at least 10 %)
    step: float
    final_round: int
    p_walk: float
    reality: float  # chance that true data meets "that's not our reality"
    base: dict  # its standard terms with the fixed ones: what it values against
    rng: random.Random
    ask: float = 0.0
    trust: float = 1.0
    hard: float = 0.0
    contradictions: int = 0
    pushes: int = 0
    best_seen: float | None = None
    credited: float = 0.0  # steps already given for data: true data moves it a little, once
    final: bool = False
    promised: bool = False
    notes: list = field(default_factory=list)

    def extra(self, case, terms):
        """EUR per unit beyond `supplier_value`'s cash items: lead time, volume, contract, index, quality."""
        t, s, ref = closing_terms(case, terms, self.base), closing_terms(case, {}, self.base), self.opening
        x = -ref * LEAD_COST * max(0, s["lead_time_days"] - t["lead_time_days"]) * CAPACITY_LEAD[self.capacity]
        more = t["volume_per_year"] / s["volume_per_year"] - 1 if s["volume_per_year"] else 0.0
        x += ref * VOLUME_GAIN * max(-0.5, min(0.5, more)) * CAPACITY_VOLUME[self.capacity]
        x += ref * CONTRACT_GAIN * (t["contract_months"] - s["contract_months"]) / 12 * (1.5 if self.cash else 1)
        share = (t.get("index_clause") or {}).get("material_share", 0) - (s.get("index_clause") or {}).get(
            "material_share", 0
        )
        x += ref * INDEX_GAIN * share
        commit = t.get("ppm_commitment")
        if commit is not None and self.ppm > commit:  # it would have to invest to keep that
            x -= ref * QUALITY_COST * (self.ppm - commit) / self.ppm
        return x

    def value(self, case, terms):
        est = {"standard": self.base, "interest_rate": self.rate, "usual_rate": BASE_RATE, "ppm": self.ppm}
        return supplier_value(case, terms, {**est, "extra": self.extra(case, terms)})

    def price_for(self, case, terms, target):
        """The price (rounded up to the cent) at which `terms` are worth `target` to it, else None."""
        v0 = self.value(case, {**terms, "unit_price": 0.0})
        slope = self.value(case, {**terms, "unit_price": 1.0}) - v0
        return math.ceil((target - v0) / slope * 100 - 1e-6) / 100 if slope > TOL else None


def _rows(package, sql, *params):
    return package.query(sql, params) if package is not None and package.db is not None else []


def _notes(package, sup):
    """The supplier's recent notes, letters and e-mails as one text."""
    out = []
    for r in _rows(package, "SELECT id FROM documents WHERE supplier_id = ? AND date >= ?", sup, RECENT):
        try:
            out.append(package.document(r["id"]))
        except FileNotFoundError:
            pass
    return " ".join(out)


def _should_cost(package, case, sup, part):
    """Its should-cost per the part's price unit: its own row, else the part's, else the case's."""
    rows = _rows(package, "SELECT * FROM should_cost WHERE part_id = ? ORDER BY supplier_id = ? DESC", part, sup)
    rows += [f for k, f in case.get("facts", {}).items() if k.startswith("cost:") and f.get("part") == part]
    if not rows:
        return None
    unit = next(iter(_rows(package, "SELECT price_unit FROM parts WHERE id = ?", part)), {}).get("price_unit") or 1
    return rows[0]["unit_cost"] * unit / (rows[0].get("price_unit") or 1)


def _ppm(package, case, sup):
    """Its latest scorecard ppm: the case's, else the package's."""
    key = f"sc:{sup.split(':')[1]}@"
    rows = [f for k, f in sorted(case.get("facts", {}).items(), reverse=True) if k.startswith(key)]
    rows = rows or _rows(package, "SELECT ppm FROM scorecards WHERE supplier_id = ? ORDER BY quarter DESC", sup)
    return float(rows[0].get("ppm") or 0) if rows else 0.0


def _standard(case, master, ppm):
    """Its standard terms for the negotiable fields: its master data, snapped where it has none."""
    own = {
        "payment_days": master.get("payment_days"),
        "incoterm": master.get("incoterm"),
        "lead_time_days": master.get("standard_lead_time_days"),
    }
    out = {}
    for f in case["negotiable"][1:]:
        opts = case["term_options"].get(f) or [None]
        if own.get(f):
            out[f] = int(own[f]) if f.endswith("days") else own[f]
        elif f == "ppm_commitment":  # a commitment it can keep
            out[f] = min((o for o in opts if o >= ppm), default=max(opts))
        elif f == "volume_per_year":
            out[f] = min(opts, key=lambda o: abs(o - case["buyer"]["need_per_year"]))
        elif f == "contract_months":
            out[f] = 12 if 12 in opts else opts[0]
        elif f == "index_clause" and case.get("type") == "raw_material":
            out[f] = next((o for o in reversed(opts) if o), None)
        else:
            out[f] = opts[0]
    return out


def _opening(case, sup, part):
    """Its recent quote for this part in the case, else as arena-sim opens: 10 % (+2 % per bidder) up."""
    quotes = [
        f["unit_price"]
        for k, f in case.get("facts", {}).items()
        if k.startswith("quote:")
        and (f.get("supplier"), f.get("part")) == (sup, part)
        and f.get("date", "") >= QUOTE_SINCE
    ]
    if quotes and len(case["parts"]) == 1:
        return quotes[0]
    b, i = case["buyer"], case["suppliers"].index(sup)
    return round((b.get("current_unit_price") or b["fallback_unit_price"]) * (1.10 + 0.02 * i), 2)


def signals(body):
    return {k: bool(re.search(rx, body or "", re.I)) for k, rx in SIGNALS.items()}


def persona(package, case, sup, rng):
    """The hidden supplier for `sup` in `case`, from the data package (None: from the case only)."""
    part = case["parts"][0]
    master = next(iter(_rows(package, "SELECT * FROM suppliers WHERE id = ?", sup)), {})
    intel = next(iter(_rows(package, "SELECT * FROM supplier_intel WHERE supplier_id = ?", sup)), {})
    news = _rows(package, "SELECT topic FROM news WHERE supplier_id = ? AND date <= ?", sup, AS_OF)
    news = {r["topic"] for r in news}
    text = _notes(package, sup)
    note = {k: bool(re.search(rx, text, re.I)) for k, rx in NOTES.items()}
    credit, before = intel.get("credit_index") or 0, intel.get("credit_index_12m_ago") or 0
    small = (intel.get("staff") or 999) < 50
    cash = bool(news & CASH_NEWS) or note["cash"] or credit >= 260 or credit - before >= 10
    cash = cash or (small and (intel.get("equity_ratio_pct") or 100) < 30)
    capacity = 1 if news & TIGHT_NEWS or note["tight"] else -1 if news & SPARE_NEWS or note["spare"] else 0
    owner = intel.get("owner_type") or "family"
    ppm = _ppm(package, case, sup)
    current = case["buyer"].get("current_unit_price") or case["buyer"]["fallback_unit_price"]
    opening = _opening(case, sup, part)
    cost = _should_cost(package, case, sup, part)
    cost = cost * rng.uniform(0.92, 1.05) if cost else current * rng.uniform(0.88, 0.96)
    margin = max(rng.uniform(0.03, 0.08), 0.06 if note["firm"] else 0.0)
    floor = max(cost * (1 + margin), 0.85 * opening * rng.uniform(0.98, 1.02)) * (1.02 if capacity == 1 else 1)
    floor = round(floor, 2)
    opening = max(opening, round(floor * rng.uniform(1.02, 1.05), 2))
    standard = _standard(case, master, ppm)
    plan = closing_terms(case, {})
    base = {"payment_days": plan["payment_days"], "incoterm": plan["incoterm"], **standard}
    p = Persona(
        name=master.get("name") or sup,
        owner=owner,
        role="owner" if owner in ("family", "founder") and small else "sales",
        cash=cash,
        capacity=capacity,
        ppm=ppm,
        open_book=owner == "group" and rng.random() < 0.7,
        rate=CASH_RATE if cash else BASE_RATE,
        opening=opening,
        floor=0.0,
        low=0.0,
        step=0.0,
        final_round=rng.randint(3, 5),  # the hosted suppliers went final after 3-5 rounds
        p_walk=min(0.9, max(0.1, rng.uniform(0.3, 0.6) + 0.2 * (capacity == 1) - 0.15 * cash)),
        reality=min(0.9, rng.uniform(0.3, 0.6) + 0.2 * cash),
        base={**base, **case.get("fixed_terms", {})},
        rng=rng,
        trust=0.85 if note["overdue"] else 1.0,
    )
    # floor and ask are values (EUR per unit at its standard terms); the opening is stated at those terms
    p.ask = p.value(case, {"unit_price": opening})
    p.floor = p.ask - (opening - floor)
    p.low = p.floor - max(opening - floor, 0.1 * opening)  # hosted P01 still moved for a bid 8 % below
    p.step = max((p.ask - p.floor) / rng.choice([3, 4, 5]) * (0.6 if note["firm"] else 1.0), 0.001 * opening)
    p.notes = ["cash"] * cash + {1: ["tight"], -1: ["spare"]}.get(capacity, [])
    return p, standard


class PersonaSupplier:
    """The `Sim(supplier=...)` hook: opens each thread, answers each buyer e-mail, prices the best deal."""

    def __init__(self, package=None, seed=0, model=None, timeout_s=WRITE_S):
        self.package, self.seed, self.model, self.timeout_s = package, seed, model, timeout_s

    def open(self, s, sup, th):
        case = s["case"]
        p, standard = persona(self.package, case, sup, random.Random(f"{self.seed}|{case['case_id']}|{sup}"))
        th.update(opening=p.opening, standard=standard, role=p.role, persona=p)
        lines = [LINES[k] for k in p.notes]
        return self._template(p, "open", {"unit_price": p.opening}, lines, opening=True)

    def answer(self, s, th, msg, sheets, claim_results):
        p, case, body = th["persona"], s["case"], msg.get("body") or ""
        sig, lines, why, files = signals(body), [], [], []
        if p.promised and not msg.get("attachments"):  # last time it promised data; nothing came
            p.trust = max(MIN_TRUST, p.trust - 0.15)
            lines.append(LINES["unkept"])
        p.promised = sig["promise"]
        verified = sum(r["result"] == "verified" for r in claim_results)
        contradicted = sum(r["result"] == "contradicted" for r in claim_results)
        if contradicted:
            p.contradictions += contradicted
            p.trust = max(MIN_TRUST, p.trust - 0.2 * contradicted)
            p.hard = min(MAX_HARD, p.hard + 0.5)
            lines.append(LINES["contradicted"])
        if sig["urgency"]:  # needing it badly is no reason to give
            p.hard = min(MAX_HARD, p.hard + 1)
            lines.append(LINES["urgency"])
        if sig["threat"]:
            lines.append(LINES["threat"])
        if sig["polite"]:
            p.trust = min(1.0, p.trust + 0.02)
        if sig["breakdown"]:
            files += ["cost-breakdown.xlsx"] if p.open_book else []
            lines.append(LINES["breakdown"] if p.open_book else LINES["breakdown_no"].format(owner=p.owner))
        ranked = sorted(((p.value(case, t), i) for i, t in enumerate(sheets)), reverse=True)
        best_v, best = (ranked[0][0], sheets[ranked[0][1]]) if ranked else (None, None)

        def reply(status, stated, extra_files=(), reason=None):
            th["reasons"] = [*why, reason or WHY[status]]
            return status, stated, self._write(p, case, status, stated, lines, body), [*files, *extra_files]

        if best is not None and best_v >= p.ask - TOL:
            return reply("agreed", best, ["quote.pdf"])
        if contradicted and p.contradictions >= 3 and p.rng.random() < 0.7:
            return reply("walked_away", th["stated"])
        if p.final:
            if best is not None:
                p.pushes += 1
                if p.rng.random() < p.p_walk + 0.2 * (p.pushes - 1) + 0.3 * sig["threat"]:
                    return reply("walked_away", th["stated"])
            return reply("final_offer", th["stated"], reason="Our last word stands.")
        give = 0.0
        if best is not None:  # reciprocity: a step only when the buyer's best sheet is worth more to us
            seen = max(best_v, p.low)  # a lowball is no basis: moves count from a serious figure only
            move = (p.step if best_v >= p.low else 0.0) if p.best_seen is None else seen - p.best_seen
            p.best_seen = seen if p.best_seen is None else max(p.best_seen, seen)
            give = min(p.step, 0.6 * move if move < TOKEN * p.step else max(0.3 * p.step, 0.6 * move))
            give = give if move > TOL else 0.0
            lines.append(LINES["moved" if give else "lowball" if best_v < p.low else "held"])
            why.append("You moved; we answered with a step." if give else "No move from you, so none from us.")
        give *= p.trust * 0.6**p.hard
        if verified and give:  # data moves it only alongside a move, and by one step at most in all
            if p.rng.random() < p.reality or p.credited >= 1:
                lines.append(LINES["reality"])
            else:
                credit = min(0.5, 0.25 * verified, 1 - p.credited)
                p.credited += credit
                give += credit * p.step
                lines.append(LINES["credited"])
        p.ask = max(p.floor, p.ask - give)
        p.final = th["round"] >= p.final_round or p.ask - p.floor <= p.step + TOL
        stated = self._counter(p, case, best or th["stated"])
        if p.final:
            return reply("final_offer", stated, ["quote.pdf"])
        return reply("open", stated, reason="Our counter-offer on your terms.")

    def _counter(self, p, case, terms):
        """Its offer at its current ask, on the buyer's terms where they can be priced, else its own."""
        rest = {k: v for k, v in terms.items() if k != "unit_price"}
        price = p.price_for(case, rest, p.ask)
        if price is None or price <= 0:
            rest, price = {}, p.price_for(case, {}, p.ask)
        return {"unit_price": price, **{f: rest[f] for f in case["negotiable"] if f in rest}}

    def best_cost(self, s):
        """C of the best deal any supplier here would sign: its floor, the buyer's best other terms."""
        case, fields, out = s["case"], s["case"]["negotiable"][1:], math.inf
        for th in s["threads"].values():
            p = th["persona"]
            choices = []
            for f in fields:
                opts = list(case["term_options"].get(f) or [])
                choices.append(opts + ([th["standard"].get(f)] if th["standard"].get(f) not in opts else []))
            for combo in itertools.product(*choices):
                terms = dict(zip(fields, combo, strict=True))
                price = p.price_for(case, terms, p.floor)
                if price and price > 0:
                    out = min(out, total_cost(case, {**terms, "unit_price": price}))
        return out

    # -- the e-mail ----------------------------------------------------------------------------
    def _template(self, p, status, stated, lines, opening=False):
        greet = "Grüß Gott," if p.role == "owner" else "Guten Tag,"
        terms = ", ".join(
            f"Preis €{v:.2f} per unit" if k == "unit_price" else f"{NAMES.get(k, k)} {_fmt(v)}"
            for k, v in stated.items()
        )
        head = "thank you for your enquiry. Our Angebot is attached:" if opening else HEAD[status]
        tail = "Everything not stated follows our standard terms (see the quote)." if status != "walked_away" else ""
        sign = f"{'Viele Grüße' if p.role == 'owner' else 'Mit freundlichen Grüßen'}\n{p.name}"
        offer = "" if status == "walked_away" else f"{terms}."
        parts = (greet, head, offer, *lines, tail, sign) if opening else (greet, *lines, head, offer, tail, sign)
        return "\n".join(x for x in parts if x)

    def _write(self, p, case, status, stated, lines, buyer_body):
        draft = self._template(p, status, stated, lines)
        if self.model is None:
            return draft
        system = WRITER.format(role=p.role, name=p.name, owner=p.owner, status=status)
        user = json.dumps({"buyer_email": buyer_body[:3000], "draft": draft, "case": case["title"]}, ensure_ascii=False)
        from .buyer.model import parse_json

        out = parse_json(self.model.complete(system, user, self.timeout_s))
        text = out.get("body") if isinstance(out, dict) else None
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            return draft
        allowed = set(NUMBER.findall(draft + buyer_body))
        return text if set(NUMBER.findall(text)) <= allowed else draft  # no figure of its own: none to leak


def persona_model():
    """LLM_SIM_MODEL on an OpenAI-compatible endpoint (LLM_BASE_URL, LLM_API_KEY), else the buyer's
    `model_from_env`; None without a model (template e-mails)."""
    from .buyer.model import Model, model_from_env, openai_chat

    name = os.environ.get("LLM_SIM_MODEL")
    if not name:
        return model_from_env()
    base = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1")
    return Model([(name, openai_chat(base, os.environ.get("LLM_API_KEY", ""), name))])
