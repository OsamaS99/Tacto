"""A hybrid negotiation agent: deterministic commercial decisions, optional LLM wording.

The agent evaluates every supplier's standing term sheet with the published cost
function, keeps hard constraints in ordinary code, and uses an LLM only to write
the subject and body of a counter-offer. This keeps financial authority outside
the language model while preserving natural supplier communication.
"""

import itertools
import json
import logging
import math
import re
from dataclasses import dataclass

from ..cost import OPS, RELATIONSHIP_GOALS, closing_terms, complete_offer, must_haves_ok, total_cost
from ..docs import standard_terms
from ..play import latest, live_threads, moves_left
from .llm import ID_RE, parse

log = logging.getLogger("arena")


WRITER_SYSTEM = """You write concise procurement e-mails for Hollerau Pumpenwerke.
The commercial decision and term sheets are already fixed by deterministic code.
Return one JSON object with exactly two string fields: subject and body.
Do not invent prices, promises, data, IDs, deadlines, or additional terms.
If evidence_argument is present, reproduce it verbatim in the body.
Keep the body polite, concrete, and below 120 words."""


@dataclass(frozen=True)
class EvaluatedOffer:
    """A supplier's standing offer enriched with buyer-side economics."""

    supplier_id: str
    offer_id: str | None
    terms: dict
    standard: dict
    supplier: dict | None
    total_cost: float
    valid: bool
    status: str
    moves_left: int
    contacted: bool


class StrategicAgent:
    """Compare suppliers, construct safe packages, and negotiate through e-mail."""

    def __init__(
        self,
        complete=None,
        package=None,
        anchor=0.08,
        concession=0.18,
        hold_concession=0.04,
        max_sheets=3,
    ):
        self.complete = complete
        self.package = package
        self.anchor = anchor
        self.concession = concession
        self.hold_concession = hold_concession
        self.max_sheets = min(3, max(1, max_sheets))
        self.case_id = None
        self.evidence = {}
        self.arguments = []

    def decide(self, case, threads):
        """Return one valid arena move for the current multi-supplier state."""
        if case["case_id"] != self.case_id:
            self.case_id = case["case_id"]
            self.evidence = self._prepare_evidence(case)
            self.arguments = self._build_arguments(case)

        offers = self.evaluate_offers(case, threads)
        if not offers:
            sid = live_threads(threads)[0]
            return self._walk_away(sid)

        negotiable = [
            offer for offer in offers if offer.status not in {"agreed", "final_offer"} and offer.moves_left > 0
        ]
        if not negotiable:
            return self._safe_close(case, offers)

        # Visit every viable supplier before spending additional rounds on one thread.
        ordered = sorted(
            negotiable,
            key=lambda offer: (offer.contacted, not offer.valid, offer.total_cost),
        )
        chosen, packages = None, []
        for candidate in ordered:
            candidate_packages = self.generate_packages(
                case,
                candidate,
                threads[candidate.supplier_id],
            )
            if candidate_packages:
                chosen, packages = candidate, candidate_packages
                break
        if not packages:
            return self._safe_close(case, offers)

        copy = self._write_email(case, chosen, packages, threads)
        return {
            "supplier_id": chosen.supplier_id,
            "message": {
                "subject": copy["subject"],
                "body": copy["body"],
                "term_sheets": [{"terms": terms} for terms in packages],
                "refs": copy["refs"],
            },
        }

    def evaluate_offers(self, case, threads):
        """Price every live supplier offer with its parsed standard terms."""
        offers = []
        for supplier_id in live_threads(threads):
            events = threads[supplier_id]
            supplier_message = latest(events)
            sheets = supplier_message.get("term_sheets") or [{}]
            standard = standard_terms(events)
            supplier = self._supplier_estimate(case, supplier_id, standard)
            for sheet in sheets:
                offer_id, terms = sheet.get("term_sheet_id"), sheet.get("terms") or {}
                cost = total_cost(case, terms, standard, supplier) if offer_id else math.inf
                offers.append(
                    EvaluatedOffer(
                        supplier_id=supplier_id,
                        offer_id=offer_id,
                        terms=terms,
                        standard=standard,
                        supplier=supplier,
                        total_cost=cost,
                        valid=(bool(offer_id) and math.isfinite(cost) and must_haves_ok(case, terms, standard)),
                        status=supplier_message.get("status", "open"),
                        moves_left=moves_left(case, events),
                        contacted=any(message.get("from") == "buyer" for message in events),
                    )
                )
        return sorted(offers, key=lambda offer: (not offer.valid, offer.total_cost, offer.supplier_id))

    def _supplier_estimate(self, case, supplier_id, standard):
        """Estimate supplier economics for goals whose score includes supplier health."""
        if case["buyer"].get("category_goal") not in RELATIONSHIP_GOALS:
            return None
        try:
            rows = []
            if self.package is not None:
                for part_id in case.get("parts", []):
                    rows.extend(
                        self.package.query(
                            "SELECT supplier_id, unit_cost, price_unit, confidence_pct "
                            "FROM should_cost WHERE part_id = ?",
                            (part_id,),
                        )
                    )
            row = next((item for item in rows if item.get("supplier_id") == supplier_id), None)
            row = row or next((item for item in rows if not item.get("supplier_id")), None)
            if row is None:
                evidence_costs = [
                    fact
                    for fact in self.evidence.values()
                    if fact.get("unit_cost") is not None and fact.get("part") in case.get("parts", [])
                ]
                row = next(
                    (fact for fact in evidence_costs if fact.get("supplier", fact.get("supplier_id")) == supplier_id),
                    None,
                )
                if row is None and len(case.get("suppliers", [])) == 1:
                    row = next(
                        (fact for fact in evidence_costs if fact.get("supplier", fact.get("supplier_id")) is None),
                        None,
                    )
            if row is None or not row.get("unit_cost"):
                return None
            confidence = float(row.get("confidence_pct") or 0) / 100
            interest_rate = max(0.08, case["buyer"].get("interest_rate", 0.08))
            if self.package is not None:
                intel = self.package.query(
                    "SELECT credit_index FROM supplier_intel WHERE supplier_id = ?",
                    (supplier_id,),
                )
                if intel and intel[0].get("credit_index") is not None:
                    credit_index = intel[0]["credit_index"]
                    interest_rate = 0.15 if credit_index >= 250 else 0.12 if credit_index >= 200 else 0.1
            scorecard = next(
                (
                    fact
                    for fact in self.evidence.values()
                    if fact.get("supplier") == supplier_id and fact.get("ppm") is not None
                ),
                {},
            )
            estimate = {
                # Blend the nominal estimate with its lower confidence edge. This still
                # protects supplier health without treating an uncertain point as exact.
                "full_cost": float(row["unit_cost"]) * (1 - confidence / 2),
                "interest_rate": interest_rate,
                "standard": closing_terms(case, {}, standard),
            }
            if scorecard:
                estimate["ppm"] = scorecard["ppm"]
            return estimate
        except Exception as error:
            log.warning("supplier economics unavailable for %s (%s)", supplier_id, error)
            return None

    def generate_packages(self, case, offer, events):
        """Build diverse packages with approximately equal total cost to the buyer.

        Non-price options reveal supplier preferences. The unit price compensates
        for each option combination so the alternatives remain economically close
        from the buyer's perspective.
        """
        standing_price = offer.terms.get("unit_price")
        if standing_price is None:
            return []

        last_buyer = next(
            (message for message in reversed(events) if message.get("from") == "buyer" and message.get("term_sheets")),
            None,
        )
        price_cap = None
        if last_buyer is None:
            benchmark = standing_price
            goal = case["buyer"].get("category_goal")
            current_price = case["buyer"].get("current_unit_price")
            if offer.supplier is None and goal in {"leverage", "quality_first"} and current_price:
                benchmark = min(benchmark, current_price)
            anchor = self._opening_anchor(case)
            target_price = benchmark * (1 - anchor)
            target_price = max(0.01, min(target_price, standing_price - 0.01))
            # Every first-round package must remain meaningfully below the benchmark.
            # Otherwise a supplier can accept the most generous alternative immediately.
            price_cap = min(standing_price - 0.01, benchmark * (1 - anchor / 2))
            base = self._best_reference_terms(case, offer, target_price)
            anchored_cost = total_cost(case, base, offer.standard, offer.supplier)
            target_cost = min(anchored_cost, offer.total_cost)
        else:
            base = complete_offer(case, offer.terms)
            previous_cost = max(
                total_cost(case, sheet["terms"], offer.standard, offer.supplier) for sheet in last_buyer["term_sheets"]
            )
            supplier_costs = []
            for message in events:
                if message.get("from") == "buyer":
                    continue
                round_costs = [
                    total_cost(case, sheet["terms"], offer.standard, offer.supplier)
                    for sheet in message.get("term_sheets") or []
                    if sheet.get("terms", {}).get("unit_price") is not None
                ]
                if round_costs:
                    supplier_costs.append(min(round_costs))
            gap = max(0.0, offer.total_cost - previous_cost)
            improvement = max(0.0, supplier_costs[-2] - supplier_costs[-1]) if len(supplier_costs) >= 2 else 0.0
            if improvement <= 0 or gap <= 0:
                fraction = self.hold_concession
            else:
                movement_ratio = improvement / gap
                maximum = min(0.40, max(self.concession, self.concession * 2))
                fraction = min(
                    maximum,
                    max(
                        self.hold_concession,
                        self.concession * (0.5 + 2 * movement_ratio),
                    ),
                )
            target_cost = previous_cost + fraction * gap

        alternative_cost = self._alternative_cost(case)
        target_cost = min(target_cost, alternative_cost)
        fields = [field for field in case["negotiable"] if field != "unit_price"]
        option_sets = []
        for field in fields:
            options = self._field_options(case, offer, field, base.get(field))
            if not options:
                return []
            option_sets.append(options)

        combinations = list(itertools.islice(itertools.product(*option_sets), 256)) if option_sets else [()]
        sent_packages = {
            json.dumps(sheet["terms"], sort_keys=True, default=str)
            for message in events
            if message.get("from") == "buyer"
            for sheet in message.get("term_sheets") or []
        }

        def build_candidates(active_price_cap):
            candidates = []
            candidate_keys = set()
            for values in combinations:
                raw = {**offer.terms, **dict(zip(fields, values, strict=True))}
                if "unit_price" in case["negotiable"]:
                    price = self._equivalent_price(
                        case,
                        raw,
                        offer.standard,
                        target_cost,
                        standing_price,
                        offer.supplier,
                        active_price_cap,
                    )
                    if price is None:
                        continue
                    raw["unit_price"] = price
                terms = complete_offer(case, raw)
                # Supplier standard values remain valid even outside buyer term options.
                for field in fields:
                    if raw.get(field) == offer.standard.get(field):
                        terms[field] = raw[field]
                if not must_haves_ok(case, terms, offer.standard):
                    continue
                # Never counter with a package that is worse than an available fallback.
                if total_cost(case, terms, offer.standard, offer.supplier) > alternative_cost:
                    continue
                key = json.dumps(terms, sort_keys=True, default=str)
                if key not in sent_packages and key not in candidate_keys:
                    candidates.append((key, terms))
                    candidate_keys.add(key)
            return candidates

        candidates = build_candidates(price_cap)
        if not candidates and last_buyer is None and offer.supplier is not None:
            # If the opening is already below our estimated sustainable floor,
            # a material price cut can increase C through relationship damage.
            # Relax the cap only when no anchored package is safe at all.
            candidates = build_candidates(None)

        # Sort by the non-price signature, then take evenly spaced alternatives.
        candidates.sort(key=lambda item: tuple(str(item[1].get(field)) for field in fields))
        if len(candidates) <= self.max_sheets:
            return [terms for _, terms in candidates]
        prefer_supplier_signature = last_buyer is not None or offer.terms.get("index_clause") is not None
        if self.max_sheets == 1:
            preferred = (
                next(
                    (
                        candidate
                        for candidate in candidates
                        if all(candidate[1].get(field) == offer.terms.get(field) for field in fields)
                    ),
                    candidates[len(candidates) // 2],
                )
                if prefer_supplier_signature
                else candidates[len(candidates) // 2]
            )
            return [preferred[1]]
        indexes = [round(i * (len(candidates) - 1) / (self.max_sheets - 1)) for i in range(self.max_sheets)]
        selected = [candidates[index] for index in dict.fromkeys(indexes)]
        preferred = (
            next(
                (
                    candidate
                    for candidate in candidates
                    if all(candidate[1].get(field) == offer.terms.get(field) for field in fields)
                ),
                None,
            )
            if prefer_supplier_signature
            else None
        )
        if preferred is not None and preferred not in selected:
            selected[-1] = preferred
        return [terms for _, terms in selected]

    def _best_reference_terms(self, case, offer, target_price):
        """Choose the cheapest complete package at the target price as the anchor."""
        fields = [field for field in case["negotiable"] if field != "unit_price"]
        option_sets = [self._field_options(case, offer, field, offer.terms.get(field)) for field in fields]
        if any(not options for options in option_sets):
            return complete_offer(case, {**offer.terms, "unit_price": target_price})
        best = None
        combinations = itertools.islice(itertools.product(*option_sets), 256) if option_sets else [()]
        for values in combinations:
            raw = {
                **offer.terms,
                "unit_price": target_price,
                **dict(zip(fields, values, strict=True)),
            }
            terms = complete_offer(case, raw)
            for field in fields:
                if raw.get(field) == offer.standard.get(field):
                    terms[field] = raw[field]
            if not must_haves_ok(case, terms, offer.standard):
                continue
            candidate = total_cost(case, terms, offer.standard, offer.supplier)
            if best is None or candidate < best[0]:
                best = (candidate, terms)
        return best[1] if best else complete_offer(case, {**offer.terms, "unit_price": target_price})

    @staticmethod
    def _field_options(case, offer, field, current):
        """Return valid options, excluding contract lengths that leave the horizon uncovered."""
        options = list(case.get("term_options", {}).get(field) or [])
        if current is not None and current not in options:
            options.append(current)
        standard_value = offer.standard.get(field)
        if field in offer.standard and standard_value not in options:
            options.append(standard_value)
        if field == "contract_months":
            horizon_months = math.ceil(12 * case["buyer"]["horizon_years"])
            covering = [value for value in options if value >= horizon_months]
            if covering:
                options = covering
        elif field == "volume_per_year":
            covering = [value for value in options if value >= case["buyer"]["need_per_year"]]
            if covering:
                options = covering
        elif field == "index_clause" and None in options and case.get("type") != "raw_material":
            paths = case["buyer"].get("index_paths", {})
            buyer_favourable = [
                value
                for value in options
                if value is None or (isinstance(value, dict) and paths.get(value.get("series"), 0) < 0)
            ]
            if buyer_favourable:
                options = buyer_favourable
        constraints = [constraint for constraint in case["buyer"].get("must_haves", []) if constraint["field"] == field]
        if constraints:
            options = [value for value in options if all(OPS[item["op"]](value, item["value"]) for item in constraints)]
        return options

    def _opening_anchor(self, case):
        """Return a category-aware first-round discount from the chosen benchmark."""
        goal = case["buyer"].get("category_goal")
        anchor = self.anchor
        if goal in RELATIONSHIP_GOALS:
            return min(anchor, 0.02)
        if case.get("type") == "raw_material" and goal == "leverage":
            return min(anchor, 0.06)
        if goal == "leverage":
            anchor = max(anchor, 0.12)
        elif goal == "quality_first":
            anchor = max(anchor, 0.10)
        if len(case.get("suppliers", [])) > 1:
            anchor = max(anchor, 0.15)
        return anchor

    @staticmethod
    def _equivalent_price(
        case,
        raw,
        standard,
        target_cost,
        standing_price,
        supplier=None,
        price_cap=None,
    ):
        """Find the highest cent price whose package does not exceed target cost.

        Supplier-health costs make C convex instead of monotonic: an unrealistically
        low price can cost more than a sustainable one. Locate the minimum first,
        then search the increasing side for the highest feasible price.
        """
        upper = standing_price - 0.01 if price_cap is None else min(standing_price - 0.01, price_cap)
        lower, upper = 0.01, max(0.01, upper)

        def cost(price):
            return total_cost(case, {**raw, "unit_price": price}, standard, supplier)

        left, right = lower, upper
        for _ in range(32):
            first = left + (right - left) / 3
            second = right - (right - left) / 3
            if cost(first) <= cost(second):
                right = second
            else:
                left = first
        minimum = (left + right) / 2
        if cost(minimum) > target_cost:
            return None
        if cost(upper) <= target_cost:
            feasible = upper
        else:
            low, high = minimum, upper
            for _ in range(28):
                middle = (low + high) / 2
                if cost(middle) <= target_cost:
                    low = middle
                else:
                    high = middle
            feasible = low
        # Floor on the increasing branch so cent conversion stays below target C.
        price = math.floor(feasible * 100) / 100
        if cost(price) > target_cost:
            price = math.ceil(feasible * 100) / 100
        if price > upper or cost(price) > target_cost:
            return None
        return price

    @staticmethod
    def _alternative_cost(case):
        """Return the buyer's priced fallback cost, or infinity when none exists."""
        alternative = case["buyer"].get("alternative") or {}
        if alternative.get("terms", {}).get("unit_price") is None:
            return math.inf
        return total_cost(case, alternative["terms"])

    def _safe_close(self, case, offers):
        """Accept the cheapest authorized standing offer, otherwise walk away."""
        alternative_cost = self._alternative_cost(case)
        acceptable = [
            offer
            for offer in offers
            if offer.valid
            and offer.offer_id
            and math.isfinite(offer.total_cost)
            and offer.total_cost <= alternative_cost
        ]
        if not acceptable:
            return self._walk_away(offers[0].supplier_id)
        best = min(acceptable, key=lambda offer: offer.total_cost)
        return {
            "supplier_id": best.supplier_id,
            "message": {"action": {"type": "accept", "term_sheet_id": best.offer_id}},
        }

    @staticmethod
    def _walk_away(supplier_id):
        return {"supplier_id": supplier_id, "message": {"action": {"type": "walk_away"}}}

    def _prepare_evidence(self, case):
        """Resolve the citable facts explicitly referenced by the case."""
        evidence = {}
        for ref in dict.fromkeys(case.get("refs", [])):
            fact = case.get("facts", {}).get(ref)
            if fact is None and self.package is not None:
                fact = self.package.lookup(ref)
            if fact is not None:
                evidence[ref] = dict(fact)
                if ref.startswith("ctr:") and self.package is not None and hasattr(self.package, "document"):
                    try:
                        evidence[ref]["_document"] = self.package.document(ref)
                    except (FileNotFoundError, KeyError, TypeError, ValueError):
                        pass
        if self.package is not None:
            try:
                for part_id in case.get("parts", []):
                    rows = self.package.query(
                        "SELECT id, supplier_id, order_date FROM po_lines "
                        "WHERE part_id = ? ORDER BY order_date DESC LIMIT 24",
                        (part_id,),
                    )
                    for row in rows:
                        ref = row["id"]
                        fact = self.package.lookup(ref)
                        if fact is not None:
                            evidence[ref] = fact
                    forecasts = self.package.query(
                        "SELECT id FROM forecasts WHERE part_id = ? ORDER BY snapshot_month DESC LIMIT 1",
                        (part_id,),
                    )
                    for row in forecasts:
                        ref = row["id"]
                        fact = self.package.lookup(ref)
                        if fact is not None:
                            evidence[ref] = fact
            except Exception as error:
                log.warning("purchase-price evidence unavailable (%s)", error)
        return evidence

    def _build_arguments(self, case):
        """Turn safe fact pairs into concise claims the arena can verify."""
        documents = [
            (ref, fact)
            for ref, fact in self.evidence.items()
            if ref.startswith("doc:") and fact.get("days") is not None
        ]
        contracts = [
            (ref, fact)
            for ref, fact in self.evidence.items()
            if ref.startswith("ctr:") and fact.get("days") is not None
        ]
        arguments = []
        for document_ref, document in documents:
            contract = next(
                (
                    (ref, fact)
                    for ref, fact in contracts
                    if not document.get("supplier")
                    or not fact.get("supplier")
                    or document["supplier"] == fact["supplier"]
                ),
                None,
            )
            if contract is None or document["days"] >= contract[1]["days"]:
                continue
            contract_ref, contract_fact = contract
            arguments.append(
                {
                    "supplier_id": document.get("supplier"),
                    "refs": [document_ref, contract_ref],
                    "priority": 100,
                    "body": (
                        f"The announced change provides {document['days']} days of notice, below "
                        f"the {contract_fact['days']} days required by our contract "
                        f"({document_ref}, {contract_ref})."
                    ),
                }
            )
        arguments.extend(self._contract_index_arguments(case))
        arguments.extend(self._purchase_price_arguments(case))
        arguments.extend(self._forecast_arguments())
        arguments.extend(self._falling_index_arguments(case))
        for ref, fact in self.evidence.items():
            if not ref.startswith("sc:") or fact.get("otd_pct") is None:
                continue
            arguments.append(
                {
                    "supplier_id": fact.get("supplier"),
                    "refs": [ref],
                    "priority": 20,
                    "body": (f"The supplier scorecard records {fact['otd_pct']:.1f}% on-time delivery ({ref})."),
                }
            )
        return arguments

    def _contract_index_arguments(self, case):
        """Cite the existing index basis when a raw-material case proposes another series."""
        if case.get("type") != "raw_material":
            return []
        proposed = {
            clause.get("series")
            for clause in case.get("term_options", {}).get("index_clause", [])
            if isinstance(clause, dict) and clause.get("series")
        }
        arguments = []
        for ref, fact in self.evidence.items():
            if not ref.startswith("ctr:") or not fact.get("_document"):
                continue
            match = re.search(r"\bindex\s+([A-Z][A-Z0-9-]+)", fact["_document"], re.IGNORECASE)
            if match is None:
                continue
            series = match.group(1)
            if not proposed or proposed == {series}:
                continue
            arguments.append(
                {
                    "supplier_id": fact.get("supplier"),
                    "refs": [ref],
                    "priority": 95,
                    "body": f"Our current agreement indexes this price to {series} ({ref}).",
                }
            )
        return arguments

    def _forecast_arguments(self):
        """Turn material positive demand growth into a supplier-relevant volume argument."""
        arguments = []
        forecasts = {}
        for ref, fact in self.evidence.items():
            if not ref.startswith("fc:"):
                continue
            part = fact.get("part")
            month = fact.get("snapshot_month") or ref.rpartition("@")[2]
            current = forecasts.get(part)
            if current is None or month > current[0]:
                forecasts[part] = (month, ref, fact)
        for _, ref, fact in forecasts.values():
            change = fact.get("change_pct")
            if change is None or change <= 0:
                continue
            arguments.append(
                {
                    "supplier_id": None,
                    "refs": [ref],
                    "priority": 80,
                    "body": (
                        f"The latest demand forecast for {fact.get('part', 'this part')} "
                        f"shows volume growth of {change:+.1f}% ({ref})."
                    ),
                }
            )
        return arguments

    def _purchase_price_arguments(self, case):
        """Build claims from recent same-part purchase orders."""
        groups = {}
        for ref, fact in self.evidence.items():
            if not ref.startswith("po:") or fact.get("unit_price") is None:
                continue
            key = (fact.get("supplier"), fact.get("part"))
            groups.setdefault(key, []).append((ref, fact))
        arguments = []
        for (supplier_id, part_id), rows in groups.items():
            rows.sort(key=lambda item: item[1].get("date", ""), reverse=True)
            if len(rows) < 2:
                continue
            first, second = rows[:2]
            if first[1].get("price_unit", 1) != second[1].get("price_unit", 1):
                continue
            price_unit = first[1].get("price_unit", 1)
            arguments.append(
                {
                    "supplier_id": supplier_id,
                    "refs": [first[0], second[0]],
                    "priority": 40,
                    "body": (
                        f"Our two latest purchase orders for {part_id} were priced at "
                        f"{first[1]['unit_price']:.2f} and {second[1]['unit_price']:.2f} EUR "
                        f"per {price_unit} unit(s) ({first[0]}, {second[0]})."
                    ),
                }
            )
        for supplier_id in case.get("suppliers", []):
            for part_id in case.get("parts", []):
                target = sorted(
                    groups.get((supplier_id, part_id), []),
                    key=lambda item: item[1].get("date", ""),
                    reverse=True,
                )
                if not target:
                    continue
                target_price = target[0][1]["unit_price"] / target[0][1].get("price_unit", 1)
                competitors = []
                for (other_supplier, other_part), rows in groups.items():
                    if other_supplier == supplier_id or other_part != part_id:
                        continue
                    latest_row = max(rows, key=lambda item: item[1].get("date", ""))
                    price = latest_row[1]["unit_price"] / latest_row[1].get("price_unit", 1)
                    if price < target_price:
                        competitors.append((price, other_supplier, latest_row))
                if not competitors:
                    continue
                _, other_supplier, (ref, fact) = min(competitors)
                arguments.append(
                    {
                        "supplier_id": supplier_id,
                        "refs": [ref],
                        "priority": 90,
                        "body": (
                            f"A recent comparable order for {part_id} from {other_supplier} was priced at "
                            f"{fact['unit_price']:.2f} EUR per {fact.get('price_unit', 1)} unit(s) ({ref})."
                        ),
                    }
                )
        return arguments

    def _falling_index_arguments(self, case):
        """Find recent negative changes among the indexed cost drivers of the part."""
        if self.package is None:
            return []
        arguments = []
        seen_series = set()
        try:
            for part_id in case.get("parts", []):
                drivers = self.package.query(
                    "SELECT series_id, weight FROM cost_drivers WHERE part_id = ?",
                    (part_id,),
                )
                for driver in drivers:
                    series_id = driver["series_id"]
                    if series_id in seen_series:
                        continue
                    seen_series.add(series_id)
                    rows = self.package.query(
                        "SELECT id, month, value FROM index_values WHERE series_id = ? ORDER BY month DESC LIMIT 13",
                        (series_id,),
                    )
                    if len(rows) < 2:
                        continue
                    latest = max(rows, key=lambda row: row["month"])
                    latest_year, latest_month = map(int, latest["month"].split("-"))

                    def months_before(
                        row,
                        latest_year=latest_year,
                        latest_month=latest_month,
                    ):
                        year, month = map(int, row["month"].split("-"))
                        return (latest_year - year) * 12 + latest_month - month

                    earlier = max(
                        (row for row in rows if 1 <= months_before(row) <= 12),
                        key=months_before,
                        default=None,
                    )
                    if earlier is None or not earlier["value"]:
                        continue
                    change = (latest["value"] / earlier["value"] - 1) * 100
                    if change >= 0:
                        continue
                    for row in (earlier, latest):
                        self.evidence[row["id"]] = self.package.lookup(row["id"]) or row
                    arguments.append(
                        {
                            "supplier_id": None,
                            "refs": [earlier["id"], latest["id"]],
                            "priority": 70,
                            "body": (
                                f"The {series_id} cost index changed {change:+.1f}% from "
                                f"{earlier['month']} to {latest['month']} "
                                f"({earlier['id']}, {latest['id']})."
                            ),
                            "impact": change * driver.get("weight", 0),
                        }
                    )
        except Exception as error:
            log.warning("cost-index evidence unavailable (%s)", error)
        return sorted(arguments, key=lambda argument: argument["impact"])

    def _select_arguments(self, supplier_id, threads):
        """Use the strongest new claims while respecting the arena's three-reference budget."""
        used_refs = {
            ref
            for events in threads.values()
            for message in events
            if message.get("from") == "buyer"
            for ref in [*(message.get("refs") or []), *ID_RE.findall(message.get("body", ""))]
        }
        first_contact = not any(message.get("from") == "buyer" for message in threads.get(supplier_id, []))
        selected = []
        selected_refs = set()
        for argument in sorted(self.arguments, key=lambda item: item.get("priority", 0), reverse=True):
            refs = set(argument["refs"])
            if argument["supplier_id"] not in (None, supplier_id) or refs & used_refs:
                continue
            if len(selected_refs | refs) > 3:
                continue
            selected.append(argument)
            selected_refs.update(refs)
            if not first_contact or len(selected_refs) == 3:
                break
        return selected

    def _write_email(self, case, offer, packages, threads):
        """Let the LLM write copy only; deterministic copy is always available."""
        arguments = self._select_arguments(offer.supplier_id, threads)
        fallback = {
            "subject": case["title"],
            "body": "Thank you for your proposal. Please review the alternative commercial packages below.",
        }
        if self.complete is not None:
            request = {
                "action": "counter",
                "case": {
                    "title": case["title"],
                    "type": case.get("type"),
                    "category_goal": case["buyer"].get("category_goal"),
                },
                "supplier_id": offer.supplier_id,
                "supplier_status": offer.status,
                "standing_offer": offer.terms,
                "packages": packages,
                "evidence_argument": "\n".join(argument["body"] for argument in arguments) or None,
            }
            try:
                written = parse(self.complete(WRITER_SYSTEM, json.dumps(request, ensure_ascii=False, default=str)))
                fallback = {
                    "subject": str(written.get("subject") or fallback["subject"]),
                    "body": str(written.get("body") or fallback["body"]),
                }
            except Exception as error:
                log.warning("LLM writer failed (%s) - using deterministic copy", error)

        if arguments:
            # Drop conflicting paraphrases that cite the same evidence before adding truth.
            sentences = re.split(r"(?<=[.!?])\s+|\n+", fallback["body"])
            fallback["body"] = " ".join(
                sentence
                for sentence in sentences
                if not any(ref in sentence for argument in arguments for ref in argument["refs"])
            ).strip()
            if not fallback["body"]:
                fallback["body"] = "Thank you for your proposal."
            evidence_text = "\n".join(argument["body"] for argument in arguments)
            fallback["body"] = f"{fallback['body'].rstrip()}\n\n{evidence_text}"

        known_offers = {offer.offer_id} if offer.offer_id else set()

        def keep_known_id(match):
            ref = match.group(0)
            if ref in known_offers or ref in self.evidence:
                return ref
            log.warning("dropped unknown data ID %s", ref)
            return ""

        body = ID_RE.sub(keep_known_id, fallback["body"]).strip()[:8000]
        refs = list(dict.fromkeys(ID_RE.findall(body)))
        return {"subject": fallback["subject"].strip()[:300], "body": body, "refs": refs[:20]}
