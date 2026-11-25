"""Safe root4 entry uses current owned speech and real binary light frames."""
import asyncio
from dataclasses import replace
from importlib.machinery import ModuleSpec
from types import SimpleNamespace
import sys

import pytest

from app.foundation.domain import Event
from app.foundation.s2s import AudioChunk, SoundDevicePlaybackController
from app.foundation.validation import ValidationDecision
from test_contextual_game_flow import Model
from test_dialogue_entry_feedback import hardware_lighting
from test_owned_menu_native_stream import setup as native_setup, close as native_close
from test_owned_private_pcm import setup as pcm_setup, install, HANDLE
from test_panel_session_actions import PanelStand
from test_public_dialogue_intro_stream import INTRO, validation_calls
from test_scene_menu_runtime import Harness
from test_stage1_nested_game_integrity import SyntheticVoice


def support(mode='root_menu'):
    h = Harness()
    hardware_lighting(h)
    h.app.canonical_runtime.exhibition_mode = mode != 'root_menu'
    h.state.mode = mode
    h.render = SyntheticVoice()
    h.app.bounded_s2s.tts = h.app.bounded_s2s.voice = h.render
    h.app.bounded_s2s.llm = Model('<say>У кота мягкая шерсть.</say>')
    return h


def frame(h):
    return h.app.led.output.snapshot()


def assert_support_channels(h):
    values = frame(h)
    assert set(values) == {str(i) for i in range(18)}
    assert set(values.values()) <= {0, 1}
    assert values['7'] == values['0'] == 1  # Real help5/back7 digits.
    assert all(values[str(i)] == 0 for i in set(range(18)) - {9, 6, 1, 7, 0})


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['root_menu', 'menu_ready', 'exhibition_menu_tutorial_buttons'])
async def test_entry_before_validator_and_tts_then_owned_terminal(mode):
    h = support(mode)
    entered, allow = asyncio.Event(), asyncio.Event()
    h.render.block = asyncio.Event()
    original = h.app.bounded_s2s.response_validator.validate
    async def validate(*args, **kwargs):
        entered.set()
        await allow.wait()
        return await original(*args, **kwargs)
    h.app.bounded_s2s.response_validator.validate = validate
    pending = asyncio.create_task(h.button(4))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert h.state.mode == 'support_intake' and h.state.safety_severity == 'none'
        assert {'speech', 'button_5', 'button_7'} <= set(h.state.expected_inputs)
        assert h.app.runtime_status['dialogue_lighting_phase'] == 'preparing'
        assert h.app.led.effect_snapshot()['effect'] == 'wave'
        assert_support_channels(h)
        assert not h.render.texts and not h.played
        allow.set()
        await asyncio.wait_for(h.render.entered.wait(), 2)
        assert h.render.texts == [INTRO] and not h.played
        h.render.block.set()
        await pending
        assert h.played[-1].response_id == 'DIALOGUE_INTRO'
        assert h.app.runtime_status['dialogue_lighting_phase'] == 'output'
        assert {int(i) for i, value in frame(h).items() if value} == {9, 7, 0}
        h.app.led.advance_effects(h.app.monotonic_now() + 2.)
        assert {int(i) for i, value in frame(h).items() if value} == {9, 7, 0}
        await h.complete()
        assert h.state.active_audio_id is None
        assert h.app.runtime_status['dialogue_lighting_phase'] == 'listening'
        assert h.app.led.effect_snapshot()['effect'] == 'breathing'
        assert_support_channels(h)
    finally:
        allow.set(); h.render.block.set()
        await asyncio.gather(pending, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('identity,up', [(False, False), (False, True), (True, False), (True, True)])
async def test_actual_four_entries_and_explicit_audio_enable_keep_hook_and_buttons(tmp_path, identity, up):
    h = PanelStand(tmp_path, identity=identity)
    try:
        if identity:
            await h.ready()
        if up:
            await h.simulator.action({'action': 'handset', 'state': 'up'})
        await h.control('start'); await h.menu(); await h.key(4)
        if not up:
            assert h.state.mode == 'awaiting_handset' and not h.state.audio_open
            result = await h.simulator.action({'action': 'enable_audio', **h.scope(),
                'state_version': h.state.state_version})
            assert result['reason'] == 'panel_audio_pending_started'
        assert h.state.mode == 'support_intake' and h.state.handset == ('up' if up else 'down')
        assert h.state.active_audio_id == 'DIALOGUE_INTRO'
        assert set(h.stand.panel_snapshot()['session']['enabled_buttons']) == {5, 7}
        assert {'button_5', 'button_7'} <= set(h.state.expected_inputs)
        await h.done()
        assert h.state.active_audio_id is None and h.state.mode == 'support_intake'
        await h.key(7)
        assert h.state.mode == 'root_menu'
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_pending_handset_lift_plays_the_same_public_intro():
    h = support()
    try:
        h.state.handset = 'down'
        assert (await h.button(4))['reason'] == 'handset_required'
        assert h.state.mode == 'awaiting_handset' and not h.played
        await h.release(4)
        await h.send('HandsetLifted')
        assert h.state.mode == 'support_intake' and h.state.active_audio_id == 'DIALOGUE_INTRO'
        assert h.render.texts == [INTRO]
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('failed', [False, True])
async def test_current_terminal_keeps_buttons_and_old_intro_cannot_finish_a_fact(failed):
    h = support()
    try:
        await h.button(4); await h.release(4)
        old = dict(h.app._active_audio[h.sid])
        result = await h.complete(failed=failed)
        assert result['reason'] == 'dialogue_prompt_finished'
        assert h.state.mode == 'support_intake' and h.state.active_audio_id is None
        assert {'button_5', 'button_7'} <= set(h.state.expected_inputs)
        assert h.app.runtime_status['dialogue_lighting_phase'] == 'listening'
        await h.send('SpeechReceived', text='Какое сегодня число?')
        current, before = dict(h.app._active_audio[h.sid]), frame(h)
        await h.send('AudioCompleted', {**old, 'response_id': old['content_id']})
        assert h.app._active_audio[h.sid] == current and frame(h) == before
        assert h.state.dialogue_information_kind == 'date'
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['back', 'help', 'hangup', 'concern', 'fact'])
async def test_preparing_intro_does_not_block_actions_or_play_after_replacement(action):
    h = support()
    h.render.block = asyncio.Event()
    pending = asyncio.create_task(h.button(4))
    try:
        await asyncio.wait_for(h.render.entered.wait(), 2)
        h.app.bounded_s2s.tts = h.app.bounded_s2s.voice = SyntheticVoice()
        if action == 'back':
            await h.button(7)
        elif action == 'help':
            await h.button(5)
        elif action == 'hangup':
            await h.send('HandsetPlaced')
        else:
            await h.send('SpeechReceived', text='Мне больно' if action == 'concern' else 'Какое сегодня число?')
        current = dict(h.app._active_audio.get(h.sid) or {})
        before = frame(h)
        h.render.block.set()
        await asyncio.gather(pending, return_exceptions=True)
        assert not any(handle.response_id == 'DIALOGUE_INTRO' for handle in h.played)
        assert dict(h.app._active_audio.get(h.sid) or {}) == current and frame(h) == before
        if action == 'help':
            assert h.state.mode == 'wellbeing_check' and h.state.active_audio_id == 'PHRASE_084'
        elif action == 'concern':
            assert h.state.mode == 'support_intake' and h.state.safety_severity == 'concern'
        elif action == 'fact':
            assert h.state.dialogue_information_kind == 'date'
    finally:
        h.render.block.set()
        await asyncio.gather(pending, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_real_bounded_turn_owns_processing_output_and_listening_frames():
    h = support()
    phases = []
    original = h.app._set_dialogue_lighting_phase
    async def observed(sid, phase, **kwargs):
        result = await original(sid, phase, **kwargs)
        if result is not None:
            phases.append((phase, frame(h)))
        return result
    h.app._set_dialogue_lighting_phase = observed
    try:
        await h.button(4); await h.complete(); phases.clear()
        result = await h.send('SpeechReceived', text='Расскажи про кота')
        assert result['reason'] == 'support_dialogue_turn'
        assert [phase for phase, _ in phases] == ['processing', 'output', 'listening']
        assert not h.app._support_lighting_owners
        assert {int(i) for i, value in phases[1][1].items() if value} == {9, 7, 0}
        assert_support_channels(h)
        h.app.led.advance_effects(h.app.led.exhibition_effects.started_at_monotonic + 1.01)
        assert frame(h)['9'] == 0 and frame(h)['7'] == frame(h)['0'] == 1
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_pending_fact_owns_light_and_rejects_old_bounded_lifecycle():
    h = support()
    entered, release = asyncio.Event(), asyncio.Event()
    original_weather = h.app.schedule_weather.weather
    async def weather(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original_weather(*args, **kwargs)
    h.app.schedule_weather.weather = weather
    pending = None
    try:
        await h.button(4); await h.complete()
        pending = asyncio.create_task(h.send('SpeechReceived', text='Какая сейчас погода?'))
        await asyncio.wait_for(entered.wait(), 2)
        before = frame(h)
        assert h.app.runtime_status['dialogue_lighting_phase'] == 'processing'
        await h.app.dialogue_lifecycle(h.sid, 'output', token=-1)
        assert frame(h) == before and h.app.runtime_status['dialogue_lighting_phase'] == 'processing'
        await h.send('SpeechReceived', text='Мне больно')
        before = frame(h)
        release.set(); await pending
        await h.app.dialogue_lifecycle(h.sid, 'listening', source='turn_finished')
        assert frame(h) == before and h.state.safety_severity == 'concern'
    finally:
        release.set()
        if pending:
            await asyncio.gather(pending, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_exact_public_intro_uses_existing_native_gate_before_eof():
    h, native, cache, sink = native_setup(support())
    trusted, semantic = validation_calls(h)
    pending = asyncio.create_task(h.button(4))
    try:
        await asyncio.wait_for(native.entered.wait(), 2)
        await asyncio.wait_for(sink.first.wait(), 2)
        assert trusted == [INTRO] and not semantic
        assert not native.closed_stream.is_set() and len(sink.frames) == 1
        assert sink.frames[0][0].response_id == 'DIALOGUE_INTRO'
        assert (await pending)['reason'] == 'button_mode'
        assert h.app.runtime_status['dialogue_lighting_phase'] == 'output'
        native.release.set(); await asyncio.wait_for(sink.finished.wait(), 2)
        assert len(sink.frames) == 2 and not sink.errors
        assert cache.snapshot()['entries'] == 0
        await h.complete()
        assert h.app.runtime_status['dialogue_lighting_phase'] == 'listening'
    finally:
        await native_close(h, native, sink, pending)


@pytest.mark.asyncio
async def test_blocked_native_intro_never_reaches_provider_and_keeps_help():
    h, native, cache, sink = native_setup(support())
    checked = []
    async def blocked(text, **kwargs):
        checked.append(text)
        return ValidationDecision('block', False, 'synthetic_block', 1., 'synthetic')
    h.app.bounded_s2s.response_validator.validate_trusted = blocked
    try:
        await h.button(4)
        assert checked == [INTRO] and not native.calls and not sink.frames
        assert h.state.mode == 'support_intake' and h.state.active_audio_id is None
        assert h.app.runtime_status['dialogue_lighting_phase'] == 'listening'
        await h.button(5)
        assert h.state.mode == 'wellbeing_check' and h.state.active_audio_id == 'PHRASE_084'
    finally:
        await native_close(h, native, sink)


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['back', 'help', 'hangup', 'fact'])
async def test_native_intro_first_frame_then_replacement_drops_old_terminal(action):
    h, native, cache, sink = native_setup(support())
    pending = asyncio.create_task(h.button(4))
    try:
        await asyncio.wait_for(sink.first.wait(), 2)
        await pending
        old = dict(h.app._active_audio[h.sid])
        if action == 'back':
            await h.button(7)
        elif action == 'help':
            await h.button(5)
        elif action == 'hangup':
            await h.send('HandsetPlaced')
        else:
            h.app.bounded_s2s.tts = h.app.bounded_s2s.voice = SyntheticVoice()
            await h.send('SpeechReceived', text='Какое сегодня число?')
        current, before = dict(h.app._active_audio.get(h.sid) or {}), frame(h)
        native.release.set(); await asyncio.wait_for(sink.finished.wait(), 2)
        await h.send('AudioCompleted', {**old, 'response_id': old['content_id']})
        assert len(sink.frames) == 1
        assert dict(h.app._active_audio.get(h.sid) or {}) == current and frame(h) == before
    finally:
        await native_close(h, native, sink, pending)


@pytest.mark.asyncio
async def test_actual_old_queue_output_hook_cannot_repaint_a_new_fact_owner():
    h = support()
    entered, release = asyncio.Event(), asyncio.Event()
    original_hook = h.app.dialogue_lifecycle
    old = new = None
    observed = []
    async def delayed(sid, phase, **details):
        if phase == 'output' and not entered.is_set():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            before = frame(h)
            await original_hook(sid, phase, **details)
            observed.append((before, frame(h)))
        else:
            await original_hook(sid, phase, **details)
    h.app.bounded_s2s.lifecycle_hook = delayed
    try:
        await h.button(4); await h.complete()
        old = asyncio.create_task(h.send('SpeechReceived', text='Расскажи про кота'))
        await asyncio.wait_for(entered.wait(), 2)
        new = asyncio.create_task(h.send('SpeechReceived', text='Какое сегодня число?'))
        # The replacement commits its kind/revision before awaiting old cleanup.
        for _ in range(20):
            if h.state.dialogue_information_kind == 'date':
                break
            await asyncio.sleep(0)
        assert h.state.dialogue_information_kind == 'date'
        release.set()
        await asyncio.wait_for(asyncio.gather(old, new, return_exceptions=True), 3)
        assert observed and all(before == after for before, after in observed)
        assert h.state.active_audio_id == 'TYPED_DATE'
        assert h.app.runtime_status['dialogue_lighting_phase'] == 'output'
        assert not h.app._support_lighting_owners
    finally:
        release.set()
        for pending in (old, new):
            if pending:
                await asyncio.gather(pending, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_new_bear_turn_keeps_phases_while_old_support_provider_drains():
    h = support()
    entered, release = asyncio.Event(), asyncio.Event()
    old = None
    phases = []
    class Delayed:
        calls = 0
        async def stream(self, messages, request_id, cancelled):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        pass
            yield '<say>У кота мягкая шерсть.</say>'
    h.app.bounded_s2s.llm = Delayed()
    original = h.app._set_dialogue_lighting_phase
    async def observed(sid, phase, **kwargs):
        result = await original(sid, phase, **kwargs)
        if result is not None:
            phases.append(phase)
        return result
    h.app._set_dialogue_lighting_phase = observed
    original_hook = h.app.dialogue_lifecycle
    async def hook(sid, phase, **details):
        await original_hook(sid, phase, **details)
        if h.state.mode == 'bear_dialogue' and phase == 'processing':
            release.set()
    h.app.bounded_s2s.lifecycle_hook = hook
    try:
        await h.button(4); await h.complete()
        old = asyncio.create_task(h.send('SpeechReceived', text='Расскажи про кота'))
        await asyncio.wait_for(entered.wait(), 2)
        await h.button(7); await h.release(7)
        await h.button(6); await h.release(6); await h.complete()
        phases.clear()
        await h.send('SpeechReceived', text='Расскажи про собаку')
        assert phases == ['processing', 'output', 'listening']
    finally:
        release.set()
        if old:
            await asyncio.wait_for(asyncio.gather(old, return_exceptions=True), 3)
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('response_id', ['DIALOGUE_INTRO', 'DIALOGUE_RETURN'])
async def test_actual_stand_intro_scope_is_safe_and_return_stays_bear_only(response_id):
    stand = await pcm_setup()
    try:
        state = stand.application.canonical_runtime.sessions[HANDLE.session_id]
        state.mode = 'support_intake'; state.safety_severity = 'none'
        handle = replace(HANDLE, response_id=response_id)
        install(stand, handle)
        assert stand._pcm_owner_current(handle, None) == (response_id == 'DIALOGUE_INTRO')
        state.safety_severity = 'concern'
        assert not stand._pcm_owner_current(handle, None)
    finally:
        await stand.aclose()


@pytest.mark.asyncio
async def test_actual_native_second_frame_is_revoked_by_same_mode_concern(monkeypatch):
    stand = await pcm_setup()
    stand.application.record_audio_diagnostic = lambda kind, **details: stand.diags.append((kind, details))
    state = stand.application.canonical_runtime.sessions[HANDLE.session_id]
    state.mode = 'support_intake'; state.safety_severity = 'none'
    handle = replace(HANDLE, response_id='DIALOGUE_INTRO')
    install(stand, handle)
    writes = []
    class Stream:
        def __init__(self, **kwargs): pass
        def start(self): pass
        def write(self, chunk): writes.append(len(chunk))
        def stop(self): self.drained = True
        def abort(self): pass
        def close(self): pass
    monkeypatch.setitem(sys.modules, 'sounddevice', SimpleNamespace(OutputStream=Stream,
        __spec__=ModuleSpec('sounddevice', loader=None), query_devices=lambda *_: {'max_output_channels': 1}))
    controller = SoundDevicePlaybackController(); controller.active_token = handle.token
    original = controller._run_io
    calls = 0
    async def io(operation):
        nonlocal calls
        result = await original(operation); calls += 1
        if calls == 2:
            await type(stand.application).receive_async(stand.application, Event('SpeechReceived',
                handle.session_id, 1, 1., 'test', {}, 'same-mode-concern', 10.), 'Мне больно')
        return result
    controller._run_io = io
    try:
        assert stand._pcm_owner_current(handle, None)
        result = await controller.play_owned_pcm(AudioChunk((.1,) * 8192, 16000, handle.token, handle.session_id),
            lambda: stand._pcm_owner_current(handle, None))
        assert writes == [4096] and not result.completed
        assert state.mode == 'support_intake' and state.safety_severity == 'concern'
    finally:
        await controller.stop(); await stand.aclose()
