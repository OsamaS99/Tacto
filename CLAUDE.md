# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

This repository contains no application code yet — only an initial commit with tooling config. There are no build, lint, or test commands and no architecture to document. Update this file once source code is added.

## Code principles

- No smoke tests.
- No bloat: add only what the task needs.
- Keep code lean.
- Keep the structure clear.

## Tooling config

- `.claude/settings.json` wires Claude Code hooks (session start/end, prompt submit, stop, and Agent/Task tool use) to the [Entire CLI](https://docs.entire.io/cli/installation#installation-methods) (`entire hooks claude-code ...`). Each hook is a no-op if `entire` isn't on PATH, so it is optional locally.
- `.entire/settings.json` enables Entire with checkpoints stored as git refs (`git-refs`) and telemetry off. `.entire/.gitignore` excludes `tmp/`, `logs/`, `metadata/`, `settings.local.json`, and `redactors/local/`.
- Reading `./.entire/metadata/**` is denied by the permissions config; don't try to read it.
