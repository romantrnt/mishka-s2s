# RVC runtime

Mishka uses one reviewed RVC v2 release:

- `models/rvc/mishka-rvc-v2.pth`
- `models/rvc/mishka-rvc-v2.index`

The worker is persistent and isolated from the ASGI process. It uses the
project's main `.venv` and the runtime assets under `vendor/rvc/rvc/models`.
There is no nested RVC environment or training checkout in the service tree.

RVC is an optional voice-quality stage. Startup, worker failure, conversion
timeout, or an unreadable model returns the original Silero PCM and logs a
warning/event. The user-facing call remains available in passthrough mode.

Canonical configuration is in `.env.example`:

```text
RVC_PROJECT_ROOT=vendor/rvc
RVC_PYTHON=.venv/bin/python
RVC_MODEL_PATH=models/rvc/mishka-rvc-v2.pth
RVC_INDEX_PATH=models/rvc/mishka-rvc-v2.index
RVC_FALLBACK_MODE=passthrough
```

The worker is started lazily on the first audio chunk and is reused for the
life of the process. Temporary WAV files are bounded to `runtime/cache/rvc-tmp`
and are removed after every request.
