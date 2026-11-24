import random
import pytest
from app.foundation.game_engine import start, advance, pause


def driver(game):
    state = start(game, sequence=0, now=0., selection_button=4)
    sequence = 0
    def send(edge, button, time=None):
        nonlocal state, sequence
        sequence += 1
        result = advance(state, edge=edge, button=button, sequence=sequence,
                         now=sequence * .1 if time is None else time)
        state = result.state
        return result
    send('ButtonReleased', 4)
    return send


def test_pair_requires_two_distinct_targets():
    send = driver('find_pair')
    assert send('ButtonPressed', 1).status == 'pair_partial'
    assert send('ButtonPressed', 1).state.round == 0
    send('ButtonReleased', 1)
    assert send('ButtonPressed', 1).state.round == 0
    assert send('ButtonPressed', 3).state.round == 0
    assert send('ButtonPressed', 2).state.round == 1


def test_simultaneous_requires_real_overlap_and_no_extra_key():
    send = driver('press_together')
    send('ButtonPressed', 1)
    send('ButtonReleased', 1)
    assert send('ButtonPressed', 2).state.round == 0
    send('ButtonPressed', 3)
    assert send('ButtonPressed', 1).state.round == 0
    send('ButtonReleased', 3)
    send('ButtonReleased', 1)
    assert send('ButtonPressed', 1).state.round == 1
    assert send('ButtonPressed', 1).state.round == 1


def test_hold_uses_measured_edges_not_claimed_hold_and_requires_release():
    send = driver('hold_light')
    assert send('ButtonLongHeld', 1, 10.).state.round == 0
    send('ButtonPressed', 1, 11.)
    assert send('ButtonReleased', 1, 11.3).status == 'early_release'
    send('ButtonPressed', 1, 12.)
    ready = send('ButtonHeld', 1, 13.)
    assert ready.feedback_id == 'PHRASE_036'
    assert ready.state.round == 0
    assert send('ButtonHeld', 1, 14.).feedback_id is None
    assert send('ButtonReleased', 1, 14.1).state.round == 1


def test_pause_revokes_hold_evidence():
    state = start('hold_light', sequence=0, now=0., selection_button=4)
    state = advance(state, edge='ButtonPressed', button=1, sequence=1, now=1.).state
    state = pause(state)
    assert advance(state, edge='ButtonReleased', button=1, sequence=2, now=30.).state.round == 0

@pytest.mark.parametrize('game', ['find_pair','press_together','hold_light'])
def test_random_mechanics_never_score_non_edges_or_reserved(game):
    for seed in range(20):
        rng = random.Random(seed)
        state = start(game, sequence=0, now=0., selection_button=4)
        for seq in range(1, 150):
            edge = rng.choice(['ButtonPressed','ButtonReleased','ButtonHeld','ButtonLongHeld'])
            button = rng.randrange(1,8)
            result = advance(state, edge=edge, button=button, sequence=seq, now=seq*.25)
            assert set(result.desired_buttons) <= {1,2,3,4}
            if button in {5,6,7} or edge in {'ButtonHeld','ButtonLongHeld'}:
                assert result.state.round == state.round
            if game != 'hold_light' and edge == 'ButtonReleased':
                assert result.state.round == state.round
            state = result.state

@pytest.mark.asyncio
@pytest.mark.parametrize('game', ['find_pair','press_together','hold_light'])
async def test_actual_application_frames_for_each_mechanic(game):
    # These retained mechanics have no current public chooser slot. Start an
    # explicit compatibility owner, then exercise real application edges/frames.
    from app.foundation.canonical_contract import CanonicalState
    from app.foundation.api import build_application
    from app.foundation.config import Settings
    from app.foundation.domain import Event
    from app.foundation.lighting import BUTTON_FILL_ELEMENTS
    seq, clock = 0, 0.
    app, *_ = build_application(settings=Settings(profile='mock', runtime_mode='clinical'),
        monotonic_now=lambda:clock, background_scheduler=False)
    engine=start(game,sequence=0,now=clock,selection_button=4,run_id=f'compat-{game}')
    state=CanonicalState('mechanic',mode='game_active',handset='down',
        active_game_id=game,game_engine_state=engine,game_target=engine.target,
        game_audio_stage='playing',expected_inputs=['button','handset_up'])
    app.canonical_runtime.sessions['mechanic']=state
    async def send(kind, button=None, elapsed=.1):
        nonlocal seq, clock
        seq += 1
        clock += elapsed
        return await app.receive_async(Event(kind,'mechanic',seq,clock,'test',
            {} if button is None else {'button_id':button},f'm:{seq}',clock))
    def assert_lit(buttons):
        # The current legacy game output is colors: digits stay off.
        ids = {BUTTON_FILL_ELEMENTS[b] for b in buttons}
        assert app.led.output.snapshot() == {str(i):int(i in ids) for i in range(18)}
    try:
        await send('ButtonPressed',4)
        await send('ButtonReleased',4)
        assert state.game_engine_state.game_id==game and not state.game_engine_state.immersive
        assert_lit([1,2] if game != 'hold_light' else [1])
        await send('ButtonPressed',1)
        if game == 'find_pair':
            assert_lit([2])
            await send('ButtonReleased',1)
            await send('ButtonPressed',1)
            assert state.game_engine_state.round == 0
            await send('ButtonPressed',2)
        elif game == 'press_together':
            await send('ButtonReleased',1)
            await send('ButtonPressed',2)
            assert state.game_engine_state.round == 0
            await send('ButtonPressed',1)
        else:
            await send('ButtonReleased',1)
            assert state.game_engine_state.round == 0
            await send('ButtonPressed',1)
            await send('ButtonHeld',1,elapsed=1.1)
            assert state.game_engine_state.round == 0
            await send('ButtonReleased',1)
        assert state.game_engine_state.round == 1
        assert_lit([2,3] if game != 'hold_light' else [2])
        assert 'mechanic' not in app._active_audio  # clinical silent on-hook
        await send('ButtonPressed',5)
        assert state.mode == 'wellbeing_check'
        assert state.game_engine_state is None
    finally:
        await app._cleanup_session_resources('mechanic')
