# Adaptive Negotiation Portfolio V2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore adaptive writer output and make safe package portfolios follow grounded supplier preferences so the buyer can exceed its current 65.2 mean score.

**Architecture:** Keep generation, cost and acceptance deterministic. Normalize only safe omissions in the communication model's approved-ID selection, then rank already-valid bid candidates with grounded conditional fields, supplier-value guarantees, novelty and operational relief; wire that selector through the existing acceptance policy and replay metadata.

**Tech Stack:** Python 3.12, frozen dataclasses, pytest, uv, Ruff.

**Spec:** `docs/superpowers/specs/2026-10-10-adaptive-negotiation-portfolio-v2.md`

## Global Constraints

- `arena_kit.cost` and `EconomicEvaluator` remain the only sources of buyer total cost and supplier value respectively.
- Acceptance and walk-away remain deterministic and may name only a current standing offer.
- No case ID or supplier ID appears in production policy.
- No `.env` file is read, printed or changed by this work.
- No hosted practice, sandbox or final call is made without explicit user approval.
- Existing supplier concurrency, durable outbox, evidence guards, deadline handling and role-model overrides remain intact.
- Normalization may append only IDs already present in the approved package and claim menus.
- Final-offer and agreed messages remain exactly one package, no claim and no question.
- Every behavior change follows RED-GREEN TDD; run the complete test suite and Ruff before completion.

## Review Focus

- Invalid or unknown model IDs must still use the template; normalization must never turn an unsafe selection into a valid one. Task 1 pins this with unknown-package and unknown-claim tests.
- Final-offer/agreed output must remain one package with no claim or question even when opening normalization exists. Task 1 retains and extends the final-state test.
- Conflicting grounded conditions must resolve deterministically without deleting the safe portfolio. Task 2 pins longest-condition/earliest-condition ordering.
- A holding supplier with no value-preserving novel package must still receive a safe novel package instead of forcing premature closure. Task 2 replaces the old empty-filter expectation.
- Three-supplier RFQ concurrency and economic dominance must remain unchanged while the portfolio policy changes. Task 4 runs the existing RFQ regressions and full suite.

---

### Task 1: Normalize safe communication selections instead of discarding them

**Files:**
- Modify: `starter-kit/arena_kit/buyer/writer.py:130-205`
- Modify: `starter-kit/tests/test_buyer_llm.py:70-145`
- Modify: `starter-kit/tests/test_buyer_llm.py:280-315`

**Interfaces:**
- Consumes: `DisclosurePacket`, parsed TURN output, approved package map `dict[str, dict]`, approved claim map `dict[str, Claim]`.
- Produces: `normalize_turn(out, packages, claims, pkt) -> tuple[dict | None, bool]`; writer source values `llm`, `llm_normalized`, or `template`.

- [ ] **Step 1: Change the opening and later-turn tests to require safe normalization**

Replace the two fallback expectations and add the invalid-ID and incompatible-trade cases in `tests/test_buyer_llm.py`:

```python
def test_opening_selection_is_normalized_to_all_packages_and_one_claim():
    pkt = replace(PKT, rev=5, first_contact=True)
    call, _ = replies(turn(package_ids=("P1",), claim_ids=()))
    msg, source, _ = writer.write_turn(Model([("m", call)]), pkt, 10.0)
    assert source == "llm_normalized"
    assert msg.term_sheets == SHEETS and msg.refs == CLAIM.ids


def test_later_open_selection_is_normalized_to_two_packages():
    call, _ = replies(turn(package_ids=("P1",)))
    msg, source, _ = writer.write_turn(Model([("m", call)]), replace(PKT, rev=3), 10.0)
    assert source == "llm_normalized" and msg.term_sheets == SHEETS


def test_later_single_matched_branch_remains_a_clean_model_selection():
    pkt = replace(PKT, packages=(SHEETS[0],), first_contact=False)
    call, _ = replies(turn(package_ids=("P1",)))
    msg, source, _ = writer.write_turn(Model([("m", call)]), pkt, 10.0)
    assert source == "llm" and msg.term_sheets == (SHEETS[0],)


@pytest.mark.parametrize("bad", [{"package_ids": ("P9",)}, {"claim_ids": ("C7",)}])
def test_normalization_never_repairs_unknown_ids(bad):
    call, _ = replies(turn(**bad))
    msg, source, _ = writer.write_turn(Model([("m", call)]), replace(PKT, first_contact=True), 10.0)
    assert source == "template" and msg == writer.template(replace(PKT, first_contact=True))


def test_normalization_clears_a_trade_missing_from_an_appended_package():
    sheets = (SHEETS[0], {"unit_price": 205.0, "contract_months": 24})
    pkt = replace(PKT, packages=sheets, trades=("payment_days",), first_contact=True)
    call, _ = replies(turn(package_ids=("P1",), trade="payment_days"))
    msg, source, _ = writer.write_turn(Model([("m", call)]), pkt, 10.0)
    assert source == "llm_normalized" and msg.term_sheets == sheets
    assert "payment terms shown" not in msg.body
```

- [ ] **Step 2: Run the focused writer tests to verify RED**

Run from `starter-kit/`:

```bash
uv run pytest -q tests/test_buyer_llm.py -k 'opening_selection_is_normalized or later_open_selection_is_normalized or later_single_matched or normalization_never_repairs or normalization_clears'
```

Expected: the normalization tests fail because valid partial selections still return `template`; unknown-ID tests remain green.

- [ ] **Step 3: Add narrow deterministic normalization**

Add this helper before `write_turn` in `writer.py`:

```python
def normalize_turn(out, packages, claims, pkt):
    """Fill safe open-turn omissions from approved menus; never repair unknown or malformed IDs."""
    if out is None or TURN.check(out):
        return out, False
    if any(i not in packages for i in out["package_ids"]) or any(i not in claims for i in out["claim_ids"]):
        return out, False
    normalized = {
        **out,
        "package_ids": list(out["package_ids"]),
        "claim_ids": list(out["claim_ids"]),
    }
    if pkt.status == "open":
        required = len(packages) if pkt.first_contact else min(2, len(packages))
        for package_id in packages:
            if len(normalized["package_ids"]) >= required:
                break
            if package_id not in normalized["package_ids"]:
                normalized["package_ids"].append(package_id)
        if pkt.first_contact and claims and not normalized["claim_ids"]:
            normalized["claim_ids"].append(next(iter(claims)))
        trade = normalized["trade"]
        if trade and any(trade not in packages[i] for i in normalized["package_ids"]):
            normalized["trade"] = None
    return normalized, normalized != out
```

Use the normalized object both in the model validator and after parsing the returned text:

```python
    def validate(raw):
        selected, _ = normalize_turn(parse_json(raw), packages, claims, pkt)
        return turn_errors(selected, packages, claims, pkt=pkt)

    raw = model.complete(
        system,
        user,
        end - time.monotonic(),
        role="communication",
        deadline=end,
        validate=validate,
    )
    out, normalized = normalize_turn(parse_json(raw), packages, claims, pkt)
    if out is None or turn_errors(out, packages, claims, pkt=pkt):
        return template(pkt), "template", None
    source = "llm_normalized" if normalized else "llm"
    return assemble(pkt, out, packages, claims), source, {"rationale": out["rationale"]}
```

Keep `turn_errors` as the final authority. Do not remove its package, claim, final-state, question, acknowledgment or trade checks.

- [ ] **Step 4: Pin final-state behavior and replay counting**

Keep `test_final_offer_still_requires_one_package_and_no_claim` unchanged and extend the replay summary test:

```python
def test_replay_counts_normalized_writer_output(tmp_path):
    lines = [
        {"kind": "send", "writer": "llm"},
        {"kind": "send", "writer": "llm_normalized"},
        {"kind": "send", "writer": "template"},
    ]
    (tmp_path / "writer.events.jsonl").write_text("\n".join(map(json.dumps, lines)))
    events, sessions = replay.load([tmp_path])
    assert replay.summarize(events, sessions)["writer"] == {
        "llm": 1,
        "llm_normalized": 1,
        "template": 1,
    }
```

- [ ] **Step 5: Run the writer and replay tests to verify GREEN**

```bash
uv run pytest -q tests/test_buyer_llm.py
```

Expected: all writer, model, prompt and replay tests pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add starter-kit/arena_kit/buyer/writer.py starter-kit/tests/test_buyer_llm.py
git commit -m "fix: normalize safe negotiation selections"
```

### Task 2: Select an adaptive bounded portfolio from safe candidates

**Files:**
- Modify: `starter-kit/arena_kit/buyer/bids.py:101-159`
- Modify: `starter-kit/arena_kit/buyer/acceptance.py:148-158`
- Modify: `starter-kit/tests/test_buyer_policy.py:187-238`
- Modify: `starter-kit/tests/test_buyer_policy.py:580-609`

**Interfaces:**
- Consumes: `list[EvaluatedPackage]`, `demand(terms) -> dict[str, float]`, live reference terms, grounded condition field tuples, optional prior supplier-value floor.
- Produces: `bids.select(evaluated, demand, ceiling, n, *, values, conditions, supplier_floor, reference) -> list[EvaluatedPackage]`; `responsive_packages(packages, thread, evaluator, assessment)` returns novel candidates without deleting lower-value exploration branches; `previous_supplier_value(thread, evaluator, assessment) -> float | None`.

- [ ] **Step 1: Replace the over-filtering tests with guarantee-plus-exploration tests**

Replace the two tests that expect an empty or friendly-only list:

```python
def test_responsive_filter_keeps_safe_novel_packages_when_none_preserves_value():
    ev = evaluator()
    old = {"unit_price": 100.0, "payment_days": 30, "contract_months": 24, "incoterm": "DAP"}
    novel = {"unit_price": 100.0, "payment_days": 90, "contract_months": 24, "incoterm": "DAP"}
    t = thread(ev, prices=(110.0, 110.0), mine=(190_000.0,))
    t.my_sheets = [old]
    package = ev.evaluate(CandidatePackage("sup:A", novel, "terms"))
    assert acceptance.responsive_packages([package], t, ev, assess(t)) == [package]


def test_responsive_filter_orders_a_value_preserving_package_before_exploration():
    ev = evaluator()
    old = {"unit_price": 100.0, "payment_days": 30, "contract_months": 24, "incoterm": "DAP"}
    explore = {"unit_price": 100.0, "payment_days": 90, "contract_months": 24, "incoterm": "DAP"}
    friendly = {"unit_price": 101.0, "payment_days": 30, "contract_months": 24, "incoterm": "DAP"}
    t = thread(ev, prices=(110.0, 110.0), mine=(190_000.0,))
    t.my_sheets = [old]
    packages = [ev.evaluate(CandidatePackage("sup:A", terms, "terms")) for terms in (explore, friendly)]
    assert acceptance.responsive_packages(packages, t, ev, assess(t)) == [packages[1], packages[0]]
```

Add direct selector coverage for conditions, supplier value, conflicting conditions and operational relief:

```python
def test_select_keeps_condition_match_value_guarantee_and_exploration():
    ev = evaluator()
    clause = {"series": "S", "material_share": 0.45, "deadband_pct": 0.0, "base_month": "2026-08"}
    terms = [
        {"unit_price": 100.0, "payment_days": 90, "contract_months": 24, "incoterm": "DAP", "index_clause": clause},
        {"unit_price": 101.0, "payment_days": 30, "contract_months": 24, "incoterm": "DAP", "index_clause": None},
        {"unit_price": 100.5, "payment_days": 60, "contract_months": 12, "incoterm": "DAP", "index_clause": None},
    ]
    packages = [ev.evaluate(CandidatePackage("sup:A", item, "terms")) for item in terms]
    reference = {"payment_days": 90, "contract_months": 24, "incoterm": "DAP", "index_clause": clause}
    out = bids.select(
        packages,
        demand=lambda item: {"payment_days": max(0, item["payment_days"] - 30) / 60},
        n=2,
        conditions=(("index_clause", "contract_months"),),
        supplier_floor=packages[1].cash_theirs,
        reference=reference,
    )
    assert out[0] is packages[0]
    assert packages[1] in out


def test_select_resolves_equal_condition_coverage_deterministically():
    ev = evaluator()
    packages = [
        ev.evaluate(CandidatePackage("sup:A", terms, "terms"))
        for terms in (
            {"unit_price": 100.0, "payment_days": 90, "contract_months": 12, "incoterm": "DAP"},
            {"unit_price": 100.0, "payment_days": 60, "contract_months": 24, "incoterm": "DAP"},
        )
    ]
    out = bids.select(
        packages,
        conditions=(("payment_days",), ("contract_months",)),
        reference={"payment_days": 90, "contract_months": 24, "incoterm": "DAP"},
        n=1,
    )
    assert out == [packages[0]]
```

- [ ] **Step 2: Run the focused package tests to verify RED**

```bash
uv run pytest -q tests/test_buyer_policy.py -k 'responsive_filter or select_keeps_condition or select_resolves_equal_condition'
```

Expected: the existing responsive filter drops exploration, and `bids.select` does not accept `conditions` or `supplier_floor`.

- [ ] **Step 3: Make responsive filtering preserve novelty and expose the prior floor**

Replace `responsive_packages` and add the floor helper in `acceptance.py`:

```python
def previous_supplier_value(t, ev, assessment):
    if not t.my_sheets or assessment.firmness == 0:
        return None
    return max(ev.cash_theirs(t.sid, terms) for terms in t.my_sheets)


def responsive_packages(packages, t, ev, assessment):
    """Prefer novel supplier-value-preserving packages without deleting safe exploration."""
    if not t.my_sheets or assessment.firmness == 0:
        return packages
    novel = [p for p in packages if p.terms not in t.my_sheets]
    if not novel:
        return packages
    floor = previous_supplier_value(t, ev, assessment)
    friendly = [p for p in novel if p.cash_theirs >= floor - 0.005]
    return [*friendly, *(p for p in novel if p not in friendly)]
```

- [ ] **Step 4: Extend `bids.select` with deterministic groups**

Change the signature to:

```python
def select(
    evaluated,
    demand=None,
    ceiling=None,
    n=3,
    *,
    values=(),
    conditions=(),
    supplier_floor=None,
    reference=None,
):
```

Inside `select`, retain the existing finite-value and ceiling guards. Compute condition coverage only for non-price fields present in both candidate and reference:

```python
    def condition_rank(e):
        rank = []
        for fields in conditions:
            relevant = [f for f in fields if f != "unit_price" and f in e.terms and f in (reference or {})]
            matched = sum(e.terms[f] == reference[f] for f in relevant)
            rank.extend((bool(relevant) and matched == len(relevant), matched))
        return tuple(rank)
```

Replace the compact numeric-only preference generator with a loop that retains its current numeric directions and gives one point for matching a non-numeric live preference:

```python
    def preference(e):
        if not reference:
            return 0
        score = 0
        for field in values:
            value, live = e.terms.get(field), reference.get(field)
            if isinstance(value, int | float) and isinstance(live, int | float):
                if field == "payment_days":
                    score += live - value
                elif field in ("volume_per_year", "contract_months", "lead_time_days"):
                    score += value - live
            elif field in e.terms and field in reference:
                score += int(value == live)
        return score
```

Protect maximum-condition candidates from `_dominated`, then build these deterministic groups:

```python
    max_condition = max((condition_rank(e) for e, _ in pairs), default=())
    pairs = [
        (e, d)
        for e, d in pairs
        if condition_rank(e) == max_condition or not demand or not _dominated(e, d, pairs)
    ]
    by_condition = [
        e
        for e, _ in sorted(
            pairs,
            key=lambda p: (condition_rank(p[0]), preference(p[0]), p[0].cash_theirs, -p[0].c),
            reverse=True,
        )
    ]
    by_friendly = [
        e
        for e, _ in sorted(pairs, key=lambda p: (-p[0].cash_theirs, p[0].c))
        if supplier_floor is not None and e.cash_theirs >= supplier_floor - 0.005
    ]
    by_relief = [e for e, d in sorted(pairs, key=lambda p: (sum(p[1].values()), -p[0].cash_theirs, p[0].c))]
```

Select in this order, deduplicating with `_distance` and stopping at `n`:

```python
    ordered = [
        *(by_condition[:1] if conditions else []),
        *by_friendly[:1],
        *by_relief[:1],
        by_price[0],
        by_cash[0],
        *by_condition,
        *by_cash,
    ]
```

Retain the existing numeric `preference` behavior and extend it only with deterministic equality to the live reference for non-numeric values such as `index_clause` and `incoterm`. Do not change `EconomicEvaluator`, `LEAD_DAY`, cost calculation or acceptance thresholds in this task.

- [ ] **Step 5: Run package and existing policy tests to verify GREEN**

```bash
uv run pytest -q tests/test_buyer_policy.py -k 'responsive_filter or holding_supplier or select_ or generated_packages or equal_cost_values'
```

Expected: all selected tests pass; lower-value exploration survives while at least one friendly package is selected when available.

- [ ] **Step 6: Commit Task 2**

```bash
git add starter-kit/arena_kit/buyer/bids.py starter-kit/arena_kit/buyer/acceptance.py starter-kit/tests/test_buyer_policy.py
git commit -m "feat: rank adaptive negotiation portfolios"
```

### Task 3: Wire grounded conditions, round-specific caps and the live-price ceiling

**Files:**
- Modify: `starter-kit/arena_kit/buyer/acceptance.py:207-245`
- Modify: `starter-kit/tests/test_buyer_policy.py:580-635`

**Interfaces:**
- Consumes: Task 2 extended `bids.select` interface and `previous_supplier_value(t, ev, assessment)`.
- Produces: `condition_fields(t, space)` returns ordered nested tuples of negotiable field names; the main `decide` path passes `n`, `ceiling`, `conditions` and `supplier_floor` to `bids.select`.

- [ ] **Step 1: Add a wiring test for grounded conditions and the live ceiling**

Add to `tests/test_buyer_policy.py`:

```python
def test_decide_passes_grounded_conditions_supplier_floor_cap_and_live_ceiling(monkeypatch):
    ev = evaluator()
    standing = {"unit_price": 108.0, "payment_days": 90, "contract_months": 24, "incoterm": "DAP"}
    t = thread(ev, prices=(110.0, 108.0), mine=(190_000.0,), standing=standing)
    t.my_sheets = [{"unit_price": 95.0, "payment_days": 30, "contract_months": 24, "incoterm": "DAP"}]
    t.observation = SupplierObservation(
        "m",
        "f",
        firmness=2,
        values=("payment_days", "contract_months"),
        conditional_trades=(
            {"fields": ["unit_price", "payment_days", "contract_months"], "evidence": "Ninety days for 24 months."},
        ),
        evidence=("Ninety days for 24 months.",),
        source="model",
    )
    cs, space = state(CASE, [t], ev)
    captured = {}
    original = bids.select

    def capture(evaluated, demand=None, ceiling=None, n=3, **kwargs):
        captured.update(ceiling=ceiling, n=n, **kwargs)
        return original(evaluated, demand, ceiling, n, **kwargs)

    monkeypatch.setattr(bids, "select", capture)
    proposal = decide(cs, ev, space, [t.sid])
    assert proposal.kind == "send"
    assert captured["ceiling"] == 108.0 and captured["n"] == 2
    assert captured["conditions"] == (("payment_days", "contract_months"),)
    assert captured["supplier_floor"] == pytest.approx(ev.cash_theirs(t.sid, t.my_sheets[0]))
```

- [ ] **Step 2: Add P18-shaped and capacity-shaped regression tests**

```python
def test_grounded_index_duration_condition_remains_in_later_portfolio():
    clause = {"series": "S", "material_share": 0.45, "deadband_pct": 0.0, "base_month": "2026-08"}
    case = {
        **CASE,
        "type": "raw_material",
        "negotiable": ["unit_price", "index_clause", "payment_days", "contract_months"],
        "fixed_terms": {k: v for k, v in CASE["fixed_terms"].items() if k != "index_clause"},
        "term_options": {"index_clause": [None, clause], "payment_days": [30, 90], "contract_months": [24, 36]},
    }
    ev = evaluator(case, {"payment_days": 30, "contract_months": 24, "incoterm": "DAP"})
    standing = {"unit_price": 110.0, "index_clause": clause, "payment_days": 90, "contract_months": 24}
    t = thread(ev, prices=(115.0, 110.0), mine=(190_000.0,), standing=standing)
    t.my_sheets = [
        {"unit_price": 95.0, "index_clause": None, "payment_days": 90, "contract_months": 24},
        {"unit_price": 94.0, "index_clause": clause, "payment_days": 90, "contract_months": 24},
    ]
    t.observation = SupplierObservation(
        "m",
        "f",
        firmness=2,
        values=("index_clause", "contract_months"),
        conditional_trades=(
            {"fields": ["unit_price", "contract_months", "index_clause"], "evidence": "24 months provided indexation is included."},
        ),
        evidence=("24 months provided indexation is included.",),
        source="model",
    )
    cs, space = state(case, [t], ev, opening=115.0)
    proposal = decide(cs, ev, space, [t.sid])
    assert proposal.kind == "send"
    assert any(p.terms["index_clause"] == clause and p.terms["contract_months"] == 24 for p in proposal.packages)
    assert len(proposal.packages) <= 2


def test_capacity_portfolio_includes_an_operational_relief_branch_when_valid():
    case = {
        **CASE,
        "type": "capacity",
        "negotiable": ["unit_price", "lead_time_days", "contract_months"],
        "fixed_terms": {"payment_days": 60, "volume_per_year": 1000, "incoterm": "DAP", "index_clause": None},
        "term_options": {"lead_time_days": [42, 56], "contract_months": [24, 36]},
    }
    case = with_buyer(case, need_lead_time_days=42, must_haves=[])
    std = {"lead_time_days": 56, "contract_months": 12, "payment_days": 30, "incoterm": "EXW"}
    ev, space = evaluator(case, std), ContractSpace(case)
    t = thread(ev, prices=(110.0,), standing={"unit_price": 110.0, "lead_time_days": 56, "contract_months": 12})
    t.observation = SupplierObservation("m", "f", values=("lead_time_days", "contract_months"))
    cs, _ = state(case, [t], ev)
    proposal = decide(cs, ev, space, [t.sid])
    assert proposal.kind == "send"
    assert {42, 56} <= {p.terms["lead_time_days"] for p in proposal.packages}
```

- [ ] **Step 3: Run the new integration tests to verify RED**

```bash
uv run pytest -q tests/test_buyer_policy.py -k 'decide_passes_grounded or grounded_index_duration or capacity_portfolio_includes'
```

Expected: `decide` does not pass the new selector context; the conditional and relief portfolio assertions fail.

- [ ] **Step 4: Add grounded condition extraction**

Add before `decide` in `acceptance.py`:

```python
def condition_fields(t, space):
    observation = t.observation
    if observation is None:
        return ()
    out = []
    for trade in observation.conditional_trades:
        fields = tuple(f for f in trade.get("fields", ()) if f in space.levers)
        if fields and fields not in out:
            out.append(fields)
    return tuple(sorted(out, key=lambda fields: -len(fields)))
```

Stable sorting preserves the observation order for equal-length conditions, satisfying the deterministic conflict rule.

- [ ] **Step 5: Wire the adaptive selector through `decide`**

Replace the package-selection block with:

```python
        candidates = responsive_packages(
            bids.generate(ev, space, sid, next_c, their_price, t.standing),
            t,
            ev,
            a,
        )
        packages = bids.select(
            candidates,
            demand,
            ceiling=their_price,
            n=3 if not t.my_sheets else 2,
            values=a.values,
            conditions=condition_fields(t, space),
            supplier_floor=previous_supplier_value(t, ev, a),
            reference={**ev.standard(sid), **t.standing},
        )
```

Keep the existing no-regressive-price `_cut` guard after selection and all acceptance/closure code unchanged.

- [ ] **Step 6: Run all policy tests to verify GREEN**

```bash
uv run pytest -q tests/test_buyer_policy.py
```

Expected: all policy, economics, RFQ, final-offer and practice-shaped regression tests pass.

- [ ] **Step 7: Commit Task 3**

```bash
git add starter-kit/arena_kit/buyer/acceptance.py starter-kit/tests/test_buyer_policy.py
git commit -m "feat: follow grounded supplier trades"
```

### Task 4: Verify agent observability, document the policy and run all local gates

**Files:**
- Modify: `starter-kit/tests/test_buyer_agents.py:17-58`
- Modify: `starter-kit/docs/BUYER.md`
- Test: `starter-kit/tests/test_buyer_agents.py`
- Test: `starter-kit/tests/test_buyer_transport.py`
- Test: `starter-kit/tests/test_buyer_policy.py`

**Interfaces:**
- Consumes: Task 1 writer source `llm_normalized`; Task 2 selector; Task 3 decision wiring.
- Produces: replay-visible agent event metadata and operator documentation for adaptive portfolios.

- [ ] **Step 1: Add an agent-level event-source test**

Add to `tests/test_buyer_agents.py`:

```python
def test_agent_event_records_normalized_writer_output():
    answer = {
        **TURN.example,
        "package_ids": ["P1"],
        "claim_ids": [],
        "question": None,
    }
    agent = BuyerAgent(model=Model([("mock", lambda system, user, timeout: json.dumps(answer))]))
    agent.prepare(CASE, allow_model=False)
    events = {
        "sup:A": [
            {
                "from": "supplier",
                "message_id": "opening",
                "status": "open",
                "body": "Standing offer.",
                "term_sheets": [{"term_sheet_id": "offer:A", "terms": {"unit_price": 110.0, **STD}}],
            }
        ]
    }
    move = agent.decide(CASE, events)
    assert move["message"]["term_sheets"]
    assert agent.event["writer"] == "llm_normalized"
```

- [ ] **Step 2: Run the agent integration test to verify it passes through the real event path**

```bash
uv run pytest -q tests/test_buyer_agents.py -k agent_event_records_normalized
```

Expected: PASS. If it fails, fix only propagation of the Task 1 source through the existing agent event update; do not add a second source field.

- [ ] **Step 3: Document the adaptive policy and replay values**

Add a short “Adaptive portfolios” subsection to `starter-kit/docs/BUYER.md` containing these operational facts:

```markdown
### Adaptive portfolios

The deterministic policy opens with up to three safe Pareto-diverse packages, then narrows later
turns to at most two. Grounded conditional trades rank matching packages first; a holding supplier
still receives at least one package that preserves estimated supplier value when one exists, while
another safe branch may remain for exploration. The live supplier price caps generated unit prices.

The communication model selects only approved IDs. Safe omissions are completed from the approved
menus and logged as `writer=llm_normalized`; `writer=llm` means no completion was needed and
`writer=template` means the model was absent, late or invalid. Cost, must-haves, acceptance and
walk-away remain deterministic.
```

- [ ] **Step 4: Run focused cross-component regression gates**

```bash
uv run pytest -q tests/test_buyer_llm.py tests/test_buyer_agents.py tests/test_buyer_policy.py tests/test_buyer_transport.py
```

Expected: all focused tests pass, including final-offer safety, three-supplier concurrency and durable transport.

- [ ] **Step 5: Run the complete local verification suite**

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

Expected: the full suite passes with only the repository's documented skip; Ruff reports no lint or formatting errors. Do not run `arena play`, `arena run`, sandbox, practice or final commands.

- [ ] **Step 6: Commit Task 4**

```bash
git add starter-kit/tests/test_buyer_agents.py starter-kit/docs/BUYER.md
git commit -m "docs: describe adaptive negotiation portfolios"
```

## Post-implementation benchmark gate

Hosted benchmarking is intentionally outside the implementation tasks. After the branch passes its final code review, request explicit approval before consuming quota. If approved, run at most two sessions each for P02, P05, P10, P18 and P11; only if no more than one targeted case regresses, run one complete ten-case batch. Promotion requires a full-run mean above 65.2, with approximately 69.6 as the first recovery target.
