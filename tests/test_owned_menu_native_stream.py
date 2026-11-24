"""Owned menu streaming through real application dispatch; synthetic native/PCM only."""
import asyncio
from dataclasses import replace

import pytest

from app.foundation.experimental_voice import ExperimentalTTSProvider
from app.foundation.public_menu_audio import enable_public_menu_audio_cache
from app.foundation.s2s import AudioChunk, PassthroughVoiceProvider
from test_scene_menu_runtime import Harness


class Native(ExperimentalTTSProvider):
    def __init__(self):
        super().__init__({'route': 'vox555', 'limits': {'inference_timeout_s': 2., 'max_text_chars': 1000}})
        self.release = asyncio.Event()
        self.idle = asyncio.Event()
        self.idle.set()
        self.entered = asyncio.Event()
        self.closed_stream = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.calls = []
        self.failure = False
        self.resist = False
        self.chunks = 2

    def configuration_readiness(self):
        return {'provider': 'synthetic-menu-native'}

    async def wait_for_idle(self, cancelled):
        self.calls.append('wait')
        await self.idle.wait()
        if cancelled():
            raise asyncio.CancelledError
        self.calls.append('idle')

    async def _start_worker(self, cancelled):
        raise AssertionError('No native worker may be started')

    async def synthesize_audio_stream(self, text, token, cancelled, *, first_chunk):
        assert first_chunk and not cancelled()
        self.calls.append(('native', text))
        self.entered.set()
        try:
            yield AudioChunk((.1, .2), 16000, token)
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.cancel_seen.set()
                    if not self.resist:
                        raise
            if self.failure:
                raise RuntimeError('SYNTHETIC_PRIVATE_PROVIDER_BODY')
            for _ in range(self.chunks - 1):
                yield AudioChunk((.3, .4), 16000, token)
        finally:
            self.closed_stream.set()


class Sink:
    def __init__(self, h):
        self.h = h
        self.frames = []
        self.tasks = []
        self.first = asyncio.Event()
        self.finished = asyncio.Event()
        self.errors = []
        self.pause = None
        if hasattr(h.app.audio, '_private_stream_play'):
            h.app.audio._private_stream_play = self.play
        else:
            h.app.audio.private_pcm_stream_supported = True
        h.app.audio.play_pcm_stream_owned = self.play

    def play(self, handle, stream, *, on_cue=None):
        async def consume():
            try:
                async for audio, cue in stream:
                    self.frames.append((handle, audio))
                    assert not cue
                    self.first.set()
                    if self.pause is not None:
                        await self.pause.wait()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors.append(type(exc).__name__)
                active = self.h.app._active_audio.get(handle.session_id)
                if active and active['playback_id'] == handle.playback_id:
                    from app.foundation.domain import Event
                    await self.h.app.receive_async(Event('AudioFailed', handle.session_id, 5000, 1.,
                        'synthetic_pcm_sink', {**active, 'response_id': handle.response_id,
                        'error_code': 'synthetic_stream_failure'}, 'stream-failed'))
            finally:
                await stream.aclose()
                self.finished.set()
        self.tasks.append(asyncio.create_task(consume()))


def setup(h=None):
    h = h or Harness()
    native = Native()
    h.app.settings = replace(h.app.settings, voice_conversion='off')
    h.app.bounded_s2s.tts = native
    h.app.bounded_s2s.voice = PassthroughVoiceProvider()
    cache = enable_public_menu_audio_cache(h.app)
    sink = Sink(h)
    return h, native, cache, sink


async def close(h, native, sink, pending=None):
    native.release.set()
    native.idle.set()
    if sink.pause is not None:
        sink.pause.set()
    if pending is not None:
        await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 2.)
    for task in sink.tasks:
        task.cancel()
    await asyncio.wait_for(asyncio.gather(*sink.tasks, return_exceptions=True), 2.)
    await h.close()


@pytest.mark.asyncio
async def test_actual_root_three_first_pcm_precedes_native_eof_and_eof_alone_caches():
    h, native, cache, sink = setup()
    pending = asyncio.create_task(h.button(3))
    try:
        await asyncio.wait_for(native.entered.wait(), 1.)
        await asyncio.wait_for(sink.first.wait(), .2)
        assert not native.closed_stream.is_set() and not native.release.is_set()
        assert h.state.mode == 'personal_story_theme'
        assert [handle.response_id for handle, _ in sink.frames] == ['MENU_STORY_THEMES']
        assert sink.frames[0][1].samples == (.1, .2)
        assert not h.played and cache.snapshot()['entries'] == 0
        assert len(h.app._owned_text_jobs) == 1
        assert (await asyncio.wait_for(pending, 1.))['reason'] == 'button_mode'
        native.release.set()
        await asyncio.wait_for(sink.finished.wait(), 1.)
        assert native.closed_stream.is_set() and not sink.errors
        assert len(sink.frames) == 2 and cache.snapshot()['entries'] == 1
        assert len(h.app._owned_text_jobs) == 0
        await h.complete()
        await h.complete()
        await h.send('SpeechReceived', text='Да')
        assert h.played[-1].response_id == 'MENU_STORY_THEMES'
        assert len([call for call in native.calls if isinstance(call, tuple)]) == 1
        assert len(sink.tasks) == 1 and cache.snapshot()['hits'] == 1
    finally:
        await close(h, native, sink, pending)


@pytest.mark.asyncio
async def test_all_public_titles_and_full_page_validated_before_native_idle_or_pcm():
    h, native, cache, sink = setup()
    validation = h.app.bounded_s2s.response_validator.validate_trusted
    validated = []
    async def validate(text, **kwargs):
        assert not native.calls and not sink.frames
        validated.append(text)
        return await validation(text, **kwargs)
    h.app.bounded_s2s.response_validator.validate_trusted = validate
    try:
        await h.button(2)
        await asyncio.wait_for(sink.first.wait(), 1.)
        from app.foundation.public_menu_audio import menu_validation_text, current_menu_page
        text = h.state.scene_menu_descriptor['spoken_text']
        page = current_menu_page(h.app, h.sid, text)
        assert validated[:-1] == [item.title for item in page.items]
        # Full page may contain only the exact approved Telefon substitution.
        h.app.bounded_s2s.response_validator.validate_trusted = validation
        assert validated[-1] == await menu_validation_text(h.app, page, text)
        assert native.calls[:2] == ['wait', 'idle']
        assert native.calls[2] == ('native', text)
        assert cache.snapshot()['entries'] == 0
    finally:
        await close(h, native, sink)


@pytest.mark.asyncio
async def test_blocked_full_page_validation_never_requests_native_or_pcm():
    from types import SimpleNamespace
    h, native, cache, sink = setup()
    original = h.app.bounded_s2s.response_validator.validate_trusted
    async def validate(text, **kwargs):
        if ' — ' in text:
            return SimpleNamespace(allowed=False)
        return await original(text, **kwargs)
    h.app.bounded_s2s.response_validator.validate_trusted = validate
    try:
        await h.button(3)
        assert h.state.scene_menu_phase == 'ready'
        assert h.state.scene_menu_answers['personal_story_theme'] == 'failed'
        assert not native.calls and not sink.tasks and not h.played
        assert cache.snapshot()['entries'] == 0
    finally:
        await close(h, native, sink)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['idle', 'provider'])
async def test_native_idle_precedes_request_and_provider_replacement_revokes_wait(change):
    h, native, cache, sink = setup()
    native.idle.clear()
    pending = asyncio.create_task(h.button(3))
    try:
        async def waiting():
            while 'wait' not in native.calls:
                await asyncio.sleep(0)
        await asyncio.wait_for(waiting(), 1.)
        assert not sink.tasks and not native.entered.is_set()
        if change == 'provider':
            h.app.bounded_s2s.voice = PassthroughVoiceProvider()
        native.idle.set()
        await asyncio.wait_for(pending, 1.)
        if change == 'provider':
            assert not native.entered.is_set() and not sink.tasks
            assert cache.snapshot()['entries'] == 0
        else:
            await asyncio.wait_for(sink.first.wait(), 1.)
            assert native.calls[:2] == ['wait', 'idle']
    finally:
        await close(h, native, sink, pending)


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['back', 'timeout', 'partial_failure'])
async def test_partial_stream_never_replays_or_caches_after_failure_or_cancel(boundary):
    h, native, cache, sink = setup()
    if boundary == 'timeout':
        h.app.audio.owned_render_timeout_s = .05
    try:
        await h.button(3)
        await asyncio.wait_for(sink.first.wait(), 1.)
        if boundary == 'back':
            native.resist = True
            await h.button(7)
            assert h.state.mode == 'root_menu'
            await asyncio.wait_for(native.cancel_seen.wait(), 1.)
            assert len(h.app._owned_text_jobs) == 1
        elif boundary == 'timeout':
            native.resist = True
            await asyncio.wait_for(native.cancel_seen.wait(), 1.)
            await asyncio.wait_for(sink.finished.wait(), 1.)
            assert len(h.app._owned_text_jobs) == 1
        else:
            native.failure = True
        native.release.set()
        await asyncio.wait_for(sink.finished.wait(), 1.)
        if h.app._owned_text_jobs:
            await asyncio.wait_for(asyncio.gather(*tuple(h.app._owned_text_jobs), return_exceptions=True), 1.)
        assert native.closed_stream.is_set()
        assert len(sink.frames) == 1 and not h.played
        assert cache.snapshot()['entries'] == 0 and not h.app._owned_text_jobs
        if boundary != 'back':
            assert h.state.scene_menu_phase == 'ready'
            assert h.state.scene_menu_answers['personal_story_theme'] == 'failed'
    finally:
        await close(h, native, sink)


@pytest.mark.asyncio
async def test_native_frame_count_and_playback_backpressure_do_not_consume_generation_budget():
    h, native, cache, sink = setup()
    native.chunks = 40  # Native frames must not inherit the 32 semantic-block limit.
    native.release.set()
    h.app.audio.owned_render_timeout_s = .05
    sink.pause = asyncio.Event()
    try:
        await h.button(3)
        await asyncio.wait_for(sink.first.wait(), 1.)
        await asyncio.sleep(.12)  # Longer than generation budget; only playback is blocked.
        assert not native.closed_stream.is_set() and len(h.app._owned_text_jobs) == 1
        assert not sink.errors and len(sink.frames) == 1
        sink.pause.set()
        await asyncio.wait_for(sink.finished.wait(), 1.)
        assert len(sink.frames) == 40 and not sink.errors
        assert native.closed_stream.is_set() and cache.snapshot()['entries'] == 1
    finally:
        await close(h, native, sink)


@pytest.mark.asyncio
@pytest.mark.parametrize('unsupported', ['port', 'rvc', 'non_native'])
async def test_unsupported_stream_retains_existing_buffered_conversion_path(unsupported):
    h, native, cache, sink = setup()
    if unsupported == 'port':
        h.app.audio.private_pcm_stream_supported = False
    elif unsupported == 'rvc':
        h.app.bounded_s2s.voice = h.render
        h.app.settings = replace(h.app.settings, voice_conversion='rvc_v2')
    else:
        h.app.bounded_s2s.tts = h.render
    native.release.set()
    try:
        await h.button(3)
        assert h.played[-1].response_id == 'MENU_STORY_THEMES'
        assert not sink.tasks and not sink.frames
        assert cache.snapshot()['entries'] == 1
        assert bool(h.render.texts) == (unsupported == 'non_native')
    finally:
        await close(h, native, sink)


@pytest.mark.asyncio
async def test_port_closing_before_first_pull_still_closes_native_producer():
    h, native, cache, sink = setup()
    captured = []
    h.app.audio.play_pcm_stream_owned = lambda handle, stream: captured.append(stream)
    try:
        await h.button(3)
        assert len(captured) == 1 and not sink.frames
        await captured[0].aclose()  # Real ports may reject a disconnected binding before iterating.
        await asyncio.wait_for(native.closed_stream.wait(), 1.)
        if h.app._owned_text_jobs:
            await asyncio.wait_for(asyncio.gather(*tuple(h.app._owned_text_jobs), return_exceptions=True), 1.)
        await asyncio.sleep(0)  # Done callbacks remove even an already-finished producer.
        assert not h.app._owned_text_jobs and cache.snapshot()['entries'] == 0
        assert not h.played
    finally:
        await close(h, native, sink)


@pytest.mark.asyncio
async def test_overall_lifetime_bounds_backpressure_without_releasing_resistant_job_early():
    h, native, cache, sink = setup()
    native.chunks = 40
    native.release.set()
    h.app.settings = replace(h.app.settings, playback_timeout_s=.05)
    sink.pause = asyncio.Event()
    try:
        await h.button(3)
        await asyncio.wait_for(sink.first.wait(), 1.)
        await asyncio.wait_for(native.closed_stream.wait(), 1.)
        sink.pause.set()
        await asyncio.wait_for(sink.finished.wait(), 1.)
        assert len(sink.frames) == 1 and cache.snapshot()['entries'] == 0
        assert not h.app._owned_text_jobs and not h.played
    finally:
        await close(h, native, sink)


@pytest.mark.asyncio
async def test_two_cancel_resistant_jobs_keep_admission_closed_until_actual_finish():
    h, native, cache, sink = setup()
    native.resist = True
    try:
        await h.button(3)
        await asyncio.wait_for(sink.first.wait(), 1.)
        # Exercise the application admission cap directly, not simultaneous device owners.
        effect = {'content_id': 'MENU_STORY_THEMES', 'playback_lease': h.state.playback_lease}
        text = h.state.scene_menu_descriptor['spoken_text']
        token = h.app.sessions.get(h.sid).cancellation_token
        assert await h.app._stream_menu_audio(h.sid, text, effect, token, lambda: True) is True
        assert len(h.app._owned_text_jobs) == 2
        for task in tuple(h.app._owned_text_jobs):
            task.cancel()
        await asyncio.wait_for(native.cancel_seen.wait(), 1.)
        result = await h.app._stream_menu_audio(h.sid, text, effect, token, lambda: True)
        assert result is False and len(native.calls) == 6
        assert len(h.app._owned_text_jobs) == 2 and cache.snapshot()['entries'] == 0
        native.release.set()
        await asyncio.wait_for(asyncio.gather(*tuple(h.app._owned_text_jobs), return_exceptions=True), 1.)
        await asyncio.sleep(0)
        assert not h.app._owned_text_jobs
    finally:
        await close(h, native, sink)


@pytest.mark.asyncio
async def test_fresh_actual_max_profile_revokes_stream_before_late_native_pcm(tmp_path):
    from test_panel_session_actions import PanelStand
    from test_card_doctors_profile import doctor_card
    h, native, cache, sink = setup(PanelStand(tmp_path, identity=True))
    native.resist = True
    try:
        await h.ready()
        await h.control('start')
        await h.control('enable_audio', {**h.scope(), 'state_version': h.state.state_version})
        await h.key(3)
        await asyncio.wait_for(sink.first.wait(), 1.)
        old_state = h.state
        h.client.value = doctor_card(['SYNTHETIC NEW SPECIALIST'])
        h.client.events.append({'kind': 'patient_ready'})
        assert (await h.boundary.step())['reason'] == 'profile_ready'
        assert h.state is not old_state and h.state.substate == 'profile_ready'
        fresh_request = h.state.identity_request_id
        assert fresh_request
        await asyncio.wait_for(native.cancel_seen.wait(), 1.)
        # The synthetic provider resists repeated cleanup cancellation until release.
        assert len(h.app._owned_text_jobs) == 1 and not native.closed_stream.is_set()
        assert len(sink.frames) == 1 and cache.snapshot()['entries'] == 0
        native.release.set()
        await asyncio.wait_for(sink.finished.wait(), 1.)
        if h.app._owned_text_jobs:
            await asyncio.wait_for(asyncio.gather(*tuple(h.app._owned_text_jobs), return_exceptions=True), 1.)
        assert len(sink.frames) == 1 and cache.snapshot()['entries'] == 0
        assert h.state.identity_request_id == fresh_request
        assert h.app._active_audio.get('stand', {}).get('playback_id') != sink.frames[0][0].playback_id
        assert not h.app._owned_text_jobs
    finally:
        await close(h, native, sink)
