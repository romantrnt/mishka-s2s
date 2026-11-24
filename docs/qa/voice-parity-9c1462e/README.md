# Voice parity evidence — 2026-11-24

Primary acceptance remains **BLOCKED**, not passed: no verified same-text pair
of the approved adult narrator original and Vox555 is available in this pack.
No model was loaded, no speech was generated, and no audio was played or recorded
during this audit. Production settings and DSP are unchanged.

Source: `9c1462e2a3176525b045edc25f77fce74fcfc2e9`, isolated branch
`ag/mishka-voice-parity-20261124`. `SOURCE_HASHES.json` identifies the inspected
worker, provider, normalizer, game and playback files. The later invalid-date
fix owned by Main is outside this revision and this audit.

| Family | Adult target | Saved Vox555 material | Same-text actor parity |
| --- | --- | --- | --- |
| Greeting | original `ecc44…`, “привет, я мишка!” | “Здорово! Я Мишка. Давай играть.”, full/native | Not run: different words |
| Numbers | original `e375…`, countdown five to one | no same-text render | Blocked |
| Weather | `e41a…` menu mentioning weather; supplied text incomplete | no current-temperature original/render pair | Blocked; menu is not a forecast |
| Dialogue | `ecbd…`, mood/button question | other public dialogue wording | Not run: different words |
| Games | `005b…`, “давай поиграем” | animal/location sentence, full/native | Not run: different words |
| Story | exact contiguous original PCM slice, `29e78…` | other story sentence, full/native | Blocked; original transcript is unreviewed ASR |

The seven original/catalogue WAVs and twenty level-matched generated WAVs are
stored only in the Obsidian evidence pack, never Git. All are from the authorized
adult corpus or public synthetic fixtures; no visitor or child recordings were
used. Adult corpus style is mixed and unreviewed (`base_voice_verified=false`);
it is not a selected neutral-voice corpus.

`TRANSCRIPT_MATCH.json` compares all twelve selected generated texts (ten saved
contract full/native pairs plus two historical checkpoint continuity phrases)
against 341 provenance rows, using both `text` and supplied plain text. Unicode
normalization, lowercase, punctuation/whitespace removal, combining-accent
removal and ё/е equivalence still yield zero whole-token or contiguous-subsequence
matches. Plus annotations are not promoted to transcripts. Human-reviewed
alignment rows: zero. For example historical dev0 begins “Открой окно,
пожалуйста”, whereas greeting original `rec_ecc44ef9e2d77f4b_seg_0001` says
“привет, я мишка!”. Historical dev phrases are continuity controls only.

## Passed local file checks

Ten historical whole/native generated pairs preserve frame count and duration.
Wave correlation is 0.999949–0.999974; envelope correlation exceeds 0.999998.
Internal detected pause lengths are identical. Difference RMS is approximately
−61.29 to −58.53 dBFS. These files do not show a global pitch/rate/truncation
change introduced by this saved native transport. They do not test physical
playback, underflow, cancellation, current model output, articulation or identity.

Active-frame RMS gain matching preserves dynamics and pitch. The subsequent
sequential `ffmpeg ebur128` check of all twenty final WAVs reports maximum A/B
integrated-loudness difference 0.0 LU at its 0.1-LU reporting precision.
`LUFS.json` binds each reading to the final file hash. This is not a listening or
voice-similarity acceptance result.

The game onset probe uses SciPy resampling as a surrogate, then the existing
0.004 threshold and retained 15-ms lead. It removes 14.33–32.5 ms in these files,
with removed energy fractions below 1.1e-8. It excludes the actual application
resampler/fade and does not prove consonant loss. No trim, EQ, pitch or rate
change is justified by this evidence.

## Read-only transport map

* `scripts/experimental_tts_worker.py:174` and `:225`: whole/native share seed42,
  max_len300, ten inference steps, cfg2, no bad-case retry/denoiser/normalization;
  native adds prefix4. Both use the same prompt cache when supported.
* `app/foundation/experimental_voice.py:509`: native Vox555 provider releases
  0.4-second first and 0.32-second later PCM blocks; validation/buffering do not
  pitch-shift or stretch samples. Whole synthesis is at `:705`.
* `app/foundation/s2s.py:2370`, `:2455`, `:2475`: buffered playback has 3-ms edge
  fades; stream playback has a conditional 3-ms start fade and a 3-ms tail.
  These are not a measured physical-output path in this audit.
* `app/foundation/game_presentation.py:673`, `:823`, `:830`: game conversion to
  24 kHz has its own padding policy and first-subchunk silence trim/10-ms fade.
  The code difference is confirmed; an audible defect is not established.

The adapter SHA is the current pinned `1142e35d…`; preserved contract samples use
base `f7f964cf…`, source `300e4260…`, reference `e5af4c97…` (the approved mood/
numbers original). The older checkpoint555 dev comparison uses greeting reference
`ecc44ef9…`. Different text/style/reference F0 estimates do not establish a
causal voice mismatch and were not used to tune the voice.

## Blocked and not run

`DEPENDENCIES.json` records the exact seven required Vox base files (~4.96 GB),
adapter, source and runtime contract. Bounded filename searches covered
Documents/Codex, AG, HF cache, Desktop including backup_desk, Downloads,
.local/share and /opt; no complete Vox base was found. Three large Desktop
`model.safetensors` hits are Qwen765, not Vox. The historical manifest explicitly
says the Vox base was excluded from LoRA backups and points to remote
`/workspace/models/voxcpm2`; no remote read was attempted. Some extra search roots
returned permission errors, so this is not a whole-machine absence claim.
`runtime.pth` is training RNG/resume state, not inference weights.

Model loading is also resource-gated: observed available CPU RAM was 957–1297 MiB,
no swap, and GPU free memory 4967 MiB. Historical comparison reserved about
6028 MiB on a different GPU. Exact optimized CUDA bfloat16 compatibility on the
local RTX2060 (compute7.5) was not executed. No download or installation was tried.

Human listening, blinded actor comparison, current-runtime same-text rendering,
raw-to-physical output and actual latency are **NOT RUN**. The live/device freeze
excludes the physical stages. A file timestamp or mocked timing cannot replace
them. No 1:1 identity guarantee is made.

## Reproduce the completed cheap audit

Run from the isolated worktree; the manifest paths identify existing authorized
assets and the command never contacts a model or audio device:

```sh
env PYTHONDONTWRITEBYTECODE=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
  /home/khugardin/Documents/AG/Mishka/.venv/bin/python scripts/qa_voice_parity.py \
  --manifest docs/qa/voice-parity-9c1462e/INPUT_MANIFEST.json \
  --output runtime/voice-parity/measurements
env PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
  /home/khugardin/Documents/AG/Mishka/.venv/bin/python -m pytest \
  tests/test_voice_parity_dsp.py -q --tb=short \
  -o cache_dir=runtime/voice-parity-cache --basetemp=runtime/voice-parity-test-final
```

The deferred same-text batch is in `OFFLINE_RENDER_RECIPE.md`; it uses the actual
provider and has not been executed. Its smallest prerequisites are a complete
pre-existing, hash-pinned offline Vox environment/manifest, released resources,
and verified original transcripts before any actor-parity score. No model or
deployment setting should be changed merely to make that command pass.
