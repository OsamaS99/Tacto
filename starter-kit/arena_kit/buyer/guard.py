"""The guard: every move - the model's, a repaired one, the template - passes here before it is
sent. Packages must be exactly the approved ones; claims come only from the code-built menu (at
most 3 arguments and 3 checked IDs, each ID once per case, a rival's quote or offer once per
supplier); the prose may not leak a number, an ID or a sentence about our position that code did
not approve. A sentence that fails is dropped; approved claim sentences go out verbatim."""

import math
import re

from .acceptance import feasible
from .dossier import rival_claims
from .records import PublicMessage

ID_RE = re.compile(r"\b(?:idx|quote|offer|sc|qn|po|cost|fc|news|doc|ctr|sup|part|gr|inv|rfq):[\w.:@#/-]*\w")
NUM_RE = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?!\w)")
DATE_RE = re.compile(r"\b\d{4}-\d{2}(?:-\d{2})?\b")
SENT_RE = re.compile(r"(?<=[.!?])\s+")
PH_RE = re.compile(r"(\x00[A-Z]\x00)")
# never said: budget, time pressure, our C, how dependent we are, internal figures - and a
# promise to send or come back that we never keep (case P11: four IOUs, flagged in every reply)
BLOCK = re.compile(
    r"\bbudget|\bdeadline|\burgen(?:t|cy|tly)\b|\bmust close\b|\btime pressure\b|\bwalk[- ]?away\b"
    r"|\bour (?:total )?costs?\b|\btotal cost\b|\bC\s*=|\breservation\b|\btarget price\b|\binternal\b"
    r"|\bjustified (?:move|increase)\b|\bno (?:other|alternative) (?:supplier|source|option)s?\b"
    r"|\bonly (?:supplier|source)\b|\bsingle source\b|\bdepend(?:ent|ence)? on you\b"
    r"|\bpay(?:ing)? (?:you )?(?:on time|promptly|punctually)\b|\b(?:on-time|punctual) payment"
    r"|\b(?:come|get) back to\b|\bwill (?:send|provide|share|forward)\b"
    r"|\b(?:we (?:accept|agree)|agreed|exclusive|exclusivity|guarantee|future business)\b"
    r"|\b(?:our|maximum) (?:budget|target|limit)\b",
    re.I,
)
# rivals: fair game in an RFQ, a leak of our alternative anywhere else
RIVAL = re.compile(
    r"\balternatives? (?:supplier|source|quote|offer|bid|vendor)s?\b|\bother (?:supplier|offer|quote|bid)(?:s|ders?)?\b"
    r"|\banother (?:supplier|bidder|offer|quote)\b|\bcompetit(?:or|ors|ion|ive (?:offer|quote|bid))\b|\bcompeting\b",
    re.I,
)
TOPIC = {"qn": "sc"}  # claim kinds that make the same point
SMALL = {1.0, 2.0, 3.0}  # counting words ("our 2 packages") leak nothing
SPELLED_VALUE = re.compile(
    r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty|thirty|forty|fifty|"
    r"sixty|seventy|eighty|ninety|hundred|thousand)(?:[- ]\w+){0,2} "
    r"(?:days?|months?|years?|euros?|percent|per cent|units?|pieces?)\b",
    re.I,
)
MAX_CLAIMS, MAX_IDS, MAX_SHEETS, MAX_SUBJECT, MAX_BODY, MAX_REFS = 3, 3, 3, 300, 8000, 20


class GuardReject(Exception):
    pass


def _weight(c):
    """Checked IDs a claim spends: an index pair is one argument."""
    return 1 if c.kind == "idx" else len(c.ids)


def _fresh(cs, sid, c):
    """Not cited yet (a rival's quote or offer: not to this supplier) and about this supplier."""
    if c.suppliers and sid not in c.suppliers:
        return False
    used = cs.cited_to.get(sid, set()) if c.per_supplier else cs.cited
    return not any(i in used for i in c.ids)


def _favours(c, price):
    """A price figure still argues for us against `price` (the lower of the supplier's standing price
    and our dearest sheet: a figure above our own bid argues against it - case P18)."""
    if c.value is None or not price:
        return True
    return c.value < (0.97 * price if c.kind == "cost" else price)


def _cap(claims, limit=MAX_CLAIMS):
    """In order: at most `limit` claims, MAX_IDS checked IDs, no ID twice, one claim per kind
    except index moves - a scorecard and a complaint are one kind (one quality figure per message:
    case P11 had both read as one claim, and the lot ppm contradicted the scorecard)."""
    out, ids, kinds = [], set(), set()
    for c in claims:
        kind = TOPIC.get(c.kind, c.kind)
        if len(out) == limit or sum(map(_weight, out)) + _weight(c) > MAX_IDS:
            continue
        if ids & set(c.ids) or (kind in kinds and kind != "idx"):
            continue
        out.append(c)
        ids |= set(c.ids)
        kinds.add(kind)
    return out


def pick_claims(cs, sid, limit=MAX_CLAIMS, bid=None):
    """The strongest unused claims for this supplier that still favour us; `bid`: the dearest price
    we send with them."""
    menu = list(cs.dossier.claims) if cs.dossier is not None else []
    menu += rival_claims(cs, sid)
    price = cs.threads[sid].standing.get("unit_price") if sid in cs.threads else None
    price = min((p for p in (price, bid) if p), default=None)
    ok = [c for c in menu if _fresh(cs, sid, c) and _favours(c, price)]
    return tuple(_cap(sorted(ok, key=lambda c: -c.strength), limit))


def _allowed(packet, claims):
    """Numbers and dates the prose may use: the packages', the approved claims', the supplier's own."""
    texts = [c.sentence for c in claims] + [packet.their_last or ""]
    values = [str(v) for s in packet.packages for v in s.values() if isinstance(v, str)]
    dates = {d for t in texts + values for d in DATE_RE.findall(t)}
    nums = set(SMALL) | {int(d[:4]) for d in dates}
    for raw in [*packet.allowed_numbers, *(n for t in texts for n in NUM_RE.findall(t))]:
        try:
            nums.add(round(float(str(raw).replace(",", "")), 2))
        except ValueError:
            pass
    return nums, dates


def _clean(s, nums, dates, rfq):
    """The sentence with stray IDs removed, or None if it says what we must not."""
    s = ID_RE.sub("", s)
    s = re.sub(r"\(\s*[,;\s]*\)", "", s)
    if BLOCK.search(s) or SPELLED_VALUE.search(s) or (not rfq and RIVAL.search(s)):
        return None
    # Numerals in one dimension cannot authorize claims in another (90 payment days != 90% OTD).
    # Approved factual sentences are protected verbatim; all remaining prose is number-free.
    if NUM_RE.search(s) or DATE_RE.search(s):
        return None
    return s


def scrub(body, claims, nums, dates, rfq):
    """(body, claims kept verbatim): claim sentences protected, every other sentence cleaned."""
    kept, marks = [], {}
    for c in claims:
        if c.sentence in body:
            mark = f"\x00{chr(65 + len(kept))}\x00"
            body = body.replace(c.sentence, mark, 1).replace(c.sentence, "")
            kept.append(c)
            marks[mark] = c.sentence
    lines = []
    for line in body.split("\n"):
        pieces = []
        for piece in PH_RE.split(line):
            if piece in marks:
                pieces.append(piece)
            elif piece.strip():
                good = [s for s in (_clean(p, nums, dates, rfq) for p in SENT_RE.split(piece)) if s and s.strip()]
                pieces.append(" ".join(good))
        text = re.sub(r"[ \t]{2,}", " ", " ".join(p for p in pieces if p)).strip()
        if text or not line.strip():
            lines.append(text)
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    for mark, sentence in marks.items():
        out = out.replace(mark, sentence)
    return out, kept


def check_message(cs, econ, space, proposal, packet, msg):
    """The message as it may go out, or GuardReject."""
    t = cs.threads[proposal.sid]
    sheets = []
    for s in msg.term_sheets:
        if s not in packet.packages:
            raise GuardReject(f"term sheet not approved: {s}")
        if errors := space.valid(s, t.std):
            raise GuardReject(f"invalid term sheet: {errors}")
        _financial(cs, econ, proposal.sid, s)
        if s not in sheets:
            sheets.append(s)
    if len(sheets) > MAX_SHEETS:
        raise GuardReject("more than 3 term sheets")
    if packet.packages and not sheets:
        raise GuardReject("the approved packages were dropped")
    price = min((s["unit_price"] for s in sheets if s.get("unit_price")), default=None)
    claims = _cap([c for c in packet.claims if _fresh(cs, proposal.sid, c) and _favours(c, price)])
    nums, dates = _allowed(packet, claims)
    rfq = cs.playbook.mode == "rfq" or len(cs.case.get("suppliers", [])) > 1
    body, kept = scrub(msg.body or "", claims, nums, dates, rfq)
    if not body:
        raise GuardReject("nothing left to send after scrubbing")
    if len(body) > MAX_BODY:
        raise GuardReject(f"body of {len(body)} characters")
    refs = tuple(dict.fromkeys(i for c in kept for i in c.ids))[:MAX_REFS]
    subject = (msg.subject or "").strip()
    if not subject or ID_RE.search(subject) or _clean(subject, nums, dates, rfq) != subject:
        subject = packet.subject
    return PublicMessage(subject[:MAX_SUBJECT], body, tuple(sheets), refs)


def _financial(cs, econ, sid, terms):
    cost = econ.c(sid, terms)
    bound = min(
        [
            cs.c_alt,
            cs.targets.get(sid, (math.inf, math.inf))[1],
            *(econ.c(o.sid, o.standing) for o in cs.threads.values() if o.sid != sid and feasible(cs, econ, o)),
        ]
    )
    if not math.isfinite(cost) or cost > bound + 0.01:
        raise GuardReject("package exceeds current economic reservation")


def check_action(cs, econ, space, proposal):
    """Economic and contract checks also apply to closure, templates and emergency actions."""
    t = cs.threads.get(proposal.sid)
    if t is None:
        raise GuardReject("unknown supplier")
    if proposal.kind == "send":
        for pkg in proposal.packages:
            if space.valid(pkg.terms, t.std):
                raise GuardReject("invalid proposed package")
            _financial(cs, econ, t.sid, pkg.terms)
    elif proposal.kind == "accept":
        if (
            not t.offer_id
            or proposal.term_sheet_id != t.offer_id
            or t.status not in ("open", "final_offer", "agreed")
            or not feasible(cs, econ, t)
        ):
            raise GuardReject("acceptance must name the current feasible standing offer")
        price = t.standing.get("unit_price")
        if (
            isinstance(price, bool)
            or not isinstance(price, int | float)
            or not math.isfinite(price)
            or price <= 0
            or not math.isfinite(econ.c(t.sid, t.standing, "none"))
            or econ.c(t.sid, t.standing, "none") > cs.c_alt
        ):
            raise GuardReject("acceptance loses to the alternative or has invalid economics")
    elif proposal.kind == "walk":
        if any(o.status in ("open", "final_offer", "agreed") and feasible(cs, econ, o) for o in cs.threads.values()):
            raise GuardReject("a feasible standing offer beats the alternative")
    else:
        raise GuardReject("unknown action")
