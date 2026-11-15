import random
import pytest
from app.foundation.game_engine import start, advance, desired_buttons
from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.canonical_services import BUTTON_ELEMENTS


def test_target_fresh_edges_and_finite_rounds():
    state = start('catch_light', sequence=1, now=0., selection_button=1)
    assert advance(state, edge='ButtonPressed', button=1, sequence=2, now=1.).status == 'held'
    seq = 1
    for count in range(8):
        target = state.target
        seq += 1
        state = advance(state, edge='ButtonReleased', button=target, sequence=seq, now=float(seq)).state
        seq += 1
        result = advance(state, edge='ButtonPressed', button=target, sequence=seq, now=float(seq))
        state = result.state
        assert state.round == count + 1
        assert result.completed == (count == 7)
    assert desired_buttons(state) == ()

@pytest.mark.parametrize('edge', ['ButtonHeld', 'ButtonLongHeld'])
def test_hold_does_not_score(edge):
    state = start('catch_light', sequence=1, now=0., selection_button=2)
    assert advance(state, edge=edge, button=1, sequence=2, now=99.).state.round == 0


def test_property_no_reserved_targets_or_duplicate_scoring():
    for seed in range(30):
        rng = random.Random(seed)
        state = start('catch_light', sequence=0, now=0., selection_button=1)
        for seq in range(1, 150):
            edge = rng.choice(['ButtonPressed', 'ButtonReleased', 'ButtonHeld'])
            button = rng.randrange(1, 8)
            prior = state
            result = advance(state, edge=edge, button=button, sequence=seq, now=float(seq))
            state = result.state
            assert set(result.desired_buttons) <= {1,2,3,4}
            if edge != 'ButtonPressed' or button in prior.held or button != prior.target:
                assert state.round == prior.round
            assert advance(state, edge=edge, button=button, sequence=seq, now=float(seq)).state == state

@pytest.mark.asyncio
@pytest.mark.parametrize('selection', [1, 2])
async def test_real_application_output(selection):
    app, *_ = build_application(settings=Settings(profile='mock', runtime_mode='clinical'), background_scheduler=False)
    seq = 0
    async def send(kind, button=None):
        nonlocal seq
        seq += 1
        return await app.receive_async(Event(kind, 'realgame', seq, float(seq), 'test',
            {} if button is None else {'button_id': button}, f'game:{seq}', float(seq)))
    def lit(buttons):
        ids = {i for b in buttons for i in BUTTON_ELEMENTS[b]}
        return {str(i): int(i in ids) for i in range(18)}
    try:
        await send('ButtonPressed', 1)
        await send('ButtonReleased', 1)
        result = await send('ButtonPressed', selection)
        assert result['reason'] == 'game_started'
        state = app.canonical_runtime.sessions['realgame']
        await send('ButtonReleased', selection)
        assert app.led.output.snapshot() == lit([1] if selection == 1 else [])
        await send('ButtonPressed', 1)
        assert app.led.output.snapshot() == lit([2] if selection == 1 else [1])
        before = state.game_engine_state.round
        await send('ButtonHeld', 1)
        await send('ButtonPressed', 1)
        assert state.game_engine_state.round == before
        if selection == 2:
            await send('ButtonPressed', 3)
            assert app.led.output.snapshot() == lit([1,3])
            await send('ButtonReleased', 1)
            assert app.led.output.snapshot() == lit([3])
        await send('ButtonPressed', 5)
        assert state.mode == 'wellbeing_check'
        assert state.game_engine_state is None
    finally:
        await app._cleanup_session_resources('realgame')

@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['clinical', 'exhibition'])
async def test_real_game_hangup_policy_and_completion(mode):
    app, *_ = build_application(settings=Settings(profile='mock', runtime_mode=mode), background_scheduler=False)
    seq = 0
    async def send(kind, button=None):
        nonlocal seq
        seq += 1
        return await app.receive_async(Event(kind, 'resetgame', seq, float(seq), 'test',
            {} if button is None else {'button_id': button}, f'reset:{seq}', float(seq)))
    try:
        await send('HandsetLifted')
        state = app.canonical_runtime.sessions['resetgame']
        if mode == 'exhibition':
            state.mode = 'menu_ready'  # isolate games from separately tested onboarding
        await send('ButtonPressed', 1)
        await send('ButtonReleased', 1)
        await send('ButtonPressed', 1)
        await send('ButtonReleased', 1)
        if state.mode == 'game_menu':
            # New first-entry narration is interrupted, not selected, by this press.
            await send('ButtonPressed', 1)
            await send('ButtonReleased', 1)
        assert state.game_engine_state is not None
        before = dict(app.led.output.snapshot())
        await send('HandsetPlaced')
        state = app.canonical_runtime.sessions.get('resetgame')
        assert 'resetgame' not in app._active_audio
        if mode == 'exhibition':
            assert state is None or state.game_engine_state is None
            assert not any(app.led.output.snapshot().values())
        else:
            assert state.game_engine_state is not None
            assert app.led.output.snapshot() == before
            for _ in range(8):
                target = state.game_engine_state.target
                await send('ButtonReleased', target)
                result = await send('ButtonPressed', target)
            assert result['reason'] == 'game_completed'
            assert state.mode == 'root_menu'
            assert state.game_engine_state is None
            assert 'resetgame' not in app._active_audio
    finally:
        await app._cleanup_session_resources('resetgame')


def test_clock_stale_invalid_and_paused_edges():
    state = start('catch_light', sequence=3, now=10., selection_button=1)
    for now in [float('nan'), float('inf'), 9.]:
        assert advance(state, edge='ButtonReleased', button=1, sequence=4, now=now).state == state
    state = advance(state, edge='ButtonReleased', button=1, sequence=4, now=11., paused=True).state
    state = advance(state, edge='ButtonPressed', button=1, sequence=5, now=12., paused=True).state
    assert state.round == 0
    assert advance(state, edge='ButtonPressed', button=1, sequence=6, now=13.).status == 'held'
