#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  echo "Missing project environment: $ROOT/.venv/bin/python" >&2
  echo "Create it and install requirements.txt before starting." >&2
  exit 2
fi

source "$ROOT/.venv/bin/activate"
export MISHKA_ROOT="$ROOT"
export MISHKA_CACHE_ROOT="${MISHKA_CACHE_ROOT:-$ROOT/runtime/cache}"
export MISHKA_ENV_FILE="${MISHKA_ENV_FILE:-$ROOT/.env}"
export MISHKA_PORT="${MISHKA_PORT:-8000}"
export OLLAMA_MODELS="${OLLAMA_MODELS:-$MISHKA_CACHE_ROOT/ollama}"

# WSLg does not export its PulseAudio endpoint into non-interactive SSH
# sessions. PortAudio/sounddevice otherwise starts with no host API and the
# physical headless runtime cannot open the default microphone or sink.
if [[ -z "${PULSE_SERVER:-}" && -S /mnt/wslg/PulseServer ]]; then
  export PULSE_SERVER="unix:/mnt/wslg/PulseServer"
fi

exec "$ROOT/.venv/bin/python" "$ROOT/scripts/run_stand.py"
