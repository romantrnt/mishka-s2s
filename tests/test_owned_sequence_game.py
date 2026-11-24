"""Owned timers/audio drive the actual stable-menu pattern game."""
import pytest
import pytest_asyncio
from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.game_engine import game_answer_buttons
from app.foundation.lighting import BUTTON_FILL_ELEMENTS


@pytest_asyncio.fixture
async def sequence_game():
    clock=[100.]
    app,*_=build_application(settings=Settings(profile='mock',runtime_mode='clinical'),
                            monotonic_now=lambda:clock[0],background_scheduler=False)
    seq=0
    async def send(kind,button=None,text=None,**payload):
        nonlocal seq
        seq+=1
        if button is not None:payload['button_id']=button
        return await app.receive_async(Event(kind,'sequence',seq,clock[0],'test',payload,
            f'seq:{seq}',clock[0]),text)
    async def choose(up=False):
        if up:await send('HandsetLifted')
        await send('ButtonPressed',1);await send('ButtonReleased',1)
        await send('ButtonPressed',3);await send('ButtonReleased',3)
        assert app.canonical_runtime.sessions['sequence'].active_game_id=='repeat_pattern'
    def frame(buttons):
        ids={BUTTON_FILL_ELEMENTS[b] for b in buttons}
        assert app.led.output.snapshot()=={str(i):int(i in ids) for i in range(18)}
    async def tick():
        scope=app._game_timer_scopes['sequence']
        clock[0]=scope[-1]
        results=await app.flush_game_timers()
        assert len(results)==1
        return results[0]
    async def terminal(kind='AudioCompleted',owner=None):
        owner=owner or dict(app._active_audio['sequence'])
        return await send(kind,**owner,response_id=owner['content_id'])
    try:yield app,clock,send,choose,frame,tick,terminal
    finally:await app._cleanup_session_resources('sequence')


@pytest.mark.asyncio
async def test_actual_demo_frames_then_input_and_endless_progress(sequence_game):
    app,clock,send,choose,frame,tick,terminal=sequence_game
    await choose()
    state=app.canonical_runtime.sessions['sequence']
    for level in range(5):
        assert state.game_engine_state.phase=='demonstrate'
        pattern=state.game_engine_state.pattern
        assert len(pattern)==min(5,2+level//2)
        for button in pattern:
            frame([button])
            deadline=app._game_timer_scopes['sequence'][-1]
            clock[0]=deadline-.001
            assert await app.flush_game_timers()==[]
            await tick();frame([]);await tick()
        assert state.game_engine_state.phase=='playing'
        frame(game_answer_buttons(state.game_engine_state))
        for button in pattern:
            await send('ButtonReleased',button)
            result=await send('ButtonPressed',button)
            if button==7:result=await send('ButtonReleased',button)
        assert result['reason']=='game_progress'
        assert state.game_level==level+1 and state.game_progress['repeat_pattern']==level+1
    assert state.mode=='game_active' and 'sequence' in app._game_timer_scopes
    assert 'sequence' not in app._active_audio


@pytest.mark.asyncio
async def test_wrong_answer_replays_and_demo_held_key_cannot_score(sequence_game):
    app,clock,send,choose,frame,tick,terminal=sequence_game
    await choose();state=app.canonical_runtime.sessions['sequence']
    first=state.game_engine_state.pattern[0]
    held=next(button for button in state.game_engine_state.pattern if button!=7)
    await send('ButtonReleased',held);await send('ButtonPressed',held)
    for _ in range(2*len(state.game_engine_state.pattern)):await tick()
    assert state.game_engine_state.phase=='playing'
    assert (await send('ButtonPressed',held))['reason']=='game_held'
    assert state.game_engine_state.answer_index==0
    await send('ButtonReleased',held)
    await send('ButtonReleased',first);await send('ButtonPressed',first)
    if first==7:await send('ButtonReleased',first)
    assert state.game_engine_state.answer_index==1
    expected=state.game_engine_state.pattern[1]
    wrong=next(b for b in game_answer_buttons(state.game_engine_state) if b!=expected and b!=7)
    await send('ButtonReleased',wrong)
    assert (await send('ButtonPressed',wrong))['reason']=='game_sequence_retry'
    assert state.game_engine_state.phase=='demonstrate' and state.game_engine_state.round==0
    frame([state.game_engine_state.pattern[0]])


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal_kind',['AudioCompleted','AudioFailed'])
async def test_owned_start_and_prompt_terminal_start_one_demo(sequence_game,terminal_kind):
    app,clock,send,choose,frame,tick,terminal=sequence_game
    await choose(up=True);state=app.canonical_runtime.sessions['sequence']
    assert state.game_engine_state.phase=='instruction' and 'sequence' not in app._game_timer_scopes
    start=dict(app._active_audio['sequence']);assert start['content_id']=='PHRASE_020'
    await terminal(terminal_kind,start)
    prompt=dict(app._active_audio['sequence']);assert prompt['content_id']=='GAME_PROMPT'
    assert 'sequence' not in app._game_timer_scopes
    # A failed complete question exits; a failed optional start phrase still
    # proceeds to this valid owned prompt.
    await terminal('AudioCompleted',prompt)
    frame([state.game_engine_state.pattern[0]])
    scope=app._game_timer_scopes['sequence']
    await terminal(terminal_kind,start);await terminal('AudioCompleted',prompt)
    assert app._game_timer_scopes['sequence']==scope
    for _ in range(2*len(state.game_engine_state.pattern)):await tick()
    assert state.game_engine_state.phase=='playing'
    assert app._active_audio['sequence']['content_id']=='PHRASE_042'


@pytest.mark.asyncio
async def test_clinical_hangup_during_prompt_starts_silent_demo(sequence_game):
    app,clock,send,choose,frame,tick,terminal=sequence_game
    await choose(up=True);await terminal()
    old=dict(app._active_audio['sequence'])
    await send('HandsetPlaced');state=app.canonical_runtime.sessions['sequence']
    frame([state.game_engine_state.pattern[0]])
    assert 'sequence' not in app._active_audio
    for _ in range(2*len(state.game_engine_state.pattern)):await tick()
    frame(game_answer_buttons(state.game_engine_state));assert 'sequence' not in app._active_audio
    await terminal('AudioFailed',old)
    frame(game_answer_buttons(state.game_engine_state))
    pattern=state.game_engine_state.pattern
    for button in pattern:
        await send('ButtonReleased',button)
        result=await send('ButtonPressed',button)
        if button==7:result=await send('ButtonReleased',button)
    assert result['reason']=='game_progress' and state.game_level==1


@pytest.mark.asyncio
@pytest.mark.parametrize('action',['help','exit','reset','disconnect'])
async def test_late_demo_step_does_not_overwrite_new_owner(sequence_game,action):
    app,clock,send,choose,frame,tick,terminal=sequence_game
    await choose();old=app._game_timer_scopes['sequence']
    if action=='help':await send('SpeechReceived',text='Позови взрослого')
    elif action=='exit':
        await send('ButtonPressed',7)
        for _ in range(7):await tick()
        assert app.canonical_runtime.sessions['sequence'].game_engine_state.seven_exit_phase=='ready'
        assert (await send('ButtonReleased',7))['reason']=='game_exited_by_long_button'
    elif action=='reset':await send('SessionReset')
    else:await send('HardwareDisconnected')
    assert app.canonical_runtime.sessions['sequence'].mode!='game_active'
    before=dict(app.led.output.snapshot());clock[0]=1000.
    assert await app._fire_game_timer('sequence',old) is None
    assert app.led.output.snapshot()==before


@pytest.mark.asyncio
async def test_sequence_hint_replays_are_bounded_by_existing_age_policy(sequence_game):
    app,clock,send,choose,frame,tick,terminal=sequence_game
    await choose();state=app.canonical_runtime.sessions['sequence']
    for _ in range(2*len(state.game_engine_state.pattern)):await tick()
    for count in (1,2):
        await tick()
        assert state.game_engine_state.phase=='demonstrate'
        assert state.game_engine_state.hints_emitted==count
        for _ in range(2*len(state.game_engine_state.pattern)):await tick()
        assert state.game_engine_state.hints_emitted==count and state.game_engine_state.round==0
    await tick()
    assert 'sequence' not in app._game_timer_scopes
    assert state.game_engine_state.phase=='playing' and state.game_engine_state.round==0


@pytest.mark.asyncio
async def test_start_recording_dispatch_failure_still_owns_a_valid_pattern_prompt(sequence_game):
    app,clock,send,choose,frame,tick,terminal=sequence_game
    def fail(*args,**kwargs):raise OSError('speaker unavailable')
    app.audio.play_owned=fail
    await choose(up=True);state=app.canonical_runtime.sessions['sequence']
    assert state.game_engine_state.phase=='instruction'
    assert app._active_audio['sequence']['content_id']=='GAME_PROMPT'
    await terminal()
    frame([state.game_engine_state.pattern[0]])
    await tick();frame([])


@pytest.mark.asyncio
async def test_exhibition_full_path_hangup_cancels_demo():
    from test_profile_onboarding import Harness,card
    h=Harness(card(age=4))
    try:
        await h.ready();await h.lift();await h.complete();await h.complete()
        # The first root tap opens games; the next chooses its third slot.
        for b in (1,3):
            await h.send('ButtonPressed',{'button_id':b});await h.send('ButtonReleased',{'button_id':b})
        assert h.state.active_game_id=='repeat_pattern'
        assert h.state.game_engine_state.phase=='instruction'
        await h.complete();assert h.state.active_audio_id=='GAME_PROMPT'
        await h.complete();sid=h.boundary.session_id
        scope=h.app._game_timer_scopes[sid]
        assert h.state.game_engine_state.phase=='demonstrate'
        await h.send('HandsetPlaced')
        assert sid not in h.app._game_timer_scopes
        assert not any(h.app.led.output.snapshot().values())
        h.clock[0]=scope[-1]
        assert await h.app._fire_game_timer(sid,scope) is None
        assert not any(h.app.led.output.snapshot().values())
    finally:await h.close()


@pytest.mark.asyncio
async def test_spoken_repeat_hint_waits_for_owned_prompt_before_flashing(sequence_game):
    app,clock,send,choose,frame,tick,terminal=sequence_game
    await choose(up=True);await terminal();await terminal()
    state=app.canonical_runtime.sessions['sequence']
    for _ in range(2*len(state.game_engine_state.pattern)):await tick()
    await terminal();await tick()
    assert state.game_engine_state.phase=='instruction' and 'sequence' not in app._game_timer_scopes
    frame([])
    owner=dict(app._active_audio['sequence']);assert owner['content_id']=='PHRASE_041'
    await terminal('AudioCompleted',owner)
    assert state.game_engine_state.phase=='demonstrate'
    frame([state.game_engine_state.pattern[0]])


@pytest.mark.asyncio
async def test_actual_uart_success_lands_on_next_pattern_before_demo_deadline(sequence_game):
    from app.foundation.lighting import FakeLightingOutput
    from app.foundation.stand import UARTLEDController
    app,clock,send,choose,frame,tick,terminal=sequence_game
    app.led=UARTLEDController(None,output=FakeLightingOutput(),
        monotonic_now=lambda:clock[0],background_scheduler=False)
    await choose()
    state=app.canonical_runtime.sessions['sequence']
    pattern=state.game_engine_state.pattern
    for _ in range(2*len(pattern)):await tick()
    for button in pattern:
        await send('ButtonReleased',button)
        result=await send('ButtonPressed',button)
        if button==7:result=await send('ButtonReleased',button)
    assert result['reason']=='game_progress'
    next_engine=state.game_engine_state
    assert next_engine.phase=='demonstrate' and next_engine.demonstration_on
    started=clock[0]
    assert next_engine.demonstration_deadline==pytest.approx(started+.7)
    # Any 4x80ms feedback must finish before the first 700ms demo dwell.
    clock[0]=started+.321
    app.led.advance_effects(clock[0])
    assert app.led.exhibition_effects.effect=='idle'
    assert clock[0]<app._game_timer_scopes['sequence'][-1]
    frame([next_engine.pattern[0]])
