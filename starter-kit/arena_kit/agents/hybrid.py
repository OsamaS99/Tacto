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
from ..play import latest, live_threads, moves_left, standing
from .evidence import dossier
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


class HybridAgent:
    """Compare suppliers, construct safe packages, and negotiate through e-mail."""

    def __init__(
        self,
        complete=None,
        package=None,
        anchor=0.08,
        concession=0.18,
        renewal_concession=0.035,
        hold_concession=0.04,
        max_sheets=3,
    ):
        self.complete = complete
        self.package = package
        self.anchor = anchor
        self.concession = concession
        self.renewal_concession = renewal_concession
        self.hold_concession = hold_concession
        self.max_sheets = min(3, max(1, max_sheets))
        self.case_id = None
        self.evidence = {}
        self.arguments = []
        self.dossier_text = ""

    def decide(self, case, threads):
        """Return one valid arena move for the current multi-supplier state."""
        if case["case_id"] != self.case_id:
            self.case_id = case["case_id"]
            self.evidence = self._prepare_evidence(case)
            self.arguments = self._build_arguments(case)
            self.dossier_text = self._prepare_dossier(case)

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
            offer_id, terms = standing(events)
            standard = standard_terms(events)
            supplier = self._supplier_estimate(case, supplier_id, standard)
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
                    status=latest(events).get("status", "open"),
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
                # The estimate is explicitly a full cost without margin. Its confidence
                # range is uncertainty, not evidence that the lower edge is more likely.
                "full_cost": float(row["unit_cost"]),
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
        if last_buyer is not None:
            closing = self._trend_closing_package(case, offer, events)
            if closing is not None:
                return [closing]

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
            supplier_costs = [
                total_cost(case, sheet["terms"], offer.standard, offer.supplier)
                for message in events
                if message.get("from") != "buyer"
                for sheet in (message.get("term_sheets") or [])[:1]
                if sheet.get("terms", {}).get("unit_price") is not None
            ]
            supplier_moved = len(supplier_costs) < 2 or supplier_costs[-1] < supplier_costs[-2] - 0.01
            hold_count = 0
            for previous, current in reversed(list(zip(supplier_costs, supplier_costs[1:], strict=False))):
                if current < previous - 0.01:
                    break
                hold_count += 1
            fraction = self._concession_fraction(case, supplier_moved, hold_count, offer.moves_left)
            target_cost = previous_cost + fraction * max(0.0, offer.total_cost - previous_cost)

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

        return self._select_packages(case, offer, candidates, fields, standing_price)

    def _select_packages(self, case, offer, candidates, fields, standing_price):
        """Select packages with distinct commercial jobs instead of sampling blindly.

        The shortlist always tries to retain the lowest buyer cost, the strongest
        non-price terms, and a package close to the supplier's current position.
        """
        if not candidates:
            return []

        def buyer_cost(item):
            return total_cost(case, item[1], offer.standard, offer.supplier)

        def non_price_cost(item):
            # Reprice every package at one common price to isolate the value of
            # payment, delivery, freight, contract, and other non-price terms.
            terms = {**item[1], "unit_price": standing_price}
            return total_cost(case, terms, offer.standard, offer.supplier)

        def supplier_distance(item):
            distance = 0.0
            terms = item[1]
            for field in fields:
                preferred = offer.terms.get(field, offer.standard.get(field))
                actual = terms.get(field)
                if actual == preferred:
                    continue
                if isinstance(actual, (int, float)) and isinstance(preferred, (int, float)):
                    options = [
                        value
                        for value in case.get("term_options", {}).get(field, [])
                        if isinstance(value, (int, float))
                    ]
                    span = max(options) - min(options) if len(options) > 1 else abs(preferred)
                    distance += abs(actual - preferred) / max(span, 1)
                else:
                    distance += 1.0
            return distance

        roles = [
            min(candidates, key=lambda item: (buyer_cost(item), item[0])),
            min(candidates, key=lambda item: (non_price_cost(item), buyer_cost(item), item[0])),
            min(candidates, key=lambda item: (supplier_distance(item), buyer_cost(item), item[0])),
        ]

        selected = []
        selected_keys = set()
        for key, terms in roles:
            if key not in selected_keys:
                selected.append((key, terms))
                selected_keys.add(key)
            if len(selected) == self.max_sheets:
                return [terms for _, terms in selected]

        # A role can point to the same package. Fill any remaining slots with
        # low-cost alternatives that add the most non-price diversity.
        remaining = [item for item in candidates if item[0] not in selected_keys]
        while remaining and len(selected) < self.max_sheets:
            def diversity(item):
                return min(
                    sum(item[1].get(field) != chosen[1].get(field) for field in fields)
                    for chosen in selected
                )

            choice = min(remaining, key=lambda item: (-diversity(item), buyer_cost(item), item[0]))
            selected.append(choice)
            selected_keys.add(choice[0])
            remaining = [item for item in remaining if item[0] != choice[0]]

        return [terms for _, terms in selected]

    def _concession_fraction(self, case, supplier_moved, hold_count=0, remaining=10):
        """Start patiently, respond to repeated holds, and converge before rounds expire."""
        if supplier_moved:
            fraction = self.concession
            if case.get("type") == "renewal":
                fraction = min(fraction, self.renewal_concession)
        else:
            fraction = self.hold_concession
            if hold_count >= 2:
                fraction = max(fraction, 0.08)
            if hold_count >= 3:
                fraction = max(fraction, 0.12)

        if remaining <= 4:
            fraction = max(fraction, 0.12)
        if remaining <= 2:
            fraction = max(fraction, 0.25)
        return fraction

    def _trend_closing_package(self, case, offer, events):
        """Match a stable next-price trend with buyer-optimal terms before a final offer."""
        if case.get("type") not in {"price_increase", "rfq"}:
            return None

        prices = [
            sheet["terms"]["unit_price"]
            for message in events
            if message.get("from") != "buyer"
            for sheet in (message.get("term_sheets") or [])[:1]
            if sheet.get("terms", {}).get("unit_price") is not None
        ]
        if len(prices) < 5:
            return None

        drops = [left - right for left, right in zip(prices[-4:-1], prices[-3:], strict=True)]
        if any(drop <= 0 for drop in drops):
            return None
        ordered = sorted(drops)
        typical_drop = ordered[len(ordered) // 2]
        if max(drops) - min(drops) > max(0.02, typical_drop * 0.25):
            return None

        predicted_price = round(prices[-1] - typical_drop, 2)
        current_price = case["buyer"].get("current_unit_price")
        if predicted_price <= 0 or current_price is None or predicted_price > current_price:
            return None

        terms = self._best_reference_terms(case, offer, predicted_price)
        if not must_haves_ok(case, terms, offer.standard):
            return None
        if total_cost(case, terms, offer.standard, offer.supplier) > self._alternative_cost(case):
            return None

        key = json.dumps(terms, sort_keys=True, default=str)
        already_sent = {
            json.dumps(sheet["terms"], sort_keys=True, default=str)
            for message in events
            if message.get("from") == "buyer"
            for sheet in message.get("term_sheets") or []
        }
        return None if key in already_sent else terms

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
            need = case["buyer"]["need_per_year"]
            # In capacity cases, a supplier's lower standing allocation is often the real
            # constraint. Keep that option instead of forcing a full-volume package that
            # turns a workable partial deal into a walk-away.
            if case.get("type") == "capacity" and current is not None and current < need:
                capped = [value for value in options if value <= current]
                if capped:
                    options = capped
            elif covering := [value for value in options if value >= need]:
                options = covering
        constraints = [constraint for constraint in case["buyer"].get("must_haves", []) if constraint["field"] == field]
        if constraints:
            options = [value for value in options if all(OPS[item["op"]](value, item["value"]) for item in constraints)]
        return options

    def _opening_anchor(self, case):
        """Return a category-aware first-round discount from the chosen benchmark."""
        goal = case["buyer"].get("category_goal")
        anchor = self.anchor
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
                evidence[ref] = fact
        if self.package is not None:
            try:
                for part_id in case.get("parts", []):
                    for supplier_id in case.get("suppliers", []):
                        rows = self.package.query(
                            "SELECT id, order_date FROM po_lines "
                            "WHERE part_id = ? AND supplier_id = ? "
                            "ORDER BY order_date DESC LIMIT 8",
                            (part_id, supplier_id),
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

    def _prepare_dossier(self, case):
        """Buyer-agent style wider context, optional because final data schemas can vary."""
        if self.package is None:
            return ""
        try:
            return dossier(self.package, case)
        except Exception as error:
            log.warning("dossier unavailable (%s)", error)
            return ""

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
                    "body": (
                        f"The announced change provides {document['days']} days of notice, below "
                        f"the {contract_fact['days']} days required by our contract "
                        f"({document_ref}, {contract_ref})."
                    ),
                }
            )
        arguments.extend(self._purchase_price_arguments())
        arguments.extend(self._forecast_arguments())
        arguments.extend(self._falling_index_arguments(case))
        for ref, fact in self.evidence.items():
            if not ref.startswith("sc:") or fact.get("otd_pct") is None:
                continue
            arguments.append(
                {
                    "supplier_id": fact.get("supplier"),
                    "refs": [ref],
                    "body": (f"The supplier scorecard records {fact['otd_pct']:.1f}% on-time delivery ({ref})."),
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
                    "body": (
                        f"The latest demand forecast for {fact.get('part', 'this part')} "
                        f"shows volume growth of {change:+.1f}% ({ref})."
                    ),
                }
            )
        return arguments

    def _purchase_price_arguments(self):
        """Build claims from the two latest comparable POs for each supplier and part."""
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
                    "body": (
                        f"Our two latest purchase orders for {part_id} were priced at "
                        f"{first[1]['unit_price']:.2f} and {second[1]['unit_price']:.2f} EUR "
                        f"per {price_unit} unit(s) ({first[0]}, {second[0]})."
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

    def _select_argument(self, supplier_id, threads):
        """Select one relevant evidence claim, preserving other claims for later rounds."""
        used_refs = {
            ref
            for events in threads.values()
            for message in events
            if message.get("from") == "buyer"
            for ref in [*(message.get("refs") or []), *ID_RE.findall(message.get("body", ""))]
        }
        return next(
            (
                argument
                for argument in self.arguments
                if argument["supplier_id"] in (None, supplier_id)
                and not any(ref in used_refs for ref in argument["refs"])
            ),
            None,
        )

    def _write_email(self, case, offer, packages, threads):
        """Let the LLM write copy only; deterministic copy is always available."""
        argument = self._select_argument(offer.supplier_id, threads)
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
                "evidence_argument": argument["body"] if argument else None,
                "dossier": self.dossier_text[:3000],
            }
            try:
                written = parse(self.complete(WRITER_SYSTEM, json.dumps(request, ensure_ascii=False, default=str)))
                fallback = {
                    "subject": str(written.get("subject") or fallback["subject"]),
                    "body": str(written.get("body") or fallback["body"]),
                }
            except Exception as error:
                log.warning("LLM writer failed (%s) - using deterministic copy", error)

        if argument:
            # Drop conflicting paraphrases that cite the same evidence before adding truth.
            sentences = re.split(r"(?<=[.!?])\s+|\n+", fallback["body"])
            fallback["body"] = " ".join(
                sentence for sentence in sentences if not any(ref in sentence for ref in argument["refs"])
            ).strip()
            if not fallback["body"]:
                fallback["body"] = "Thank you for your proposal."
            fallback["body"] = f"{fallback['body'].rstrip()}\n\n{argument['body']}"

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


# Backwards-compatible name for tests or scripts copied from the one_ai_agent branch.
StrategicAgent = HybridAgent
