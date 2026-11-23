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
# Expose the CUDA libraries bundled with the pinned PyTorch wheel to
# CTranslate2 and ONNX Runtime, regardless of Python import order.
for cuda_lib in "$ROOT"/.venv/lib/python*/site-packages/nvidia/*/lib; do
  if [[ -d "$cuda_lib" ]]; then
    export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:+$LD_LIBRARY_PATH:}$cuda_lib"
  fi
done
if [[ -d /usr/lib/wsl/lib && ":$PATH:" != *":/usr/lib/wsl/lib:"* ]]; then
  export PATH="$PATH:/usr/lib/wsl/lib"
fi
export MISHKA_ROOT="$ROOT"
export MISHKA_CACHE_ROOT="${MISHKA_CACHE_ROOT:-$ROOT/runtime/cache}"
export MISHKA_ENV_FILE="${MISHKA_ENV_FILE:-$ROOT/.env}"
export MISHKA_PORT="${MISHKA_PORT:-8000}"
# Ollama runs as a systemd service; its model store belongs to that daemon.

# WSLg does not export its PulseAudio endpoint into non-interactive SSH
# sessions. PortAudio/sounddevice otherwise starts with no host API and the
# physical headless runtime cannot open the default microphone or sink.
if [[ -z "${PULSE_SERVER:-}" && -S /mnt/wslg/PulseServer ]]; then
  export PULSE_SERVER="unix:/mnt/wslg/PulseServer"
fi

# Maximize USB mic hardware capture gain if ALSA card 0 exposes a Mic control.
"$ROOT/.venv/bin/python" - << 'PY' 2>/dev/null || true
import ctypes
asound = ctypes.CDLL("libasound.so.2")
handle = ctypes.c_void_p()
if asound.snd_mixer_open(ctypes.byref(handle), 0) == 0:
    try:
        if (asound.snd_mixer_attach(handle, b"hw:0") == 0
                and asound.snd_mixer_selem_register(handle, None, None) == 0
                and asound.snd_mixer_load(handle) == 0):
            asound.snd_mixer_elem_next.restype = ctypes.c_void_p
            asound.snd_mixer_selem_get_name.restype = ctypes.c_char_p
            elem = asound.snd_mixer_first_elem(handle)
            while elem:
                name = asound.snd_mixer_selem_get_name(elem)
                if name == b"Mic":
                    if asound.snd_mixer_selem_has_capture_switch(elem):
                        asound.snd_mixer_selem_set_capture_switch_all(elem, 1)
                    if asound.snd_mixer_selem_has_capture_volume(elem):
                        pmin, pmax = ctypes.c_long(), ctypes.c_long()
                        asound.snd_mixer_selem_get_capture_volume_range(elem, ctypes.byref(pmin), ctypes.byref(pmax))
                        asound.snd_mixer_selem_set_capture_volume_all(elem, pmax.value)
                elem = asound.snd_mixer_elem_next(elem)
    finally:
        asound.snd_mixer_close(handle)
PY

export PYTHONUNBUFFERED=1
export MISHKA_MIC_GAIN="${MISHKA_MIC_GAIN:-4.0}"
exec "$ROOT/.venv/bin/python" -u "$ROOT/scripts/run_stand.py"
