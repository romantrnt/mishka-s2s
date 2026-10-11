#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${1:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"
ROOT="$(cd "$ROOT" && pwd)"
VENV="$ROOT/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"
CACHE_ROOT="${MISHKA_CACHE_ROOT:-/workspace/.cache/mishka}"
OLLAMA_MODELS="${OLLAMA_MODELS:-$CACHE_ROOT/ollama}"
WHISPER_MODEL="${WHISPER_MODEL:-large-v3-turbo}"
LLM_MODEL="${LLM_MODEL:-qwen3:14b}"
SILERO_MODEL=v5_5_ru
SILERO_SPEAKER=aidar
RVC_F0_UP_KEY=0
BASE_OS="ubuntu-24.04"
LOCK_FILE="$ROOT/requirements-production.lock"
TEST_LOCK_FILE="$ROOT/requirements-test.lock"
INSTALL_TEST_DEPS="${MISHKA_INSTALL_TEST_DEPS:-0}"
DRY_RUN="${MISHKA_DRY_RUN:-0}"
MIN_DRIVER_MAJOR="570"
MIN_CUDA_MAJOR="12"
MIN_CUDA_MINOR="8"
TORCH_SPEC="torch==2.8.0+cu128"
TORCH_INDEX="https://download.pytorch.org/whl/cu128"
SILERO_CACHE="${SILERO_CACHE_PATH:-$CACHE_ROOT/silero/v5_5_ru.pt}"

fatal() { echo "ERROR: $*" >&2; exit 1; }
warn() { echo "WARNING: $*" >&2; }

[[ -d "$ROOT" ]] || fatal "missing project directory: $ROOT"
cd "$ROOT"
[[ -r /etc/os-release ]] || fatal "cannot inspect /etc/os-release; Ubuntu 24.04 is required"
# shellcheck disable=SC1091
. /etc/os-release
[[ "${ID:-}" == "ubuntu" && "${VERSION_ID:-}" == "24.04" ]] || fatal "unsupported OS: ${PRETTY_NAME:-unknown}; required Ubuntu 24.04"
[[ "${VERSION_CODENAME:-}" == "noble" ]] || fatal "unsupported Ubuntu codename: ${VERSION_CODENAME:-unknown}; required noble"
[[ "$(id -u)" == "0" ]] || fatal "setup_clean.sh must run as root so it can install apt prerequisites"
command -v apt-get >/dev/null 2>&1 || fatal "apt-get is required on Ubuntu 24.04"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || fatal "Python executable not found: $PYTHON_BIN"
[[ -s "$LOCK_FILE" ]] || fatal "missing production dependency lock: $LOCK_FILE"
[[ -s "$TEST_LOCK_FILE" ]] || fatal "missing regression dependency lock: $TEST_LOCK_FILE"

command -v nvidia-smi >/dev/null 2>&1 || fatal "nvidia-smi is unavailable; install a supported NVIDIA driver before setup"
GPU_TABLE="$(nvidia-smi --query-gpu=driver_version,name --format=csv,noheader 2>/dev/null)" || fatal "nvidia-smi cannot communicate with the NVIDIA driver"
[[ -n "$GPU_TABLE" ]] || fatal "NVIDIA GPU inventory is empty"
DRIVER_VERSION="${GPU_TABLE%%,*}"
DRIVER_MAJOR="${DRIVER_VERSION%%.*}"
[[ "$DRIVER_MAJOR" =~ ^[0-9]+$ ]] || fatal "cannot parse NVIDIA driver version: $DRIVER_VERSION"
(( DRIVER_MAJOR >= MIN_DRIVER_MAJOR )) || fatal "NVIDIA driver $DRIVER_VERSION is too old; CUDA 12.8 requires driver major >= $MIN_DRIVER_MAJOR"
NVIDIA_SMI="$(nvidia-smi 2>/dev/null)" || fatal "nvidia-smi health check failed"
CUDA_CAPABILITY="$(printf '%s\n' "$NVIDIA_SMI" | sed -n 's/.*CUDA Version: \([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p' | head -n1)"
if [[ -z "$CUDA_CAPABILITY" ]]; then
  warn "nvidia-smi did not expose a CUDA Version field; Python/Torch CUDA validation remains mandatory"
else
  CUDA_MAJOR="${CUDA_CAPABILITY%%.*}"
  CUDA_MINOR="${CUDA_CAPABILITY##*.}"
  if (( CUDA_MAJOR < MIN_CUDA_MAJOR || (CUDA_MAJOR == MIN_CUDA_MAJOR && CUDA_MINOR < MIN_CUDA_MINOR) )); then
    fatal "driver reports CUDA capability $CUDA_CAPABILITY; CUDA >= ${MIN_CUDA_MAJOR}.${MIN_CUDA_MINOR} is required"
  fi
fi
printf 'GPU preflight OK: driver=%s; %s\n' "$DRIVER_VERSION" "$(printf '%s' "$GPU_TABLE" | head -n1)"

if [[ "$DRY_RUN" == "1" ]]; then
  echo "DRY RUN: Ubuntu 24.04/GPU/CUDA preflight passed; no apt, venv, pip, model, or service changes performed."
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends python3 python3-venv python3-dev build-essential libsndfile1 libportaudio2 portaudio19-dev alsa-utils socat ca-certificates curl

if [[ ! -x "$VENV/bin/python" ]]; then
  "$PYTHON_BIN" -m venv --system-site-packages "$VENV"
fi
"$VENV/bin/python" - <<'PY'
import sys
if sys.version_info[:2] != (3, 12):
    raise SystemExit(f"Python 3.12 is required, got {sys.version.split()[0]}")
PY

if "$VENV/bin/python" -c 'import torch; raise SystemExit(0 if torch.__version__.split("+",1)[0] == "2.8.0" and torch.version.cuda == "12.8" else 1)' >/dev/null 2>&1; then
  echo "Torch base contract already present; skip Torch installation"
elif "$VENV/bin/python" -c 'import torch' >/dev/null 2>&1; then
  fatal "an incompatible Torch is already installed in $VENV; refusing to overwrite it"
else
  echo "Torch is absent; installing the pinned clean-Ubuntu wheel after GPU preflight"
  "$VENV/bin/python" -m pip install --no-deps "$TORCH_SPEC" --index-url "$TORCH_INDEX"
fi
"$VENV/bin/python" - <<'PY'
import torch
if torch.__version__.split("+", 1)[0] != "2.8.0" or torch.version.cuda != "12.8":
    raise SystemExit(f"Torch contract mismatch: version={torch.__version__}, cuda={torch.version.cuda}")
if not torch.cuda.is_available():
    raise SystemExit("Torch cannot see a CUDA GPU after driver preflight")
print("Torch/CUDA OK", torch.__version__, torch.version.cuda, torch.cuda.get_device_name(0))
PY

"$VENV/bin/python" -m pip install --no-deps -r "$LOCK_FILE"
if [[ "$INSTALL_TEST_DEPS" == "1" ]]; then
  "$VENV/bin/python" -m pip install --no-deps -r "$TEST_LOCK_FILE"
else
  echo "Regression overlay not installed; set MISHKA_INSTALL_TEST_DEPS=1 for test mode"
fi

mkdir -p "$CACHE_ROOT/whisper" "$CACHE_ROOT/torch-cache" "$OLLAMA_MODELS"
export TORCH_HOME="$CACHE_ROOT/torch-cache"
"$VENV/bin/python" - <<PY
from faster_whisper import WhisperModel
from silero_vad import load_silero_vad
WhisperModel('$WHISPER_MODEL', device='cuda', compute_type='float16', download_root='$CACHE_ROOT/whisper')
load_silero_vad()
PY

"$VENV/bin/python" - <<'PY'
import importlib
for name in ('fastapi','faster_whisper','httpx','langgraph','numpy','onnxruntime','omegaconf','num2words','pydantic','silero_vad','sounddevice','soundfile','uvicorn','websockets'):
    importlib.import_module(name)
print('production import smoke: PASS')
PY
"$VENV/bin/python" -m pip check

command -v ollama >/dev/null 2>&1 || fatal "Ollama is required; install it separately before clean setup"
if ! curl --fail --silent http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  nohup ollama serve >"$ROOT/ollama.log" 2>&1 &
  for _ in $(seq 1 60); do curl --fail --silent http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break; sleep 1; done
fi
curl --fail --silent http://127.0.0.1:11434/api/tags >/dev/null
"$ROOT/scripts/generate_bootstrap_contract.sh" "$ROOT"
echo "Clean bootstrap completed; start: PYTHONPATH=. $VENV/bin/python scripts/run_stand.py"
