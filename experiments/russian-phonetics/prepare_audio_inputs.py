"""Prepare authored corpus variants; never apply experimental marks to visitors."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

from frontend_probe import lexical_text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--models', required=True)
    parser.add_argument('--normalizer-python', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    corpus = json.loads(Path(__file__).with_name('corpus.json').read_text())
    assert corpus['synthetic_public_text_only'] is True
    # Run the existing application normalizer in its unchanged environment.
    code = ('import json,sys;from app.foundation.voice_pipeline import prepare_vox_text;'
            'print(json.dumps([prepare_vox_text(x) for x in json.load(sys.stdin)],ensure_ascii=False))')
    normalized = json.loads(subprocess.check_output([args.normalizer_python, '-c', code],
        input=json.dumps([row['text'] for row in corpus['cases']]).encode(), cwd=root))
    from ruaccent import RUAccent
    import ruaccent.session_helper as sessions
    original_options = sessions.SessionOptions
    def options():
        value = original_options()
        value.intra_op_num_threads = 2
        value.inter_op_num_threads = 1
        return value
    sessions.SessionOptions = options
    accent = RUAccent()
    accent.load(omograph_model_size='turbo3.1', use_dictionary=True,
                device='CPU', workdir=args.models)
    rows = []
    for case, baseline in zip(corpus['cases'], normalized, strict=True):
        marked = accent.process_all(baseline)
        assert lexical_text(marked) == lexical_text(baseline), case['id']
        acute = re.sub(r'\+([аеёиоуыэюя])',
            lambda match: match[1] + ('' if match[1].casefold() == 'ё' else '\u0301'), marked, flags=re.I)
        assert '+' not in acute
        rows.append({'id': case['id'], 'baseline': baseline,
                     'yo': marked.replace('+', ''), 'acute': acute})
    args.output.write_text(json.dumps({'synthetic_public_text_only': True,
        'normalizer_sha256': hashlib.sha256((root/'app/foundation/voice_pipeline.py').read_bytes()).hexdigest(),
        'accent_model_revision': 'b78ae5ea1e62beaf138bed1865cd8c3b0b5ca855',
        'cases': rows}, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'prepared_cases': len(rows), 'production_changed': False}))


if __name__ == '__main__':
    main()
