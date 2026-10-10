#!/usr/bin/env bash
# Launch one pass over the hosted practice set. Consumes practice quota when executed.
# The Arena and OpenAI credentials are loaded by the existing CLI from starter-kit/.env.
set -euo pipefail

competition_repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
competition_kit="${competition_repo}/starter-kit"
cd "${competition_kit}"

export LLM_PROVIDER=openai
export LLM_BASE_URL=https://api.openai.com/v1
export LLM_MODEL=gpt-6.1-sol
export LLM_FALLBACK_MODEL=gpt-6-luna
export LLM_SERVICE_TIER=priority
export LLM_PREPARATION_EFFORT=high
export LLM_INTERPRETATION_EFFORT=high
export LLM_CRITIC_EFFORT=high
export LLM_COMMUNICATION_EFFORT=low
export LLM_FALLBACK_EFFORT=low
export LLM_TURN=1
export BUYER_CONCURRENT=0

if [[ ! -x "${competition_kit}/.venv/bin/python" ]]; then
  printf 'Install dependencies first: cd starter-kit && uv sync --all-extras\n' >&2
  exit 1
fi

mkdir -p runs
competition_log="${competition_kit}/runs/practice-$(date +%Y%m%d-%H%M%S).log"
printf 'Starting the practice leaderboard run. Log: %s\n' "${competition_log}"
printf 'One pass over practice cases, three cases at a time.\n'

exec "${competition_kit}/.venv/bin/python" -m arena_kit.cli run \
  --set practice --agent buyer --parallel 3 \
  --data "${competition_repo}/data" --log "${competition_log}"
