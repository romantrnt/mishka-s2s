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

- `/live` checks process liveness.
- `/health` exposes redacted component and observability state.
- `/ready` describes primary service readiness.

RVC, optional memory probes and stale-worker cleanup are non-critical. Their
failures are recorded as warnings and do not make RFID or WebSocket traffic
unavailable. RVC audio falls back to clean Silero output.

## Cleanup policy

Do not copy virtual environments, notebook checkpoints, training datasets,
model caches, generated bootstrap contracts or `.env` files into a release.
The only RVC weights retained by the service are the canonical `.pth/.index`
pair and the runtime assets required by the worker.
