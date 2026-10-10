"""A selector chooses approved IDs. All supplier-facing sentences and numbers are code-rendered."""

import json
import time

from .guard import ID_RE, favours
from .model import parse_json
from .playbook import playbook
from .prompts import PREP, TURN
from .records import DisclosurePacket, NegotiationBrief, PublicMessage

MIN_CALL_S = 3.0
PREP_BUDGET_S = PREP.budget_s
SIGN_OFF = ["", "Best regards", "Hollerau Pumpenwerke, Purchasing"]
QUESTIONS = {
    "breakdown": "Could you send us a cost breakdown for this part?",
    "lead_time": "What is the shortest lead time you can confirm?",
    "variant": "Is there a cheaper variant or specification you would suggest?",
    "validity": "Could you confirm until when your offer remains valid?",
    "technical": "Could you clarify which technical approval or specification is required?",
}
ACKNOWLEDGMENTS = {
    "neutral": "Thank you for your message.",
    "final": "Thank you for clarifying your position.",
    "cash": "Thank you for explaining the importance of payment timing.",
    "technical": "Thank you for outlining the technical requirements.",
}
FIELD_NAMES = {
    "payment_days": "payment",
    "contract_months": "contract duration",
    "volume_per_year": "volume",
    "lead_time_days": "delivery",
    "effective_date": "effective date",
    "incoterm": "delivery responsibility",
    "index_clause": "indexation",
    "ppm_commitment": "quality",
    "tooling_eur": "tooling",
    "chargeback_eur_per_complaint": "complaint chargeback",
}


def disclosure(cs, t, proposal, claims, *, blocked_questions=()):
    sheets = tuple(p.terms for p in proposal.packages)
    observation = t.observation
    is_open = t.status == "open"
    brief_questions = cs.brief.questions if is_open and getattr(cs, "brief", None) else ()
    questions = [q for q in (proposal.question, *brief_questions) if q in QUESTIONS and q not in blocked_questions]
    if is_open and not questions and observation and observation.dependencies and "technical" not in blocked_questions:
        questions = ["technical"]
    acks = ["neutral"]
    if t.status in ("final_offer", "agreed"):
        acks.append("final")
    if observation and observation.cash_pressure:
        acks.append("cash")
    if observation and observation.dependencies:
        acks.append("technical")
    trades = [
        f
        for f in FIELD_NAMES
        if sheets
        and all(f in s for s in sheets)
        and (len({json.dumps(s[f], sort_keys=True) for s in sheets}) > 1 or (observation and f in observation.values))
    ]
    return DisclosurePacket(
        sid=t.sid,
        mode=cs.playbook.mode,
        goal=cs.playbook.goal,
        rev=t.rev,
        packages=sheets,
        claims=tuple(claims),
        question=questions[0] if questions else None,
        their_last=t.last_body[:3000],
        allowed_numbers=frozenset(),
        subject=f"Re: {cs.case['title']}",
        status=t.status,
        questions=tuple(dict.fromkeys(questions)),
        acknowledgments=tuple(acks),
        trades=tuple(trades),
    )


def _render(pkt, sheets, claims, question, acknowledgment="neutral", trade=None, tone="professional"):
    lines = ["Dear Sir or Madam,", "", ACKNOWLEDGMENTS[acknowledgment]]
    if tone == "partner":
        lines.append("We appreciate your work with us on this supply.")
    lines += [c.sentence for c in claims]
    if len(sheets) > 1:
        lines.append("We attach alternative packages; any of them works for us.")
    elif sheets:
        lines.append("Please find our proposal in the attached term sheet.")
    if trade:
        lines.append(f"We can offer the {FIELD_NAMES[trade]} terms shown if you confirm this package as a whole.")
    if sheets:
        lines.append(
            "Please confirm which attached package you can support."
            if len(sheets) > 1
            else "Please confirm whether you can support the attached package."
        )
    if question:
        lines.append(QUESTIONS[question])
    refs = tuple(dict.fromkeys(i for c in claims for i in c.ids))
    return PublicMessage(pkt.subject, "\n".join(lines + SIGN_OFF), tuple(sheets), refs)


def template(pkt: DisclosurePacket) -> PublicMessage:
    sheets = pkt.packages[:1] if pkt.status in ("final_offer", "agreed") else pkt.packages
    price = min((s["unit_price"] for s in sheets if s.get("unit_price")), default=None)
    claims = tuple(c for c in pkt.claims if favours(c, price)) if pkt.status == "open" else ()
    question = pkt.question if pkt.question in pkt.questions else None
    return _render(
        pkt,
        sheets,
        claims,
        question,
        "final" if "final" in pkt.acknowledgments else "neutral",
        tone="partner" if pkt.goal in ("stabilise", "secure_supply") else "professional",
    )


def write(model, pkt, budget_s, case_id="", focus=""):
    return write_turn(model, pkt, budget_s, case_id, focus)[0]


def write_turn(model, pkt, budget_s, case_id="", focus="", *, deadline=None, observation=None):
    end = min(time.monotonic() + max(0, budget_s), deadline if deadline is not None else float("inf"))
    packages = {f"P{i}": s for i, s in enumerate(pkt.packages, 1)}
    claims = {f"C{i}": c for i, c in enumerate(pkt.claims, 1)}
    if model is None or not packages or end - time.monotonic() < MIN_CALL_S:
        return template(pkt), "template", None
    system, user = TURN.render(
        case_id=case_id,
        case_type=pkt.mode,
        goal=pkt.goal,
        rev=pkt.rev,
        budget_s=max(0, budget_s),
        focus=focus or "-",
        packages=json.dumps(packages),
        claims="\n".join(f"{i}: {c.sentence}" for i, c in claims.items()) or "(none)",
        questions=json.dumps(pkt.questions),
        acknowledgments=json.dumps(pkt.acknowledgments),
        trades=json.dumps(pkt.trades),
        status=pkt.status,
        observation=json.dumps(observation or {}),
        their_last=pkt.their_last or "(none)",
    )
    raw = model.complete(
        system,
        user,
        end - time.monotonic(),
        role="communication",
        deadline=end,
        validate=lambda text: turn_errors(parse_json(text), packages, claims, pkt=pkt),
    )
    out = parse_json(raw)
    if out is None:
        return template(pkt), "template", None
    return assemble(pkt, out, packages, claims), "llm", {"rationale": out["rationale"]}


def turn_errors(out, packages, claims, allowed_numbers=None, *, pkt=None):
    errors = TURN.check(out)
    if errors:
        return errors
    errors += [f"package {i} not offered" for i in out["package_ids"] if i not in packages]
    errors += [f"claim {i} not offered" for i in out["claim_ids"] if i not in claims]
    if errors:
        return errors
    chosen = [packages[i] for i in out["package_ids"]]
    price = min((s["unit_price"] for s in chosen if s.get("unit_price")), default=None)
    errors += ["claim no longer buyer-favourable" for i in out["claim_ids"] if not favours(claims[i], price)]
    if pkt:
        if out["question"] and out["question"] not in pkt.questions:
            errors.append("question not offered")
        if out["acknowledgment"] not in pkt.acknowledgments:
            errors.append("acknowledgment not offered")
        if out["trade"] and (out["trade"] not in pkt.trades or any(out["trade"] not in s for s in chosen)):
            errors.append("trade not present in selected packages")
        if pkt.status in ("final_offer", "agreed") and (len(chosen) != 1 or out["claim_ids"] or out["question"]):
            errors.append("final offer requires a single confirmation package")
    return errors


def assemble(pkt, out, packages, claims):
    return _render(
        pkt,
        [packages[i] for i in out["package_ids"]],
        [claims[i] for i in out["claim_ids"]],
        out["question"],
        out["acknowledgment"],
        out["trade"],
        out["tone"],
    )


def prep(model, case, dossier, budget_s=PREP_BUDGET_S):
    default = playbook(case)
    claims = dossier.claims if dossier else []
    sources = {i for c in claims for i in c.ids} | set(ID_RE.findall(dossier.text if dossier else ""))
    sources |= set(ID_RE.findall(json.dumps(case)))
    claim_ids = {c.cid for c in claims}
    suppliers = set(case.get("suppliers", []))
    system, user = PREP.render(
        case_id=case["case_id"],
        case_type=case.get("type", ""),
        goal=default.goal,
        rev=0,
        budget_s=budget_s,
        case=json.dumps(case, default=str)[:8000],
        dossier=(dossier.text or "-")[:12000] if dossier else "(none)",
        claim_ids=json.dumps(sorted(claim_ids)),
        defaults=json.dumps({"beta": default.beta, "k": default.k, "p_walk": default.p_walk}),
    )

    def validate(raw):
        out = parse_json(raw)
        errors = PREP.check(out)
        if errors:
            return errors
        for fact in out["facts"]:
            if any(i not in sources for i in fact["source_ids"]) or (
                fact["kind"] != "hypothesis" and not fact["source_ids"]
            ):
                errors.append("fact requires supplied sources")
        for interest in out["interests"]:
            if (
                interest["supplier_id"] not in suppliers
                or not interest["source_ids"]
                or any(i not in sources for i in interest["source_ids"])
            ):
                errors.append("interest requires supplier and sources")
        if any(i not in claim_ids for i in out["permitted_disclosures"]):
            errors.append("unapproved disclosure")
        if any(f not in case["negotiable"] for f in out["lever_priority"]):
            errors.append("non-negotiable lever")
        return errors

    raw = model.complete(system, user, budget_s, role="preparation", validate=validate) if model else None
    out = parse_json(raw)
    if out is None:
        return NegotiationBrief(permitted_disclosures=tuple(c.cid for c in claims), lever_priority=default.levers)
    return NegotiationBrief(
        tuple(out["facts"]),
        tuple(out["interests"]),
        tuple(out["risks"]),
        tuple(out["lever_priority"]),
        tuple(out["questions"]),
        tuple(out["permitted_disclosures"]),
        out["playbook"],
    )
