"""Prepare the supplied public name recordings; never read visit/profile data."""
from pathlib import Path
import argparse
import hashlib
import json
import math
import wave


def prepare(source: Path, destination: Path):
    import numpy as np
    from scipy.signal import resample_poly
    entries, rejected = [], []
    source_rows = json.loads((source / 'source-index.json').read_text())
    destination.mkdir(parents=True, exist_ok=True)
    for row in source_rows:
        original = source / 'drive-originals' / (row['id'] + '.wav')
        with wave.open(str(original)) as w:
            channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            if width != 2 or channels not in (1, 2):
                raise ValueError('unsupported source WAV format')
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype='<i2').astype(np.float64)
        pcm = pcm.reshape(-1, channels).mean(axis=1) / 32768
        hop = max(1, rate // 100)
        energy = np.array([np.sqrt(np.mean(pcm[i:i + hop] ** 2)) for i in range(0, len(pcm), hop)])
        active = np.flatnonzero(energy >= max(0.0015, energy.max() * 0.04))
        if not len(active):
            raise ValueError('silent name recording')
        spans = []
        for frame in active:
            if not spans or frame - spans[-1][1] > 22:
                spans.append([int(frame), int(frame)])
            else:
                spans[-1][1] = int(frame)
        # Source exports include short, identical trailing clicks. Retain the
        # sustained name utterance, not all nonzero samples in the export.
        spans.sort(key=lambda span: span[1] - span[0], reverse=True)
        if len(spans) > 1 and spans[1][1] - spans[1][0] >= 25:
            rejected.append({'source_id': row['id'], 'reason': 'multiple_sustained_spans_needs_human_segmentation'})
            continue
        first, last = spans[0]
        start = max(0, int(first * hop - rate * .06))
        end = min(len(pcm), int((last + 1) * hop + rate * .08))
        factor = math.gcd(rate, 24000)
        audio = resample_poly(pcm[start:end], 24000 // factor, rate // factor)
        if not 0.1 < len(audio) / 24000 < 6:
            rejected.append({'source_id': row['id'], 'reason': 'duration_needs_human_segmentation'})
            continue
        fade = min(120, len(audio) // 2)
        audio[:fade] *= np.linspace(0, 1, fade)
        audio[-fade:] *= np.linspace(1, 0, fade)
        out = destination / (row['id'] + '.wav')
        with wave.open(str(out), 'wb') as w:
            w.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            w.writeframes((np.clip(audio, -.98, .98) * 32767).astype('<i2').tobytes())
        entries.append({'key': Path(row['name']).stem, 'case': 'nominative', 'file': out.name,
                        'sha256': hashlib.sha256(out.read_bytes()).hexdigest(), 'aliases': [],
                        'source_sha256': hashlib.sha256(original.read_bytes()).hexdigest(),
                        'trim_frames': [start, end], 'source_rate': rate})
    manifest = {'schema_version': 1, 'default_case': 'nominative',
                'source_folder': 'https://drive.google.com/drive/folders/1aO0rjuDSLINPkfMSf7hJHGOBlB5aBSKS',
                'verification': 'WAV integrity verified; pronunciation and usage rights not independently verified',
                'entries': entries, 'not_prepared': rejected}
    (destination / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'prepared': len(entries), 'not_prepared': len(rejected), 'bytes': sum((destination / e['file']).stat().st_size for e in entries)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    prepare(args.source, args.destination)
