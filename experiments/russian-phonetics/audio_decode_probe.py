"""CPU-only intelligibility proxy on hash-verified, authored Vox corpus WAVs.

ASR cannot grade stress, timbre or perceived quality. Never use visitor audio here.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import time
import unicodedata


def words(text):
    text = ''.join(c for c in unicodedata.normalize('NFD', text) if c != '\u0301')
    return re.findall(r'[а-яёa-z0-9]+', unicodedata.normalize('NFC', text).casefold().replace('ё', 'е'))


def distance(expected, actual):
    previous = list(range(len(actual) + 1))
    for i, left in enumerate(expected, 1):
        current = [i]
        for j, right in enumerate(actual, 1):
            current.append(min(current[-1] + 1, previous[j] + 1, previous[j-1] + (left != right)))
        previous = current
    return previous[-1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    inputs = json.loads(args.inputs.read_text())
    if inputs.get('synthetic_public_text_only') is not True:
        raise ValueError('Authored synthetic corpus declaration required')
    result = json.loads((args.corpus/'result.json').read_text())
    expected = {case['id']: case['baseline'] for case in inputs['cases']}
    rows = result['rows']
    for row in rows:
        path = args.corpus/row['wav']
        if path.parent != args.corpus or not path.name.endswith('.wav'):
            raise ValueError('WAV must be a direct corpus member')
        if hashlib.sha256(path.read_bytes()).hexdigest() != row['sha256']:
            raise ValueError('WAV hash mismatch: ' + row['wav'])
    from faster_whisper import WhisperModel
    model = WhisperModel(str(args.model), device='cpu', compute_type='int8', cpu_threads=2,
                         num_workers=1, local_files_only=True)
    report = {'scope': 'CPU ASR on generated public WAVs; not human pronunciation acceptance',
              'model': str(args.model), 'device': 'cpu', 'compute_type': 'int8', 'rows': []}
    for row in rows:
        started = time.perf_counter()
        segments, info = model.transcribe(str(args.corpus/row['wav']), language='ru', beam_size=5,
                                          temperature=0, vad_filter=False, condition_on_previous_text=False)
        text = ' '.join(segment.text.strip() for segment in segments)
        reference = expected[row['case']]
        tokens = words(reference)
        record = {key: row[key] for key in ('case', 'variant', 'mode', 'wav', 'sha256')}
        record.update(expected=reference, transcript=text, word_edits=distance(tokens, words(text)),
                      reference_words=len(tokens), seconds=round(time.perf_counter()-started, 3))
        report['rows'].append(record)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
        print(json.dumps(record, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
