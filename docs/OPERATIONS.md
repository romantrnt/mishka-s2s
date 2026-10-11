# Operations

## Canonical RunPod runtime

The supported RunPod contract has one setup path and one launch path. Create the Pod from the pinned base image:

```text
runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404
```

Then, from the project checkout at `/workspace/mishka`, run the idempotent setup and launch the single canonical API runtime:

```bash
cd /workspace/mishka
./scripts/setup_runpod.sh /workspace/mishka
./scripts/runpod_start.sh
```

`setup_runpod.sh` validates the Torch 2.8/CUDA 12.8 supplied by the base image and installs only project/system dependencies and model assets. It must not install or replace Torch/CUDA. Re-running it is supported; existing compatible venv/model assets are reused.

The canonical production dependency set is `requirements-production.lock`; the optional regression overlay is `requirements-test.lock` and is not installed by production bootstrap. The legacy `requirements.txt` and `requirements-mock.txt` manifests are deprecated.

`runpod_start.sh` is the only canonical launcher. It runs one `scripts/run_stand.py` process on `0.0.0.0:8000`. The public `/health` and `/ready` endpoints must be queried on port `8000`; they describe that same process and in-memory runtime state. The canonical launcher refuses a non-8000 `MISHKA_PORT`.

The foundation production app does not expose `/mock/*`, `/session/start`, or `/session/reset`. Those control routes are registered only when the Python factory is called with the explicit `include_test_routes=True` test surface. A production profile without `MISHKA_PROFILE_DB` fails during construction; it never creates a fixture profile or false session.

The runtime starts one internal stale-resource watchdog after lifecycle startup. It uses monotonic ages and the current generation to inspect audio/LLM task ownership, playback, and queue generations. Configuration is explicit and bounded:

MISHKA_WATCHDOG_INTERVAL_SECONDS=5
MISHKA_WATCHDOG_STALE_AFTER_SECONDS=30
MISHKA_WATCHDOG_COOLDOWN_SECONDS=30

The watchdog is not a public endpoint. Its structured events are exposed through the existing /health observability snapshot. Confirmed session-local stale ownership is cleaned through the existing cancellation path; cleanup failure degrades lifecycle. Startup is idempotent and shutdown awaits the worker task.

`scripts/runpod_bootstrap.sh` is deprecated and is not part of setup or startup. It remains only as a guard against accidentally using the historical Torch 2.11 bootstrap. `scripts/run_mock.py` is a deprecated local-only mock launcher and is not a production or RunPod entrypoint.
`docker/entrypoint.sh` is only a compatibility wrapper to `scripts/runpod_start.sh`; it does not select `app.main:app`, `run_mock.py`, or an alternate port.

## Temporary dev-only query-token compatibility

The canonical authentication path is header or cookie based. Query-token compatibility is disabled by default and is not present in the canonical .env.runpod manifest. It may be enabled only in an untracked development environment for the console compatibility path; it must never be enabled for production_candidate or any production profile. Settings rejects such production configuration during startup, before the app can become ready.

Tracked configuration uses only the disabled example: MISHKA_ALLOW_QUERY_TOKEN=0.

The dev exception is temporary: do not place query links in shared channels, tickets, screenshots, browser-shared sessions, or external systems. Disable the flag and rotate the token before office or production acceptance. Application and Uvicorn logs redact query values, but that does not protect browser history, proxy logs, referrer data, or external logging systems.

## P1 profile, dependency and observability contract

Profile readiness is reported as a structured provider contract. SQLiteProfileProvider distinguishes provider_unavailable, schema_invalid, operational_empty, and operational_with_profiles; an empty valid database is operational but cannot authorize an unknown RFID. CachedProfileProvider reports not_found only for a confirmed backend miss and preserves timeout/network/malformed-response failures as structured provider errors. Production never converts those failures into fixture or mock profiles.

/health is liveness plus factual dependency identity. /ready is production readiness only after the resource gate and real E2E warmup. A dependency that has not been initialized is reported as null/unavailable with a reason, not as an external or mock success. The current runtime labels are emitted by the actual adapters: SQLite profile provider, Ollama LLM, faster-whisper STT, Silero TTS v5.5, and the PTY simulator (physical hardware acceptance is separate).

Memory metrics distinguish rss_current_bytes from rss_peak_bytes; ru_maxrss is used only for the peak field. GPU, cgroup and Ollama values remain null with a diagnostic reason when unavailable. Metrics, stage samples and watchdog events use explicit bounded deques and expose limit/current/evicted counters in JSON snapshots. No cache flush or model unload is performed in a hot generation path.

## Clean Ubuntu 24.04 setup

## Versioned deployment policy

The canonical RunPod profile is `mishka-deployment/v1` with
`MISHKA_PROFILE=production_candidate`. Its resource reserve is
`MIN_AVAILABLE_RAM_GB=12` and `MIN_AVAILABLE_VRAM_GB=8`, matching the
RunPod environment and the preserved 8 GB acceptance evidence. These are
availability reserves, not a benchmark claim: `benchmark_status` remains
`benchmark_pending` until a target deployment benchmark is recorded.

`app/foundation/config.py::Settings` is the single validated boundary for
production environment values. `.env.example` and `.env.runpod` carry the
same policy version and canonical thresholds. Mock/local profiles may use
explicit overrides for disposable tests; those overrides are never the
canonical production policy. `/health` and `/ready` expose the effective
profile, policy version, bounded non-secret settings and resource provenance,
but never tokens, API keys, database paths or full DSNs.

Malformed, missing, negative, non-finite or out-of-range typed values fail
closed with a field-scoped `SettingsError`. Direct environment access in the
active foundation production path is limited to `config.py`; the remaining
legacy demo weather adapter and child-process environment copy are explicitly
non-canonical compatibility boundaries.

`scripts/setup_clean.sh` is the separate clean-host path. It requires Ubuntu 24.04/Noble, root access, an NVIDIA driver with `nvidia-smi`, driver major >= 570, and CUDA capability >= 12.8. It fails closed with an actionable error before apt/pip/model changes when those prerequisites are absent.

The script installs the pinned Torch 2.8 CUDA 12.8 wheel only when Torch is absent after successful preflight. If an incompatible Torch is already present, it stops and never overwrites it. Production dependencies come from `requirements-production.lock`; the optional regression overlay comes from `requirements-test.lock` when `MISHKA_INSTALL_TEST_DEPS=1`.

Safe preflight-only validation: `MISHKA_DRY_RUN=1 ./scripts/setup_clean.sh /workspace/mishka`. This validates the current host only; it is not clean-Ubuntu acceptance. Real acceptance requires a disposable Ubuntu 24.04 host and a complete install/idempotency/runtime report.

## Migration artifact separation

Migration evidence is split into four independent artifact classes. The recorder is read-only with respect to source/model/cache content: it records metadata and never copies or freezes a running service.

```text
/workspace/mishka/
  backups/
    source/                         # source archives only
    artifacts/
      environment/*.json            # host/base image/runtime/lock/env-name manifest
      model_inventory/*.json        # model paths, providers, sizes, file digests
      ollama_cache/*.json           # Ollama path, model tags and cache metadata
/workspace/.cache/mishka/ollama/            # Ollama cache itself; never part of source backup
```

The manifest format is `mishka-artifact/v1`. Every JSON file has `artifact_type` and `created_at`; file-bearing entries have byte counts and SHA-256 digests or a deterministic metadata digest. Environment manifests contain OS/base-image/runtime data, lock hashes and environment variable names only. They do not contain values, API keys, tokens or passwords. Model inventory reports only paths that actually exist; missing models are not invented. Ollama manifests describe the cache and model tags but do not copy blobs or temporary files.

Safe recording and validation:

```bash
cd /workspace/mishka
python3 scripts/artifact_manifest.py record \
  --root /workspace/mishka \
  --artifact-root /workspace/mishka/backups/artifacts \
  --ollama-path /workspace/.cache/mishka/ollama
python3 scripts/artifact_manifest.py validate /workspace/mishka/backups/artifacts
```

To validate an already-created source archive without mixing it into the source tree:

```bash
python3 scripts/artifact_manifest.py record \
  --root /workspace/mishka \
  --artifact-root /workspace/mishka/backups/artifacts \
  --ollama-path /workspace/.cache/mishka/ollama \
  --source-archive /workspace/mishka/backups/source/<source-archive>.tar.gz
sha256sum -c /workspace/mishka/backups/source/<source-archive>.tar.gz.sha256
tar -xzf /workspace/mishka/backups/source/<source-archive>.tar.gz -C /workspace/mishka
```

The recorder rejects source archives containing `.venv`, `models`, Ollama, cache or `.env` members, refuses to overwrite a same-timestamp manifest, and does not delete older backups. This block does not create a new source archive, migrate caches, or modify the current Pod; existing historical archives were only inventoried/read as candidates for future validation.

## Canonical source-tree archive policy

Canonical production roots are `app/` and `scripts/`. They must not contain isolated historical snapshots named like `*-final-*.py` or `*.py.*final*`; such files are not importable runtime modules and can silently confuse maintenance.

Before archiving a candidate, inspect imports/references, compare it with the active module, record its SHA-256, and move it explicitly (never with recursive or wildcard deletion). Archived snapshots are retained under `backups/archive/<date>-canonical-tree-cleanup/`. Each move has a `mishka-source-tree-archive/v1` manifest containing original path, archive path, reason, size and checksum.

Run the static guard from the project root:

```bash
python3 scripts/check_canonical_tree.py --root /workspace/mishka
```

Historical logs remain in place when they contain diagnostic evidence or are referenced by backup/acceptance records; a short log is not automatically disposable.

## Canonical regression harness

Run from the project root:

    bash scripts/run_tests_nonprod.sh full
    bash scripts/run_tests_production.sh full

The runner never installs packages and never starts, stops or restarts a runtime. It uses an isolated test Python (MISHKA_TEST_PYTHON, or the project test venv), checks exact versions from requirements-test.lock, and verifies production imports. It never mutates the production venv. If the test overlay is missing, it exits with code 2 and prints the missing test-Python path:

    /workspace/mishka/.venv/bin/python -m pip install --requirement /workspace/mishka/requirements-test.lock

Supported modes:

    bash scripts/run_tests_nonprod.sh unit
    bash scripts/run_tests_nonprod.sh integration
    bash scripts/run_tests_production.sh acceptance
    bash scripts/run_tests_production.sh smoke
    bash scripts/run_tests_production.sh targeted --target tests/test_s2s.py::test_name
    bash scripts/run_tests_nonprod.sh full

unit, integration, acceptance and smoke use pytest markers. Acceptance and smoke are live canonical HTTP/WebSocket boundary tests and must use the explicit production wrapper; without a live correctly configured runtime they fail, never become a green empty selection. targeted requires explicit test node IDs. Non-production full excludes only live acceptance tests; production full includes them. Each invocation creates acceptance/regression/<run-id>/report.json, stdout.log and stderr.log with command, exit code, summary, source/git availability, OS/Python/Torch/CUDA, base-image contract, lock hashes, selected environment mode and runtime-safety flags. Missing Git metadata is recorded, not treated as a test failure.

Marker definitions live in pytest.ini. The harness self-check works without pytest:

    python3 scripts/run_regression.py --self-check

A preflight-failure artifact proves that regression was not run and must not be reported as a green suite.

## Acceptance evidence policy

These claims are intentionally separate:

| Evidence level | Proves | Does not prove |
| --- | --- | --- |
| health | process is alive and responding | production readiness |
| ready | resource gate and full production E2E warmup passed | physical hardware acceptance |
| fixture/mock | deterministic test contract | production provider/model behavior |
| UI/state | UI signal and state serialization | physical LED/UART/RFID/speaker behavior |
| PTY/simulator | protocol/adapter behavior in simulation | physical hardware acceptance |
| physical hardware | external device path at the stated hardware level | readiness unless resource gate and E2E are also evidenced |

Acceptance reports use the policy schema mishka-acceptance/v1 with acceptance_type, environment, real_dependencies, hardware_level, test_doubles and readiness_evidence. The validator rejects health-only READY claims, fixture/mock claims presented as production, and PTY/simulator claims presented as physical acceptance.

Validate a saved report:

    python3 scripts/validate_acceptance_report.py acceptance/regression/<run-id>/report.json

Historical pre-restart acceptance artifacts include a degraded `/ready` result
with `resource_capacity_insufficient`; that is not the current runtime claim.
The latest canonical runtime probe is recorded separately with its timestamp
and effective policy values.
