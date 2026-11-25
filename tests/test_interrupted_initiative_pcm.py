"""Interrupted question leases; synthetic providers/socket, real Stand and APP ownership."""
import asyncio
import json

import pytest

from app.foundation.config import Settings
from app.foundation.stand import HardwareIntegrationStand
from test_browser_private_pcm import Socket, drain
from test_unavailable_activity_app import conversation
from test_dialogue_initiative_app import model, proposal, content


async def opened():
    h = await conversation()
    stand = HardwareIntegrationStand(Settings(profile='mock'), background_scheduler=False)
    original = stand.application
    stand.application, stand.active_session_id = h.app, h.sid
    h.app.audio = stand.audio_output
    writer = Socket()
    assert await stand.hub.connect(writer)
    model(h, proposal(), content())
    return h, stand, original, writer


async def close(h, stand, original):
    await h.close()
    stand.application = original
    await stand.aclose()


@pytest.mark.asyncio
async def test_lost_writer_mid_question_reopens_without_watchdog_or_new_visit():
    h, stand, original, writer = await opened()
    try:
        epoch = h.app._session_epochs.get(h.sid, 0)
        writer.on_pcm = lambda: stand.hub.disconnect(writer)
        await h.send('SpeechReceived', text='Мне нравится лес')
        await drain(stand)
        assert h.state.active_audio_id is None and h.state.pending_initiative is None
        assert h.sid not in h.app._active_audio and h.sid not in h.app._audio_timeout_tasks
        assert h.app.conversation_contexts[h.sid].initiative is None
        assert h.app._session_epochs.get(h.sid, 0) == epoch and h.state.mode == 'bear_dialogue'
        assert {'speech', 'button_5', 'button_7'} <= set(h.state.expected_inputs)
        assert not stand._browser_pcm_callback_current(next(x['playback'] for x in writer.messages
            if x['type'] == 'audio_pcm_output'))
        replacement = Socket()
        assert await stand.hub.connect(replacement)
        model(h, json.dumps({'type':'say','text':'Я слушаю тебя.'}, ensure_ascii=False))
        await h.send('SpeechReceived', text='Давай')
        assert h.state.pending_initiative is None and h.state.game_engine_state is None
        assert not any('Кто мяукает' in value for value in h.render.texts)
        model(h, json.dumps({'type':'say','text':'Динозавры были разными.'}, ensure_ascii=False))
        await h.send('SpeechReceived', text='Мне нравятся динозавры')
        assert 'Динозавры были разными.' in h.render.texts
    finally:
        await close(h, stand, original)


@pytest.mark.asyncio
async def test_lost_writer_after_final_delivery_before_completed_disarms_only_that_question():
    h, stand, original, writer = await opened()
    try:
        await h.send('SpeechReceived', text='Мне нравится лес')
        await drain(stand)
        saved = stand._browser_pcm_playback
        assert stand._browser_pcm_delivered is saved and not h.state.pending_initiative.ready
        stand.hub.disconnect(writer)
        await stand._cleanup_interrupted_browser_offer(saved)
        assert h.state.active_audio_id is None and h.state.pending_initiative is None
        assert h.app.conversation_contexts[h.sid].initiative is None
        await stand._cleanup_interrupted_browser_offer(saved)  # Repeated teardown is harmless.
        assert h.state.mode == 'bear_dialogue' and h.sid not in h.app._active_audio
    finally:
        await close(h, stand, original)


@pytest.mark.asyncio
@pytest.mark.parametrize('next_action', ['new_offer', 'help', 'urgent', 'end'])
async def test_delayed_interruption_cleanup_cannot_change_newer_owner_or_lighting(next_action):
    h, stand, original, writer = await opened()
    release, entered = asyncio.Event(), asyncio.Event()
    pending = None
    try:
        await h.send('SpeechReceived', text='Мне нравится лес'); await drain(stand)
        saved = stand._browser_pcm_playback
        stand.hub.disconnect(writer)
        assert await stand.hub.connect(Socket())
        original_receive = h.app._receive_canonical_event
        async def held(event, *args, **kwargs):
            if event.source == 'browser_offer_interrupted':
                entered.set(); await release.wait()
            return await original_receive(event, *args, **kwargs)
        h.app._receive_canonical_event = held
        pending = asyncio.create_task(stand._cleanup_interrupted_browser_offer(saved))
        await asyncio.wait_for(entered.wait(), 2)
        if next_action == 'new_offer':
            model(h, proposal('joke', text='Хочешь короткую шутку?'))
            await h.send('SpeechReceived', text='Мне нравятся динозавры'); await drain(stand)
            assert h.state.active_audio_id == 'INITIATIVE_OFFER'
        elif next_action == 'help': await h.key(5)
        elif next_action == 'urgent': await h.send('SpeechReceived', text='Меня бьют.')
        else: await h.app.session_action(h.sid, 'end', ownership_guard=lambda: True)
        owner = h.app._active_audio.get(h.sid)
        watchdog = h.app._audio_timeout_tasks.get(h.sid)
        state = h.state.snapshot()
        light = h.app.led.effect_snapshot()
        release.set(); await asyncio.wait_for(pending, 2)
        assert h.state.snapshot() == state
        assert h.app._active_audio.get(h.sid) is owner
        assert h.app._audio_timeout_tasks.get(h.sid) is watchdog
        assert h.app.led.effect_snapshot() == light
    finally:
        release.set()
        if pending: await asyncio.gather(pending, return_exceptions=True)
        await close(h, stand, original)
