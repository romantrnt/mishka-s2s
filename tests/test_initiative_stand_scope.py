"""Stand capture ownership, with speaker and initiative state deliberately synthetic."""
import pytest
from test_headless_turn_taking import active_stand, close


@pytest.mark.asyncio
async def test_queued_physical_pcm_cannot_adopt_a_later_dialogue_proposal(monkeypatch):
    stand, _ = await active_stand(monkeypatch)
    app, sid = stand.application, stand.active_session_id
    scope = [id(app.canonical_runtime.sessions[sid]), 0, None, None, False]
    app._dialogue_initiative_scope = lambda current_sid: tuple(scope) if current_sid == sid else None
    try:
        old = stand._headless_frame_owner()
        assert old[1]()
        stand._enqueue_headless_audio_frame((.2,) * 512, old)
        scope[2:] = ['new-proposal', 'offer', True]
        assert old[1].lifecycle_current(), 'the visit itself remains current'
        assert old[1](), 'admitted same-visit speech must remain decodable for urgent content'
        assert old[1].initiative_scope[2] is None, 'old speech must not acquire the new proposal'
        delivered = []
        async def inject(*args, **kwargs):
            delivered.append(kwargs['_ingress_guard'].initiative_scope)
            return {}
        stand.inject_pcm = inject
        await stand._headless_audio_queue.put(None)
        await stand._headless_audio_worker()
        assert len(delivered) == 1 and delivered[0][2] is None
        fresh = stand._headless_frame_owner()
        assert fresh[1]()
        assert fresh[1].initiative_scope == tuple(scope)
        scope[2:] = ['replacement', 'offer', True]
        assert fresh[1]() and fresh[1].initiative_scope[2] == 'new-proposal'
    finally:
        await close(stand)


@pytest.mark.asyncio
async def test_browser_upload_guard_preserves_the_current_proposal_scope(monkeypatch):
    stand, _ = await active_stand(monkeypatch)
    app, sid = stand.application, stand.active_session_id
    scope = [id(app.canonical_runtime.sessions[sid]), 0, 'current', 'offer', True]
    app._dialogue_initiative_scope = lambda current_sid: tuple(scope) if current_sid == sid else None
    try:
        guard = stand._audio_owner_guard(sid)
        assert guard() and guard.initiative_scope == tuple(scope)
        scope[-1] = False
        assert guard() and guard.lifecycle_current(), 'urgent speech retains its visit owner'
        assert guard.initiative_scope[-1] is True, 'the old admission snapshot remains immutable'
        scope[2:] = [None, None, False]
        ordinary = stand._audio_owner_guard(sid)
        assert ordinary()
        scope[2:] = ['new-proposal', 'offer', True]
        assert ordinary(), 'visit ownership must survive a proposal-only change'
        assert ordinary.initiative_scope[2] is None, 'an upload without an offer cannot acquire one later'
    finally:
        await close(stand)


@pytest.mark.asyncio
async def test_owned_browser_completion_cannot_overtake_final_pcm_delivery():
    """A real HTTP ACK held ahead of the socket send is not a completed offer."""
    import asyncio
    import httpx
    from test_browser_private_pcm import setup_browser, cleanup, welcome

    api, stand, boundary, _, clock, writer, _ = await setup_browser()
    held, release = asyncio.Queue(), asyncio.Event()
    original = writer.send_json
    async def delayed_send(payload):
        if payload.get('type') == 'audio_pcm_output' and payload.get('final'):
            await held.put(payload['playback'])
            await release.wait()
        await original(payload)
    writer.send_json = delayed_send
    pending = asyncio.create_task(welcome(stand, clock))
    try:
        identity = await asyncio.wait_for(held.get(), 3)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url='http://stand',
                headers={'x-stand-token': 'private-pcm-unit-test'}) as client:
            reply = await client.post('/stand/audio/completed', json=identity)
            assert reply.json()['reason'] == 'stale_private_pcm_callback', 'the final PCM was not delivered'
            release.set()
            await asyncio.wait_for(pending, 3)
            reply = await client.post('/stand/audio/completed', json=identity)
            assert reply.json()['reason'] == 'personal_welcome_menu_started'
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await cleanup(stand, boundary)


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['played_then_urgent', 'help_while_capture_closed'])
async def test_owned_offer_physical_style_output_epoch_and_capture_boundaries(monkeypatch, boundary):
    """Real capture callback/queue, UART help and app safety; synthetic PCM/STT/speaker."""
    import asyncio
    from app.foundation.s2s import STTResult
    from app.foundation.stand import HeadlessAudioOutput, HEADLESS_OUTPUT_TAIL_S
    from test_unavailable_activity_app import conversation
    from test_dialogue_initiative_app import model, proposal, content

    h = await conversation()
    stand, calls = await active_stand(monkeypatch)
    original = stand.application
    stand.application, stand.active_session_id = h.app, h.sid
    stand.audio_output = HeadlessAudioOutput()
    stand.audio_output.on_pcm_play = stand._headless_pcm_play
    stand.audio_output.on_cancel = lambda token: stand._cancel_response_audio()
    h.app.audio = stand.audio_output
    h.app.bounded_s2s.playback = stand.playback
    recognized = []
    class Recognizer:
        text = 'Давай'
        async def transcribe(self, audio, cancelled):
            recognized.append(audio.samples)
            return STTResult(self.text, .99, 0., False, {})
    recognizer = Recognizer()
    h.app.s2s_audio.recognizer = recognizer
    release = None
    try:
        model(h, proposal(), content())
        old = stand._headless_frame_owner()
        assert old[1]() and old[1].initiative_scope[2] is None
        stand._enqueue_headless_audio_frame((.5,) * 512, old)
        await h.send('SpeechReceived', text='Мне нравится лес')
        release = await asyncio.wait_for(calls.get(), 3)
        assert h.state.active_audio_id == 'INITIATIVE_OFFER'
        assert not stand._headless_audio_accepting()
        assert not h.state.pending_initiative.ready
        assert 5 in stand.panel_snapshot()['session']['enabled_buttons']
        denied = stand._headless_frame_owner()
        before = stand._headless_audio_queue.qsize()
        stand._enqueue_headless_audio_frame((.5,) * 512, denied)
        assert stand._headless_audio_queue.qsize() == before
        if boundary == 'help_while_capture_closed':
            result = await stand.inject('button', {'button_id': 5, 'state': 'pressed'}, h.sid)
            await stand.inject('button_released', {'button_id': 5, 'state': 'released'}, h.sid)
            assert not result.get('error') and h.state.pending_initiative is None
            assert h.state.active_audio_id != 'INITIATIVE_OFFER'
            assert not recognized and len(h.model.calls) == 1
            return
        release.set()
        await asyncio.gather(*tuple(stand._response_audio_tasks))
        await asyncio.sleep(HEADLESS_OUTPUT_TAIL_S + .02)
        assert h.state.pending_initiative.ready and stand._headless_audio_accepting()
        assert old[1].lifecycle_current() and not old[1]() and not denied[1]()
        stand._headless_audio_queue.put_nowait(None)
        await stand._headless_audio_worker()
        assert not recognized and len(h.model.calls) == 1, 'old output-epoch speech cannot accept the later offer'
        recognizer.text = 'Меня бьют.'
        stand._headless_audio_queue = asyncio.Queue()
        for _ in range(14): stand._enqueue_headless_audio_frame((.5,) * 512)
        for _ in range(20): stand._enqueue_headless_audio_frame((0.,) * 512)
        stand._headless_audio_queue.put_nowait(None)
        await asyncio.wait_for(stand._headless_audio_worker(), 3)
        pending = tuple(h.app._audio_completions.get(h.sid, ()))
        if pending: await asyncio.wait_for(asyncio.gather(*pending), 3)
        assert len(recognized) == 1
        assert h.state.mode == 'safety_stop' and h.state.safety_severity == 'urgent'
        assert h.state.pending_initiative is None and h.state.game_engine_state is None
    finally:
        if release is not None: release.set()
        for task in tuple(stand._response_audio_tasks): task.cancel()
        await asyncio.gather(*tuple(stand._response_audio_tasks), return_exceptions=True)
        await h.close()
        stand.application = original
        await close(stand)
