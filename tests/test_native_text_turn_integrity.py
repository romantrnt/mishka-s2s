"""Local HTTP/headless cancellation boundary; providers/PCM are synthetic only."""
import asyncio

import httpx
import pytest

from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.hardware import encode_frame
from app.foundation.s2s import EnergyVADAdapter, MockPlaybackController, STTResult
from app.foundation.stand import HeadlessAudioOutput, create_stand_app


@pytest.mark.asyncio
@pytest.mark.parametrize('interrupt', ['none', 'vad_before_output', 'vad_during_output', 'client',
                                      'provider', 'unmarked', 'shutdown', 'end', 'reset_new_visit', 'vad_raw'])
async def test_root_four_http_text_survives_headless_vad_barge_in(interrupt):
    api = create_stand_app(Settings(profile='mock', runtime_mode='clinical',
                                    stand_access_token='synthetic-native-text'))
    stand, app = api.state.stand, api.state.stand.application
    sid = stand.active_session_id
    entered, release = asyncio.Event(), asyncio.Event()
    output_started, output_release = asyncio.Event(), asyncio.Event()
    recognized, cancelled_tasks = [], []

    class Model:
        async def stream(self, messages, request_id, cancelled):
            entered.set()
            await release.wait()
            if interrupt == 'provider':
                raise asyncio.CancelledError('synthetic-provider-cancel')
            yield '<say>Облака состоят из маленьких капель воды.</say>'

    class Recognizer:
        async def transcribe(self, audio, cancelled):
            recognized.append(audio)
            return STTResult('', .99, .1)

    class Playback(MockPlaybackController):
        async def play(self, audio):
            await super().play(audio)
            output_started.set()
            await output_release.wait()

    pending = None
    try:
        # Isolate HTTP semantics from hardware/warmup admission; never start devices.
        api.state.lifecycle.admits_users = lambda: True
        await app.receive_async(Event('HandsetLifted', sid, 1, 1., 'test', {}, 'lift', 0.))
        app._cancel_welcome_timer(sid)
        state = app.canonical_runtime.sessions[sid]
        state.mode = 'root_menu'
        app.sessions.get(sid).current_node = 'root_menu'
        stand.hardware_mode = 'physical'
        stand.playback = playback = Playback()
        playback._generation = 0
        app.bounded_s2s.playback = playback
        app.bounded_s2s.llm = Model()
        app.bounded_s2s.allow_quick_replies = False
        app.s2s_audio.vad, app.s2s_audio.recognizer = EnergyVADAdapter(), Recognizer()
        output = HeadlessAudioOutput()
        output.on_play = stand._headless_audio_play
        output.on_cancel = lambda token: stand._cancel_response_audio()
        app.audio = output
        transport = httpx.ASGITransport(app=api, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url='http://synthetic',
                                    headers={'x-stand-token': 'synthetic-native-text'}) as client:
            selected = await client.post('/stand/button', json={'button_id': 4})
            assert selected.status_code == 200 and state.mode == 'support_intake'
            question = {'text': 'Почему облака белые?'}
            if interrupt == 'vad_raw':
                raw = encode_frame('text', stand.adapter._last_received_sequence + 1, question).decode()
                pending = asyncio.create_task(client.post('/stand/raw', json={'frame': raw}))
            else:
                pending = asyncio.create_task(client.post('/stand/text', json=question))
            await asyncio.wait_for(entered.wait(), 2.)
            foreground = app.bounded_s2s.active_tasks[sid]
            assert not foreground.done() and foreground not in stand._response_audio_tasks
            if interrupt == 'client':
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                assert not app.bounded_s2s.active_tasks and not app.bounded_s2s.active_queues
                assert not playback.played
                return
            if interrupt == 'unmarked':
                foreground.cancel('synthetic-unmarked-cancel')
            elif interrupt == 'shutdown':
                await app.shutdown_identity()
                await app.bounded_s2s.cancel_session(sid)
            elif interrupt in {'end', 'reset_new_visit'}:
                def scope():
                    return {'instance_id': stand._panel_instance_id, 'epoch': stand.remote_devices.epoch,
                            'session_id': sid,
                            'context_generation': app._profile_context_generations.get(sid, 0)}
                # The simulator API was constructed before selecting synthetic headless I/O.
                if interrupt == 'end':
                    boundary = await client.post('/stand/simulator/action', json={'action': 'end', **scope()})
                    assert boundary.status_code == 200 and state.mode == 'session_closed'
                else:
                    boundary = await client.post('/stand/reset', json={})
                    assert boundary.status_code == 200
                    started = await client.post('/stand/simulator/action', json={'action': 'start', **scope()})
                    assert started.status_code == 200
                    fresh = app.canonical_runtime.sessions[sid]
                    assert fresh is not state and fresh.mode == 'root_menu' and fresh.handset == 'down'
                    fresh.profile_ref = 'SYNTHETIC_PRIVATE_NEXT_PROFILE'
            if interrupt == 'vad_during_output':
                release.set()
                await asyncio.wait_for(output_started.wait(), 2.)
                assert playback.played and not pending.done()
            if interrupt.startswith('vad_'):
                original_cancel = app.bounded_s2s.cancel_session
                async def observe_cancel(session_id, **kwargs):
                    cancelled_tasks.append(app.bounded_s2s.active_tasks.get(session_id))
                    return await original_cancel(session_id, **kwargs)
                app.bounded_s2s.cancel_session = observe_cancel
                stand._headless_audio_queue = asyncio.Queue(64)
                assert stand._headless_audio_accepting()
                for _ in range(12):
                    stand._enqueue_headless_audio_frame((.25,) * 512)
                stand._headless_audio_queue.put_nowait(None)
                await asyncio.wait_for(stand._headless_audio_worker(), 3.)
                assert foreground in cancelled_tasks
                assert any(event['event'] == 'barge_in' for event in app.bounded_s2s.metrics.events)
                assert not recognized  # Cancellation occurs at confirmed VAD, before STT.
            release.set()
            output_release.set()
            response = await asyncio.wait_for(pending, 3.)
            if interrupt in {'provider', 'unmarked', 'shutdown'}:
                assert response.status_code == 500
                assert 'text_turn_cancelled' not in response.text
                assert not playback.played and not app.bounded_s2s.active_tasks
                assert not app.bounded_s2s.active_queues
                return
            assert response.status_code == 200, response.text
            if interrupt in {'end', 'reset_new_visit'}:
                assert response.json()['reason'] == 'stale_session_generation'
                assert response.json()['outputs'] == [] and not playback.played
                assert 'canonical_state' not in response.json()
                assert 'SYNTHETIC_PRIVATE_NEXT_PROFILE' not in response.text
                assert sid not in app.conversation_contexts and not app._active_audio
                return
            assert state.mode == 'support_intake'
            if interrupt.startswith('vad_'):
                result = response.json()
                assert result['reason'] == 'text_turn_cancelled' and result['outputs'] == []
                assert result['audio_status'] == 'cancelled' and not result['pending']
                assert 'canonical_state' not in result
                assert not playback.played
                assert sid not in app.conversation_contexts or not app.conversation_contexts[sid].history
            else:
                assert playback.played and app.conversation_contexts[sid].turns == 1
    finally:
        release.set()
        output_release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        stand.hardware_mode = 'simulator'
        await stand.aclose()
