"""Real simulator edges cannot reuse a held key as a new mode's answer."""
import copy
import socket

import pytest

from app.foundation.canonical_contract import CanonicalEvent, CanonicalRuntime
from test_panel_session_actions import PanelStand
from test_contextual_game_flow import Harness as GameHarness


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError('network is forbidden in held-edge tests')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket.socket, 'connect_ex', denied)


class HeldStand(PanelStand):
    async def enter(self, up):
        if self.client:
            await self.ready()
        if up:
            await self.simulator.action({'action': 'handset', 'state': 'up'})
        await self.control('start')
        await self.menu()
        assert self.state.mode == 'menu_ready'
        assert self.state.profile_status == ('identified' if self.client else 'anonymous')

    def action_state(self):
        return (copy.deepcopy(self.state.snapshot()),
            copy.deepcopy(self.app._active_audio.get('stand')),
            len(self.app.bounded_s2s.tts.texts),
            copy.deepcopy(self.app._game_timer_scopes.get('stand')),
            self.app._session_revisions.get('stand', 0),
            {sid: context.turns for sid, context in self.app.conversation_contexts.items()})

    async def close(self):
        if self.state and self.state.interaction_open:
            await self.control('end')
        await super().close()


@pytest.mark.asyncio
@pytest.mark.parametrize('identity', [False, True])
@pytest.mark.parametrize('up', [False, True])
async def test_held_no_cannot_answer_replacement_wellbeing_question(tmp_path, identity, up):
    h = HeldStand(tmp_path, identity=identity)
    try:
        await h.enter(up)
        await h.key(5)
        assert h.state.support_question_kind == 'pain_or_discomfort'
        assert (await h.edge(1))['reason'] == 'support_continue'
        assert h.state.mode == 'wellbeing_check' and h.state.support_question_kind == 'fear'
        assert h.state.accepted_buttons_down == [1]
        before = h.action_state()
        result = await h.edge(1)  # New UART sequence; same physical down interval.
        assert h.action_state() == before
        assert result['outputs'] == []
        await h.edge(1, 'release')
        assert 1 not in h.state.accepted_buttons_down
        assert (await h.edge(1))['reason'] == 'support_declined'
        assert h.state.mode == 'root_menu' and h.state.support_question_kind is None
        await h.edge(1, 'release')
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('identity', [False, True])
@pytest.mark.parametrize('root,selection,pending,next_mode', [
    (1, 2, 'quiz', 'fixed_story_menu'),
    (2, 1, 'story', 'game_menu'),
])
async def test_awaiting_handset_held_choice_cannot_become_another_root_action(
        tmp_path, identity, root, selection, pending, next_mode):
    h = HeldStand(tmp_path, identity=identity)
    try:
        await h.enter(False)
        await h.key(root)
        await h.edge(selection)
        assert h.state.mode == 'awaiting_handset'
        assert h.state.accepted_buttons_down == [selection]
        if pending == 'quiz':
            assert h.state.pending_game_id == 'quiz'
        else:
            assert h.state.active_story_id and h.state.pending_mode == 'fixed_story_playing'
        before = h.action_state()
        result = await h.edge(selection)
        assert h.action_state() == before
        assert result['outputs'] == [], 'a held edge must not narrate a new scene'
        await h.edge(selection, 'release')
        await h.edge(selection)
        assert h.state.mode == next_mode, 'a genuinely new press remains an action'
        await h.edge(selection, 'release')
    finally:
        await h.close()


def send(runtime, kind, sequence, payload=None, *, at=10., source='synthetic'):
    state = runtime.sessions.get('held-controls')
    return runtime.handle(CanonicalEvent(kind, 'held-controls', sequence,
        state.state_version if state else 0, source, payload or {},
        f'{source}:{sequence}', 100., at))


def test_same_sequence_is_still_idempotent_before_held_edge_guard():
    runtime = CanonicalRuntime()
    state = runtime._new_state('held-controls', None)
    state.mode, state.handset, state.support_question_kind = 'wellbeing_check', 'up', 'pain_or_discomfort'
    runtime.sessions[state.session_id] = state
    event = CanonicalEvent('ButtonPressed', state.session_id, 1, state.state_version,
        'synthetic', {'button_id': 1}, 'one-physical-edge', 100., 10.)
    assert runtime.handle(event)['reason'] == 'support_continue'
    before = state.snapshot()
    duplicate = runtime.handle(event)
    assert duplicate['reason'] == 'idempotent_duplicate'
    assert state.snapshot() == before and duplicate['effects'] == []


def test_existing_early_menu_held_reason_and_release_remain_intact():
    runtime = CanonicalRuntime(exhibition_mode=True)
    state = runtime._new_state('held-controls', None)
    state.mode, state.handset = 'exhibition_menu_tutorial_buttons', 'up'
    runtime.sessions[state.session_id] = state
    send(runtime, 'ButtonPressed', 1, {'button_id': 1})
    assert state.mode == 'game_menu' and state.menu_early_choice_button == 1
    held = send(runtime, 'ButtonPressed', 2, {'button_id': 1})
    assert held['reason'] == 'exhibition_menu_choice_held' and held['effects'] == []
    send(runtime, 'ButtonReleased', 3, {'button_id': 1})
    assert state.menu_early_choice_button is None and 1 not in state.accepted_buttons_down
    assert send(runtime, 'ButtonPressed', 4, {'button_id': 1})['reason'] == 'game_started'


def test_silent_hold_six_timer_and_release_are_not_filtered_as_duplicate_press():
    runtime = CanonicalRuntime(exhibition_mode=True)
    assert send(runtime, 'ButtonPressed', 1, {'button_id': 6})['reason'] == 'silent_hold_started'
    state = runtime.sessions['held-controls']
    generation = state.silent_hold_generation
    send(runtime, 'ButtonLongHeld', 2, {'button_id': 6, 'duration_ms': 5000}, at=15.)
    tick = send(runtime, 'SilentHoldTick', 3, {'generation': generation}, at=15., source='timer')
    assert tick['reason'] == 'silent_hold_6_started' and state.mode == 'menu_ready'
    assert 6 in state.accepted_buttons_down
    assert send(runtime, 'ButtonReleased', 4, {'button_id': 6}, at=15.)['reason'] == 'silent_hold_release_consumed'
    assert 6 not in state.accepted_buttons_down


@pytest.mark.asyncio
async def test_game_seven_timer_and_release_exit_survive_held_down_rejection():
    h = GameHarness(band='6-7')
    try:
        await h.launch('quiz')  # Real heard offer/consent/start/prompt callbacks.
        sid = h.state.session_id
        assert (await h.send('ButtonPressed', {'button_id': 7}))['reason'] == 'game_exit_pending'
        began = h.state.game_engine_state.seven_exit_down_at
        scope = h.app._game_timer_scopes[sid]
        await h.send('ButtonPressed', {'button_id': 7})
        assert h.state.game_engine_state.seven_exit_down_at == began
        assert h.app._game_timer_scopes[sid] == scope
        for step in range(1, 8):
            h.clock = h.app._game_timer_scopes[sid][-1]
            result = (await h.app.flush_game_timers())[0]
            assert result['reason'] == ('game_exit_ready' if step == 7 else 'game_exit_progress')
        assert h.state.game_engine_state.seven_exit_phase == 'ready'
        await h.send('ButtonLongHeld', {'button_id': 7, 'duration_ms': 5000})
        assert h.state.mode == 'game_active', 'ready does not exit before release'
        assert (await h.send('ButtonReleased', {'button_id': 7}))['reason'] == 'game_exited_by_long_button'
        assert h.state.mode == 'bear_dialogue' and h.state.game_engine_state is None
        assert 7 not in h.state.accepted_buttons_down
    finally:
        await h.close()


def test_replayed_panel_end_cannot_disable_fresh_silent_six_entry():
    runtime = CanonicalRuntime(exhibition_mode=True)
    started = runtime.handle(CanonicalEvent('SessionStarted', 'held-controls', 1, 0,
        'panel', {}, 'panel-start', 100., 10.), session_action_authorized=True)
    state = runtime.sessions['held-controls']
    assert started['reason'] == 'panel_silent_session_started' and state.interaction_open
    end = CanonicalEvent('SessionCancel', state.session_id, 2, state.state_version,
        'panel', {}, 'panel-end', 100., 11.)
    assert runtime.handle(end, session_action_authorized=True)['reason'] == 'soft_end'
    assert state.mode == 'session_closed' and state.substate == 'soft_end'
    duplicate = runtime.handle(end, session_action_authorized=True)
    assert duplicate['reason'] == 'idempotent_duplicate' and duplicate['effects'] == []
    fresh = send(runtime, 'ButtonPressed', 3, {'button_id': 6}, at=12.)
    assert fresh['reason'] == 'silent_hold_started', 'replay cannot overwrite semantic soft_end'
    assert state.silent_hold_active and state.silent_hold_started_monotonic == 12.
