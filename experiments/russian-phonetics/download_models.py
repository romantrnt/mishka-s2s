"""Download pinned public CPU assets into an explicit experiment directory."""
import argparse
from huggingface_hub import snapshot_download

REVISION = 'b78ae5ea1e62beaf138bed1865cd8c3b0b5ca855'

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--models', required=True)
    parser.add_argument('--accent-model', choices=('tiny2.1', 'turbo3.1'), default='tiny2.1')
    args = parser.parse_args()
    snapshot_download('ruaccent/accentuator', revision=REVISION, local_dir=args.models,
        allow_patterns=['README.md', 'dictionary/omographs.json.gz', 'dictionary/yo_words.json.gz',
            'dictionary/yo_homographs.json.gz', 'dictionary/accents.json.gz',
            'nn/nn_accent/*', 'nn/nn_yo_homograph_resolver/*',
            'nn/nn_stress_usage_predictor/*', f'nn/nn_omograph/{args.accent_model}/*'],
        ignore_patterns=['nn/nn_accent/big.onnx'], token=False, max_workers=2)
