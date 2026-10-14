# Mishka operations

The checkout is self-contained. The only committed configuration template is
`.env.example`; production secrets are injected by the process manager or by a
local ignored `.env`.

## Bootstrap

From the repository root:

```bash
cp .env.example .env
./start.sh
```

All project paths are resolved relative to the checkout. Runtime state,
downloaded models, logs and SQLite data live under `runtime/` and are ignored
by Git.

The single dependency surface is `requirements.txt`. The host image supplies
the hardware-matched PyTorch/torchaudio pair; the same environment is used by
the persistent RVC worker.

## Health

- `/live` checks process liveness only.
- `/health` exposes bounded, redacted component and observability state; it never means production release approval.
- `/stand/health`, `/stand/diagnostics` and `/stand/trace` remain observer endpoints during degraded hardware states.
- `/ready` is the software-admission contract. `ready=true` and HTTP 200 mean the initialized software/simulator path is usable; HTTP 503 means admission is unavailable. `production_ready` is a separate strict exhibition-release gate and may remain false while `/ready` is 200 (for example in simulator mode or with unapproved content).
- MISHKA_MUTATION_AUTH_POLICY=token_required is the only deployment policy. Every state-changing HTTP route and /stand/ws requires the operator token from secure configuration/environment; firewall/VPN is not a prerequisite. A missing server token returns a generic configuration error and keeps production_ready=false; a missing/invalid presented token returns generic unauthorized. Read-only redacted observer endpoints may remain available. Query-string tokens are never accepted or emitted.

Configuration is loaded by `scripts/run_stand.py` with process environment precedence over `.env` or an explicitly declared `MISHKA_ENV_FILE`. An explicitly declared file is mandatory and malformed/unreadable files fail closed. Operational projections expose only bounded source labels and presence flags, never values or private absolute paths.

RVC, optional memory probes and stale-worker cleanup are non-critical. Their
failures are recorded as warnings and do not make RFID or WebSocket traffic
unavailable. RVC audio falls back to clean Silero output.

## Cleanup policy

Do not copy virtual environments, notebook checkpoints, training datasets,
model caches, generated bootstrap contracts or `.env` files into a release.
The only RVC weights retained by the service are the canonical `.pth/.index`
pair and the runtime assets required by the worker.
