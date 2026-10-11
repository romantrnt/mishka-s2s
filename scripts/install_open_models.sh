#!/usr/bin/env bash
set -Eeuo pipefail

# Install open, reviewable assets. Mishka RVC checkpoints are never downloaded here.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${MISHKA_ROOT:-$(cd -- "$SCRIPT_DIR/.." && pwd)}"
PYTHON="${MISHKA_PYTHON:-$ROOT/.venv/bin/python}"
SILERO_CACHE="${SILERO_CACHE_PATH:-$HOME/.cache/silero_v5_5_ru.pt}"
SILERO_URL="${SILERO_URL:-https://models.silero.ai/models/tts/ru/v5_5_ru.pt}"
RVC_DIR="${RVC_DIR:-$ROOT/vendor/rvc}"
RVC_REF="${RVC_REF:-main}"
[[ -x "$PYTHON" ]] || { echo "Missing Python environment: $PYTHON" >&2; exit 2; }
mkdir -p "$(dirname "$SILERO_CACHE")" "$(dirname "$RVC_DIR")" "$ROOT/models/rvc"

echo "[1/2] Installing Silero dependencies without replacing Torch/CUDA"
"$PYTHON" -m pip install --disable-pip-version-check --no-deps --timeout 1000 --retries 10 omegaconf==2.4.0 num2words==0.5.14 soundfile==0.14.0
if [[ ! -s "$SILERO_CACHE" ]]; then
  echo "[2/2] Downloading Silero v5.5 Russian package"
  curl -fL --retry 5 --retry-all-errors --connect-timeout 20 "$SILERO_URL" -o "$SILERO_CACHE"
else
  echo "[2/2] Silero cache already present: $SILERO_CACHE"
fi
if [[ ! -d "$RVC_DIR/.git" ]]; then
  git clone --depth 1 --branch "$RVC_REF" https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI.git "$RVC_DIR"
else
  git -C "$RVC_DIR" fetch --depth 1 origin "$RVC_REF"
  git -C "$RVC_DIR" checkout --detach FETCH_HEAD
fi
cat > "$ROOT/models/rvc/README.open-models.txt" <<'EOT'
RVC runtime source: https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI
This directory intentionally contains no Mishka speaker checkpoint or index.
Supply an approved .pth/.index pair with voice-owner consent before production.
EOT
echo "Open assets installed:"
echo "  Silero model: $SILERO_CACHE"
echo "  Silero model id: v5_5_ru; speaker: aidar"
echo "  RVC source: $RVC_DIR"
echo "RVC_MODEL/RVC_INDEX remain intentionally unset until approved Mishka assets are validated."
