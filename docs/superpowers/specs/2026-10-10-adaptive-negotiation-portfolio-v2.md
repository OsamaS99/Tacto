# Adaptive Negotiation Portfolio V2 Specification

## Goal

Raise the buyer agent's repeatable mean score beyond the current 65.2 by restoring adaptive
communication and making later offers follow supplier preferences revealed during the negotiation.
The first target is to recover the already demonstrated best-score mean of approximately 69.6
without case-specific production rules or unapproved hosted Arena calls.

## Evidence and problem statement

The latest ten-case run improved from 58.0 to 65.2, principally through supplier concurrency,
verified evidence and broader package menus. The remaining failure mechanisms are structural:

1. Twenty-seven of 39 send decisions rejected the communication model's output and used the
   generic template. The prompt and example permit one selected package, while writer validation
   requires every package on first contact and at least two on later open turns.
2. `responsive_packages` says it preserves at least one supplier-value package, but its filter
   requires every surviving package to meet the previous maximum supplier value. This discards
   useful exploration branches.
3. The interpreter extracts grounded `conditional_trades`, but package generation and selection
   use only the flattened `values` list. A condition such as "24 months provided indexation is
   included" therefore does not constrain or rank the next package portfolio.
4. Supplier-value estimates can understate the value of operational relief. In the capacity case,
   the supplier revealed a materially larger price premium for short lead time than the static
   estimate, while the initial portfolio exposed only the short-lead branch.
5. `bids.select` implements a unit-price ceiling but the main decision path does not pass the live
   supplier price into it.

Case IDs may be used in regression-test names and fixtures to identify observed behavior. Runtime
policy must remain case- and supplier-agnostic.

## Requirements

### 1. Preserve valid model choices without silent template collapse

- The communication model continues to choose only from code-approved package, claim, question,
  acknowledgment and trade menus.
- Open-turn portfolio requirements are normalized deterministically before final validation:
  missing required packages are appended from the approved menu in its existing order, and a
  missing first-contact claim is filled with the first approved claim.
- Normalization never invents or edits a package, claim or public sentence.
- A structurally invalid selection, an unknown ID, an unsafe selection or an invalid final-offer
  selection still fails validation and uses the existing safe fallback.
- Final-offer and agreed messages remain exactly one package, no claim and no question.
- The event log must distinguish a clean model selection from a safely normalized one so replay
  analysis can measure both.

### 2. Explore broadly, then narrow on revealed preferences

- First contact sends a Pareto-diverse portfolio of two or three packages when that many safe
  packages exist.
- A later open turn normally sends up to two packages.
- When the supplier states a grounded conditional trade, the portfolio contains at least one
  package matching all negotiable fields of one condition.
- A single matched package is permitted when the supplier has clearly selected one branch and no
  materially distinct safe exploration package remains.
- Portfolio narrowing cannot raise buyer total cost above the current reservation, break a
  must-have, repeat an identical package when a novel safe one exists, or reduce terms on an
  otherwise identical package.

### 3. Guarantee, rather than require everywhere, supplier value

- After a hold or request for reciprocity, at least one selected package preserves or improves the
  estimated supplier value of the best package in the immediately preceding portfolio.
- Other selected packages may have lower estimated supplier value when they explore a grounded
  conditional trade or a distinct Pareto branch.
- If no novel package can preserve supplier value, the selector may retain the safest novel
  package rather than erase the full candidate set.
- Supplier value remains a ranking estimate, never a factual supplier floor or an acceptance rule.

### 4. Use grounded conditional trades in deterministic selection

- Convert each interpreter-produced conditional trade into a field set only after its evidence has
  passed the existing verbatim-grounding validation.
- Ignore fields that are not negotiable in the current case.
- Rank candidate packages by: economic validity, condition coverage, supplier-value guarantee,
  novelty, then buyer total cost. The exact ordering between equally valid candidates must be
  deterministic.
- Conditional trades may rank approved packages but may not create new term values.
- Unresolved technical dependencies may select an approved question, but never create a promise or
  commercial commitment.

### 5. Represent operational relief without overfitting

- Package diversity must include an operationally easier branch when the supplier explicitly
  values lead time, volume, contract duration, payment timing, quality or indexation and a safe
  alternative exists.
- Improve the supplier-value estimate only through a general, documented rule. Do not encode a
  practice-case price, supplier identity or hidden supplier floor.
- Lead-time relief must remain bounded and conservative; buyer total cost and must-haves continue
  to come from the published cost engine.
- The live supplier unit price is passed as the `ceiling` to `bids.select`, preventing a generated
  package above the current ask when a package at or below it exists.

### 6. Preserve safety and operational constraints

- `arena_kit.cost` and `EconomicEvaluator` remain the only sources of buyer total cost and supplier
  value respectively.
- Acceptance and walk-away remain deterministic and may name only a current standing offer.
- No case ID or supplier ID appears in production policy.
- No `.env` file is read, printed or changed by this work.
- No hosted practice, sandbox or final call is made without explicit user approval.
- Existing supplier concurrency, durable outbox, evidence guards, deadline handling and role-model
  overrides remain intact.

## Architecture

The change stays inside the existing deterministic-policy boundaries:

1. `interpreter.py` continues to produce grounded `SupplierObservation` records.
2. `acceptance.py` derives a small package-selection context from the observation: valued fields,
   conditional field sets, firmness, prior portfolio and live standing price.
3. `bids.py` receives that context and returns a bounded adaptive portfolio from already evaluated,
   valid candidates.
4. `writer.py` lets the model choose communication elements, then safely normalizes only missing
   portfolio/first-claim requirements before validation and rendering.
5. `agent.py` records whether the model output was clean, normalized or replaced by the template.

No new LLM role, persistence format or network dependency is introduced.

## Component design

### Adaptive portfolio selector

The selector will operate in two phases:

1. Build eligible candidate groups: condition-matching, supplier-value-preserving, buyer-price,
   and exploration/novelty.
2. Select a bounded union in priority order, deduplicated by full non-price terms and capped at
   three packages on first contact or two later.

For later turns with a grounded condition, the matched group comes first. Without a condition,
supplier interests and supplier-value estimates determine the first branch. At least one
supplier-value-preserving candidate is included when available, but that property is not applied
as a filter to every candidate.

### Communication normalization

Normalization is deliberately narrow:

- it operates on parsed, schema-valid IDs;
- it appends only IDs already present in the approved menus;
- it does not repair unknown IDs, final-offer violations or unsafe claims;
- it preserves the model's acknowledgment, question, trade, tone and rationale when those remain
  compatible with the normalized package set;
- when a selected trade is not present in every normalized package, the trade is cleared rather
  than changing packages or falling back entirely.

The returned source is recorded as `llm`, `llm_normalized` or `template`.

### Supplier-value calibration

The existing estimate remains conservative. Selection will first gain an explicit operational
relief branch, which reduces dependence on the estimate's exact coefficient. Any coefficient
change must be justified by a general relationship and covered by monotonic tests: more supplier
relief cannot reduce estimated supplier value, and no estimate can bypass buyer-cost or must-have
guards.

## Data flow

1. Supplier reply becomes a grounded observation.
2. Deterministic concession logic calculates the next buyer-cost target.
3. Bid generation creates only valid packages at that target.
4. Adaptive selection uses the live ask, previous portfolio and grounded observation to choose a
   small portfolio.
5. The communication model selects approved communication elements.
6. Narrow normalization fills portfolio/first-claim omissions.
7. The existing guard validates packages, claims, prose and economics before dispatch.
8. Replay events record the proposal, observation and writer outcome.

## Error handling

- No condition-matching candidate: continue with supplier-value and exploration groups.
- No novel supplier-value-preserving candidate: keep safe novel candidates; do not return an empty
  portfolio solely because the estimate rejects them.
- Conflicting grounded conditions: prefer the condition covering the most negotiable fields, then
  the earliest condition in the observation.
- Normalization makes a selected trade incompatible: clear the optional trade sentence.
- Schema, unknown-ID, final-state or guard failure: retain the existing template fallback.
- Empty safe candidate set: retain the existing deterministic closure path.

## Testing strategy

All behavior changes use RED-GREEN TDD.

### Writer tests

- A one-package model selection on first contact is normalized to the approved opening portfolio
  and retains its valid claim, acknowledgment and tone.
- A missing first-contact claim is filled without replacing the rest of a valid selection.
- Later explicit single-branch selection remains legal when the portfolio selector supplied one
  matched package.
- Unknown IDs and invalid final-offer structures still fall back safely.
- Event metadata distinguishes clean, normalized and template sources.

### Portfolio tests

- A holding supplier receives at least one value-preserving package plus a distinct exploration
  package when both exist.
- A grounded indexation/contract condition ranks the matching package first and keeps it in later
  turns.
- A capacity-constrained supplier receives an operational-relief branch when safe.
- No output exceeds reservation cost, breaks must-haves or repeats a prior package unnecessarily.
- The live-price ceiling is exercised through the real `acceptance.decide` call, not only through
  a direct unit test of `bids.select`.

### Regression fixtures

- P02-shaped fixture: payment diversity survives without forcing a template.
- P18-shaped fixture: an indexation plus duration condition remains represented after a hold.
- P10-shaped fixture: the first portfolio contains both buyer-preferred and supplier-relief lead
  branches when valid.
- P05-shaped fixture: three-supplier concurrency and economic dominance remain unchanged.

### Verification

- Focused tests for every changed component.
- Full `pytest` suite.
- Ruff lint and formatting checks.
- Hosted benchmarking only after separate approval. The recommended first benchmark is two runs
  each of the weak cases P02, P05, P10, P18 and P11, followed by one full ten-case run if no
  regression appears; maximum 20 sessions in this validation stage.

## Success criteria

1. No valid open-turn model output falls back solely because it selected too few approved packages
   or omitted a first-contact claim.
2. Replays can measure clean, normalized and template writer outcomes separately.
3. A grounded conditional trade materially changes the next package ranking.
4. Supplier-value preservation guarantees one branch without deleting all exploration branches.
5. P18- and P10-shaped regression tests demonstrate the desired package portfolios.
6. The full local suite and Ruff checks pass.
7. No hosted session is consumed during implementation.
8. With later benchmark approval, the candidate must not regress more than one of the five targeted
   weak cases and should exceed the current 65.2 full-run mean before promotion.

## Non-goals

- Predicting hidden supplier reservation prices.
- Training a learned opponent model from the hosted practice quota.
- Case-specific or supplier-specific production heuristics.
- Replacing deterministic economics or acceptance with an LLM.
- Changing orchestration, concurrency, persistence or API contracts unrelated to package choice.
- Automatically executing or promoting a hosted benchmark result.
