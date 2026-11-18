#!/usr/bin/env bash
set +x
set -euo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
WORKSPACE="${MISHKA_WORKSPACE:-/workspace}"
VENV="${MISHKA_VENV:-$WORKSPACE/mishka-env}"
STATE="${MISHKA_REMOTE_STATE:-$WORKSPACE/mishka-runtime}"
: "${MISHKA_EXTERNAL_ORIGIN:?Set the exact external HTTPS origin, without a path}"
: "${MISHKA_MAX_SOURCE:?Set the reviewed unchanged Max source directory}"
export HF_HOME="$WORKSPACE/cache/huggingface"
export TORCH_HOME="$WORKSPACE/cache/torch"
export OLLAMA_MODELS="$WORKSPACE/models/ollama"
export OLLAMA_HOST=127.0.0.1:11434
export MISHKA_VOICE_CONVERSION=off
# Never enable optional trained voices in this baseline environment.
cd "$ROOT"
exec "$VENV/bin/python" -u scripts/run_remote_stand.py \
  --profile local_gpu --host 0.0.0.0 --port 8000 \
  --external-origin "$MISHKA_EXTERNAL_ORIGIN" \
  --whisper-model "${MISHKA_WHISPER_MODEL:-$WORKSPACE/models/faster-whisper-large-v3-turbo}" \
  --max-source "$MISHKA_MAX_SOURCE" --state-dir "$STATE"
