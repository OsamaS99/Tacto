"""The guard: every move - the model's, a repaired one, the template - passes here before it is
sent. Package validity, the claim rules (at most 3 checked IDs, each once per case, a rival's quote
once per supplier), message limits and privacy."""

import re

from .records import PublicMessage

ID_RE = re.compile(r"\b(?:idx|quote|offer|sc|qn|po|cost|fc|news|doc|ctr|sup|part|gr|inv|rfq):[\w.:@#/-]*\w")
MAX_CLAIMS, MAX_SUBJECT, MAX_BODY, MAX_REFS = 3, 300, 8000, 20


class GuardReject(Exception):
    pass


def pick_claims(cs, sid, limit=MAX_CLAIMS):
    """The strongest unused claims for this supplier, at most `limit` IDs-sets."""
    if cs.dossier is None:
        return ()
    used, to = cs.cited, cs.cited_to.get(sid, set())
    out = []
    for c in sorted(cs.dossier.claims, key=lambda c: -c.strength):
        if c.suppliers and sid not in c.suppliers:
            continue
        if any((i in to) if c.per_supplier else (i in used) for i in c.ids):
            continue
        out.append(c)
        if len(out) == limit:
            break
    return tuple(out)


def check_message(cs, econ, space, proposal, packet, msg):
    """The message as it may go out, or GuardReject."""
    t = cs.threads[proposal.sid]
    sheets = list(msg.term_sheets)
    if len(sheets) > 3:
        raise GuardReject("more than 3 term sheets")
    for s in sheets:
        if errors := space.valid(s, t.std):
            raise GuardReject(f"invalid term sheet: {errors}")
    approved = {i for c in packet.claims for i in c.ids}
    if not set(msg.refs) <= approved:
        raise GuardReject(f"refs not approved: {set(msg.refs) - approved}")
    body = ID_RE.sub(lambda m: m.group(0) if m.group(0) in approved else "", msg.body)
    if len(approved) > MAX_CLAIMS * 2 or len(msg.refs) > MAX_REFS:
        raise GuardReject("too many refs")
    return PublicMessage(msg.subject[:MAX_SUBJECT], body.strip()[:MAX_BODY], tuple(sheets), tuple(msg.refs))
