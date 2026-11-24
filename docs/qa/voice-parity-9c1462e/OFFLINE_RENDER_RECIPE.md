# Deferred offline same-text render command — NOT RUN

This is a concrete batch recipe, not evidence of generated audio. It needs an
operator-owned local manifest satisfying the existing `_load_route` inventory
and hashes, complete local base/source/runtime assets, resource release from
Main, and an output directory in this isolated worktree. The manifest is absent
locally; `/ABS/APPROVED_LOCAL_VOX_MANIFEST.json` below is an explicit missing
input, not a discovered file. No downloads or environment installation are part
of this recipe. Do not execute it while the resource gate remains closed.

It renders each public/adult transcript candidate twice through the actual
current provider (whole and native); no RVC, audio device, network endpoint,
fallback, reference switch or training is involved. Timing measures provider
file-render arrival only, never actual audible latency. The selected E5AF
reference/model/adapter/source hashes must all match or it stops. The story and
weather-menu draft transcripts remain unsuitable for actor parity until checked
against their originals. The synthetic temperature case tests pronunciation
only. No missing original WAV is fabricated or assembled.

```sh
env PYTHONDONTWRITEBYTECODE=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 PYTHONPATH=. \
  /home/khugardin/Documents/AG/Mishka/.venv/bin/python - \
  /ABS/APPROVED_LOCAL_VOX_MANIFEST.json \
  docs/qa/voice-parity-9c1462e/OFFLINE_RENDER_CASES.json \
  runtime/voice-parity/new-offline-render <<'PY'
import asyncio, hashlib, json, sys, time
from pathlib import Path
import numpy as np
from scipy.io import wavfile
from app.foundation.experimental_voice import create_experimental_tts_provider
from app.foundation.voice_pipeline import prepare_vox_text

async def main():
    manifest, cases_path, output = map(Path, sys.argv[1:])
    if not manifest.is_file():
        raise SystemExit('Required approved local manifest is absent; no render started')
    plan = json.loads(cases_path.read_text())
    provider = create_experimental_tts_provider('vox555', manifest)
    identity = provider.configuration_readiness()
    for key, expected in plan['expected_identity'].items():
        if identity.get(key) != expected:
            raise SystemExit('Pinned current-contract identity mismatch')
    output.mkdir(parents=True, exist_ok=False)
    records = []
    try:
        await provider.start()
        for index, case in enumerate(plan['cases'], 1):
            if case['speaker_scope'] not in {
                'approved_adult_original', 'approved_adult_derived',
                'public_synthetic_vox555',
            }:
                raise ValueError('Unapproved source scope')
            prepared = prepare_vox_text(case['text'])
            # Record the provider's exact post-normalization worker input too.
            spoken = provider._spoken_request_text(prepared)
            for mode in ('full', 'native'):
                started = time.perf_counter()
                chunks, arrival_ms = [], []
                if mode == 'full':
                    chunks = [await provider.synthesize(prepared, index, lambda: False)]
                    arrival_ms.append(1000 * (time.perf_counter() - started))
                else:
                    async for chunk in provider.synthesize_audio_stream(
                        prepared, index, lambda: False, first_chunk=True,
                    ):
                        chunks.append(chunk)
                        arrival_ms.append(1000 * (time.perf_counter() - started))
                rate = chunks[0].sample_rate
                if any(c.sample_rate != rate for c in chunks):
                    raise ValueError('Changing sample rate')
                pcm = np.concatenate([np.asarray(c.samples, dtype='<f4') for c in chunks])
                path = output / f'{case["id"]}-{mode}.wav'
                wavfile.write(path, rate, pcm)
                records.append({
                    'case': case, 'mode': mode, 'prepared_text': prepared,
                    'worker_text': spoken, 'frames': len(pcm), 'rate': rate,
                    'arrival_ms': arrival_ms, 'physical_latency_measured': False,
                    'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                    'provider': provider.request_metadata(),
                })
                (output / 'RENDERS.json').write_text(
                    json.dumps(records, ensure_ascii=False, indent=2) + '\n')
    finally:
        await provider.close()

asyncio.run(main())
PY
```

The import/model-loading path itself is intentionally not tested in this audit.
It must first be checked against the final integrated revision (including Main's
later date fix), then run serially under the project's QA resource lock when
authorized. Successful rendering still does not complete adult same-text
listening or physical-output acceptance.
