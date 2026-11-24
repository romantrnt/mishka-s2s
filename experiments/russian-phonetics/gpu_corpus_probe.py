"""Offline Vox555 WAV comparison. Run only after the stand's GPU owner stops."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import wave


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--validated-config', type=Path, required=True)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--ids', default='greeting,demo,lock,castle,names,declension,sibilants,game,story,new_phrase')
    args = parser.parse_args()
    config = json.loads(args.validated_config.read_text())
    assert config['route'] == 'vox555'
    inputs = json.loads(args.inputs.read_text())
    assert inputs['synthetic_public_text_only'] is True
    sys.path.insert(0, str(args.source.resolve()))
    from scripts.experimental_tts_worker import load_model, synthesize, synthesize_stream
    started = time.perf_counter()
    model, np, torch, _, _ = load_model(config)
    load_seconds = time.perf_counter() - started
    args.output.mkdir(parents=True, exist_ok=True)
    selected = set(args.ids.split(','))
    rows = []
    result = {'scope': 'synthetic digital WAV generation; not acoustic/human or production acceptance',
              'load_seconds': round(load_seconds, 3),
              'identities': {key: config[key] for key in ('source_revision', 'model_sha256', 'adapter_sha256', 'reference_sha256')},
              'input_sha256': hashlib.sha256(args.inputs.read_bytes()).hexdigest(), 'rows': rows}
    for case in inputs['cases']:
        if case['id'] not in selected:
            continue
        for mode, variant in [('full', 'baseline'), ('stream', 'baseline'), ('stream', 'yo'), ('stream', 'acute')]:
            text = case[variant]
            first_ms = None
            begin = time.perf_counter()
            if mode == 'full':
                raw, count, _ = synthesize(config, model, np, torch, text)
                first_ms = (time.perf_counter() - begin) * 1000
            else:
                parts = []
                count = 0
                for raw, size, _ in synthesize_stream(config, model, np, torch, text):
                    if first_ms is None:
                        first_ms = (time.perf_counter() - begin) * 1000
                    parts.append(raw)
                    count += size
                raw = b''.join(parts)
            samples = np.frombuffer(raw, dtype='<f4')
            assert len(samples) == count and count and np.isfinite(samples).all()
            name = f'{case["id"]}-{variant}-{mode}.wav'
            pcm = (np.clip(samples, -1, 1) * 32767).astype('<i2').tobytes()
            with wave.open(str(args.output/name), 'wb') as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(config['sample_rate'])
                output.writeframes(pcm)
            row = {'case': case['id'], 'variant': variant, 'mode': mode, 'text': text,
                   'first_generated_pcm_ms': round(first_ms, 3),
                   'generation_ms': round((time.perf_counter()-begin)*1000, 3),
                   'seconds': round(count/config['sample_rate'], 3),
                   'peak': round(float(np.max(np.abs(samples))), 5),
                   'wav': name, 'sha256': hashlib.sha256((args.output/name).read_bytes()).hexdigest()}
            rows.append(row)
            (args.output/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
            print(json.dumps({key: row[key] for key in ('case', 'variant', 'mode', 'first_generated_pcm_ms', 'generation_ms', 'seconds')}), flush=True)


if __name__ == '__main__':
    main()
