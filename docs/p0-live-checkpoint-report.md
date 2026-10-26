# MISHKA live checkpoint — 2026-10-26

## Scope and rollback

The isolated source is `/home/server/mishka-fixes`, branch `fix/p0-lifecycle-20261026`, based on `d8af32c40adacabdb28e84b1cc766cc0f20f6782`. A local pre-transfer snapshot of the live checkout is `/home/server/mishka-rollback-20261026-0900` (private permissions). Its `transfer-manifest.json` records the 33 paths copied from the isolated branch to `/home/server/mishka`; `rollback-transferred.py` restores only those paths. Neither rollback nor a Git reset was run. The existing live setup files, private runtime configuration, installed environment, models, and device settings were not overwritten. No push or deploy occurred.

All 33 manifest files have identical SHA-256 digests in the isolated and live checkouts after the final transfer. The live checkout also has local setup changes outside the manifest:

- `.env.example`: the example key `MAX_TTS_FIRST_CHUNK_CHARS` has a live-specific sample value; preserve or reconcile intentionally without copying actual secrets.
- `app/foundation/tts_engine.py`: direct reviewed Silero package artifact instead of fetching unpinned `torch.hub` code.
- `requirements.txt`: pins `av==18.1.0` for the installed faster-whisper compatibility.
- `start.sh`: exposes the bundled NVIDIA library directories to CTranslate2/ONNX, leaves Ollama model ownership with the systemd service, and uses the existing WSLg PulseAudio socket when available.
- Untracked setup evidence: `docs/wsl-setup-manifest.md`, `docs/wsl-pip-freeze.txt`, and `docs/windows-microphone-consent-before.txt`. Treat these as setup records, not part of the code commit. No private configuration values are copied here.

## Verification

The pre-fix isolated/live headless suite was 343 passed, 13 failed, 3 xfailed. The 13 failures were already present in the accepted audit matrix; no baseline failure is claimed resolved. An initial interactive Session 1 launch responded on port 8000 but entered `DEGRADED` after a subsecond `silero_vad` warmup import failure. A clean retry failed at the same stage with a different incomplete PyTorch import error. An internal non-playback warmup retry also failed on partially initialized `torch`. The Silero loader and runtime warmup were AST-identical to the pre-transfer snapshot; a separate no-device loader succeeded. This pointed to lazy PyTorch import during threaded warmup.

A new red test established that the production builder returned before `torch` was fully imported. The minimal fix completes that import during production construction, before model workers run. A separate red test reproduced double LED clearing after `stop_hardware`, which raised `ConnectionError: lighting output is closed` during shutdown; `aclose` and `close` now skip that second clear after hardware has stopped. Targeted cleanup tests: 4 passed. The final isolated headless suite: **345 passed, 13 failed, 3 xfailed in 40.82s**. `git diff --check` passed in both trees. The same final code and test files were copied to live and hash checked.

The final launch used the existing active Windows Session 1 via a temporary Task Scheduler principal with `Interactive` logon and `Limited` run level, without credentials or Windows security changes. It invoked the existing `start.sh`. At the final check there was exactly one MISHKA process, PID **26312**, listening at **0.0.0.0:8000**. `/ready` returned HTTP 200, `ready=true`, lifecycle `READY`, and successful VAD, Whisper, LLM, Silero TTS, RVC fallback, playback readiness, and end-to-end warmup stages. Local `/health` returned HTTP 200: audio `available` through sounddevice; HID `active`, `physical_ready=true`, and initial levels synced; handset down. Our unauthenticated `/stand/health` request correctly returned HTTP 401; an independent authenticated read-only reviewer reported HTTP 200 with the same degraded lighting, available audio, and active HID. Ollama was already serving `qwen3:0.6b` and `qwen3:14b` on the target. No real audio or urgent event was triggered.

## Remaining acceptance

`/health` reports `degraded` because lighting is `unavailable`. The lack of startup diagnostics indicates serial lighting opened during construction; its later live `diagnostics().ready` check was false. The exact adapter `last_error` was not obtained in our unauthenticated check. Serial/HID device nodes existed and were accessible, but physical lighting must be checked on the stand. `/ready` reports `production_ready=false` with `content_not_production_ready`: 108 recorded assets are approved and catalog ready, while `physical_audio_verified=false`. This is a software-ready checkpoint, not physical or product production acceptance. Hearing the audio, LED behavior, and the remaining scenario/product gaps need separate acceptance. The 13 failing tests and three expected failures remain documented in the audit reports.

An automatic approval review rejected one diagnostic-only attempt to import `run_stand` in a test process because that would load the live `.env`. That experiment was not rerun or routed indirectly; ordinary authorized production launches continued through `start.sh`. No secret values were printed or copied into this report.

The final temporary Windows launch task remains the active owner of the running MISHKA process. The temporary WSL helper is `/tmp/mishka-interactive-checkpoint-20261026.sh`; its log is `/home/server/mishka/logs/checkpoint-interactive-20261026-retry.log`. Keep the launcher while the live process is running; do not start a second instance.
