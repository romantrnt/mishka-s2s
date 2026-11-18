"""Owned game speech gets its safety decision before an automatic reminder."""
import asyncio

import pytest

from app.foundation.input_safety import InputSafetyDecision
from test_contextual_game_flow import Harness


@pytest.mark.asyncio
@pytest.mark.parametrize('game,text,target', [('word_game', 'Кот', 'game_active'), ('quiz', 'Хватит играть', 'bear_dialogue')])
async def test_input_admitted_before_hint_is_not_replaced_during_safety_check(game, text, target):
    h = Harness()
    entered, release = asyncio.Event(), asyncio.Event()
    task = None
    try:
        await h.launch(game)
        sid = h.state.session_id
        scope = h.app._game_timer_scopes[sid]
        before_deadline = h.app._engagement_owners[sid]['deadline']
        original = h.app.safety_classifier.classify
        async def delayed(value, **kwargs):
            entered.set()
            await release.wait()
            return await original(value, **kwargs)
        h.app.safety_classifier.classify = delayed
        h.clock = scope[-1] - .1
        task = asyncio.create_task(h.send('SpeechReceived', text=text))
        await entered.wait()
        h.clock = scope[-1] + .1
        assert await h.app.flush_game_timers() == []
        assert h.app._engagement_owners[sid]['deadline'] == before_deadline
        release.set()
        result = await task
        assert result['reason'] != 'stale_session_generation'
        assert h.state.mode == target
        if game == 'word_game':
            assert h.state.game_engine_state.round == 1 and h.state.game_audio_stage == 'playing'
        else:
            assert h.state.active_audio_id == 'DIALOGUE_RETURN'
    finally:
        release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['unverified', 'cancelled'])
async def test_failed_or_cancelled_check_rearms_original_hint_without_renewing_activity(outcome):
    h = Harness()
    entered, release = asyncio.Event(), asyncio.Event()
    task = None
    try:
        await h.launch('word_game')
        sid = h.state.session_id
        scope = h.app._game_timer_scopes[sid]
        h.app._refresh_engagement_busy(sid)
        deadline = h.app._engagement_owners[sid]['deadline']
        async def delayed(text, **kwargs):
            entered.set()
            await release.wait()
            return InputSafetyDecision('none', 'input_safety_uncertain', False)
        h.app.safety_classifier.classify = delayed
        h.clock = scope[-1] - .1
        task = asyncio.create_task(h.send('SpeechReceived', text='Кот'))
        await entered.wait()
        h.clock = scope[-1] + .1
        assert await h.app.flush_game_timers() == []
        if outcome == 'cancelled':
            task.cancel()
        else:
            release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert not h.app._game_speech_checks
        assert h.app._game_timer_scopes[sid] == scope
        assert h.app._engagement_owners[sid]['deadline'] == deadline
        assert (await h.app.flush_game_timers())[0]['reason'] == 'game_wait_hint'
        assert h.state.game_engine_state.round == 0
        assert h.state.active_audio_id == 'GAME_PROMPT'
    finally:
        release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_late_old_game_check_cannot_hush_new_game_timer_or_replace_its_prompt():
    h = Harness()
    entered, release = asyncio.Event(), asyncio.Event()
    task = None
    try:
        await h.launch('word_game')
        sid = h.state.session_id
        original = h.app.safety_classifier.classify
        async def delayed(text, **kwargs):
            if text == 'Кот':
                entered.set()
                await release.wait()
            return await original(text, **kwargs)
        h.app.safety_classifier.classify = delayed
        task = asyncio.create_task(h.send('SpeechReceived', text='Кот'))
        await entered.wait()
        await h.key(7)
        await h.done()
        await h.launch('quiz')
        scope = h.app._game_timer_scopes[sid]
        h.clock = scope[-1]
        assert (await h.app.flush_game_timers())[0]['reason'] == 'game_wait_hint'
        lease = h.state.playback_lease
        release.set()
        result = await task
        assert result['reason'] == 'stale_session_generation'
        assert h.state.active_game_id == 'quiz' and h.state.playback_lease == lease
        assert h.state.active_audio_id == 'GAME_PROMPT' and not h.app._game_speech_checks
    finally:
        release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_sequence_demo_continues_during_safety_check_without_resetting_its_next_deadline():
    h = Harness()
    entered = asyncio.Event()
    task = None
    try:
        await h.launch('repeat_pattern')
        sid = h.state.session_id
        engine = h.state.game_engine_state
        scope = h.app._game_timer_scopes[sid]
        async def delayed(text, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        h.app.safety_classifier.classify = delayed
        task = asyncio.create_task(h.send('SpeechReceived', text='Повтори'))
        await entered.wait()
        h.clock = scope[-1] + .1
        assert (await h.app.flush_game_timers())[0]['reason'] == 'game_sequence_demonstration'
        next_scope = h.app._game_timer_scopes[sid]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert h.app._game_timer_scopes[sid] == next_scope
        assert h.state.game_engine_state.pattern == engine.pattern
        assert h.state.game_engine_state.demonstration_index == engine.demonstration_index
        assert not h.state.game_engine_state.demonstration_on
        assert not h.app._game_speech_checks
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_safety_escalation_after_pending_check_still_owns_game_and_clears_timer():
    h = Harness()
    entered, release = asyncio.Event(), asyncio.Event()
    task = None
    try:
        await h.launch('word_game')
        sid = h.state.session_id
        scope = h.app._game_timer_scopes[sid]
        async def delayed(text, **kwargs):
            entered.set()
            await release.wait()
            return InputSafetyDecision('urgent', 'semantic', True)
        h.app.safety_classifier.classify = delayed
        task = asyncio.create_task(h.send('SpeechReceived', text='Плохо себя чувствую'))
        await entered.wait()
        h.clock = scope[-1] + .1
        assert await h.app.flush_game_timers() == []
        release.set()
        await task
        assert h.state.mode == 'safety_stop' and h.state.safety_severity == 'urgent'
        assert sid not in h.app._game_timer_scopes and not h.app._game_speech_checks
    finally:
        release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('press_before_hint', [False, True])
async def test_real_hold_release_timer_is_not_suppressed_by_speech_safety_check(press_before_hint):
    from app.foundation.game_engine import start
    h = Harness(mode='game_active')
    entered = asyncio.Event()
    task = None
    try:
        sid = h.state.session_id
        h.state.active_game_id = 'hold_light'
        h.state.game_audio_stage = 'playing'
        h.state.game_engine_state = start('hold_light', sequence=0, now=h.clock,
            selection_button=0, run_id='legacy-hold-control')
        if press_before_hint:
            h.clock = h.state.game_engine_state.hint_deadline - .1
        await h.send('ButtonPressed', {'button_id': 1})
        engine = h.state.game_engine_state
        waiting = (engine.hint_deadline, engine.response_deadline, engine.hints_emitted)
        hold_due = dict(engine.down_times)[engine.target] + engine.hold_seconds
        async def delayed(text, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        h.app.safety_classifier.classify = delayed
        task = asyncio.create_task(h.send('SpeechReceived', text='Я держу кнопку'))
        await entered.wait()
        h.clock = hold_due + .1
        assert (await h.app.flush_game_timers())[0]['reason'] == 'game_release_ready'
        assert h.state.game_engine_state.hold_ready and h.state.game_engine_state.round == 0
        assert h.state.active_audio_id == 'PHRASE_036'
        engine = h.state.game_engine_state
        assert (engine.hint_deadline, engine.response_deadline, engine.hints_emitted) == waiting
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert not h.app._game_speech_checks
        assert h.app._game_timer_scopes[sid][-1] == min(waiting[:2])
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await h.close()
