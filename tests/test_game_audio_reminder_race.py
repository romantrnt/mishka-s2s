"""Actual VAD/STT ingress retains its current game's input before a reminder."""
import asyncio
from dataclasses import replace

import pytest

from app.foundation.s2s import STTResult
from app.foundation.input_safety import InputSafetyDecision
from test_contextual_game_flow import Harness


class DelayedRecognizer:
    def __init__(self, text, *, outcome='valid', resist_cancel=False):
        self.text = text
        self.outcome, self.resist_cancel = outcome, resist_cancel
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.calls = 0

    async def transcribe(self, audio, cancelled):
        self.calls += 1
        assert len(audio.samples) >= 8000  # Real VAD, speech plus its silence window.
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            if not self.resist_cancel:
                raise
        if self.outcome == 'failure':
            raise RuntimeError('recognition unavailable')
        return STTResult(self.text, .1 if self.outcome == 'untrusted' else .99, 1.)


SPEECH = (.1,) * (512 * 10)
SILENCE = (0.,) * (512 * 7)


@pytest.mark.asyncio
@pytest.mark.parametrize('game,text,target', [
    ('word_game', 'Кот', 'game_active'),
    ('quiz', 'Хватит играть', 'bear_dialogue'),
])
async def test_actual_audio_before_hint_survives_pending_stt(game, text, target):
    h = Harness()
    recognizer = DelayedRecognizer(text)
    task = None
    try:
        await h.launch(game)
        sid = h.state.session_id
        scope = h.app._game_timer_scopes[sid]
        h.app.s2s_audio.recognizer = recognizer
        safety_calls = []
        original_safety = h.app.safety_classifier.classify
        async def classify(value, **kwargs):
            safety_calls.append(value)
            return await original_safety(value, **kwargs)
        h.app.safety_classifier.classify = classify
        h.clock = scope[-1] - .1
        h.seq += 1
        sequence = h.seq
        task = asyncio.create_task(h.app.receive_audio_async(sid, SPEECH + SILENCE, sequence))
        await asyncio.wait_for(recognizer.entered.wait(), 2)
        h.clock = scope[-1] + .1
        timers = await h.app.flush_game_timers()
        recognizer.release.set()
        result = await task
        assert (timers, result['reason']) == ([], 'word_game_turn_finished' if game == 'word_game' else 'game_exited_by_voice')
        assert h.state.mode == target
        assert recognizer.calls == 1
        assert safety_calls == [text]
        replay = await h.app.receive_audio_async(sid, SPEECH + SILENCE, sequence)
        assert replay['reason'] in {'late_or_duplicate_audio', 'idempotent_duplicate', 'stale_sequence'}
        assert safety_calls == [text]
        assert not h.app._game_audio_waits
        if game == 'word_game':
            assert h.state.game_engine_state.round == 1
        else:
            assert h.state.active_audio_id == 'DIALOGUE_RETURN'
    finally:
        recognizer.release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_vad_onset_spanning_uploads_and_detached_stt_keeps_one_fixed_owner():
    h = Harness()
    recognizer = DelayedRecognizer('Кот')
    try:
        await h.launch('word_game')
        sid = h.state.session_id
        scope = h.app._game_timer_scopes[sid]
        h.app.s2s_audio.recognizer = recognizer
        h.clock = scope[-1] - .2
        h.seq += 1
        assert (await h.app.receive_audio_async(sid, SPEECH, h.seq))['reason'] == 'collecting'
        wait = next(iter(h.app._game_audio_waits.values()))
        h.clock = scope[-1] + .1
        assert await h.app.flush_game_timers() == []
        h.seq += 1
        result = await h.app.receive_audio_async(sid, SILENCE, h.seq, wait_for_turn=False)
        assert result['pending'] and result['reason'] == 'audio_processing'
        await asyncio.wait_for(recognizer.entered.wait(), 2)
        completion = tuple(h.app._audio_completions[sid])
        assert next(iter(h.app._game_audio_waits.values())) is wait
        assert h.app._game_timer_scopes[sid][4] == scope[4]
        assert await h.app.flush_game_timers() == []
        recognizer.release.set()
        await asyncio.gather(*completion)
        assert h.state.game_engine_state.round == 1 and recognizer.calls == 1
        assert not h.app._game_audio_waits
    finally:
        recognizer.release.set()
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('samples,status', [
    ((0.,) * 100, 'collecting'),  # Pending sub-window.
    ((.001,) * 8192, 'collecting'),  # Low-VAD pre-roll, not an utterance.
    ((.1,) * 512 + SILENCE, 'too_short'),
])
async def test_silence_noise_and_short_audio_do_not_hold_or_renew_waiting(samples, status):
    h = Harness()
    try:
        await h.launch('word_game')
        sid = h.state.session_id
        scope = h.app._game_timer_scopes[sid]
        h.app._refresh_engagement_busy(sid)
        engagement = h.app._engagement_owners[sid]['deadline']
        h.clock = scope[-1] - .1
        h.seq += 1
        assert (await h.app.receive_audio_async(sid, samples, h.seq))['reason'] == status
        assert not h.app._game_audio_waits
        assert h.app._game_timer_scopes[sid] == scope
        assert h.app._engagement_owners[sid]['deadline'] == engagement
        h.clock = scope[-1] + .1
        assert (await h.app.flush_game_timers())[0]['reason'] == 'game_wait_hint'
        assert h.state.game_engine_state.round == 0
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_stalled_vad_wall_cap_does_not_move_with_more_audio_or_renew_idle():
    h = Harness()
    try:
        await h.launch('word_game')
        sid = h.state.session_id
        scope = h.app._game_timer_scopes[sid]
        h.app._refresh_engagement_busy(sid)
        engagement = h.app._engagement_owners[sid]['deadline']
        h.clock = scope[-1] - .1
        h.seq += 1
        await h.app.receive_audio_async(sid, SPEECH, h.seq)
        wait = next(iter(h.app._game_audio_waits.values()))
        cap = wait['deadline']
        h.clock += 1
        h.seq += 1
        await h.app.receive_audio_async(sid, SPEECH, h.seq)
        assert next(iter(h.app._game_audio_waits.values())) is wait
        assert wait['deadline'] == cap
        assert h.app._game_timer_scopes[sid][4] == scope[4]
        assert h.app._engagement_owners[sid]['deadline'] == engagement
        h.clock = cap + .1
        assert (await h.app.flush_game_timers())[0]['reason'] == 'game_wait_hint'
        assert not h.app._game_audio_waits
        assert h.state.game_engine_state.round == 0
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome,detached', [('failure', False), ('untrusted', True), ('cancelled', False), ('timeout', True)])
async def test_stt_failure_cancel_timeout_or_untrusted_rearms_the_original_hint(outcome, detached):
    h = Harness()
    recognizer = DelayedRecognizer('Кот', outcome=outcome)
    task = None
    try:
        await h.launch('word_game')
        sid = h.state.session_id
        scope = h.app._game_timer_scopes[sid]
        h.app._refresh_engagement_busy(sid)
        engagement = h.app._engagement_owners[sid]['deadline']
        h.app.s2s_audio.recognizer = recognizer
        if outcome == 'timeout':
            h.app.s2s_audio.config = replace(h.app.s2s_audio.config, stt_timeout_s=.05)
        h.clock = scope[-1] - .1
        h.seq += 1
        task = asyncio.create_task(h.app.receive_audio_async(sid, SPEECH + SILENCE, h.seq, wait_for_turn=not detached))
        await asyncio.wait_for(recognizer.entered.wait(), 2)
        if detached:
            assert (await task)['pending']
            completion = tuple(h.app._audio_completions[sid])
        h.clock = scope[-1] + .1
        assert await h.app.flush_game_timers() == []
        if outcome == 'cancelled':
            task.cancel()
        elif outcome != 'timeout':
            recognizer.release.set()
        await asyncio.gather(*(completion if detached else (task,)), return_exceptions=True)
        assert not h.app._game_audio_waits and not h.app._game_speech_checks
        assert h.app._game_timer_scopes[sid] == scope
        assert h.app._engagement_owners[sid]['deadline'] == engagement
        assert h.state.game_engine_state.round == 0
        assert (await h.app.flush_game_timers())[0]['reason'] == 'game_wait_hint'
    finally:
        recognizer.release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['exit', 'reset', 'hangup', 'typed'])
async def test_late_recognition_after_explicit_change_cannot_advance_old_game(change):
    h = Harness()
    recognizer = DelayedRecognizer('Кот', resist_cancel=True)
    task = None
    try:
        await h.launch('word_game')
        sid = h.state.session_id
        h.app.s2s_audio.recognizer = recognizer
        h.seq += 1
        task = asyncio.create_task(h.app.receive_audio_async(sid, SPEECH + SILENCE, h.seq))
        await asyncio.wait_for(recognizer.entered.wait(), 2)
        calls = len(h.model.calls)
        if change == 'exit':
            await h.key(7)
        elif change == 'typed':
            await h.send('SpeechReceived', text='Лиса')
        else:
            await h.send('SessionReset' if change == 'reset' else 'HandsetPlaced')
        recognizer.release.set()
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        assert isinstance(result, asyncio.CancelledError) or result['reason'] == 'stale_session_generation'
        assert len(h.model.calls) == calls + (change == 'typed')
        assert not h.app._game_audio_waits
        if change == 'typed':
            assert h.state.game_engine_state.round == 1
        elif change == 'hangup':
            assert sid not in h.app.canonical_runtime.sessions and h.app.sessions.get(sid) is None
        else:
            assert h.state.game_engine_state is None
    finally:
        recognizer.release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_old_capture_completion_does_not_release_new_capture_wait():
    h = Harness()
    old, fresh = DelayedRecognizer('Кот', resist_cancel=True), DelayedRecognizer('Лиса')
    old_task = fresh_task = None
    generation = 0
    try:
        await h.launch('word_game')
        sid = h.state.session_id
        h.app.s2s_audio.recognizer = old
        old_generation = generation
        h.seq += 1
        old_task = asyncio.create_task(h.app.receive_audio_async(sid, SPEECH + SILENCE, h.seq,
            _ingress_guard=lambda: generation == old_generation))
        await asyncio.wait_for(old.entered.wait(), 2)
        generation += 1
        h.app.s2s_audio.close_capture(sid)
        h.app.s2s_audio.recognizer = fresh
        h.seq += 1
        fresh_task = asyncio.create_task(h.app.receive_audio_async(sid, SPEECH + SILENCE, h.seq))
        await asyncio.wait_for(fresh.entered.wait(), 2)
        fresh_wait = next(iter(h.app._game_audio_waits.values()))
        old.release.set()
        assert (await old_task)['reason'] == 'stale_session_generation'
        assert list(h.app._game_audio_waits.values()) == [fresh_wait]
        scope = h.app._game_timer_scopes[sid]
        h.clock = scope[4] + .1
        assert await h.app.flush_game_timers() == []
        fresh.release.set()
        assert (await fresh_task)['reason'] == 'word_game_turn_finished'
        assert h.state.game_engine_state.round == 1 and not h.app._game_audio_waits
    finally:
        old.release.set()
        fresh.release.set()
        for task in (old_task, fresh_task):
            if task:
                await asyncio.gather(task, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cleanup', ['capture_disconnect', 'reset_session'])
async def test_actual_capture_cleanup_releases_wait_and_restores_original_timer(cleanup):
    h = Harness()
    try:
        await h.launch('word_game')
        sid = h.state.session_id
        scope = h.app._game_timer_scopes[sid]
        h.clock = scope[4] - .1
        h.seq += 1
        await h.app.receive_audio_async(sid, SPEECH, h.seq)
        pipeline = h.app.s2s_audio.ingress[sid]
        assert h.app._game_audio_waits and h.app._game_timer_scopes[sid][-1] > scope[4]
        if cleanup == 'reset_session':
            await h.app.reset_session(sid)
            assert sid not in h.app.canonical_runtime.sessions and sid not in h.app._game_timer_scopes
        else:
            await h.app._cancel_audio_owner(h.app.s2s_audio, sid, operation='browser_capture_disconnect')
            assert h.app._game_timer_scopes[sid] == scope
            h.clock = scope[4] + .1
            assert (await h.app.flush_game_timers())[0]['reason'] == 'game_wait_hint'
        assert not h.app._game_audio_waits
        assert getattr(pipeline, '_canonical_game_audio_wait', None) is None
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_terminal_resource_cleanup_does_not_rearm_a_game_timer():
    h = Harness()
    try:
        await h.launch('word_game')
        sid = h.state.session_id
        scope = h.app._game_timer_scopes[sid]
        h.seq += 1
        await h.app.receive_audio_async(sid, SPEECH, h.seq)
        await h.app._cleanup_session_resources(sid)
        assert not h.app._game_audio_waits and sid not in h.app._game_timer_scopes
        h.clock = scope[4] + .1
        assert await h.app.flush_game_timers() == []
        assert sid not in h.app._active_audio
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_detached_completion_cancelled_before_start_releases_its_wait():
    h = Harness()
    recognizer = DelayedRecognizer('Кот')
    try:
        await h.launch('word_game')
        sid = h.state.session_id
        scope = h.app._game_timer_scopes[sid]
        h.app.s2s_audio.recognizer = recognizer
        h.clock = scope[4] - .1
        h.seq += 1
        assert (await h.app.receive_audio_async(sid, SPEECH + SILENCE, h.seq, wait_for_turn=False))['pending']
        completion = next(iter(h.app._audio_completions[sid]))
        completion.cancel()
        await asyncio.gather(completion, return_exceptions=True)
        assert not h.app._game_audio_waits and h.app._game_timer_scopes[sid] == scope
        h.clock = scope[4] + .1
        assert (await h.app.flush_game_timers())[0]['reason'] == 'game_wait_hint'
        assert h.state.game_engine_state.round == 0
    finally:
        recognizer.release.set()
        await h.close()


@pytest.mark.asyncio
async def test_audio_crossing_hint_still_runs_mandatory_urgent_safety():
    h = Harness()
    recognizer = DelayedRecognizer('Плохо себя чувствую')
    task = None
    try:
        await h.launch('word_game')
        sid = h.state.session_id
        scope = h.app._game_timer_scopes[sid]
        h.app.s2s_audio.recognizer = recognizer
        safety_calls = []
        async def urgent(text):
            safety_calls.append(text)
            return InputSafetyDecision('urgent', 'semantic', True)
        h.app.safety_classifier.classify = urgent
        h.clock = scope[4] - .1
        h.seq += 1
        task = asyncio.create_task(h.app.receive_audio_async(sid, SPEECH + SILENCE, h.seq))
        await asyncio.wait_for(recognizer.entered.wait(), 2)
        h.clock = scope[4] + .1
        assert await h.app.flush_game_timers() == []
        recognizer.release.set()
        await task
        assert h.state.mode == 'safety_stop' and h.state.safety_severity == 'urgent'
        assert safety_calls == ['Плохо себя чувствую']
        assert not h.app._game_audio_waits and sid not in h.app._game_timer_scopes
    finally:
        recognizer.release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_danger_from_live_capture_survives_an_ordinary_round_change():
    h = Harness()
    recognizer = DelayedRecognizer('Плохо себя чувствую')
    task = None
    try:
        await h.launch('word_game')
        sid = h.state.session_id
        h.app.s2s_audio.recognizer = recognizer
        original = h.app.safety_classifier.classify
        async def safety(text, **kwargs):
            return (InputSafetyDecision('urgent', 'semantic', True) if text == recognizer.text
                    else await original(text, **kwargs))
        h.app.safety_classifier.classify = safety
        h.seq += 1
        task = asyncio.create_task(h.app.receive_audio_async(sid, SPEECH + SILENCE, h.seq))
        await asyncio.wait_for(recognizer.entered.wait(), 2)
        await h.send('SpeechReceived', text='Лиса')
        assert h.state.game_engine_state.round == 1
        recognizer.release.set()
        await task
        assert h.state.mode == 'safety_stop' and h.state.safety_severity == 'urgent'
        assert not h.app._game_audio_waits
    finally:
        recognizer.release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('game', ['repeat_pattern', 'hold_light'])
async def test_audio_wait_does_not_hide_sequence_or_hold_mechanics(game):
    from app.foundation.game_engine import start
    h = Harness(mode='game_active' if game == 'hold_light' else 'bear_dialogue')
    try:
        sid = h.state.session_id
        if game == 'hold_light':
            h.state.active_game_id = game
            h.state.game_audio_stage = 'playing'
            h.state.game_engine_state = start(game, sequence=0, now=h.clock,
                selection_button=0, run_id='audio-hold-control')
            h.clock = h.state.game_engine_state.hint_deadline - .1
            await h.send('ButtonPressed', {'button_id': 1})
            engine = h.state.game_engine_state
            due = dict(engine.down_times)[engine.target] + engine.hold_seconds
            expected = 'game_release_ready'
        else:
            await h.launch(game)
            due = h.app._game_timer_scopes[sid][-1]
            expected = 'game_sequence_demonstration'
        h.seq += 1
        await h.app.receive_audio_async(sid, SPEECH, h.seq)
        assert h.app._game_audio_waits
        h.clock = due + .1
        assert (await h.app.flush_game_timers())[0]['reason'] == expected
        assert h.state.game_engine_state.round == 0
        if game == 'hold_light':
            assert h.state.game_engine_state.hold_ready
            assert h.state.active_audio_id == 'PHRASE_036'
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_late_disconnect_cleanup_cannot_rearm_after_terminal_cleanup():
    h = Harness()
    entered, release = asyncio.Event(), asyncio.Event()
    task = None
    try:
        await h.launch('word_game')
        sid = h.state.session_id
        h.seq += 1
        await h.app.receive_audio_async(sid, SPEECH, h.seq)
        original = h.app.s2s_audio.cancel_session
        calls = 0
        async def delayed(session_id, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
            await original(session_id, **kwargs)
        h.app.s2s_audio.cancel_session = delayed
        task = asyncio.create_task(h.app._cancel_audio_owner(h.app.s2s_audio, sid,
            operation='browser_capture_disconnect'))
        await entered.wait()
        await h.app._cleanup_session_resources(sid)
        assert sid not in h.app._game_timer_scopes
        release.set()
        await task
        assert sid not in h.app._game_timer_scopes and not h.app._game_audio_waits
    finally:
        release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await h.close()
