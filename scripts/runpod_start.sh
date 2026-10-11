#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${MISHKA_ROOT:-$(cd -- "$SCRIPT_DIR/.." && pwd)}"
CACHE_ROOT="${MISHKA_CACHE_ROOT:-/workspace/.cache/mishka}"
ENV_FILE="${MISHKA_ENV_FILE:-/workspace/.config/mishka/.env.runpod}"
OLLAMA_MODELS="${OLLAMA_MODELS:-$CACHE_ROOT/ollama}"
OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11434}"
OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"
LLM_MODEL="${LLM_MODEL:-qwen3:14b}"

cd "$ROOT"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  . "$ENV_FILE"
  set +a
fi
export MISHKA_ROOT="$ROOT" MISHKA_CACHE_ROOT="$CACHE_ROOT" OLLAMA_MODELS

if [[ "${MISHKA_PORT:-8000}" != "8000" && "${MISHKA_ALLOW_NONCANONICAL_PORT:-0}" != "1" ]]; then
  echo "Canonical RunPod launch requires MISHKA_PORT=8000; refusing non-canonical port ${MISHKA_PORT}." >&2
  exit 2
fi
export MISHKA_PORT=8000
mkdir -p "$OLLAMA_MODELS" "$CACHE_ROOT/rvc-tmp" "$CACHE_ROOT/silero"
resolve_project_path() { case "$1" in /*) printf "%s" "$1" ;; *) printf "%s/%s" "$ROOT" "$1" ;; esac; }

if [[ "${MISHKA_PROFILE:-production_candidate}" == "local_gpu" || "${MISHKA_PROFILE:-production_candidate}" == "production" || "${MISHKA_PROFILE:-production_candidate}" == "production_candidate" || "${MISHKA_PROFILE:-production_candidate}" == "exhibition_candidate" || "${MISHKA_PROFILE:-production_candidate}" == "production_qwen_rvc" ]]; then
  command -v nvidia-smi >/dev/null 2>&1 || { echo "GPU preflight failed: nvidia-smi is unavailable" >&2; exit 1; }
  nvidia-smi >/dev/null 2>&1 || { echo "GPU preflight failed: driver is not responding" >&2; exit 1; }
  "$ROOT/.venv/bin/python" - <<'PY'
import torch
if torch.__version__.split("+", 1)[0] != "2.8.0" or torch.version.cuda != "12.8" or not torch.cuda.is_available():
    raise SystemExit(f"Torch/CUDA preflight failed: version={torch.__version__}, cuda={torch.version.cuda}, available={torch.cuda.is_available()}")
print("GPU/Torch preflight OK", torch.__version__, torch.version.cuda, torch.cuda.get_device_name(0))
PY
fi

if [[ "${MISHKA_VOICE_CONVERSION:-rvc_v2}" == "rvc_v2" ]]; then
  MODEL="$(resolve_project_path "${RVC_MODEL:-models/rvc/mishka_rvc_v2_e250_2250s.pth}")"
  INDEX="$(resolve_project_path "${RVC_INDEX:-models/rvc/mishka_rvc_v2_e250_2250s.index}")"
  [[ -s "$MODEL" && -s "$INDEX" ]] || { echo "RVC model preflight failed: canonical model/index missing" >&2; exit 1; }
  [[ "$(sha256sum "$MODEL" | awk '{print $1}')" == "160daf2120b461b257b76e4fb9b94b3b8f586b81611a2337533a8d4e213fa1d2" ]] || { echo "RVC model hash mismatch" >&2; exit 1; }
  [[ "$(sha256sum "$INDEX" | awk '{print $1}')" == "6a6dec7908090fbf8c330167379a624d0b008d28cea054734e6d5ba5e80e7cbe" ]] || { echo "RVC index hash mismatch" >&2; exit 1; }
  for asset in "$ROOT/vendor/rvc/rvc/models/predictors/rmvpe.pt" "$ROOT/vendor/rvc/rvc/models/embedders/contentvec/pytorch_model.bin" "$ROOT/vendor/rvc/rvc/models/embedders/contentvec/config.json"; do [[ -s "$asset" ]] || { echo "RVC runtime asset missing: $asset" >&2; exit 1; }; done
fi
[[ -s "${SILERO_CACHE_PATH:-$CACHE_ROOT/silero/v5_5_ru.pt}" ]] || { echo "Silero cache preflight failed" >&2; exit 1; }

"$ROOT/.venv/bin/python" - <<'PY'
import importlib
for name in ("fastapi", "faster_whisper", "langgraph", "num2words", "docopt", "onnxruntime", "psutil"):
    importlib.import_module(name)
print("production import preflight: PASS")
PY
"$ROOT/.venv/bin/python" -m pip check

if ! command -v ollama >/dev/null 2>&1; then
  echo "Canonical startup requires the pinned Ollama installation; refusing an unpinned runtime install." >&2
  exit 1
fi
if ! curl -fsS "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
  mkdir -p "$ROOT/logs"
  nohup env OLLAMA_HOST="$OLLAMA_HOST" OLLAMA_MODELS="$OLLAMA_MODELS" ollama serve > "$ROOT/logs/ollama.log" 2>&1 &
fi
for _ in $(seq 1 90); do
  if curl -fsS "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then break; fi
  sleep 2
done
curl -fsS "$OLLAMA_URL/api/tags" >/dev/null
if ! curl -fsS "$OLLAMA_URL/api/tags" | grep -q '"name":"'"$LLM_MODEL"'"'; then
  OLLAMA_MODELS="$OLLAMA_MODELS" ollama pull "$LLM_MODEL"
fi

exec env PYTHONPATH=. MISHKA_PORT=8000 .venv/bin/python scripts/run_stand.py
