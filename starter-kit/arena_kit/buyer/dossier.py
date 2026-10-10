"""EvidenceDossier: what the data says about this case, before the session opens.

Claims are written from the very values the arena checks (facts.json, the Sunday release, the
case's own `facts`): one sentence each, the ID and the figure in it, the figure computed from
those values so it verifies. Only arguments on the buyer's side become claims - a driver index
that rose is the supplier's argument, not ours. Everything else (the justified move, the ask, past
settlements, finances, letters, news) is internal context for the model and never quoted."""

import logging
import math
from collections import defaultdict

from .records import Claim, EvidenceDossier

log = logging.getLogger("arena")

ALIAS = {"sup:71999": "sup:70210"}  # the old vendor record of a renamed supplier (KNOWN_ISSUES 5)
GOOD_NEWS = {"free_capacity", "energy_contract", "new_plant"}  # news that weakens a supplier's ask
CASH_NEWS = {"credit_downgrade", "credit_watch", "short_time_work", "restructuring"}

# mode -> claim kind -> base strength (higher is cited first); a magnitude bonus < 0.5 breaks ties
STRENGTH = {
    "price_increase": dict(notice=5, idx=4.5, cost=4, po=3, sc=2.5, quote=2, offer=2, fc=1, news=1, qn=1.5),
    "renewal": dict(quote=5, offer=5, sc=4, cost=3.5, idx=3, po=2.5, notice=2, fc=1.5, qn=1.5, news=1),
    "rfq": dict(offer=5, quote=4.5, cost=3.5, sc=3, idx=2, po=2, fc=1.5, qn=1.5, notice=1, news=1),
    "bundling": dict(fc=4, quote=4, offer=4, cost=3.5, idx=3, po=2.5, sc=2, notice=2, qn=1.5, news=1),
    "raw_material": dict(idx=5, cost=3.5, quote=3, offer=3, po=2.5, sc=2, notice=2, fc=1.5, qn=1, news=1),
    "capacity": dict(fc=4, sc=3.5, quote=3.5, offer=3.5, cost=3, news=3, idx=2.5, po=2, notice=2, qn=1.5),
    "quality": dict(qn=5, sc=4.5, cost=2, idx=2, quote=2, offer=2, po=1.5, notice=1.5, fc=1, news=1),
}


def _sup(sid):
    return ALIAS.get(sid, sid)


def _records(sid):
    """Both vendor records of a supplier (current, old), for SQL `IN (?, ?)`."""
    now = _sup(sid)
    return now, next((old for old, new in ALIAS.items() if new == now), now)


def _n(v):
    """A number, or None for '' / None / text (CSV rows use '' for NULL)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _months(later, earlier):
    """Whole months from `earlier` to `later` ('YYYY-MM...')."""
    return (int(later[:4]) - int(earlier[:4])) * 12 + int(later[5:7]) - int(earlier[5:7])


def per(pu):
    return "per unit" if pu in (None, 1) else f"per {pu:g} units"


def strength(mode, kind, bonus=0.0):
    return STRENGTH.get(mode, STRENGTH["price_increase"]).get(kind, 1.0) + min(0.49, max(0.0, bonus))


class _Data:
    """The case's view of the package: merged facts grouped by ID kind, guarded SQL."""

    def __init__(self, package, case):
        self.package, self.case = package, case
        self.mode = case.get("type") or "price_increase"
        self.sids = list(case.get("suppliers", []))
        self.parts = list(case.get("parts", []))
        self.facts = {**(getattr(package, "facts", None) or {}), **(case.get("facts") or {})}
        self.by = defaultdict(list)
        for ref, fact in self.facts.items():
            if isinstance(fact, dict):
                self.by[ref.split(":", 1)[0]].append((ref, fact))
        self.series = defaultdict(list)  # series -> [(month, id, value)], oldest first
        for ref, f in self.by["idx"]:
            s, _, month = ref[4:].partition("@")
            if (v := _n(f.get("value"))) and month:
                self.series[f.get("series") or s].append((f.get("month") or month, ref, v))
        for pts in self.series.values():
            pts.sort()
        self.asof = max((p[-1][0] for p in self.series.values() if p), default="2026-09")
        self.pu = self._price_unit()

    def rows(self, sql, *params):
        try:
            return self.package.query(sql, params)
        except Exception as e:  # one missing table must not cost the case
            log.debug("dossier query failed: %s", e)
            return []

    def fact(self, ref):
        if not ref:
            return None
        if ref in self.facts:
            return self.facts[ref]
        try:
            return self.package.lookup(ref)
        except Exception:
            return None

    def ours(self, fact, sid):
        return _sup(fact.get("supplier") or "") == _sup(sid)

    def _price_unit(self):
        for r in self.rows("SELECT price_unit FROM parts WHERE id = ?", self.parts[0] if self.parts else ""):
            if _n(r.get("price_unit")):
                return _n(r["price_unit"])
        for kind in ("quote", "po", "cost"):
            for _, f in self.by[kind]:
                if f.get("part") in self.parts and _n(f.get("price_unit")):
                    return _n(f["price_unit"])
        return 1.0

    def in_case_unit(self, value, pu):
        return value * self.pu / (_n(pu) or self.pu)

    def part_name(self, part):
        if len(self.parts) == 1:
            return "this part"
        rows = self.rows("SELECT description FROM parts WHERE id = ?", part)
        return rows[0]["description"] if rows and rows[0].get("description") else part.split(":", 1)[-1]


# -- claims ----------------------------------------------------------------------------------------
def _fall(points, asof):
    """The buyer's window on one index: from its highest value 3-12 months before the latest month
    down to the latest month - only when the latest month lies within the last 12 months of the
    data and the series did not rise over the year (then the move is the supplier's argument)."""
    if not points or _months(asof, points[-1][0]) > 11:
        return None
    end = points[-1]
    before = [p for p in points if 3 <= _months(end[0], p[0]) <= 12]
    if not before or end[2] > before[0][2]:
        return None
    start = max(before, key=lambda p: p[2])
    change = (end[2] / start[2] - 1) * 100
    return (start, end, change) if change <= -0.5 else None


def _move12(points):
    """The series' change over the 12 months to its latest value (or the longest window it has)."""
    if len(points) < 2:
        return None
    end = points[-1]
    start = next((p for p in points if _months(end[0], p[0]) <= 12), None)
    return (end[2] / start[2] - 1) * 100 if start and start is not end else None


def _drivers(x):
    """{part: {series: weight}} - the category team's cost drivers."""
    out = defaultdict(dict)
    for part in x.parts:
        for r in x.rows("SELECT series_id, weight FROM cost_drivers WHERE part_id = ?", part):
            if (w := _n(r.get("weight"))) is not None:
                out[part][r["series_id"]] = w
    return out


def _series_name(x, s):
    rows = x.rows("SELECT name FROM index_series WHERE id = ?", s)
    return rows[0]["name"] if rows and rows[0].get("name") else f"The {s} index"


def index_claims(x, drivers):
    weights = {s: w for d in drivers.values() for s, w in d.items()}
    refs = {x.fact(r).get("series") for r in x.case.get("refs", []) if r.startswith("idx:") and x.fact(r)}
    out = []
    for s in sorted(set(weights) | (refs - {None})):
        if not (win := _fall(x.series.get(s), x.asof)):
            continue
        (m0, i0, _), (m1, i1, _), change = win
        out.append(
            Claim(
                cid=f"idx:{s}",
                ids=(i0, i1),
                sentence=f"{_series_name(x, s)} changed {change:+.1f}% from {m0} to {m1} ({i0}, {i1}).",
                kind="idx",
                strength=strength(x.mode, "idx", -change * weights.get(s, 0.1) / 10),
            )
        )
    return out


def scorecard_claims(x):
    out = []
    for sid in x.sids:
        rows = [(f.get("quarter") or ref.split("@")[-1], ref, f) for ref, f in x.by["sc"] if x.ours(f, sid)]
        if not rows:
            continue
        _, ref, f = max(rows)
        otd, ppm, colour = _n(f.get("otd_pct")), _n(f.get("ppm")), f.get("colour") or ""
        if ppm and (ppm >= 1000 or (x.mode == "quality" and ppm > 0)):
            text = f"Your rolling 12-month scorecard {ref} shows {ppm:.0f} ppm of complaints."
            bonus = ppm / 10000
        elif otd is not None and (otd < 90 or colour in ("amber", "red")):
            rated = f", rated {colour}" if colour in ("amber", "red") else ""
            text = f"Your rolling 12-month scorecard {ref} shows on-time delivery of {otd:.1f}%{rated}."
            bonus = (90 - otd) / 40
        else:
            continue
        out.append(Claim(ref, (ref,), text, "sc", strength(x.mode, "sc", bonus), (sid,)))
    return out


def complaint_claims(x):
    if x.mode != "quality":
        return []
    out = []
    for sid in x.sids:
        recent = [
            (_n(f.get("ppm")) or 0, ref, f)
            for ref, f in x.by["qn"]
            if x.ours(f, sid) and f.get("part") in x.parts and _months(x.asof, f.get("date") or "1900-01") <= 12
        ]
        if not recent:
            continue
        ppm, ref, f = max(recent)
        if ppm > 0:
            text = f"Complaint {ref} on {x.part_name(f['part'])} recorded {ppm:.0f} ppm."
            out.append(Claim(ref, (ref,), text, "qn", strength(x.mode, "qn", ppm / 20000), (sid,)))
    return out


def should_cost(x, ceiling, d):
    """Fills d.should_cost and returns the claims: an estimate below both the ask and what we pay
    now argues for us (one between them would argue for part of the increase)."""
    owners = {r["id"]: r.get("supplier_id") or "" for r in x.rows("SELECT id, supplier_id FROM should_cost")}
    out = []
    for ref, f in sorted(x.by["cost"]):
        unit = _n(f.get("unit_cost"))
        if f.get("part") not in x.parts or not unit:
            continue
        owner = owners.get(ref) or (f"sup:{ref.rsplit('-', 1)[1]}" if "-" in ref else "")
        sid = next((s for s in x.sids if _sup(s) == _sup(owner)), None)
        if owner and sid is None:
            continue  # another supplier's plant
        value = x.in_case_unit(unit, f.get("price_unit"))
        d.should_cost.setdefault(sid or "*", (round(value, 2), _n(f.get("confidence_pct")) or 10.0))
        if ceiling is not None and value >= 0.97 * ceiling:
            continue
        text = (
            f"Our bottom-up should-cost estimate {ref} for {x.part_name(f['part'])} is {unit:.2f} EUR "
            f"{per(_n(f.get('price_unit')))}."
        )
        out.append(Claim(ref, (ref,), text, "cost", strength(x.mode, "cost"), (sid,) if sid else (), value=value))
    return out


def quote_claims(x, ceiling):
    """Quotes of this case (its refs, the alternative) below the ask and what we pay now: one argues
    to every other bidder."""
    refs = [r for r in x.case.get("refs", []) if r.startswith("quote:")]
    alt = ((x.case.get("buyer") or {}).get("alternative") or {}).get("ref")
    out = []
    for ref in dict.fromkeys([*refs, *([alt] if alt and alt.startswith("quote:") else [])]):
        f = x.fact(ref)
        price = _n(f.get("unit_price")) if f else None
        if not price or f.get("part") not in x.parts:
            continue
        value = x.in_case_unit(price, f.get("price_unit"))
        if ceiling is not None and value >= ceiling:
            continue
        owner = _sup(f.get("supplier") or "")
        to = tuple(s for s in x.sids if _sup(s) != owner) or tuple(s for s in x.sids if _sup(s) == owner)
        own = all(_sup(s) == owner for s in to)
        unit = f"{price:.2f} EUR {per(_n(f.get('price_unit')))}"
        text = (
            f"Your own quote {ref} for {x.part_name(f['part'])} was {unit}."
            if own
            else f"We hold a quote, {ref}, for {x.part_name(f['part'])} at {unit}."
        )
        out.append(Claim(ref, (ref,), text, "quote", strength(x.mode, "quote"), to, per_supplier=True, value=value))
    return out


def order_claims(x, ceiling):
    """Our two latest order lines per supplier and part, when neither is above the ask or what we
    pay now."""
    out = []
    for sid in x.sids:
        for part in x.parts:
            lines = sorted(
                ((f.get("date") or "", ref, f) for ref, f in x.by["po"] if x.ours(f, sid) and f.get("part") == part),
                reverse=True,
            )[:2]
            prices = [_n(f.get("unit_price")) for *_, f in lines]
            if len(lines) < 2 or not all(prices):
                continue
            value = x.in_case_unit(max(prices), lines[0][2].get("price_unit"))
            if ceiling is not None and value > ceiling:
                continue
            (_, a, fa), (_, b, _) = lines
            text = (
                f"Our latest orders for {x.part_name(part)}, {a} and {b}, were placed at {prices[0]:.2f} and "
                f"{prices[1]:.2f} EUR {per(_n(fa.get('price_unit')))}."
            )
            out.append(Claim(f"po:{sid}:{part}", (a, b), text, "po", strength(x.mode, "po"), (sid,), value=value))
    return out


def forecast_claims(x):
    """The latest forecast snapshot per part, when demand grows (volume to trade for price)."""
    out = []
    for part in x.parts:
        snaps = [(ref.split("@")[-1], ref, f) for ref, f in x.by["fc"] if f.get("part") == part]
        if not snaps:
            continue
        _, ref, f = max(snaps)
        change = _n(f.get("change_pct"))
        if change is not None and change >= 1:
            text = f"Our latest demand forecast {ref} shows a change of {change:+.1f}% for {x.part_name(part)}."
            out.append(Claim(ref, (ref,), text, "fc", strength(x.mode, "fc", change / 50)))
    return out


def _contract(x, sid):
    rows = [
        (f.get("valid_to") or "", ref, f)
        for ref, f in x.by["ctr"]
        if "#" not in ref and x.ours(f, sid) and _n(f.get("days"))
    ]
    return max(rows)[1:] if rows else None


def notice_claims(x):
    """A letter giving less notice than the contract requires."""
    docs = [x.case.get("trigger_doc"), *x.case.get("refs", []), *(x.case.get("facts") or {})]
    out = []
    for ref in dict.fromkeys(r for r in docs if r and r.startswith("doc:")):
        f = x.fact(ref) or {}
        days = _n(f.get("days"))
        sid = next((s for s in x.sids if x.ours(f, s)), None)
        if days is None or sid is None or not (ctr := _contract(x, sid)):
            continue
        cref, cf = ctr
        if days < _n(cf["days"]):
            text = (
                f"Your letter {ref} gives {days:.0f} days' notice, while our agreement {cref} requires "
                f"{_n(cf['days']):.0f} days."
            )
            out.append(Claim(f"notice:{ref}", (ref, cref), text, "notice", strength(x.mode, "notice"), (sid,)))
    return out


def news_claims(x):
    out = []
    for ref, f in x.by["news"]:
        sid = next((s for s in x.sids if x.ours(f, s)), None)
        if sid and f.get("topic") in GOOD_NEWS and f.get("headline"):
            if _months(x.asof, f.get("date") or "1900-01") <= 12:
                text = f"{ref} reports: {f['headline'].rstrip('.')}."
                out.append(Claim(ref, (ref,), text, "news", strength(x.mode, "news"), (sid,)))
    return out


def rival_claims(cs, sid):
    """RFQ only: other bidders' standing offers in this session that are cheaper than this
    supplier's - citable to each other bidder once."""
    if cs.playbook.mode != "rfq" and len(cs.case.get("suppliers", [])) < 2:
        return []
    mine = cs.threads[sid].standing.get("unit_price") if sid in cs.threads else None
    out = []
    for other, t in cs.threads.items():
        price = t.standing.get("unit_price")
        if other == sid or not t.offer_id or not price or (mine and price >= mine):
            continue
        gap = (mine - price) / mine if mine else 0.0
        text = f"Another bidder's offer {t.offer_id} for this part stands at {price:.2f} EUR."
        out.append(
            Claim(
                t.offer_id,
                (t.offer_id,),
                text,
                "offer",
                strength(cs.playbook.mode, "offer", gap * 5),
                (sid,),
                per_supplier=True,
                value=float(price),
            )
        )
    return out


# -- internal figures ------------------------------------------------------------------------------
def _ask(x):
    """(asked price in the case's unit, asked %) from the trigger letter, else (None, None)."""
    f = x.fact(x.case.get("trigger_doc")) or {}
    price, pct = _n(f.get("asked_price")), _n(f.get("asked_pct"))
    current = _n((x.case.get("buyer") or {}).get("current_unit_price"))
    if price and current:
        pct = (price / current - 1) * 100
    elif pct is not None and current and not price:
        price = current * (1 + pct / 100)
    return price, pct


def _justified(x, drivers):
    """The cost-weighted 12-month move of each part's drivers, averaged over the parts."""
    moves = []
    for d in drivers.values():
        parts = [(w, _move12(x.series.get(s, []))) for s, w in d.items()]
        if any(m is not None for _, m in parts):
            moves.append(sum(w * m for w, m in parts if m is not None))
    return round(sum(moves) / len(moves), 2) if moves else None


def _settled(x):
    out = {}
    for sid in x.sids:
        for part in x.parts:
            for r in x.rows(
                "SELECT settled_pct FROM negotiation_history WHERE supplier_id IN (?, ?) AND part_id = ? ORDER BY date",
                *_records(sid),
                part,
            ):
                if (v := _n(r.get("settled_pct"))) is not None:
                    out.setdefault(sid, []).append(v)
    return out


def _cash(x):
    out = {}
    for sid in x.sids:
        short = any(x.ours(f, sid) and f.get("topic") in CASH_NEWS for _, f in x.by["news"])
        for r in x.rows("SELECT * FROM supplier_intel WHERE supplier_id IN (?, ?)", *_records(sid)):
            now, ago, equity = (
                _n(r.get("credit_index")),
                _n(r.get("credit_index_12m_ago")),
                _n(r.get("equity_ratio_pct")),
            )
            small = str(r.get("turnover_band") or "").startswith("0-2")
            short |= bool(now and ago and now - ago >= 20) or (equity is not None and equity < 20) or small
        out[sid] = short
    return out


def _excerpt(x, ref, n):
    try:
        text = x.package.document(ref)
    except Exception:
        text = (x.fact(ref) or {}).get("text") or ""
    return " ".join(str(text).split())[:n]


def _notes(x):
    """Short internal excerpts: the trigger letter, the latest letters and news, how we pay."""
    out = []
    if trig := x.case.get("trigger_doc"):
        out.append(f"trigger {trig}: {_excerpt(x, trig, 600)}")
    for sid in x.sids:
        for r in x.rows(
            "SELECT id, date, kind, subject FROM documents WHERE supplier_id IN (?, ?) ORDER BY date DESC LIMIT 3",
            *_records(sid),
        ):
            if r["id"] != trig:
                out.append(f"{sid} {r['date']} {r['kind']} '{r['subject']}': {_excerpt(x, r['id'], 220)}")
        news = sorted(((f.get("date") or "", f.get("headline") or "") for _, f in x.by["news"] if x.ours(f, sid)))
        out += [f"{sid} news {d}: {h}" for d, h in news[-2:]]
        for r in x.rows(
            "SELECT AVG(julianday(paid_date) - julianday(due_date)) AS late FROM invoices "
            "WHERE supplier_id IN (?, ?) AND discount_taken = 0 AND paid_date IS NOT NULL AND paid_date != ''",
            *_records(sid),
        ):
            if (late := _n(r.get("late"))) is not None and late > 1:
                out.append(f"{sid}: we pay {late:.0f} days late on average - never claim we pay on time")
        for r in x.rows(
            "SELECT c.id, c.series, c.material_share, c.deadband_pct FROM contract_clauses c "
            "JOIN contracts k ON k.id = c.contract_id WHERE k.supplier_id IN (?, ?) AND c.type = 'index'",
            *_records(sid),
        ):
            out.append(f"{sid}: contract index clause {r['id']} on {r['series']}, share {r['material_share']}")
    return out


def _render(d):
    lines = ["CLAIMS (cite verbatim, one per sentence):"]
    lines += [f"- [{c.cid}] {c.sentence}" for c in sorted(d.claims, key=lambda c: -c.strength)] or ["- none"]
    lines.append("INTERNAL (never quote these figures):")
    if d.justified_move_pct is not None:
        lines.append(f"- cost-weighted driver move over 12 months: {d.justified_move_pct:+.1f}%")
    if d.ask_pct is not None:
        lines.append(f"- the supplier asks {d.ask_pct:+.1f}%")
    lines += [f"- should-cost {k}: {v:.2f} EUR (+-{c:g}%)" for k, (v, c) in d.should_cost.items()]
    lines += [f"- past settlements {k}: {', '.join(f'{v:+.1f}%' for v in s)}" for k, s in d.settled_pct.items()]
    lines += [f"- {k} looks short of cash" for k, v in d.cash_pressure.items() if v]
    lines += [f"- {n}" for n in d.notes]
    return "\n".join(lines)


def build_dossier(package, case):
    """The case's evidence: buyer-favourable claims plus internal figures. Never raises on data."""
    x = _Data(package, case)
    d = EvidenceDossier(case_id=case["case_id"])
    ask, d.ask_pct = _ask(x)
    current = _n((case.get("buyer") or {}).get("current_unit_price"))
    ceiling = min((p for p in (ask, current) if p), default=None)
    drivers = _drivers(x)
    sections = (
        lambda: index_claims(x, drivers),
        lambda: scorecard_claims(x),
        lambda: complaint_claims(x),
        lambda: should_cost(x, ceiling, d),
        lambda: quote_claims(x, ceiling),
        lambda: order_claims(x, ceiling),
        lambda: forecast_claims(x),
        lambda: notice_claims(x),
        lambda: news_claims(x),
    )
    for section in sections:
        try:
            d.claims += section()
        except Exception as e:  # a malformed fact must not cost the case
            log.warning("%s dossier section failed: %s", case["case_id"], e)
    for name, fill in (
        ("justified_move_pct", lambda: _justified(x, drivers)),
        ("settled_pct", lambda: _settled(x)),
        ("cash_pressure", lambda: _cash(x)),
        ("notes", lambda: _notes(x)),
    ):
        try:
            setattr(d, name, fill())
        except Exception as e:
            log.warning("%s dossier %s failed: %s", case["case_id"], name, e)
    d.claims.sort(key=lambda c: -c.strength)
    d.text = _render(d)
    return d
