# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Our entry for the Tacto Negotiation Arena: a buyer agent for Hollerau Pumpenwerke that negotiates by
e-mail with hosted supplier bots; the score is total cost in euros against the opening and best
possible deal. Spec: `brief/brief.md`, `brief/api.md`, `brief/submission.md`. Data: `data/` (SQLite,
`facts.json`, contracts, correspondence; quirks in `data/KNOWN_ISSUES.md`). Kit and our code:
`starter-kit/` (forked from tacto-ai/negotiation-arena-kit, MIT).

## Commands (run in `starter-kit/`)

- Install: `uv sync --all-extras`
- Lint: `uv run ruff check . && uv run ruff format --check .`
- Tests: `uv run pytest -q`; one test: `uv run pytest -q tests/test_buyer_policy.py -k name`
- Local evaluation (no quota): `uv run arena eval --agents naive,buyer --cases eval_cases`
- Final: `uv run arena run --set final --agent buyer --parallel 10`

Hosted practice costs quota (200 sessions per rolling 24 h); ask before running it. Sandbox
(`case:D01`, `case:D02`) is free but still needs the user's go-ahead.

## Architecture

- `arena_kit/cost.py` is the single source for C; never re-implement cost elsewhere.
- `arena_kit/play.py` runs a case (`play`, `play_all`, `play_waves`); an agent is any object with
  `decide(case, threads)`.
- `arena_kit/buyer/`: the deterministic core (`space`, `economics`, `playbook`, `assess`, `bids`,
  `acceptance`) decides close / walk / which packages; `dossier` + `guard` handle evidence and claims;
  the model (`model`, `prompts`, `writer`, `decisions`) only interprets and phrases, always with a
  template fallback; `agent.py` wires one turn.
- `agents/naive.py` and `agents/llm.py` are baselines.
- `arena-sim` (`arena_kit/sim.py`) only reads the price: use it for validity, timing and packaging, not
  persuasion.

## Code principles

- No smoke tests.
- No bloat: add only what the task needs.
- Keep code lean.
- Keep the structure clear.

## Safety

- Never print or commit `.env` values. Never push the local `entire/*` branches (they hold keys).
- The final runner must not call `/v1/events*`, `/screen`, `/leaderboard`, `/results` or their JSON.

## Tooling config

- `.claude/settings.json` wires Claude Code hooks to the [Entire CLI](https://docs.entire.io/cli/installation#installation-methods) (`entire hooks claude-code ...`); no-op if `entire` isn't on PATH.
- `.entire/settings.json` enables Entire with checkpoints as git refs, telemetry off.
- Reading `./.entire/metadata/**` is denied by the permissions config; don't try to read it.
