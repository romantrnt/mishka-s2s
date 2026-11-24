# Russian pronunciation experiment

This directory is isolated from the application and its dependencies. Its corpus
contains authored public test sentences, not visitor speech or profile data.
No production TTS, voice selection, privacy setting or model manifest is changed.

## Reproduce the CPU front-end comparison

```sh
python3 -m venv /tmp/mishka-phonetics-env
/tmp/mishka-phonetics-env/bin/python -m pip install -r experiments/russian-phonetics/environment.lock
/tmp/mishka-phonetics-env/bin/python experiments/russian-phonetics/download_models.py --models /tmp/mishka-phonetics-data/ruaccent
HF_HUB_OFFLINE=1 /tmp/mishka-phonetics-env/bin/python experiments/russian-phonetics/frontend_probe.py --models /tmp/mishka-phonetics-data/ruaccent --output /tmp/mishka-phonetics-frontend.json
```

Use `--accent-model turbo3.1` on both scripts for the larger context model.
ONNX sessions use CPU with two intra-op threads; application/model environments
are untouched. Downloads use a pinned revision and no implicit Hugging Face token.

Initial tiny2.1 + dictionary result on 2026-11-24: 30 sentences, 45/50 explicitly
annotated stress targets, no lexical changes, warm p50 5.761 ms / p95 9.600 ms,
cold load 3.466 s. These are text-front-end measurements, **not TTS accuracy**.
The unaccented baseline's marker count does not measure how Vox pronounces it.
Errors include payment/crying, comparative `уже`, fabric `атлас`, and declined
names. Applying this output blindly could introduce new pronunciation errors.

The same run with turbo3.1 + dictionary gives 49/50 targets, no lexical changes,
warm p50 39.805 ms / p95 124.118 ms, cold load 5.491 s. Fabric `атлас` remains
wrong. This improves the text score, not yet the measured audio quality.

The same corpus covers greeting/demo controls, contextual homographs, names and
declensions, ё, borrowings, sibilants/clusters, games/stories, dates/numbers and
sentence boundaries. Plain/full-utterance/native-streaming audio comparison,
stress-format support, subjective voice quality and acoustic timings remain open.
Do not enable this front end in the running application from the text score.

## Actual digital audio comparison (24 November)

`prepare_audio_inputs.py` runs the application's actual `prepare_vox_text` in its
unchanged environment, then prepares plain, ё-only and Unicode acute variants.
`gpu_corpus_probe.py` uses the pinned Vox555 model, adapter and approved reference
to generate ten public phrases in four modes (40 WAVs). Run the GPU experiment
only in an explicitly coordinated window with the application stopped; do not
start a second GPU voice worker during a live demonstration.

```sh
HF_HUB_OFFLINE=1 /tmp/mishka-phonetics-env/bin/python experiments/russian-phonetics/prepare_audio_inputs.py \
  --models /tmp/mishka-phonetics-data/ruaccent \
  --normalizer-python /path/to/application/venv/bin/python --output /tmp/audio-inputs.json
# On the isolated GPU host, using the existing validated experimental-worker config:
/path/to/vox555/venv/bin/python experiments/russian-phonetics/gpu_corpus_probe.py \
  --source /path/to/source --validated-config /private/validated-config.json \
  --inputs /tmp/audio-inputs.json --output /outside/git/vox-wavs
```

All 40 generated files were copied back and SHA256 checked. The existing
whole-utterance path's median first generated PCM was 1437.686 ms; the existing
native streaming path's was 74.408 ms (ten samples each). This is an engine-mode
comparison, **not a newly implemented application speed improvement**, and not
time to audible sound. The accent front-end time is not included in those numbers.

`audio_decode_probe.py` checks each WAV hash and transcribes only this authored
corpus with the existing Whisper large-v3-turbo snapshot, explicitly CPU/int8,
two threads, no downloads, microphone or playback:

```sh
/path/to/application/venv/bin/python experiments/russian-phonetics/audio_decode_probe.py \
  --corpus /outside/git/vox-wavs --inputs /tmp/audio-inputs.json \
  --model /existing/whisper/snapshot --output /outside/git/audio-decode.json
```

Actual ten-clip results: plain full, plain streaming and ё-only streaming each
had 1 word edit in 64 reference words; acute streaming had 2/64. Acute made
`демо` recognizable instead of `дема`, but introduced `на горы` for `на горе`
and `чётко` for `щётка` in the decoder output. ASR is a diagnostic proxy, not
proof of the perceived consonant, stress or voice quality. The clips' samples
and durations change with acute marks, but no human auditory improvement has
been established. **Do not deploy the experimental accent front end.**

The corpus/readback/decode receipts and public WAVs are retained in the existing
Obsidian checkpoint `Antigravity/demo-checkpoint-20261124/`. Listening to these
clips and acoustic/real-microphone evaluation remain separate open checks.

Morphology is probed separately: `обезьяну` normalizes to `обезьяна`; `мышью` has
multiple analyses. This does not implement or validate the word game's rules.

## Primary sources and provenance

- [RUAccent source](https://github.com/Den4ikAI/ruaccent) and
  [paper](https://aclanthology.org/2025.coling-main.444/): contextual stress/ё;
  code MIT. [Public model card](https://huggingface.co/ruaccent/accentuator)
  declares MIT; pinned model revision is in `download_models.py`.
- [pymorphy3](https://github.com/no-plagiarism/pymorphy3): MIT morphology engine.
  Installed dictionary METADATA declares code MIT and data CC BY-SA 3.0;
  preserve attribution and license metadata when packaging (see
  `docs/word-game-rules.md`).
- [VoxCPM upstream](https://github.com/OpenBMB/VoxCPM) and
  [usage guide](https://voxcpm.readthedocs.io/en/latest/usage_guide.html).
  Current docs describe phoneme conventions for Chinese/English; they do not
  establish Russian phoneme/SSML support in our pinned Vox555 runtime.
  The deployed worker uses pinned `tts_model.generate*` directly with reference
  `PHRASE_014`, seed 42, cfg 2.0, ten inference steps, streaming prefix four.
  Its tokenizer has Chinese splitting and no Russian stress/SSML parser.
  Actual Russian behavior of stress marks therefore requires audio experiments.
  The application already calls `prepare_vox_text` before synthesis (number/ё
  normalization and terminal punctuation); audio comparisons must use that
  current path as the baseline, rather than comparing against unnormalized text.

Keep model binaries and WAV results outside Git. Save receipts and experiment
results in the existing Obsidian checkpoint; never mix them with child recordings.
