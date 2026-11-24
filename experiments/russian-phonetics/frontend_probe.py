"""Offline, synthetic corpus only; this never changes the running TTS pipeline."""
import argparse
import json
from pathlib import Path
import re
import statistics
import time


def word_stress(value):
    letters, stressed, pending = [], set(), False
    for char in value.casefold():
        if char == '+':
            pending = True
        elif char == '\u0301':
            stressed.add(len(letters) - 1)
        else:
            if pending or char == 'ё':
                stressed.add(len(letters))
            letters.append(char.replace('ё', 'е'))
            pending = False
    vowels = [i for i, char in enumerate(letters) if char in 'аеиоуыэюя']
    if not stressed and len(vowels) == 1:
        stressed.add(vowels[0])
    return ''.join(letters), stressed


def evaluate(text, expected):
    words = [word_stress(word) for word in re.findall(r'[А-Яа-яЁё+\u0301]+', text)]
    return [target for target in expected if word_stress(target) not in words]


def lexical_text(text):
    return ' '.join(re.sub(r'[+\u0301]', '', text).casefold().replace('ё', 'е').split())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--models', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--accent-model', choices=('tiny2.1', 'turbo3.1'), default='tiny2.1')
    args = parser.parse_args()
    assert word_stress('зам+ок') == word_stress('замо\u0301к')
    assert word_stress('берёг') == word_stress('бер+ег')
    assert not evaluate('На двери висит зам+ок.', ['зам+ок'])
    assert evaluate('На горе стоит зам+ок.', ['з+амок']) == ['з+амок']
    from ruaccent import RUAccent
    import ruaccent.session_helper as sessions
    import pymorphy3
    # This isolated experiment has a two-thread budget per ONNX session.
    options_type = sessions.SessionOptions
    def options():
        value = options_type()
        value.intra_op_num_threads = 2
        value.inter_op_num_threads = 1
        return value
    sessions.SessionOptions = options
    load_started = time.perf_counter()
    accent = RUAccent()
    accent.load(omograph_model_size=args.accent_model, use_dictionary=True,
                device='CPU', workdir=str(args.models))
    load_seconds = time.perf_counter() - load_started
    corpus = json.loads(Path(__file__).with_name('corpus.json').read_text())
    rows = []
    for case in corpus['cases']:
        started = time.perf_counter()
        output = accent.process_all(case['text'])
        elapsed = (time.perf_counter() - started) * 1000
        rows.append({**case, 'output': output, 'ms': round(elapsed, 3),
                     'baseline_missing': evaluate(case['text'], case['expected_targets']),
                     'accent_missing': evaluate(output, case['expected_targets']),
                     'lexical_text_preserved': lexical_text(output) == lexical_text(case['text'])})
    morph = pymorphy3.MorphAnalyzer()
    morphology = [{'input': word, 'analyses': sorted({p.normal_form for p in morph.parse(word)})}
                  for word in ('обезьяна', 'обезьяну', 'обезьяной', 'слон', 'слона', 'мышь', 'мышью', 'подъезд')]
    durations = sorted(row['ms'] for row in rows)
    result = {'scope': 'CPU text-front-end only; no TTS/audio/hearing or production acceptance',
              'ruaccent_model': args.accent_model + '+dictionary', 'load_seconds': round(load_seconds, 3),
              'p50_ms': round(statistics.median(durations), 3),
              'p95_ms': durations[min(len(durations) - 1, int(len(durations) * .95))],
              'baseline_target_matches': sum(len(r['expected_targets']) - len(r['baseline_missing']) for r in rows),
              'accent_target_matches': sum(len(r['expected_targets']) - len(r['accent_missing']) for r in rows),
              'targets': sum(len(r['expected_targets']) for r in rows),
              'lexical_changes': sum(not r['lexical_text_preserved'] for r in rows),
              'rows': rows, 'morphology': morphology}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k not in {'rows', 'morphology'}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
