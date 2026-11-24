"""Root six announces its button exit through real owned PCM; no microphone input."""
import asyncio
from dataclasses import replace

import pytest

from app.foundation.s2s import AudioChunk
from test_panel_session_actions import PanelStand
from test_scene_menu_runtime import Harness


INTRO = 'Давай поговорим. Чтобы вернуться в меню, нажми семь.'


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', ['audible_root', 'quiet_sound', 'quiet_lift'])
async def test_panel_root_six_owned_intro_never_claims_microphone_and_keeps_button_seven(tmp_path, entry):
    h = PanelStand(tmp_path)
    played = []
    h.app.audio.play_pcm_owned = lambda handle, audio: played.append((handle, audio))
    try:
        await h.control('start')
        if entry == 'audible_root':
            await h.control('enable_audio', {**h.scope(), 'state_version': h.state.state_version})
        await h.key(6)
        if entry != 'audible_root':
            assert h.state.mode == 'awaiting_handset' and not played
            if entry == 'quiet_sound':
                await h.control('enable_audio', {**h.scope(), 'state_version': h.state.state_version})
            else:
                await h.simulator.action({'action': 'handset', 'state': 'up'})
        assert h.state.mode == 'bear_dialogue' and h.state.active_audio_id == 'DIALOGUE_INTRO'
        assert h.state.expected_inputs == ['speech', 'button_7', 'handset_down']
        assert len(played) == 1 and played[0][0].response_id == 'DIALOGUE_INTRO'
        handle, audio = played[0]
        assert audio.samples and audio.session_id == 'stand' and audio.token == handle.token
        assert h.app.bounded_s2s.tts.texts == [INTRO]
        assert h.stand._pcm_owner_current(handle, (h.state.profile_ref, h.state.identity_request_id))
        assert not h.stand._pcm_owner_current(replace(handle, playback_lease='old'), None)
        await h.done()
        assert h.state.mode == 'bear_dialogue' and not h.app._active_audio
        await h.key(7)
        assert h.state.mode == 'root_menu' and not h.app._active_audio
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_root_menu_entry_uses_same_exit_intro_outside_exhibition_menu_ready():
    h = Harness()
    try:
        assert h.state.mode == 'root_menu'
        await h.button(6)
        assert h.state.mode == 'bear_dialogue' and h.state.active_audio_id == 'DIALOGUE_INTRO'
        assert h.played[-1].response_id == 'DIALOGUE_INTRO'
        assert h.render.texts == [INTRO]
        await h.complete()
        await h.button(7)
        assert h.state.mode == 'root_menu' and not h.app._active_audio
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['synthesis', 'playback', 'no_pcm', 'no_engine'])
async def test_intro_failure_keeps_real_button_exit_without_recorded_fallback(failure):
    h = Harness()
    try:
        if failure == 'synthesis':
            async def broken(*args):
                raise RuntimeError('SYNTHETIC_PRIVATE_PROVIDER_BODY')
            h.render.synthesize = broken
        elif failure == 'no_pcm':
            h.app.audio.private_pcm_supported = False
        elif failure == 'no_engine':
            h.app.bounded_s2s = None
        result = await h.button(6)
        if failure == 'playback':
            assert h.state.active_audio_id == 'DIALOGUE_INTRO'
            result = await h.complete(failed=True)
        assert h.state.mode == 'bear_dialogue' and not h.app._active_audio
        assert 'button_7' in h.state.expected_inputs
        assert 'SYNTHETIC_PRIVATE_PROVIDER_BODY' not in repr(result)
        assert not any(command.get('response_id') == 'PHRASE_094' for command in h.app.audio.commands)
        if failure != 'playback':
            assert not h.played
        await h.button(7)
        assert h.state.mode == 'root_menu' and not h.app._active_audio
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['back', 'new_visit'])
async def test_late_cancel_resistant_intro_cannot_play_after_exit_or_new_visit(tmp_path, boundary):
    h = PanelStand(tmp_path)
    played = []
    h.app.audio.play_pcm_owned = lambda handle, audio: played.append(handle)
    await h.control('start')
    await h.control('enable_audio', {**h.scope(), 'state_version': h.state.state_version})
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed(text, token, cancelled):
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
        return AudioChunk((.1, .2), 16000, token)
    h.app.bounded_s2s.tts.synthesize = delayed
    pending = asyncio.create_task(h.key(6))
    try:
        await asyncio.wait_for(entered.wait(), 1.)
        if boundary == 'back':
            await h.key(7)
            assert h.state.mode == 'root_menu'
        else:
            old_scope = h.scope()
            await h.control('end')
            await h.control('start')
            assert h.state.mode == 'menu_ready' and h.state.handset == 'down'
            assert h.scope()['epoch'] != old_scope['epoch']
        assert h.state.active_audio_id != 'DIALOGUE_INTRO'
        release.set()
        await asyncio.wait_for(pending, 2.)
        if h.app._owned_text_jobs:
            await asyncio.wait_for(asyncio.gather(*tuple(h.app._owned_text_jobs), return_exceptions=True), 2.)
        assert not any(handle.response_id == 'DIALOGUE_INTRO' for handle in played)
        assert h.app._active_audio.get('stand', {}).get('content_id') != 'DIALOGUE_INTRO'
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 2.)
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('activity', ['game', 'catalogue'])
async def test_owned_return_from_game_or_catalogue_also_speaks_button_seven(activity):
    from test_contextual_game_flow import Harness as Conversation
    from test_conversation_catalogue_gateway import quick_public_audio
    h = Conversation('<action type="open_catalog" catalog="stories"/>') if activity == 'catalogue' else Conversation()
    render = quick_public_audio(h)
    try:
        if activity == 'game':
            await h.launch('quiz')
            await h.key(7, duration=6.)
        else:
            await h.send('SpeechReceived', text='Хочу послушать сказку')
            assert h.state.mode == 'fixed_story_menu'
            await h.key(7)
        assert h.state.mode == 'bear_dialogue' and h.state.active_audio_id == 'DIALOGUE_RETURN'
        assert render.texts[-1] == 'Вернулись к разговору. Чтобы вернуться в меню, нажми семь.'
        await h.done()
        await h.key(7)
        assert h.state.mode == 'root_menu'
    finally:
        await h.close()
