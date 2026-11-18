#!/usr/bin/env bash
# Run only on the separately authorized GPU server. No pod/billing API calls.
set +x
set -euo pipefail
umask 077
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
WORKSPACE="${MISHKA_WORKSPACE:-/workspace}"
VENV="${MISHKA_VENV:-$WORKSPACE/mishka-env}"
PYTHON312="${MISHKA_PYTHON312:-python3.12}"
mkdir -p "$WORKSPACE/mishka-artifacts" "$WORKSPACE/cache"
"$PYTHON312" -c 'import sys; assert sys.version_info[:2] == (3,12), "Python 3.12 is required"'
if [[ ! -x "$VENV/bin/python" ]]; then
  "$PYTHON312" -m venv "$VENV"
fi
"$VENV/bin/python" -c 'import sys; assert sys.version_info[:2] == (3,12)'
export HF_HOME="$WORKSPACE/cache/huggingface"
export TORCH_HOME="$WORKSPACE/cache/torch"
export PIP_CACHE_DIR="$WORKSPACE/cache/pip"
# The host driver must support CUDA 12.8. Never inherit the image's torch 2.6.
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader > "$WORKSPACE/mishka-artifacts/gpu.txt"
"$VENV/bin/python" -m pip install --index-url https://download.pytorch.org/whl/cu128 torch==2.8.0 torchaudio==2.8.0
"$VENV/bin/python" -m pip install -c "$ROOT/deployment/remote-stand/constraints.txt" -r "$ROOT/requirements.txt"
"$VENV/bin/python" -m pip check
"$VENV/bin/python" - <<'PY' > "$WORKSPACE/mishka-artifacts/runtime.json"
import json, platform, torch, torchaudio
assert torch.__version__.split('+')[0] == '2.8.0'
assert torchaudio.__version__.split('+')[0] == '2.8.0'
assert torch.cuda.is_available(), 'CUDA is unavailable'
print(json.dumps(dict(python=platform.python_version(), torch=torch.__version__,
                     torchaudio=torchaudio.__version__, cuda=torch.version.cuda,
                     gpu=torch.cuda.get_device_name(0)), indent=2))
PY
"$VENV/bin/python" -m pip freeze > "$WORKSPACE/mishka-artifacts/pip-freeze.txt"
sha256sum "$ROOT/requirements.txt" > "$WORKSPACE/mishka-artifacts/requirements.sha256"
printf '%s\n' 'Runtime verified. Provision Ollama qwen3:14b, then run launch.sh with the exact HTTPS origin.'
