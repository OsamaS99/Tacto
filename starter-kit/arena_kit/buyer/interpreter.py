"""Grounded supplier observations; fixed classifications first, rich interpretation only when useful."""

import hashlib
import json
import re
from dataclasses import asdict

from .model import parse_json
from .prompts import FIELDS, INTERPRET
from .records import SupplierObservation

MATERIAL = re.compile(
    r"\b(if|provided|conditional|unless|drawing|specification|approval|approve|dispute|incorrect|"
    r"confidential|breakdown|wenn|sofern|freigabe|zeichnung|intern|falsch)\b",
    re.I,
)
FINAL = re.compile(r"\b(final|last offer|cannot move|no further|endgültig|letztes angebot|mehr ist nicht drin)\b", re.I)
CASH = re.compile(r"\b(cash flow|liquidity|prepayment|faster payment|shorter payment|liquidität|vorkasse)\b", re.I)
FIELD_WORDS = {
    "payment_days": r"payment|prepayment|zahlung|vorkasse",
    "contract_months": r"contract|commitment|vertrag",
    "volume_per_year": r"volume|quantity|volumen|menge",
    "lead_time_days": r"lead time|capacity|lieferzeit|kapazität",
    "effective_date": r"effective|start date|wirksam",
    "ppm_commitment": r"ppm|quality|qualität",
}


def identity(sid, message):
    body = message.get("body") or ""
    mid = str(
        message.get("message_id")
        or message.get("id")
        or hashlib.sha256(json.dumps(message, sort_keys=True, default=str).encode()).hexdigest()[:16]
    )
    fingerprint = hashlib.sha256(f"{sid}\0{mid}\0{body}".encode()).hexdigest()
    return mid, fingerprint


def interpret(sid, message, *, model=None, decisions=None, budget_s=0, deadline=None, brief=None, asked=()):
    body = message.get("body") or ""
    mid, fingerprint = identity(sid, message)
    label = (
        decisions.classify_reply(body, timeout_s=min(3.0, max(0, budget_s))) if decisions and budget_s >= 3 else None
    )
    label = label or {}
    values = tuple(label.get("values") or [f for f, words in FIELD_WORDS.items() if re.search(words, body, re.I)])
    firmness = 3 if label.get("firm_final") or FINAL.search(body) else 1
    # These rules preserve business dependencies without promoting them to official offer status.
    dependencies = tuple(
        {"text": "Technical approval or specification remains unresolved.", "evidence": body[:300]}
        for _ in [0]
        if re.search(
            r"(?:requires?|pending|subject to|awaiting|need|benötig|vorbehalt).{0,80}"
            r"(?:drawing|specification|approval|freigabe|zeichnung)",
            body,
            re.I,
        )
    )
    refused = (
        ("breakdown",)
        if re.search(r"do not disclose|confidential|figures stay|keine.*kosten|intern", body, re.I)
        else ()
    )
    answered = (
        ("breakdown",) if re.search(r"attached.*breakdown|breakdown.*attached|cost breakdown.*sent", body, re.I) else ()
    )
    if "lead_time" in asked and re.search(r"(?:lead time|lieferzeit).{0,30}\d", body, re.I):
        answered += ("lead_time",)
    fallback = SupplierObservation(
        mid,
        fingerprint,
        intent=label.get("intent", "walk_threat" if firmness == 3 else "hold"),
        firmness=firmness,
        cash_pressure=bool(label.get("cash_pressure") or CASH.search(body)),
        values=values,
        dependencies=dependencies,
        answered=answered,
        refused=refused,
        source="decisions" if label else "rules",
    )
    if not model or budget_s < 15 or not MATERIAL.search(body):
        return fallback
    system, user = INTERPRET.render(
        case_id=sid,
        case_type="reply",
        goal="procurement",
        rev=0,
        budget_s=budget_s,
        brief=json.dumps(asdict(brief)) if brief else "(none)",
        message_id=mid,
        asked=json.dumps(list(asked)),
        reply=body[:6000],
    )

    def validate(raw):
        out = parse_json(raw)
        errors = INTERPRET.check(out)
        if errors:
            return errors
        evidence = [
            *out["evidence"],
            *(t["evidence"] for t in out["conditional_trades"]),
            *(t["evidence"] for t in out["dependencies"]),
        ]
        if not evidence or any(not e.strip() or e not in body for e in evidence):
            errors.append("observation requires verbatim reply evidence")
        if any(f not in FIELDS for f in out["values"]):
            errors.append("unknown field")
        if (out["answered"] or out["refused"]) and not out["evidence"]:
            errors.append("question status requires evidence")
        if set(out["answered"]) & set(out["refused"]):
            errors.append("question cannot be answered and refused together")
        return errors

    raw = model.complete(
        system,
        user,
        min(INTERPRET.budget_s, max(0, budget_s - 3)),
        role="interpretation",
        deadline=deadline,
        validate=validate,
    )
    out = parse_json(raw)
    if not out:
        return fallback
    return SupplierObservation(
        mid,
        fingerprint,
        intent=out["intent"],
        firmness=out["firmness"],
        cash_pressure=out["cash_pressure"],
        values=tuple(out["values"]),
        conditional_trades=tuple(out["conditional_trades"]),
        dependencies=tuple(out["dependencies"]),
        answered=tuple(out["answered"]),
        refused=tuple(out["refused"]),
        evidence=tuple(out["evidence"]),
        source="model",
    )
