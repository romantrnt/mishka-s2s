# Mishka RVC v2 runtime

The canonical production path is:

`Silero v5.5 (aidar, 48 kHz) -> Mishka RVC v2 e250 -> limiter -> playback`.

It is enabled with `MISHKA_VOICE_CONVERSION=rvc_v2`. The default policy is
fail-closed: `RVC_FALLBACK_MODE=fail_closed` never presents direct Silero
audio as successful voice conversion. The explicitly opt-in
`controlled_passthrough` mode is for diagnostics and is surfaced in runtime
metrics.

## Pinned release

- Release: `mishka_rvc_v2_e250_2250s`
- Model: `models/rvc/mishka_rvc_v2_e250_2250s.pth`
- Index: `models/rvc/mishka_rvc_v2_e250_2250s.index`
- Model SHA-256: `160daf2120b461b257b76e4fb9b94b3b8f586b81611a2337533a8d4e213fa1d2`
- Index SHA-256: `6a6dec7908090fbf8c330167379a624d0b008d28cea054734e6d5ba5e80e7cbe`
- F0 method: RMVPE; embedder: ContentVec; vocoder: HiFi-GAN
- Inference settings: `f0_up_key=0`, `index_rate=0.0`, `protect=0.33`
- Canonical manifest: `models/rvc/canonical.sha256`

The checkpoint and FAISS index are deployment artifacts, not source-control
content. They must be mounted or restored at the paths above and are verified
by `scripts/runpod_start.sh` before startup.

## Runtime isolation

The resident worker runs from `vendor/rvc/.venv`, which is isolated from the
application `.venv`. Its verified inference lock is
`vendor/rvc/requirements-mishka-inference.lock`; it uses the image-provided
Torch/CUDA stack and does not install a second Torch distribution.

The worker is started through the application adapter and communicates over a
bounded JSONL protocol. It owns one CUDA context, accepts one active
conversion,
and reloads after timeout, cancellation, malformed output, or process death.
The parent adapter uses `RVC_TMP_DIR` for per-job WAVs.

## Timeouts and readiness

- `RVC_STARTUP_TIMEOUT_SECONDS` bounds worker initialization (default 60).
- `RVC_TIMEOUT_SECONDS` bounds each conversion (default 45).
- `E2E_STAGE_TIMEOUT_SECONDS` bounds each warmup stage.
- Startup preflight verifies Python imports, Torch/CUDA, canonical model hashes,
  Silero cache, and dependency consistency before admitting traffic.

Use:

```bash
cd /workspace/mishka
MISHKA_ENV_FILE=/workspace/.config/mishka/.env.runpod ./start.sh
```

For a deterministic contract snapshot, run
`scripts/generate_bootstrap_contract.sh`. It writes only versions, paths,
hashes, and non-secret settings to `.bootstrap-contract`.
