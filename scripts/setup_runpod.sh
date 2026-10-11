#!/usr/bin/env bash
set -euo pipefail
ROOT="${1:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"; ROOT="$(cd "$ROOT" && pwd)"; VENV="$ROOT/.venv"
BASE_IMAGE="runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"
LOCK_FILE="$ROOT/requirements-production.lock"
PYTHON_BIN="${PYTHON_BIN:-python3}"; CACHE_ROOT="${MISHKA_CACHE_ROOT:-/workspace/.cache/mishka}"; OLLAMA_MODELS="${OLLAMA_MODELS:-$CACHE_ROOT/ollama}"; WHISPER_MODEL="${WHISPER_MODEL:-large-v3-turbo}"; LLM_MODEL="${LLM_MODEL:-qwen3:14b}"
[[ -d "$ROOT" ]] || { echo "ERROR: missing $ROOT" >&2; exit 1; }; cd "$ROOT"; [[ -s "$LOCK_FILE" ]] || { echo "ERROR: missing dependency lock $LOCK_FILE" >&2; exit 1; }; export DEBIAN_FRONTEND=noninteractive
apt-get update; apt-get install -y --no-install-recommends python3-venv python3-dev build-essential libsndfile1 libportaudio2 portaudio19-dev alsa-utils socat ca-certificates curl
command -v nvidia-smi >/dev/null || { echo "ERROR: nvidia-smi unavailable" >&2; exit 1; }; nvidia-smi >/dev/null
$PYTHON_BIN -c 'import torch; assert torch.__version__.split("+",1)[0]=="2.8.0" and torch.version.cuda=="12.8" and torch.cuda.is_available(), (torch.__version__,torch.version.cuda); print("GPU contract OK",torch.__version__,torch.version.cuda,torch.cuda.get_device_name(0))'
if [[ -x "$VENV/bin/python" ]] && ! "$VENV/bin/python" -c 'import torch; assert torch.__version__.split("+",1)[0]=="2.8.0" and torch.version.cuda=="12.8"' >/dev/null 2>&1; then STALE="$ROOT/.venv.stale.$(date +%Y%m%d%H%M%S)"; mv "$VENV" "$STALE"; echo "Moved incompatible venv to $STALE"; fi
if [[ ! -x "$VENV/bin/python" ]]; then $PYTHON_BIN -m venv --system-site-packages "$VENV"; fi
"$VENV/bin/python" -c 'import torch; assert torch.__version__.split("+",1)[0]=="2.8.0" and torch.version.cuda=="12.8", (torch.__version__,torch.version.cuda)'
"$VENV/bin/python" -m pip install --no-deps -r "$LOCK_FILE"
mkdir -p "$CACHE_ROOT/whisper" "$CACHE_ROOT/torch-cache" "$CACHE_ROOT/silero" "$OLLAMA_MODELS" "$ROOT/logs"; export TORCH_HOME="$CACHE_ROOT/torch-cache"
"$VENV/bin/python" -c "from faster_whisper import WhisperModel; from silero_vad import load_silero_vad; WhisperModel('$WHISPER_MODEL',device='cuda',compute_type='float16',download_root='$ROOT/models/whisper'); load_silero_vad()"
command -v ollama >/dev/null || { echo "ERROR: ollama is required" >&2; exit 1; }; if ! curl --fail --silent http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then nohup ollama serve >"$ROOT/logs/ollama.log" 2>&1 & for _ in $(seq 1 60); do curl --fail --silent http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break; sleep 1; done; fi; curl --fail --silent http://127.0.0.1:11434/api/tags >/dev/null; OLLAMA_MODELS="$OLLAMA_MODELS" ollama pull "$LLM_MODEL"
SILERO_CACHE="${SILERO_CACHE_PATH:-$CACHE_ROOT/silero/v5_5_ru.pt}"; [[ -s "$SILERO_CACHE" ]] || { echo "ERROR: Silero cache missing: $SILERO_CACHE" >&2; exit 1; }
printf 'IMAGE=runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404\nPYTHON=%s\nTORCH=%s\nCUDA=%s\nWHISPER_MODEL=%s\nLLM_MODEL=%s\nSILERO_MODEL=v5_5_ru\nSILERO_SPEAKER=aidar\nSILERO_CACHE_PATH=%s\nRVC_F0_UP_KEY=0\nOLLAMA_MODELS=%s\n' "$BASE_IMAGE" "$($VENV/bin/python -c 'import sys; print(sys.version.split()[0])')" "$($VENV/bin/python -c 'import torch; print(torch.__version__)')" "$($VENV/bin/python -c 'import torch; print(torch.version.cuda)')" "$WHISPER_MODEL" "$LLM_MODEL" "$SILERO_CACHE" "$OLLAMA_MODELS" > "$ROOT/.bootstrap-contract"
"$ROOT/scripts/generate_bootstrap_contract.sh" "$ROOT"
"$VENV/bin/python" -m pip check; "$VENV/bin/python" -c "import importlib; [importlib.import_module(n) for n in ('fastapi','faster_whisper','httpx','langgraph','numpy','onnxruntime','omegaconf','num2words','pydantic','silero_vad','sounddevice','soundfile','uvicorn','websockets')]"
echo "RunPod bootstrap completed; start: PYTHONPATH=. $VENV/bin/python scripts/run_stand.py"
