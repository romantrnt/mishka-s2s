"""Fail-closed inventory of production-candidate model and device prerequisites."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path


def present(value: str) -> bool:
    return bool(value) and (Path(value).exists() or shutil.which(value) is not None)


def main() -> int:
    checks = {
        "qwen": {"provider": os.getenv("LLM_PROVIDER", "ollama"), "model": os.getenv("LLM_MODEL", "qwen3:14b"), "endpoint": os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")},
        "whisper": {"model": os.getenv("WHISPER_MODEL", "large-v3-turbo"), "package": _module("faster_whisper")},
        "silero": {"provider": "silero", "model": os.getenv("SILERO_MODEL", "v5_5_ru"), "speaker": os.getenv("SILERO_SPEAKER", "aidar"), "cache_path": os.path.expanduser(os.getenv("SILERO_CACHE_PATH", "/workspace/.cache/mishka/silero/v5_5_ru.pt")), "package": _module("omegaconf"), "model_present": present(os.path.expanduser(os.getenv("SILERO_CACHE_PATH", "/workspace/.cache/mishka/silero/v5_5_ru.pt")))},
        "rvc": {"project_root": os.getenv("RVC_PROJECT_ROOT", "vendor/rvc"), "model_present": present(os.getenv("RVC_MODEL", "")), "index_present": present(os.getenv("RVC_INDEX", ""))},
        "gpu": {"nvidia_smi": bool(shutil.which("nvidia-smi")), "torch": _module("torch")},
        "playback": {"audio_output_device": os.getenv("AUDIO_OUTPUT_DEVICE", "TBD")},
    }
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    return 0


def _module(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
