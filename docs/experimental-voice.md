# Experimental trained voice adapters

These adapters are optional research routes. The application baseline remains
outside this module; select it by default. This module does not edit baseline
configuration or install/download models. A successful CPU protocol test is not
proof of trained-model inference, voice quality, GPU fit, browser playback or
end-to-end latency.

## Integration API

```python
import asyncio
from app.foundation.experimental_voice import (
    ExperimentalVoiceUnavailable, create_experimental_tts_provider,
)

# Values come from private operator configuration, never request-provided paths.
# Factory hashes model files; keep that blocking disk work off the event loop.
tts = await asyncio.to_thread(
    create_experimental_tts_provider, "vox555", "/workspace/private/voices.json"
)
await tts.start()  # Loads actual model; errors fail this selected route closed.
try:
    audio = await tts.synthesize_stream_chunk(
        "Привет! Давай поговорим.", token=7, cancelled=lambda: False,
        first_chunk=True,
    )
    status = tts.readiness()
    block_metadata = tts.request_metadata()
finally:
    await tts.close()
```

The stable interface is:

- `create_experimental_tts_provider(route, manifest_path)` validates the explicit
  route and private files; it starts no subprocess or GPU operation.
- `await start()` loads one resident worker/model. `configuration_readiness()`
  describes the factory-validated configuration. `readiness()["ready"]` requires
  a live worker with a matching loaded-model handshake; it is false before load
  and after failure, cancellation, process exit or close.
- `await synthesize(text, token, cancelled)` and
  `await synthesize_stream_chunk(text, token, cancelled, *, first_chunk)` return
  the current `s2s.AudioChunk` contract. Each call returns one complete block.
- `synthesize_audio_stream(text, token, cancelled, *, first_chunk)` is an async
  iterator over native Vox audio. The first block contains 0.8 seconds, later
  blocks contain 2 seconds, and the final remainder is preserved. The queue
  converts every block through RVC before playback and bounds pending PCM.
- `request_metadata()` returns bounded metadata for the last admitted block;
  `native_worker_snapshot()` returns counters. Neither includes text, private
  paths, audio, exception details or worker stderr.
- `await close()` cancels active work, kills and drains/reaps the worker and
  rejects future calls. It is safe to call twice. Startup, requests and close
  must run on the same application event loop.

The provider owns one active command and no waiting queue. An overlapping call
fails with `experimental_voice_busy`. Keep semantic-block buffering in the
existing bounded LLM/TTS/playback pipeline. Workers never change adapters; use
separate provider instances for distinct voices. Admit only the models that fit
measured target VRAM. Keeping all candidate instances resident is not required
and has not been shown to fit alongside ASR/LLM.

Lifecycle integration must close every selected provider during application
shutdown. Do not hide this provider behind a cache that bypasses cancellation,
identity metadata, shutdown or readiness. Fail an unavailable selected research
route visibly. Any independently implemented baseline fallback must report the
actual baseline route and failure reason; it cannot count as candidate success.

## Private deployment manifest

The manifest is local operator configuration, not an HTTP payload. A public
selector may contain only an allowlisted route ID. It must never contain the
manifest path, interpreter path, worker path, source directory, shell command or
an executable argument. The worker is always this checkout's
`scripts/experimental_tts_worker.py`; no override exists in the manifest.

Schema version 1:

```json
{
  "schema_version": 1,
  "routes": {
    "vox555": {
      "python": "/workspace/envs/voxcpm/bin/python",
      "model_root": "/workspace/models/voxcpm2",
      "adapter_root": "/workspace/models/narrator/vox555",
      "reference_audio": "/workspace/models/narrator/reference.wav",
      "source_root": "/workspace/src/VoxCPM/src",
      "source_revision": "300e4260b509066151d623dc9c0aa922cf82232b",
      "files": [],
      "limits": {
        "max_text_chars": 600,
        "max_audio_seconds": 30,
        "startup_timeout_s": 180,
        "inference_timeout_s": 60
      }
    }
  }
}
```

This skeleton is intentionally unavailable until `files` contains the complete
verified inventory. Each entry has exactly:

```json
{"root":"adapter","path":"lora_weights.safetensors","bytes":72397184,"sha256":"1142e35de2eb2bcad19bae70a35a5360b90a2909834c83c7a7ffb41e226c5270"}
```

`root` is `model`, `source`, `adapter` or `reference`. Paths are relative to the
corresponding configured root; the reference root is the WAV's parent directory.
All private configured paths are absolute. Files escaping a root, missing files,
size/hash mismatches, incomplete inventories, unknown routes and unknown route
settings fail closed. Use materialized model snapshots with assets inside their
root, rather than symlinks into an unlisted external cache.

Inventory every file in the model and adapter directories, including preserved
config/tokenizer/license metadata; alternate unlisted weights are rejected.
Include every Python source file under `source_root/qwen_tts` or
`source_root/voxcpm`, plus preserved source-license/provenance files where
available inside the source root. The worker verifies the imported package is
inside that source root and rehashes inventoried files on every restart.
`source_revision` must match the recorded revision below; operators must restore
that exact source and derive expected file digests from its trusted snapshot.
The revision label plus source-inventory hash records that provenance; it is not
an independent remote Git attestation or a dependency lock.

For Qwen, omit `adapter_root` and `reference_audio`; its full inference export
already contains the speaker. Required model assets are `config.json`,
`generation_config.json`, `model.safetensors`, `merges.txt`, `vocab.json`,
`preprocessor_config.json`, `tokenizer_config.json`, and
`speech_tokenizer/{config.json,configuration.json,model.safetensors}`. A separate
reference or the resumable training checkpoint is not a substitute.

For Vox, required base assets are `config.json`, `model.safetensors`,
`audiovae.pth`, `tokenizer.json`, `tokenizer_config.json`, `special_tokens_map.json`
and `tokenization_voxcpm2.py`. The adapter directory must contain exactly
`lora_config.json` and `lora_weights.safetensors`, with a separate fixed reference
WAV. Preserve the original archive/checkpoints outside this serving directory.
The adapter and reference hashes, and each route's main model hash, are pinned
in code to the inventory identities; changing a private manifest cannot relabel
Vox765 as Vox555.

Use dedicated interpreters; the venv executable path is preserved even when it
is a symlink. Qwen requires `transformers==4.57.3`; Vox requires
`transformers==5.3.0`. The worker checks the effective transformers version,
requires CUDA and reports the actual torch version. The historical torch/CUDA
and other packages remain recorded in `DEPENDENCY_PROFILES.json`; this adapter
neither installs that profile nor claims exact environment reproduction merely
from its profile label. The baseline application environment is separate.

Default limits are shown above. Accepted ranges: text 1–1000 characters and at
most 8192 UTF-8 bytes; output 0.01–60 seconds; startup timeout 0.05–600 seconds;
inference timeout 0.05–180 seconds. Blank input, all-zero/non-finite PCM,
incorrect sample rate, oversized/truncated frames, wrong route/request IDs and
malformed headers fail closed. Coordinate the outer pipeline TTS timeout with
the chosen worker timeout; its cancellation also kills the worker.

## Exact inference contracts

| Route | Artifacts/source | API and output |
| --- | --- | --- |
| `qwen765` | Full `exports/step-0000765/`; Qwen source `022e286b98fbec7e1e916cb940cdf532cd9f488e` | `Qwen3TTSModel.from_pretrained(..., device_map="cuda:0", dtype=torch.bfloat16, attn_implementation="sdpa", local_files_only=True)`; `generate_custom_voice(text=..., language="Russian", speaker="mishka", max_new_tokens=300, do_sample=False)`; 24 kHz |
| `voxcpm765` | VoxCPM2 base revision `7936080b41163874f38610f46f438e2c0757f9fd`, adapter `step_0000765`; source `300e4260b509066151d623dc9c0aa922cf82232b` | `VoxCPM.from_pretrained(..., load_denoiser=False, local_files_only=True, optimize=False, device="cuda:0", lora_weights_path=...)`; complete LoRA load required; 48 kHz |
| `vox555` | Same shared base/source, independently preserved `step_0000555` adapter | Same API, fixed train reference and generation settings; 48 kHz |

Both Vox routes call the exact saved comparison's
`pipeline.tts_model.generate(target_text=..., reference_wav_path=..., max_len=300,
inference_timesteps=10, cfg_value=2.0, retry_badcase=False, seed=42)` under CUDA
bfloat16 autocast and inference mode. The pipeline adapter is loaded and then
explicitly reloaded once so the worker can verify its actual loaded/skipped key
lists. Loaded keys must equal the safetensors inventory, with zero skipped keys.
The adapter is enabled before admission. No denoiser, normalization, continuation
prompt, post-processing or automatic bad-case retry is introduced. Seed 42 is
reset per block for both families. Returned float32 PCM is unchanged.

Evidence read for these contracts:

- Inventory and effective dependency records:
  `/home/khugardin/Documents/Codex/2026-11-16/project-manager/outputs/mishka-remote-stand/models/{MODEL_MANIFEST.json,DEPENDENCY_PROFILES.json,DEPLOYMENT.md}`.
- Qwen reproduction script:
  `/home/khugardin/Desktop/MISHKA-Voice-2026-11-16/snapshots/20261116T092012.876020Z-remote-milestone/outputs/voice-project/training/qwen-full-corpus-v2-20261116/generate_qwen_dev.py`.
- Vox comparison script:
  `/home/khugardin/Documents/Codex/2026-11-16/project-manager/outputs/voice-project/training/voxcpm2-full-20261116/reproduction/generate_comparison.py`.
- [Pinned Qwen wrapper source](https://github.com/QwenLM/Qwen3-TTS/blob/022e286b98fbec7e1e916cb940cdf532cd9f488e/qwen_tts/inference/qwen3_tts_model.py)
  and [pinned Vox model source](https://github.com/OpenBMB/VoxCPM/blob/300e4260b509066151d623dc9c0aa922cf82232b/src/voxcpm/model/voxcpm2.py).

Preserve source snapshots, exact export/config/license files, model cards and
provenance. Qwen export/source and Vox source declare Apache-2.0. The pinned Vox
base has an Apache-2.0 model-card declaration but no LICENSE file in that model
tree; preserve its exact card and source LICENSE without inventing a model
license file. Deployment/licensing conclusions remain those in the inventory.

## Cancellation, timing and optional RVC100

The worker protocol carries a generated request ID, exact route identity, loaded
model/adapter/source inventory hashes, sample rate and bounded PCM. Native Vox
uses ordered `audio_chunk` frames and a checked `done` terminal with totals.
Stdout is reserved for framed protocol output (including native fd redirection);
stderr is continuously drained without retention. No HTTP server is added.
A cancellation callback is polled at most every 25 ms while awaiting startup or
output. Cancellation or early iterator closure discards the rest of the current
reply through its sole stdout reader and recovers the same warm model. New
commands are refused while draining. Corrupt replies or an expired drain cause
one bounded worker restart with model identity revalidation; cancelled text is
never replayed. Explicit close kills and reaps the worker.

The native request reports `streaming_mode="native_audio_chunks"`,
`native_streaming=true` and `inference_mode="native_incremental"`. Full-block
requests retain their explicit full-block metadata. Native first-chunk timing
is provider output timing, before RVC and browser playback; `audible_ttfa_ms`
remains unmeasured. Measure LLM validation, first converted PCM, transport and
actual browser playback separately. The queue binds current session and trace
generation after conversion so the browser can accept the owned PCM.

The Vox loader uses upstream `optimize=True` with `device="cuda"`; this pinned
upstream revision skips optimization for the literal string `cuda:0`. Loaded
LoRA tensors are verified and the compiled components are checked before an
exact native/reference warmup. Compilation and warmup finish before `ready`
and use an explicit startup budget of 600 seconds. Serving keeps ten inference
steps and the same CFG/seed; a compile flag alone is not latency evidence.

Native dialogue splits accepted speech into individual sentences. Tagged output
still waits for `</say>`; the existing plain-text fallback waits for model EOF.
Each queued sentence receives cumulative semantic validation. That validator
uses a compact JSON schema for the decision and
confidence; its policy, confidence threshold and fail-closed behavior remain.

The remote trained launcher prewarms a bounded RAM cache of validated public
menu PCM after TTS and RVC. Each hit revalidates the current menu descriptor and
returns fresh session/playback ownership. Dialogue, welcome, game prompts and
personal content are excluded. Cache failure is a performance status and does
not disable the validated live speech path.

Optional `vox555_rvc100` is composed by the application from this `vox555`
provider and the existing `s2s.RVCV2Provider.convert`. This module intentionally
has no RVC route or passthrough fallback. Composition must retain upstream Vox
identity, add RVC generator/index hashes and actual conversion evidence, and
fail candidate acceptance if conversion falls back. The saved RVC100 experiment
converted existing Vox555 audio; live LLM → Vox → RVC success and TTFA require a
new measurement. RVC100's research-only asset notice remains unresolved in the
inventory and is not changed here.

## Verification boundary

Run `python -m pytest -q tests/test_experimental_voice.py` for CPU protocol and
manifest checks. Tests launch a clearly synthetic stdio peer through an internal
monkeypatch; no fake model route or successful stub exists in the deployment
factory. They cover resident repeat calls, route/hash metadata, cancellation by
callback/task, PID death and restart, timeout, busy admission, close during work,
stderr backpressure, malformed/oversized/truncated/NaN/all-zero PCM and invalid
or missing private manifests. Source inventory completeness and venv symlink
preservation are also checked.

GPU loading/inference, target runtime restoration, actual tensor compatibility,
VRAM concurrency, real browser streaming/playback, end-to-end ASR/LLM integration
and voice quality must be measured on the selected research server. They are
not established by the CPU tests or a ready handshake alone.

## Latest Vox765 with the historical RVC100 runtime

The optional latest chain is `voxcpm765` → `scripts/rvc_trained_worker.py`.
The new worker preserves the old Applio `scripts/rvc_worker.py` baseline and uses
the confirmed pinned `infer.cli.create_config` / `infer.vc.modules.VC` API.
Select it through the private `RVCV2Provider.worker_script` configuration; never
accept a worker/interpreter/source path from a public request. Native conversion
must produce generator calls, retrieval queries and zero index failures per
request; `route_evidence` is returned in its compatible `converted` response.
It preloads HuBERT/RMVPE and validates exact model/index/assets/critical-source
hashes before ready. nprobe16 with recorded full-probe retry is explicit.
The existing parent provider's peak normalization remains a distinct post-process.

The concrete local transfer/bootstrap package is
`/home/khugardin/Documents/Codex/2026-11-16/project-manager/outputs/mishka-remote-stand/models/voxcpm765-rvc100-staging/`.
It contains freshly verified exact local artifact paths, pinned public download
URLs/SHA256, separate headless requirements, a private-manifest builder and
`BOOTSTRAP.md`. Its prepared commands target only the authorized new research
pod; they are not evidence that installation, model loading or service activation
has already occurred. GPU and browser acceptance remain separate.
