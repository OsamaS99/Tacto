"""The Communication step: a DisclosurePacket (approved facts, claims, packages) becomes the
e-mail. One combined model call (analyst, planner, writer) may pick a subset of the approved
packages, claims and one question kind by ID and write the opening and the ask; code assembles the
body, so claim figures are verbatim and the model writes none. Anything invalid gets one repair
re-ask if time allows, then the template - which always works."""

import json
import logging
import re
import time

from .guard import ID_RE
from .model import parse_json
from .playbook import playbook
from .prompts import PREP, QUESTION_KINDS, TURN
from .records import DisclosurePacket, PublicMessage

log = logging.getLogger("arena")
MIN_CALL_S = 3.0  # below this a call cannot finish: the template instead
PREP_BUDGET_S = 25.0
NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")
SIGN_OFF = ["", "Best regards", "Hollerau Pumpenwerke, Purchasing"]

QUESTIONS = {
    "breakdown": "Could you send us a cost breakdown for this part?",
    "lead_time": "What is the shortest lead time you can confirm?",
    "variant": "Is there a cheaper variant or specification you would suggest?",
    "validity": "Could you confirm until when your offer remains valid?",  # a keep-alive: code only
}


def disclosure(cs, t, proposal, claims):
    sheets = tuple(p.terms for p in proposal.packages)
    numbers = {_num(v) for s in sheets for v in s.values() if isinstance(v, int | float)}
    return DisclosurePacket(
        sid=t.sid,
        mode=cs.playbook.mode,
        goal=cs.playbook.goal,
        rev=t.rev,
        packages=sheets,
        claims=tuple(claims),
        question=proposal.question,
        their_last=t.last_body[:1500],
        allowed_numbers=frozenset(numbers),
        subject=f"Re: {cs.case['title']}",
    )


def _num(v):
    return f"{v:.2f}" if isinstance(v, float) else str(v)


def template(pkt: DisclosurePacket) -> PublicMessage:
    """The deterministic e-mail: polite, the claims verbatim, the packages described by the sheets."""
    lines = ["Dear Sir or Madam,", "", "thank you for your message."]
    lines += [c.sentence for c in pkt.claims]
    if len(pkt.packages) > 1:
        lines.append("We attach alternative packages; any of them works for us.")
    elif pkt.packages:
        lines.append("Please find our proposal in the attached term sheet.")
    if pkt.question:
        lines.append(QUESTIONS.get(pkt.question, pkt.question))
    lines += ["", "Best regards", "Hollerau Pumpenwerke, Purchasing"]
    refs = tuple(dict.fromkeys(i for c in pkt.claims for i in c.ids))
    return PublicMessage(pkt.subject, "\n".join(lines), pkt.packages, refs)


def write(model, pkt, budget_s, case_id="", focus=""):
    """The model's e-mail within `budget_s`; the template when it cannot."""
    return write_turn(model, pkt, budget_s, case_id, focus)[0]


def write_turn(model, pkt, budget_s, case_id="", focus=""):
    """(PublicMessage, source llm|repaired|template, the model's assessment or None)."""
    end = time.monotonic() + budget_s
    packages = {f"P{i}": s for i, s in enumerate(pkt.packages, 1)}
    claims = {f"C{i}": c for i, c in enumerate(pkt.claims, 1)}
    if model is None or not packages:
        return template(pkt), "template", None
    system, user = TURN.render(
        case_id=case_id,
        case_type=pkt.mode,
        goal=pkt.goal,
        rev=pkt.rev,
        budget_s=budget_s,
        focus=focus or "-",
        packages="\n".join(f"{i}: {json.dumps(s)}" for i, s in packages.items()),
        claims="\n".join(f"{i}: {c.sentence}" for i, c in claims.items()) or "(none)",
        questions=", ".join(QUESTION_KINDS),
        message_id=f"msg-{pkt.rev}",
        their_last=pkt.their_last or "(none)",
    )
    source = "llm"
    while (left := end - time.monotonic()) >= MIN_CALL_S:
        out = parse_json(model.complete(system, user, left))
        if out is None:
            break  # no answer from any model: a re-ask would not get one either
        errors = turn_errors(out, packages, claims, pkt.allowed_numbers | set(NUM_RE.findall(pkt.subject)))
        if not errors:
            return assemble(pkt, out, packages, claims), source, out["assessment"]
        if source == "repaired":
            break
        log.info("turn call invalid (%s) - one repair", "; ".join(errors[:3]))
        user += f"\n\nYOUR LAST REPLY WAS REJECTED: {'; '.join(errors[:5])}. Return the corrected JSON object only."
        source = "repaired"
    return template(pkt), "template", None


def turn_errors(out, packages, claims, allowed_numbers):
    """Schema errors, IDs that were not offered, and figures or data IDs in the model's own text."""
    errors = TURN.check(out)
    if errors:
        return errors
    p, m = out["proposal"], out["message"]
    errors += [f"package {i} not offered" for i in p["package_ids"] if i not in packages]
    errors += [f"claim {i} not offered" for i in p["claim_ids"] if i not in claims]
    for field in ("subject", "opening", "ask"):
        if ID_RE.search(m[field]):
            errors.append(f"{field}: data IDs are added by code")
        if bad := [n for n in NUM_RE.findall(m[field]) if n not in allowed_numbers]:
            errors.append(f"{field}: figures {bad} are not approved")
    return errors


def assemble(pkt, out, packages, claims):
    p, m = out["proposal"], out["message"]
    sheets = tuple(packages[i] for i in dict.fromkeys(p["package_ids"]))
    chosen = [claims[i] for i in dict.fromkeys(p["claim_ids"])]
    lines = ["Dear Sir or Madam,", "", m["opening"].strip(), *(c.sentence for c in chosen)]
    lines.append(
        "We attach alternative packages; any of them works for us."
        if len(sheets) > 1
        else "Please find our proposal in the attached term sheet."
    )
    if m["ask"].strip():
        lines.append(m["ask"].strip())
    if p["question"]:
        lines.append(QUESTIONS[p["question"]])
    refs = tuple(dict.fromkeys(i for c in chosen for i in c.ids))
    return PublicMessage(m["subject"].strip() or pkt.subject, "\n".join(lines + SIGN_OFF), sheets, refs)


def prep(model, case, dossier, budget_s=PREP_BUDGET_S):
    """Playbook adjustments {beta, k, p_walk} plus notes from the preparation call; {} on failure.
    `playbook.bounded` clamps them again."""
    default = playbook(case)
    system, user = PREP.render(
        case_id=case["case_id"],
        case_type=case.get("type", ""),
        goal=default.goal,
        rev=0,
        budget_s=budget_s,
        case=json.dumps(case, ensure_ascii=False, default=str)[:6000],
        dossier=(dossier.text or "-")[:8000],
        defaults=json.dumps({"beta": default.beta, "k": default.k, "p_walk": default.p_walk}),
    )
    out = parse_json(model.complete(system, user, budget_s)) if model is not None else None
    if out is None or (errors := PREP.check(out)):
        if out is not None:
            log.info("%s prep invalid: %s", case["case_id"], "; ".join(errors[:3]))
        return {}
    return {**out["playbook"], "notes": out["notes"]}
