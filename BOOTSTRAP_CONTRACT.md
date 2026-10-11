# Deterministic bootstrap contract

- RunPod image: `runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404`.
- Project root: `/workspace/mishka`.
- Runtime Python/Torch/CUDA are validated as Python 3.12, Torch 2.8.0,
  CUDA 12.8 on the RunPod image.
- Production dependencies come from `requirements-production.lock`;
  the optional regression overlay comes from `requirements-test.lock`.
- Voice assets: Whisper `large-v3-turbo`, Silero VAD/TTS v5.5 with speaker
  `aidar`, Ollama `qwen3:14b`, and Mishka RVC v2 e250.
- Downloaded data and secrets live outside the source tree:
  `/workspace/.cache/mishka`, `/workspace/.config/mishka`, and
  `/workspace/.data/mishka`.
- RVC model/index paths and SHA-256 values are recorded in
  `.bootstrap-contract` and `models/rvc/canonical.sha256`.
- `start.sh` and `scripts/runpod_start.sh` fail before serving on missing
  GPU, incompatible Torch/CUDA, missing assets, import errors, or dependency
  conflicts.
- `scripts/setup_clean.sh` deliberately does not guess a kernel-coupled
  NVIDIA driver package; it fails clearly if `nvidia-smi` is absent.
- Regenerate the machine-readable contract with
  `scripts/generate_bootstrap_contract.sh`; it never records secrets.
