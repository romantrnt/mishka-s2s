"""Synthetic WAV catalogue for pagination tests; no production assets changed."""
import hashlib
import json
import wave

from app.foundation.canonical_contract import FixedStoryManifest


def tiny_stories(tmp_path):
    rows = {}
    # Seven verified synthetic recordings exercise two real five-slot pages.
    items = [('fixed_aibolit_v1', 'Айболит')]
    items += [(f'test_story_{i}', f'Тестовая сказка {i}') for i in range(1, 5)]
    items += [('fixed_squirrel_v1', 'Как бельчонок страх преодолел'),
              ('fixed_neznayka_v1', 'Телефон')]
    for story_id, title in items:
        path = tmp_path / (story_id+'.wav')
        with wave.open(str(path),'wb') as output:
            output.setparams((1,2,16000,1600,'NONE','not compressed'))
            output.writeframes(b'\0\0'*1600)
        rows[story_id] = {'display_title':title, 'audio_file':path.name,
            'audio_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'rights_status':'operator_approved', 'availability':'available',
            'eligible_age_bands':['1-3','3-5','5-7']}
    path = tmp_path/'manifest.json'
    path.write_text(json.dumps({'stories':rows}), encoding='utf-8')
    return FixedStoryManifest(path,tmp_path)
