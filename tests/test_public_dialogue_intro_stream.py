"""Exact authored INTRO, real app/owned stream; synthetic native frames only."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.foundation.s2s import PassthroughVoiceProvider
from test_dialogue_entry_feedback import hardware_lighting
from test_owned_menu_native_stream import setup, close


INTRO = 'Давай поговорим. Чтобы вернуться в меню, нажми семь.'


def validation_calls(h):
    validator = h.app.bounded_s2s.response_validator
    trusted, semantic = [], []
    trusted_original, semantic_original = validator.validate_trusted, validator.validate
    async def public(text, **kwargs):
        trusted.append(text)
        return await trusted_original(text, **kwargs)
    async def ordinary(text, **kwargs):
        semantic.append(text)
        return await semantic_original(text, **kwargs)
    validator.validate_trusted, validator.validate = public, ordinary
    return trusted, semantic


@pytest.mark.asyncio
async def test_exact_intro_first_pcm_precedes_blocked_native_eof_without_cache_or_mini():
    h, native, cache, sink = setup()
    hardware_lighting(h)
    trusted, semantic = validation_calls(h)
    pending = asyncio.create_task(h.button(6))
    try:
        await asyncio.wait_for(native.entered.wait(), 1.)
        await asyncio.wait_for(sink.first.wait(), .2)
        assert not native.release.is_set() and not native.closed_stream.is_set()
        assert trusted == [INTRO] and not semantic
        assert h.app.runtime_status['dialogue_lighting_phase'] == 'output'
        assert h.state.mode == 'bear_dialogue' and h.state.active_audio_id == 'DIALOGUE_INTRO'
        assert [handle.response_id for handle, _ in sink.frames] == ['DIALOGUE_INTRO']
        assert sink.frames[0][1].samples == (.1, .2)
        assert (await asyncio.wait_for(pending, 1.))['reason'] == 'bear_dialogue_started'
        assert not h.played and cache.snapshot()['entries'] == 0
        native.release.set()
        await asyncio.wait_for(sink.finished.wait(), 1.)
        assert len(sink.frames) == 2 and not sink.errors and not h.app._owned_text_jobs
        assert cache.snapshot()['entries'] == 0
        assert h.app.runtime_status['dialogue_lighting_phase'] == 'output'
        await h.complete()  # Real owned terminal, not first-frame readiness.
        assert h.app.runtime_status['dialogue_lighting_phase'] == 'listening'
        assert h.state.active_audio_id is None
    finally:
        await close(h, native, sink, pending)


@pytest.mark.asyncio
async def test_blocked_trusted_intro_never_requests_native_or_replays_whole_audio():
    h, native, cache, sink = setup()
    native.release.set()
    trusted, semantic = [], []
    async def blocked(text, **kwargs):
        trusted.append(text)
        return SimpleNamespace(allowed=False)
    async def forbidden(*args, **kwargs):
        semantic.append(args)
        raise AssertionError('A rejected public stream must not replay through MiniLLM')
    h.app.bounded_s2s.response_validator.validate_trusted = blocked
    h.app.bounded_s2s.response_validator.validate = forbidden
    try:
        result = await h.button(6)
        assert trusted == [INTRO] and not semantic
        assert not native.calls and not sink.frames and not sink.tasks and not h.played
        assert h.state.mode == 'bear_dialogue' and h.state.active_audio_id is None
        assert cache.snapshot()['entries'] == 0
        assert result['audio_origin']['error_code'] == 'dialogue_prompt_unavailable'
        assert result['reason'] == 'dialogue_prompt_finished'
    finally:
        await close(h, native, sink)


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['back', 'hangup'])
async def test_cancel_resistant_intro_cannot_emit_late_frames_or_replace_new_owner(boundary):
    h, native, cache, sink = setup()
    h.app.canonical_runtime.exhibition_mode = True
    native.resist = True
    try:
        await h.button(6)
        await asyncio.wait_for(sink.first.wait(), 1.)
        if boundary == 'back':
            await h.button(7)
        else:
            await h.send('HandsetPlaced')
        fresh = dict(h.app._active_audio.get(h.sid) or {})
        await asyncio.wait_for(native.cancel_seen.wait(), 1.)
        assert len(h.app._owned_text_jobs) == 1 and len(sink.frames) == 1
        native.release.set()
        await asyncio.wait_for(sink.finished.wait(), 1.)
        if h.app._owned_text_jobs:
            await asyncio.wait_for(asyncio.gather(*tuple(h.app._owned_text_jobs), return_exceptions=True), 1.)
        assert len(sink.frames) == 1 and not h.played and not h.app._owned_text_jobs
        assert h.app._active_audio.get(h.sid, {}) == fresh
        if boundary == 'back':
            assert h.state.mode == 'root_menu'
        else:
            assert h.sid not in h.app.canonical_runtime.sessions
        assert cache.snapshot()['entries'] == 0
    finally:
        await close(h, native, sink)


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['hangup', 'provider'])
async def test_delayed_first_frame_rechecks_current_and_provider_before_request(boundary):
    h, native, cache, sink = setup()
    h.app.canonical_runtime.exhibition_mode = True
    hardware_lighting(h)
    native.idle.clear()
    pending = asyncio.create_task(h.button(6))
    try:
        async def waiting():
            while 'wait' not in native.calls:
                await asyncio.sleep(0)
        await asyncio.wait_for(waiting(), 1.)
        assert h.app.runtime_status['dialogue_lighting_phase'] == 'preparing'
        assert not sink.tasks and not native.entered.is_set()
        if boundary == 'hangup':
            await h.send('HandsetPlaced')
        else:
            h.app.bounded_s2s.voice = PassthroughVoiceProvider()
        native.idle.set()
        await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 1.)
        assert not native.entered.is_set() and not sink.frames and not h.played
        assert cache.snapshot()['entries'] == 0
    finally:
        await close(h, native, sink, pending)


@pytest.mark.asyncio
async def test_two_cancel_resistant_intro_jobs_hold_admission_until_actual_completion():
    h, native, cache, sink = setup()
    native.resist = True
    try:
        await h.button(6)
        await asyncio.wait_for(sink.first.wait(), 1.)
        # Resource-cap seam only; there are not two concurrent physical owners.
        effect = {'content_id': 'DIALOGUE_INTRO', 'playback_lease': h.state.playback_lease}
        token = h.app.sessions.get(h.sid).cancellation_token
        assert await h.app._stream_menu_audio(h.sid, INTRO, effect, token, lambda: True) is True
        assert len(h.app._owned_text_jobs) == 2
        for job in tuple(h.app._owned_text_jobs):
            job.cancel()
        await asyncio.wait_for(native.cancel_seen.wait(), 1.)
        assert await h.app._stream_menu_audio(h.sid, INTRO, effect, token, lambda: True) is False
        assert len(h.app._owned_text_jobs) == 2 and len(native.calls) == 6
        native.release.set()
        await asyncio.wait_for(asyncio.gather(*tuple(h.app._owned_text_jobs), return_exceptions=True), 1.)
        assert not h.app._owned_text_jobs and cache.snapshot()['entries'] == 0
    finally:
        await close(h, native, sink)


@pytest.mark.asyncio
@pytest.mark.parametrize('unsupported', ['port', 'rvc', 'non_native', 'text_mismatch'])
async def test_unsupported_or_changed_intro_keeps_existing_semantic_whole_path(unsupported):
    h, native, cache, sink = setup()
    native.release.set()
    trusted, semantic = validation_calls(h)
    expected = INTRO
    if unsupported == 'port':
        h.app.audio.private_pcm_stream_supported = False
    elif unsupported == 'rvc':
        h.app.settings = replace(h.app.settings, voice_conversion='rvc_v2')
        h.app.bounded_s2s.voice = h.render
    elif unsupported == 'non_native':
        h.app.bounded_s2s.tts = h.render
    else:
        expected += ' Ещё.'
        original = h.app.canonical_runtime._dialogue_prompt
        h.app.canonical_runtime._dialogue_prompt = lambda content_id, text: original(content_id, expected)
    try:
        await h.button(6)
        assert semantic == [expected] and not trusted
        assert h.played[-1].response_id == 'DIALOGUE_INTRO'
        assert not sink.tasks and not sink.frames and cache.snapshot()['entries'] == 0
    finally:
        await close(h, native, sink)


@pytest.mark.asyncio
@pytest.mark.parametrize('content_id', ['DIALOGUE_RETURN', 'DIALOGUE_OFFER', 'TYPED_WHAT_NEXT', 'TYPED_CARD_DOCTORS'])
async def test_other_owned_speech_does_not_enter_the_public_intro_exception(content_id):
    h, native, cache, sink = setup()
    trusted, semantic = validation_calls(h)
    try:
        effect = {'content_id': content_id, 'playback_lease': 'synthetic-lease'}
        assert await h.app._stream_menu_audio(h.sid, INTRO, effect, 1, lambda: True) is None
        assert not native.calls and not trusted and not semantic and not h.app._owned_text_jobs
        assert cache.snapshot()['entries'] == 0
    finally:
        await close(h, native, sink)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['after_first_pcm', 'generation_budget', 'port_rejected'])
async def test_intro_owned_failure_clears_lease_without_replay_and_keeps_button_exit(failure):
    h, native, cache, sink = setup()
    hardware_lighting(h)
    trusted, semantic = validation_calls(h)
    failed_events = []
    receive = h.app.receive_async
    async def record(event, *args, **kwargs):
        if event.event_type == 'AudioFailed':
            failed_events.append(dict(event.payload))
        return await receive(event, *args, **kwargs)
    h.app.receive_async = record
    if failure == 'generation_budget':
        h.app.audio.owned_render_timeout_s = .05
        native.resist = True
    elif failure == 'port_rejected':
        def reject(*args, **kwargs):
            raise RuntimeError('SYNTHETIC_PORT_REJECTION')
        h.app.audio.play_pcm_stream_owned = reject
    try:
        result = await h.button(6)
        if failure != 'port_rejected':
            await asyncio.wait_for(sink.first.wait(), 1.)
            assert h.state.active_audio_id == 'DIALOGUE_INTRO'
            if failure == 'after_first_pcm':
                native.failure = True
                native.release.set()
            else:
                await asyncio.wait_for(native.cancel_seen.wait(), 1.)
            await asyncio.wait_for(sink.finished.wait(), 1.)
            assert failed_events and failed_events[-1]['response_id'] == 'DIALOGUE_INTRO'
            assert sink.errors == ['RuntimeError'] and len(sink.frames) == 1
            native.release.set()
        else:
            assert result['audio_origin']['error_code'] == 'dialogue_prompt_unavailable'
            assert result['reason'] == 'dialogue_prompt_finished'
            assert not sink.frames and not sink.tasks
        if h.app._owned_text_jobs:
            await asyncio.wait_for(asyncio.gather(*tuple(h.app._owned_text_jobs), return_exceptions=True), 1.)
        assert trusted == [INTRO] and not semantic and not h.played
        assert cache.snapshot()['entries'] == 0 and not h.app._active_audio
        assert h.state.mode == 'bear_dialogue' and h.state.active_audio_id is None
        assert h.app.runtime_status['dialogue_lighting_phase'] == 'listening'
        assert 'button_7' in h.state.expected_inputs
        assert 'SYNTHETIC' not in repr(h.app.last_canonical_effects)
        await h.button(7)
        assert h.state.mode == 'root_menu' and not h.app._active_audio
    finally:
        await close(h, native, sink)


@pytest.mark.asyncio
async def test_new_synthetic_profile_revokes_intro_before_cancel_resistant_late_pcm(tmp_path):
    from test_panel_session_actions import PanelStand
    from test_card_doctors_profile import doctor_card
    h, native, cache, sink = setup(PanelStand(tmp_path, identity=True))
    native.resist = True
    played = []
    h.app.audio.play_pcm_owned = lambda handle, audio: played.append(handle)
    try:
        await h.ready()
        await h.control('start')
        await h.control('enable_audio', {**h.scope(), 'state_version': h.state.state_version})
        await h.key(6)
        await asyncio.wait_for(sink.first.wait(), 1.)
        old_state = h.state
        h.client.value = doctor_card(['SYNTHETIC NEW SPECIALIST'])
        h.client.events.append({'kind': 'patient_ready'})
        assert (await h.boundary.step())['reason'] == 'profile_ready'
        assert h.state is not old_state and h.state.substate == 'profile_ready'
        fresh_request = h.state.identity_request_id
        fresh_owner = dict(h.app._active_audio.get('stand') or {})
        await asyncio.wait_for(native.cancel_seen.wait(), 1.)
        assert len(h.app._owned_text_jobs) == 1 and len(sink.frames) == 1
        native.release.set()
        await asyncio.wait_for(sink.finished.wait(), 1.)
        if h.app._owned_text_jobs:
            await asyncio.wait_for(asyncio.gather(*tuple(h.app._owned_text_jobs), return_exceptions=True), 1.)
        assert len(sink.frames) == 1 and not played and not h.app._owned_text_jobs
        assert h.state.identity_request_id == fresh_request
        assert h.app._active_audio.get('stand', {}) == fresh_owner
        assert cache.snapshot()['entries'] == 0
    finally:
        await close(h, native, sink)


@pytest.mark.asyncio
async def test_old_internal_intro_failure_cannot_flatten_fresh_bear_turn_processing():
    h, native, cache, sink = setup()
    hardware_lighting(h)
    terminal_ready, release_terminal = asyncio.Event(), asyncio.Event()
    processing, release_turn = asyncio.Event(), asyncio.Event()
    original = h.app._receive_canonical_event
    async def terminal_gate(event, *args, **kwargs):
        result = await original(event, *args, **kwargs)
        if event.event_type == 'AudioFailed' and result.get('reason') == 'dialogue_prompt_finished':
            terminal_ready.set()
            await release_terminal.wait()
        return result
    h.app._receive_canonical_event = terminal_gate
    async def turn(*args, **kwargs):
        await h.app._set_dialogue_lighting_phase(h.sid, 'processing')
        processing.set()
        await release_turn.wait()
        return SimpleNamespace(fallback_reason=None, return_to_scenario=False, actions=(), chunks=())
    h.app.bounded_s2s.run_text_turn = turn
    def reject(*args, **kwargs):
        raise RuntimeError('SYNTHETIC_PORT_REJECTION')
    h.app.audio.play_pcm_stream_owned = reject
    pending = asyncio.create_task(h.button(6))
    fresh = None
    try:
        await asyncio.wait_for(terminal_ready.wait(), 1.)
        old_version = h.state.state_version
        fresh = asyncio.create_task(h.send('SpeechReceived', text='Привет'))
        await asyncio.wait_for(processing.wait(), 1.)
        assert h.state.state_version > old_version and h.state.active_audio_id is None
        release_terminal.set()
        await asyncio.wait_for(pending, 1.)
        assert h.app.runtime_status['dialogue_lighting_phase'] == 'processing'
        assert h.app.led.exhibition_effects.effect == 'wave'
        assert not h.played and not sink.frames and cache.snapshot()['entries'] == 0
    finally:
        release_terminal.set(); release_turn.set()
        if fresh is not None:
            await asyncio.wait_for(asyncio.gather(fresh, return_exceptions=True), 1.)
        await close(h, native, sink, pending)


@pytest.mark.asyncio
async def test_cancel_resistant_trusted_validation_cannot_start_tts_after_hangup():
    h, native, cache, sink = setup()
    h.app.canonical_runtime.exhibition_mode = True
    entered, release = asyncio.Event(), asyncio.Event()
    async def delayed(*args, **kwargs):
        entered.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass
        return SimpleNamespace(allowed=True)
    h.app.bounded_s2s.response_validator.validate_trusted = delayed
    pending = asyncio.create_task(h.button(6))
    try:
        await asyncio.wait_for(entered.wait(), 1.)
        await h.send('HandsetPlaced')
        release.set()
        await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 1.)
        assert not native.calls and not sink.tasks and not h.played
        assert h.sid not in h.app.canonical_runtime.sessions and not h.app._active_audio
        assert cache.snapshot()['entries'] == 0
    finally:
        release.set()
        await close(h, native, sink, pending)


@pytest.mark.asyncio
async def test_sync_rejected_old_browser_binding_does_not_fail_replacement_intro_owner():
    from app.foundation.canonical_contract import CanonicalState
    h, native, cache, sink = setup()
    replacement = CanonicalState(h.sid, mode='bear_dialogue', handset='up', age_band='3-5')
    replacement.active_audio_id, replacement.playback_lease = 'DIALOGUE_INTRO', 'new-intro-lease'
    def reject_stale(*args, **kwargs):
        h.app.canonical_runtime.sessions[h.sid] = replacement
        raise RuntimeError('SYNTHETIC_STALE_BROWSER_BINDING')
    h.app.audio.play_pcm_stream_owned = reject_stale
    try:
        result = await h.button(6)
        assert result['reason'] == 'stale_session_generation'
        assert h.state is replacement and h.state.playback_lease == 'new-intro-lease'
        assert h.state.active_audio_id == 'DIALOGUE_INTRO'
        assert not h.played and not sink.frames and cache.snapshot()['entries'] == 0
        assert 'audio_origin' not in result
    finally:
        await close(h, native, sink)
