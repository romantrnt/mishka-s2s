"""Synthetic Max/application through the actual owned PCM stand callback path.

Run with repository root and tests/ on PYTHONPATH. No device or server access.
"""
import asyncio
import pytest
from test_private_welcome import setup, start, deadline, speech
from test_owned_private_pcm import Playback
from app.foundation.stand import HardwareIntegrationStand, HeadlessAudioOutput


def wired(outcome):
    h, render = setup()
    stand = HardwareIntegrationStand.__new__(HardwareIntegrationStand)
    stand.application = h.app
    stand.active_session_id = h.boundary.session_id
    stand.playback = Playback(outcome)
    stand._response_audio_tasks = set()
    stand._audio_event_sequence = 0
    port = HeadlessAudioOutput()
    port.on_pcm_play = stand._headless_pcm_play
    port.on_cancel = lambda token: stand._cancel_response_audio()
    port.on_asset_play = lambda response, *args: h.played.append(response)
    stand.audio_output = port
    h.app.audio = port
    return h, render, stand


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['success', 'error', 'incomplete'])
async def test_actual_app_stand_owned_pcm_completion(outcome):
    h, render, stand = wired(outcome)
    try:
        await start(h)
        assert (await deadline(h, 14.999999))['reason'] == 'welcome_not_due'
        assert not stand.playback.played
        assert (await deadline(h))['reason'] == 'personal_welcome_started'
        await asyncio.gather(*tuple(stand._response_audio_tasks))
        assert len(stand.playback.played) == 1
        assert not h.app.bounded_s2s.tts.cache
        if outcome == 'success':
            assert h.state.mode == 'exhibition_menu_tutorial_prompt'
            assert h.state.active_audio_id == 'PHRASE_007' and h.played[-1] == 'PHRASE_007'
            assert not stand._headless_audio_accepting()
            current = dict(h.app._active_audio[h.boundary.session_id])
            await speech(h, 'Хорошо')
            assert h.state.mode == 'exhibition_menu_tutorial_prompt' and h.state.mood_result is None
            assert h.app._active_audio[h.boundary.session_id] == current
        else:
            assert 'PHRASE_007' not in h.played
            assert h.state.audio_status == 'failed'
            assert not stand._headless_audio_accepting()
            await speech(h, 'Хорошо')
            assert 'PHRASE_007' not in h.played
    finally:
        stand._cancel_response_audio()
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('interrupt', ['hangup', 'revoke'])
async def test_actual_app_stand_inflight_private_interrupt(interrupt):
    h, render, stand = wired('wait')
    try:
        await start(h)
        await deadline(h)
        tasks = tuple(stand._response_audio_tasks)
        await stand.playback.started.wait()
        if interrupt == 'hangup':
            await h.send('HandsetPlaced')
        else:
            h.client.value = None
            h.client.events.append({'kind': 'clear', 'reason_code': 'scan_started'})
            await h.boundary.step()
        stand.playback.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        state = h.app.canonical_runtime.sessions.get(h.boundary.session_id)
        assert state is None or state.mode != 'personal_welcome_listen'
        assert not stand._headless_audio_accepting()
        assert not h.app._private_welcomes
        assert 'PHRASE_007' not in h.played
        assert not h.app.bounded_s2s.tts.cache
    finally:
        stand.playback.release.set()
        stand._cancel_response_audio()
        await h.close()
