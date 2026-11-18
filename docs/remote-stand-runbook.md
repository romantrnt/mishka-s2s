# Private remote simulator

This launcher exposes the existing Stand with synthetic Max HTTPS/SQLite data,
browser audio and simulated devices. It never runs physical capture, never
forces lifecycle readiness, and always remains `production_ready=false`.
The clean baseline is faster-whisper `large-v3-turbo` (CUDA float16), Ollama
`qwen3:14b`, Silero `v5_5_ru` / `aidar` / 48000 Hz, voice conversion `off`.
Optional trained voices require separate, explicitly selected runtimes and
are unavailable in this deployment.

## Runtime and server boundary

Use only the new separately authorized Community server. Never start or change
the old training pod `1ymrrmw7a2e8hh`. The current provisioned base image is
`runpod/pytorch@sha256:5f605ecfc24bd0a7743dcc3f61a9bcf71fc2083f0fd700b6eb67278b9be0e601`
(Ubuntu 22.04 / torch 2.6 base only). Its host reports CUDA 13.0 capability.
Provision a verified Python 3.12 interpreter, then `bootstrap.sh` creates an
isolated `/workspace/mishka-env` with torch 2.8.0 + torchaudio 2.8.0 cu128 and
the unchanged application `requirements.txt`. Do not use the base image's
Python/torch environment. The alternative official image
`runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404` still requires an adequate host
driver and verification; an image label alone is not runtime evidence.

Copy this checkout and the reviewed unchanged Max source to `/workspace`.
Keep the whole `/workspace` tree on the authorized persistent volume; do not
copy environments, model caches or private runtime data into Git.

```bash
MISHKA_PYTHON312=/path/to/python3.12 bash deployment/remote-stand/bootstrap.sh
```

The container must also provide OpenSSL, CA certificates, PortAudio and
libsndfile. Bootstrap does not mutate cloud resources, install an interpreter,
or install/start Ollama. Install a reviewed pinned Ollama binary separately,
record its version/hash, and bind it only to `127.0.0.1:11434`. Set
`OLLAMA_MODELS=/workspace/models/ollama`, pull `qwen3:14b`, and record
`ollama show qwen3:14b` plus its manifest/blob digests. The first application
warmup can fetch baseline Whisper and Silero model assets to `/workspace`;
record their resolved revisions/hashes after installation. Do not download
these heavyweight assets on the local workstation.

For the current server use the already installed loopback Ollama daemon; the
launcher never starts or stops it. `launch.sh` selects the pinned local Whisper
tree `/workspace/models/faster-whisper-large-v3-turbo` (override explicitly with
`MISHKA_WHISPER_MODEL`). Direct launcher calls accept `--whisper-model` for the
same purpose and validate model/config/tokenizer presence before startup.
Copy the verified Silero asset to
`/workspace/mishka-runtime/cache/silero/v5_5_ru.pt` before launch to reuse it.
The protected launch manifest records whether Whisper uses local assets and
the selected model directory; no local model paths enter public projections.

```bash
export MISHKA_EXTERNAL_ORIGIN=https://YOUR-EXACT-8000-PROXY-HOST
export MISHKA_MAX_SOURCE=/workspace/max-client-fixture
bash deployment/remote-stand/launch.sh
```

Port 8000 is a private application behind an external HTTPS proxy. The Max TLS
server and Ollama remain loopback-only; no notebook listener is needed. The
launcher does not trust forwarded headers. Its configured HTTPS origin makes
cookies Secure even when the HTTPS proxy uses HTTP upstream. Direct browser
HTTP to a remote host is not a supported login path.

## Login and local checks

Open `/console` (or `/simulator` when installed by the UI) in a browser and use
HTTP Basic username `mishka`. The password is the generated token in
`/workspace/mishka-runtime/stand-token`, an owner-only `0600` file. Transfer the
secret privately; never put it in a URL, command-line argument, screenshot,
report or log. An existing `STAND_ACCESS_TOKEN` environment value may seed a
new token file; conflicting values on restart fail closed. A successful Basic
request issues `stand_token` with `HttpOnly; SameSite=Strict; Path=/; Secure`
on HTTPS. Browser scripts use the cookie and never need the password in JS.

All HTTP and WebSocket routes are protected, including health, static assets,
OpenAPI and unknown URLs. Query authentication is rejected. Cookie-based
mutations and WS require the exact configured Origin. Other origins are not
implicitly allowed; repeat `--external-origin` only for intentionally allowed
browser origins. Basic API clients may omit Origin, but a supplied foreign
Origin is rejected on mutations/WS. Authentication does not bypass canonical
admission; `/ready` can honestly remain 503 in mock mode.

### Current recovery instance and protected files (2026-11-18)

The active instance is `fihihgl91rnwu4`, display name `mishka` (RTX 3090, reported compute rate
`$0.22/hour`, persistent `/workspace` 80 GB). The panel is
`https://fihihgl91rnwu4-8000.proxy.runpod.net/console`. Silero and voice
conversion `off` remain the baseline. The onboarding/welcome fixes were
deployed with backups in
`/workspace/mishka-artifacts/onboarding-repeat-20261117T224411Z`.
The subsequent browser-capture cancellation, dialogue/story guidance and
input-classifier prompt fixes were deployed with backups and exact hashes in
`/workspace/mishka-artifacts/capture-ui-safety-20261117T230642Z`
(application PID 6678 after a graceful restart). Application readiness and
exact served JavaScript hashes passed; affected live acceptance is in progress.
This is not whole-product or physical-device acceptance.

On PID 6678, actual Chromium accepted a cold first game entry and returned to
root on the voice-exit text. Root button 2 had channel 13 on and `datafill=true`.
Two consecutive microphone turns completed through real STT, LLM, Silero and
Web Audio using synthetic Silero WAV input in one browser/WS session (184 PCM
frames total), with the microphone still enabled/accepting and no JS/HTTP
errors. Evidence: `runtime/local-integration-20261117/runpod-silero-microphone-acceptance.json`.
This verifies the software path; physical microphone capture and audibility
remain unverified.

The game audio failure was reproduced with the installed CUDA Silero model:
two of 154,200 samples exceeded the valid range (peak 1.0080756), causing the
entire utterance to be rejected. The engine now attenuates only finite
over-range buffers; normal waveforms and positional pre-roll remain unchanged,
and invalid/empty audio still fails validation. The same native phrase passed
after the change, alongside 24 focused tests and independent review. The exact
engine candidate was deployed to application PID 7614 with backups in
`/workspace/mishka-artifacts/silero-overload-20261117T2340Z`; readiness returned
`READY` / `warmup_complete`. Commit `2960010` and its focused regression are
pushed to `origin/codex/remote-device-simulator`; this is an intermediate fix,
not a release acceptance.

A second native failure was reproduced after 48→24 kHz resampling: one sample
of the complete game hint exceeded the range (peak 1.0010872). Conditional
attenuation of the assembled utterance now preserves all 338,820 samples and
seven cue offsets while producing valid PCM. This and the independently reviewed
public-story expiry handoff and catalogue-derived menu guidance were deployed
together in application PID 8291. Exact eight-file hashes, prior files and
deployment metadata are under
`/workspace/mishka-artifacts/public-story-ui-20261117T2347Z`. Readiness returned
`READY` / `warmup_complete`, and served console JavaScript matched the candidate.
Actual browser Max expiry FAILED: the public story stopped at identity expiry,
with `story_paused` / checkpoint pending and no browser checkpoint POST despite
a matching playback identity. The earlier playing lifecycle was rejected as
`stale_audio_lifecycle`; the exact ordering still needs investigation. Evidence:
`runtime/local-integration-20261117/runpod-public-story-expiry.json`.
Chief closed the failed test after handset-down cleanup. Work has since resumed:
an existing Sol writer is repairing the demonstrated browser stop/checkpoint and
current-lease lifecycle gaps; Chief owns independent review and coordinated live
acceptance. Coordinate before any restart or deployment. The Runpod pod and
providers remain running. Whole-product acceptance is incomplete.

The follow-up causal repair was accepted in application PID 9303. Current-lease
lifecycle no longer depends on the last effect list after a button release;
a stopped public story retains only an exact one-shot position receipt for
five seconds. The same deployment supplies contextual game guidance and a
fixed-field TTS failure event without utterance/error text. Four-file manifests,
backups and rollback metadata are in
`/workspace/mishka-artifacts/expiry-word-diagnostic-20261118`.
Readiness and exact served JavaScript passed. Actual Max expiry while playing
passed: one checkpoint transferred offset 91,221 ms, the anonymous generation
resumed at 91.221 seconds, and a late old completion was rejected. The observed
resume gap was 7.779 seconds because the public WAV was fetched/decoded again;
the public-buffer reuse fix is now deployed for acceptance. The paused idle
boundary also passed after 120.145 seconds, followed by a successful new game.
Evidence: `runtime/local-integration-20261117/runpod-expiry-repair-9303.json`.
Source checkpoint `d4bf136` is pushed to `codex/remote-device-simulator`; it
contains no raw operator recordings, weights, credentials or runtime state.

The next reviewed bundle is live in application PID 10134: a single public
story buffer reused only by a new owned positive-offset command, explicit
launcher binding of the verified story receipt, and filtering of standalone
decorative TTS chunks. A wholly nonverbal model reply still takes the honest
fallback. Two native probes after the word fix produced valid meaningful PCM
and no Silero failure. Exact three-file hashes and backups are under
`/workspace/mishka-artifacts/story-buffer-receipt-20261118`. Readiness and the
served console hash passed. Actual browser public pause/resume and Max expiry
passed with zero repeat WAV fetches and a 229 ms stop-to-start gap, against
7.779 seconds previously. The pause idle boundary passed at 120.155 seconds,
followed by a new word game; both «Кот» and «Мяч» completed actual PCM with
zero native TTS failures, and voice exit returned to the root menu. Evidence:
`runtime/local-integration-20261117/runpod-batch-10134.json`.
Personal themes became available but generation still failed after about
13 seconds without personal PCM. A separate native synthetic 3–5/dinosaurs
request reproduced a non-integer duration contract rejection before semantic
validation or TTS. Its exact other field type and the original browser age
were not retained, so that native probe does not establish an identical
browser failure cause. Source fixes `cfbca9a` (private browser PCM pause and
three reproduced races) and `0e5914b` (typed story-only JSON plus the integer
prompt) are independently reviewed and pushed. The strict semantic and actual
PCM-duration checks remain intact. The combined native after probe confirmed
the actual typed HTTP format, integer duration 80 and matching age, then failed
the next existing sentence gate: 24 words against the 3–5 limit of 18. Semantic
validation and TTS were not reached, so no personal PCM success is established.
A short-sentence correction subsequently passed the age gate and full semantic
validation. All 13 real Silero chunks were valid (peak 0.96044), but their
2,757,600 samples at 48 kHz totalled 57.45 seconds, below the unchanged 60-second
minimum. No silence, rate change or metadata override was used. The next drafting
volume guide is approximate and still requires actual duration acceptance.
The volume guide subsequently passed a complete real native request: 16 Silero
chunks, 3,134,400 samples at 48 kHz = 65.3 seconds, peak 0.99784; generation and
validation took 29.195 seconds within the 60-second deadline. This proves the
synthetic 3–5/dinosaurs service path, not physical hearing or other ages.
Evidence: operator scratch `work/personal-native-probe-20261118/after-volume-result.json`.
A separate actual-code test found browser button 7 queued behind personal
generation while the generating state was unpublished. Early state publication
and a scoped urgent cancel now preserve the queued releases and reject late
HTTP/provider work; ASGI-to-Node tests and independent review passed.
The exact five-file bundle is deployed in application PID 12428, with readiness
`READY` and served console SHA `4b3c30aeac2fdc29990b0793650a0f304a16d84e4ac2ac1f8fec687648f90e49`.
Hashes, prior files and deployment metadata are in
`/workspace/mishka-artifacts/personal-pcm-pause-20261118`. Pod/providers were not
restarted. Actual browser cancellation passed: button 7 returned to the root in
2.463 seconds through the proxy, both held keys were released, and no private
PCM remained. The next generation in the same visit failed after about 300 ms;
a clean visit also lost the generation choices. Chief reproduced a capability
deadlock: cancelling metadata refresh clears verification, while subsequent
entry requires verification before it can refresh. The live source matches
that reproduction and the receipt is unexpired; the precise live rejection
stage was not logged. Recovery after cancellation is being fixed with fresh
verification before any model request, preserving fail-closed model, receipt
and context checks. Whole personal browser playback/pause/resume remains open.
Evidence: `runtime/local-integration-20261117/runpod-personal-12428.json` and
`story-contract-cancel-deadlock-probe.json` in the same directory. Chief closed
the browser and released the live audio/GPU slot after this failed acceptance.
Recovery commit `629f255` changes only `story_context.py` in production: cancelled
verification preserves the last successful availability observation, while any
real metadata failure invalidates it and fences older successes. Each actual
model request still performs a fresh bounded verification. The 22 new recovery
regressions passed independently, and Chief/Reviewer approved the exact source.
It is deployed as PID 12927 with story-context SHA
`827a1c1fcf2500dbbb95f010f6ad0529fe33550eafbf9316e1e95e269328971a`;
readiness is `READY`, console `4b3c` is unchanged, and the pod/providers were not
restarted. Deployment evidence is in
`/workspace/mishka-artifacts/story-contract-cancel-recovery-20261118`.
The subsequent PID 12927 browser run still failed: cancellation passed in
2.522 seconds, but the next generation fell back after 297 ms without private
PCM. The corrected contract bug does not establish the cause of this live
failure. No further generation fix is inferred from the isolated service probe.
PID 13655 now adds local whitelisted stage diagnostics in `personal_story.py`
only (SHA `e032b10e998d66474eeaa55ab1e6ee74c21628743179351e68ffcf397b1ddc2d`).
Ten diagnostic/privacy tests passed independently; Chief approved the exact
diff. Constant stages/reasons, booleans, counts, known age bands and bounded
error classes reach local WARNING logs; story text, PCM, identities and raw
exceptions do not. Generation conditions remain unchanged. The coordinated
restart intentionally interrupted Chief's public story to check remote recovery.
Evidence: `/workspace/mishka-artifacts/personal-story-diagnostics-20261118`.
Chief owns the sole browser/audio slot for reload/new-visit recovery and one
actual personal request to locate the rejecting stage. That result is pending.

On PID 10134, actual support and wellbeing paths passed, including local support
recording, declined assistance, and requested adult help. After an injected input
fault, reconnect alone did not clear the safety latch; a full handset placement
and subsequent lift restored a clean welcome. Use handset placement/reset for
operator recovery rather than treating transport reconnect as recovery. The UI
does not persist a distinct technical cause for that latch; do not infer it from
the last event. Authenticated Jupyter login and workspace reopening also passed
in Chief's actual browser; this does not claim personal observation of Roman's
login or physical hearing.

Pinned protocol source: [Ollama v0.40.16 API](https://github.com/ollama/ollama/blob/v0.40.16/api/types.go), [runner scheduling](https://github.com/ollama/ollama/blob/v0.40.16/server/sched.go), and [llamacpp execution](https://github.com/ollama/ollama/blob/v0.40.16/llm/llama_server.go). Native probe results are retained beside the four-file deployment evidence.

Full-story admission now uses the operator-owned receipt
`/workspace/private/story-backend-receipt.json` (0600, 30-day expiry), bound to
the observed Ollama 0.40.16 / Qwen3:14b digest and llamacpp runner. The actual
backend rejected an oversized 20,017-token prompt against context 4,096 with
HTTP 400 under `truncate=false`; `shift=false` launched without context shifting.
A 16,384-context load and clean-stop request passed (11.85 GB model VRAM).
Both generator and validator metadata contracts refreshed successfully;
all curated generation briefs fit the accepted ceiling. Full-story quality,
duration and live playback remain subject to actual acceptance. This receipt
is deployment evidence, not clinical or child-safety certification. Reuse the
existing verified receipt explicitly on a manual relaunch:

```bash
export MISHKA_STORY_BACKEND_RECEIPT_PATH=/workspace/private/story-backend-receipt.json
```

The short operator path `/workspace/mishka` points to the existing application
checkout. It is an alias, not a migration of active process paths. The pod name
change preserved its ID, running process, ports and persistent storage.
After an accidental Jupyter move, the existing Whisper path and Ollama model
links were restored without restarting providers or downloading weights.
Preserve `/workspace/mishka-runtime/models/ollama`: its original blobs remain in
use through verified hardlinks. A real classifier request passed after recovery.

Versioning starts at `0.1.0`; this is a starting checkpoint, not the mission's
completion criterion. Each recorded version must identify its exact source and
actual checks, including unresolved limits. Implementation and independent
acceptance continue until the originally requested complete, reliable product
works. Existing API schema versions are not product-release evidence.


Jupyter runs on the pod's loopback port 8888 with token authentication. The
authorized SSH tunnel exposes it only at `http://127.0.0.1:18888/lab` on Roman's
laptop. SSH host identity was checked through the Runpod management channel;
strict host-key checking stays enabled. The current Wi-Fi source address is
`192.168.1.195`; this is a connection-specific bind, not a VPN or routing change.
Authenticated contents API returned 200 twice on reopening, unauthenticated API returned 403,
and the login page opened in Codex. Browser sign-in remains a separate check.

The owner-only local token file is
`/home/khugardin/Documents/Codex/2026-11-16/project-manager/work/mishka-remote-stand/recovery-20261117/private/jupyter-token`.
Paste its value into the Jupyter login form; never put it in a URL or report.
The remote token remains `/workspace/private/jupyter-token` (mode 0600).
If the laptop connection ends, reopen the tunnel using the current approved
source address and the verified task-specific known-hosts file:

```bash
ssh -b 192.168.1.195 -o BatchMode=yes -o StrictHostKeyChecking=yes \
  -o UserKnownHostsFile=/home/khugardin/Documents/Codex/2026-11-16/project-manager/work/mishka-remote-stand/recovery-20261117/runpod-known-hosts \
  -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -N -L 127.0.0.1:18888:127.0.0.1:8888 -p 8191 root@64.119.209.250
```

No model downloads are needed for the local mock control path:

```bash
/path/to/existing/python3.12 scripts/run_remote_stand.py \
  --profile mock --host 127.0.0.1 --port 8765 \
  --external-origin http://127.0.0.1:8765 \
  --max-source /absolute/reviewed/max-client-fixture \
  --state-dir "$PWD/runtime/remote-local"
```

The state root must be owner-only and contain no symlinks. A fresh `max-demo/`
is initialized once; an existing database/TLS identity is only validated and
reused. A port collision fails instead of replacing an existing listener.
SIGTERM/interrupt closes the Stand, capture client and only this launcher's
owned Max TLS server. A restart reuses the same credentials/database; choose a
new state directory for a fresh synthetic session store.

## Evidence and limits

`launch.json` in the protected state directory records profile, lifecycle
snapshot, listener/Origin, unchanged Max source hashes and final cleanup flags.
It contains no token or patient card. `stand-token` and Max demo state remain
private. Uvicorn access logs are disabled so rejected URL credentials are not
logged. Bootstrap saves GPU/runtime versions, dependency freeze and requirements
hash in `/workspace/mishka-artifacts/`.

Test the public HTTPS path unauthenticated first (401 for HTML, state and
assets; rejected WS), then login, same-origin device controls, canonical WS,
microphone permission/capture/playback, cancellation, hangup and reconnect.
Check `/ready` separately from `production_ready`; the latter must stay false.
Mock checks prove control/auth/cleanup behavior only. Real server CUDA model
warmup and an ASR → LLM → Silero audio request, latency, cancellation and joint
VRAM use require measurements on the authorized server and browser.
