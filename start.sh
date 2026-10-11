#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "Missing project environment: $ROOT/.venv/bin/python" >&2
  echo "Create it and install requirements-production.lock before starting." >&2
  exit 2
fi

source "$ROOT/.venv/bin/activate"
export MISHKA_ROOT="$ROOT"
export MISHKA_CACHE_ROOT="${MISHKA_CACHE_ROOT:-/workspace/.cache/mishka}"
export MISHKA_ENV_FILE="${MISHKA_ENV_FILE:-/workspace/.config/mishka/.env.runpod}"
export MISHKA_PORT="${MISHKA_PORT:-8000}"
export OLLAMA_MODELS="${OLLAMA_MODELS:-$MISHKA_CACHE_ROOT/ollama}"

exec "$ROOT/scripts/runpod_start.sh"
