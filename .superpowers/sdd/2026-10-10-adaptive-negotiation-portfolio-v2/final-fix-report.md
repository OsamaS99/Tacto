# Final corrective wave

## Changes

- `normalize_turn` now clears a trade only when it is in the approved `pkt.trades` menu and became incompatible with a package appended during normalization. A trade absent from that menu remains intact so `turn_errors` rejects it and `write_turn` uses the safe template fallback.
- Added a regression covering the valid-schema but unapproved `lead_time_days` trade.
- Clarified the adaptive-portfolio documentation: the live supplier price bounds selected/generated prices when feasible candidates at or below it exist; otherwise the safest cheapest feasible branch may remain above that price.

## TDD evidence

RED, before the production change:

```text
Command: ./.venv/bin/python -m pytest -q tests/test_buyer_llm.py -k unknown_trade_uses_template_instead_of_normalizing_it_away
Result: 1 failed, 32 deselected; expected `source == "template"`, received `llm_normalized`.
```

GREEN, after the minimal production change:

```text
Command: ./.venv/bin/python -m pytest -q tests/test_buyer_llm.py -k unknown_trade_uses_template_instead_of_normalizing_it_away
Result: 1 passed, 32 deselected.
```

Focused writer tests:

```text
Command: ./.venv/bin/pytest -q tests/test_buyer_llm.py
Result: 33 passed in 1.95s.
```

Targeted Ruff:

```text
Command: ./.venv/bin/ruff check arena_kit/buyer/writer.py tests/test_buyer_llm.py && ./.venv/bin/ruff format --check arena_kit/buyer/writer.py tests/test_buyer_llm.py
Result: All checks passed; 2 files already formatted.
```

Full suite:

```text
Command: ./.venv/bin/pytest -q
Result: 218 passed, 1 skipped, 22 errors in 5.18s.
```

All 22 full-suite errors were simulator fixtures attempting to bind a local ephemeral port and receiving `PermissionError: [Errno 1] Operation not permitted` from `tests/conftest.py`; this is the execution sandbox restriction. No hosted Arena or external provider was called.

## Commit

Focused corrective commit: `914c859072f81816d931d97f67185d7eccec5a68` (`fix(buyer): reject unknown communication trades`).
