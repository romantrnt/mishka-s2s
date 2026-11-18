"""Capture ownership uses a committed menu; exact asset admission stays fresh."""
import asyncio
import hashlib
import json
from pathlib import Path
import wave

import pytest

from app.foundation.canonical_contract import FixedStoryManifest
from app.foundation.domain import Event
from test_contextual_game_flow import Harness
from test_conversation_catalogue_gateway import quick_public_audio


def tiny_stories(tmp_path):
    rows = {}
    for story_id, title in [('fixed_aibolit_v1','Айболит'), ('fixed_squirrel_v1','Как бельчонок страх преодолел')]:
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


async def catalogue(tmp_path):
    h = Harness(mode='root_menu',band='1-3')
    quick_public_audio(h)
    h.app.canonical_runtime.stories = tiny_stories(tmp_path)
    await h.key(2)
    assert h.state.scene_menu_active == h.state.mode == 'fixed_story_menu'
    assert h.state.scene_menu_descriptor['items'][0]['id'] == 'fixed_aibolit_v1'
    return h


@pytest.mark.asyncio
async def test_repeated_capture_scope_and_pcm_never_verify_or_read_story_files(tmp_path, monkeypatch):
    h = await catalogue(tmp_path)
    checks, reads = [], []
    class NoSTT:
        async def transcribe(self,*args):
            pytest.fail('short collecting PCM must not call STT')
    h.app.s2s_audio.recognizer = NoSTT()
    original_read = Path.read_bytes
    def forbidden_verify(story_id):
        checks.append(story_id)
        raise AssertionError('capture guard must not reverify a recorded asset')
    def guarded_read(path):
        if path.suffix == '.wav':
            reads.append(path)
            raise AssertionError('capture guard must not read WAV bytes')
        return original_read(path)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(h.app.canonical_runtime.stories,'get_verified',forbidden_verify)
            patch.setattr(Path,'read_bytes',guarded_read)
            owner = h.app._catalogue_voice_scope(h.state.session_id)
            assert owner is not None
            for _ in range(4):
                assert h.app._catalogue_voice_scope(h.state.session_id) == owner
            for _ in range(2):
                h.seq += 1
                result = await h.app.receive_audio_async(h.state.session_id,(.12,)*341,h.seq)
                assert result['audio_status'] in {'collecting','waiting'}
            assert h.app._catalogue_voice_scope(h.state.session_id) == owner
            assert not checks and not reads and not h.model.calls
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',['unchanged','bytes','missing','rights'])
async def test_selected_asset_still_reverified_before_recorded_playback(tmp_path, monkeypatch, mutation):
    h = await catalogue(tmp_path)
    manifest = h.app.canonical_runtime.stories
    selected = 'fixed_aibolit_v1'
    timeline = []
    verify, play = manifest.get_verified, h.app.audio.play_owned
    def checked(story_id):
        timeline.append(('verify',story_id))
        return verify(story_id)
    def played(handle,*args,**kwargs):
        timeline.append(('play',handle.response_id))
        return play(handle,*args,**kwargs)
    try:
        monkeypatch.setattr(manifest,'get_verified',checked)
        monkeypatch.setattr(h.app.audio,'play_owned',played)
        path = tmp_path/manifest.stories[selected]['audio_file']
        if mutation == 'bytes':path.write_bytes(b'changed source')
        elif mutation == 'missing':path.unlink()
        elif mutation == 'rights':manifest.stories[selected]['rights_status']='revoked'
        await h.key(1)
        assert ('verify',selected) in timeline
        if mutation == 'unchanged':
            assert h.state.mode == 'fixed_story_playing' and h.state.active_story_id == selected
            assert timeline.index(('verify',selected)) < timeline.index(('play',selected))
        else:
            assert h.state.active_story_id is None
            assert ('play',selected) not in timeline
            assert not any(command.get('response_id') == selected for command in h.app.audio.commands)
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('named',[False,True])
async def test_page_change_keeps_entry_owner_but_never_remaps_pending_numeric_choice(tmp_path, named):
    h = await catalogue(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    task = None
    classify = h.app.safety_classifier.classify
    async def delayed(text,**kwargs):
        entered.set()
        await release.wait()
        return await classify(text,**kwargs)
    try:
        owner = h.app._catalogue_voice_scope(h.state.session_id)
        h.app.safety_classifier.classify = delayed
        task = asyncio.create_task(h.send('SpeechReceived',text='Хочу Айболита' if named else 'Один'))
        await asyncio.wait_for(entered.wait(),2)
        await h.app.receive_async(Event('ButtonPressed',h.state.session_id,1,1.,'hid',{'button_id':6},'page:1',h.clock))
        assert h.state.scene_menu_descriptor['items'][0]['id'] == 'fixed_squirrel_v1'
        assert h.app._catalogue_voice_scope(h.state.session_id) == owner
        release.set()
        result = await task
        if named:
            assert h.state.active_story_id == 'fixed_aibolit_v1'
        else:
            assert result['reason'] == 'scene_menu_choice_stale'
            assert h.state.active_story_id is None and h.state.mode == 'fixed_story_menu'
    finally:
        release.set()
        if task:await asyncio.gather(task,return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_capture_owner_requires_current_committed_entry_and_revokes_reentry(tmp_path):
    h = await catalogue(tmp_path)
    try:
        old = h.app._catalogue_voice_scope(h.state.session_id)
        await h.key(7)
        assert h.app._catalogue_voice_scope(h.state.session_id) is None
        await h.key(2)
        assert h.app._catalogue_voice_scope(h.state.session_id) != old
        current = h.app._catalogue_voice_scope(h.state.session_id)
        manifest = h.app.canonical_runtime.stories
        h.app.canonical_runtime.stories = FixedStoryManifest(manifest.path,manifest.root)
        assert h.app._catalogue_voice_scope(h.state.session_id) != current
        descriptor = h.state.scene_menu_descriptor
        h.state.scene_menu_descriptor = {**descriptor,'scene':'game_menu'}
        assert h.app._catalogue_voice_scope(h.state.session_id) is None
        h.state.scene_menu_descriptor = descriptor
        h.state.scene_menu_active = None
        assert h.app._catalogue_voice_scope(h.state.session_id) is None
    finally:
        await h.close()
