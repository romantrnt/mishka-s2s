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

Morphology is probed separately: `обезьяну` normalizes to `обезьяна`; `мышью` has
multiple analyses. This does not implement or validate the word game's rules.

## Primary sources and provenance

- [RUAccent source](https://github.com/Den4ikAI/ruaccent) and
  [paper](https://aclanthology.org/2025.coling-main.444/): contextual stress/ё;
  code MIT. [Public model card](https://huggingface.co/ruaccent/accentuator)
  declares MIT; pinned model revision is in `download_models.py`.
- [pymorphy3](https://github.com/no-plagiarism/pymorphy3): MIT morphology engine.
  Dictionary provenance/licensing must also be checked before production packaging.
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
