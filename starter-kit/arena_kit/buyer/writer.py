"""The Communication step: a DisclosurePacket (approved facts, claims, packages) becomes the
e-mail. The template always works; the model version (`write`) is added in the LLM layer."""

from .records import DisclosurePacket, PublicMessage

QUESTIONS = {
    "breakdown": "Could you send us a cost breakdown for this part?",
    "lead_time": "What is the shortest lead time you can confirm?",
    "variant": "Is there a cheaper variant or specification you would suggest?",
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


def write(model, pkt, budget_s):
    """The model's e-mail within `budget_s`; the template when it cannot."""
    return template(pkt)


def prep(model, case, dossier):
    """Playbook adjustments from the preparation call (bounded by `playbook.bounded`)."""
    return {}
