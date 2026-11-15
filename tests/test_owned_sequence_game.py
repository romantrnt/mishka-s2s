import asyncio
import pytest
import pytest_asyncio
from app.foundation.game_engine import AVAILABLE_GAME_SPECS
from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.canonical_services import BUTTON_ELEMENTS

@pytest_asyncio.fixture
async def sequence_game(monkeypatch):
    # Public selector catalog seam only. No canonical state or phase mutation.
    import app.foundation.canonical_contract as contract
    monkeypatch.setattr(contract,'AVAILABLE_GAME_SPECS',tuple(x for x in AVAILABLE_GAME_SPECS if x.game_id=='repeat_pattern'))
    clock=[100.]
    app,*_=build_application(settings=Settings(profile='mock',runtime_mode='clinical'),
                            monotonic_now=lambda:clock[0],background_scheduler=False)
    seq=0
    async def send(kind,button=None,**payload):
        nonlocal seq
        seq+=1
        if button is not None: payload['button_id']=button
        return await app.receive_async(Event(kind,'sequence',seq,clock[0],'test',payload,f'seq:{seq}',clock[0]))
    async def choose(up=False):
        if up: await send('HandsetLifted')
        for kind in ['ButtonPressed','ButtonReleased'] * (3 if up else 2):
            await send(kind,1)  # lifted first submenu press interrupts; release then selects
    def frame(buttons):
        ids={i for b in buttons for i in BUTTON_ELEMENTS[b]}
        assert app.led.output.snapshot()=={str(i):int(i in ids) for i in range(18)}
    async def tick():
        scope=app._game_timer_scopes['sequence']
        clock[0]=scope[-1]
        results=await app.flush_game_timers()
        assert len(results)==1
        return results[0]
    try: yield app,clock,send,choose,frame,tick
    finally: await app._cleanup_session_resources('sequence')

@pytest.mark.asyncio
async def test_actual_demo_frames_then_input_and_finite_success(sequence_game):
    app,clock,send,choose,frame,tick=sequence_game
    await choose()
    state=app.canonical_runtime.sessions['sequence']
    for round_index in range(3):
        assert state.game_engine_state.phase=='demonstrate'
        pattern=state.game_engine_state.pattern
        assert len(pattern)==round_index+2
        for index,button in enumerate(pattern):
            frame([button])
            deadline=app._game_timer_scopes['sequence'][-1]
            clock[0]=deadline-.001
            assert await app.flush_game_timers()==[]
            await tick();frame([])
            await tick()
        assert state.game_engine_state.phase=='playing'
        frame([1,2,3,4])
        for button in pattern:
            await send('ButtonReleased',button)
            result=await send('ButtonPressed',button)
        assert result['reason']==('game_completed' if round_index==2 else 'game_progress')
    assert state.mode=='root_menu'
    assert state.game_engine_state is None
    assert 'sequence' not in app._game_timer_scopes
    assert 'sequence' not in app._active_audio

@pytest.mark.asyncio
async def test_wrong_answer_replays_and_demo_held_key_cannot_score(sequence_game):
    app,clock,send,choose,frame,tick=sequence_game
    await choose()
    state=app.canonical_runtime.sessions['sequence']
    await send('ButtonPressed',1)  # held while demonstrating
    for _ in range(4): await tick()
    assert state.game_engine_state.phase=='playing'
    assert (await send('ButtonPressed',1))['reason']=='game_held'
    assert state.game_engine_state.answer_index==0
    await send('ButtonReleased',1)
    await send('ButtonPressed',1)
    assert state.game_engine_state.answer_index==1
    assert (await send('ButtonPressed',4))['reason']=='game_sequence_retry'
    assert state.game_engine_state.phase=='demonstrate'
    assert state.game_engine_state.round==0
    frame([1])

@pytest.mark.asyncio
@pytest.mark.parametrize('terminal',['AudioCompleted','AudioFailed'])
async def test_owned_instruction_terminal_starts_one_demo(sequence_game,terminal):
    app,clock,send,choose,frame,tick=sequence_game
    await choose(up=True)
    state=app.canonical_runtime.sessions['sequence']
    assert state.game_engine_state.phase=='instruction'
    assert 'sequence' not in app._game_timer_scopes
    owner=dict(app._active_audio['sequence'])
    assert owner['content_id']=='PHRASE_040'
    await send(terminal,**owner,response_id='PHRASE_040')
    frame([1])
    scope=app._game_timer_scopes['sequence']
    await send(terminal,**owner,response_id='PHRASE_040')
    assert app._game_timer_scopes['sequence']==scope
    for _ in range(4): await tick()
    assert state.game_engine_state.phase=='playing'
    assert app._active_audio['sequence']['content_id']=='PHRASE_042'

@pytest.mark.asyncio
async def test_clinical_hangup_during_instruction_starts_silent_demo(sequence_game):
    app,clock,send,choose,frame,tick=sequence_game
    await choose(up=True)
    old=dict(app._active_audio['sequence'])
    await send('HandsetPlaced')
    frame([1])
    assert 'sequence' not in app._active_audio
    for _ in range(4): await tick()
    frame([1,2,3,4])
    assert 'sequence' not in app._active_audio
    await send('AudioFailed',**old,response_id='PHRASE_040')
    frame([1,2,3,4])

@pytest.mark.asyncio
@pytest.mark.parametrize('action',['help','exit','reset','disconnect'])
async def test_late_demo_step_does_not_overwrite_new_owner(sequence_game,action):
    app,clock,send,choose,frame,tick=sequence_game
    await choose()
    old=app._game_timer_scopes['sequence']
    if action=='help': await send('ButtonPressed',5)
    elif action=='exit': await send('ButtonPressed',7)
    elif action=='reset': await send('SessionReset')
    else: await send('HardwareDisconnected')
    before=dict(app.led.output.snapshot())
    clock[0]=1000.
    assert await app._fire_game_timer('sequence',old) is None
    assert app.led.output.snapshot()==before

@pytest.mark.asyncio
async def test_sequence_hint_replays_are_bounded_by_existing_age_policy(sequence_game):
    app,clock,send,choose,frame,tick=sequence_game
    await choose()
    state=app.canonical_runtime.sessions['sequence']
    for _ in range(4): await tick()
    for count in (1,2):
        await tick()  # existing 18-second hint deadline
        assert state.game_engine_state.phase=='demonstrate'
        assert state.game_engine_state.hints_emitted==count
        for _ in range(4): await tick()
        assert state.game_engine_state.hints_emitted==count
        assert state.game_engine_state.round==0
    await tick()  # final response-window observation, not another hint
    assert 'sequence' not in app._game_timer_scopes
    assert state.game_engine_state.phase=='playing'
    assert state.game_engine_state.round==0

@pytest.mark.asyncio
async def test_instruction_dispatch_failure_still_starts_visual_demo(sequence_game):
    app,clock,send,choose,frame,tick=sequence_game
    def fail(*args,**kwargs): raise OSError('speaker unavailable')
    app.audio.play_owned=fail
    await choose(up=True)
    state=app.canonical_runtime.sessions['sequence']
    assert state.game_engine_state.phase=='demonstrate'
    assert state.audio_failure_code=='game_instruction_audio_unavailable'
    frame([1])
    await tick();frame([])

@pytest.mark.asyncio
async def test_exhibition_full_path_hangup_cancels_demo(monkeypatch):
    import app.foundation.canonical_contract as contract
    from test_profile_onboarding import Harness,card
    monkeypatch.setattr(contract,'AVAILABLE_GAME_SPECS',tuple(x for x in AVAILABLE_GAME_SPECS if x.game_id=='repeat_pattern'))
    h=Harness(card(age=4))
    try:
        await h.ready();await h.lift();await h.complete();await h.complete()
        for kind in ['ButtonPressed','ButtonReleased'] * 4:
            await h.send(kind,{'button_id':1})  # root interrupt, enter, submenu interrupt, select
        assert h.state.game_engine_state.phase=='instruction'
        await h.complete()
        sid=h.boundary.session_id
        scope=h.app._game_timer_scopes[sid]
        assert h.state.game_engine_state.phase=='demonstrate'
        await h.send('HandsetPlaced')
        assert sid not in h.app._game_timer_scopes
        assert not any(h.app.led.output.snapshot().values())
        h.clock[0]=scope[-1]
        assert await h.app._fire_game_timer(sid,scope) is None
        assert not any(h.app.led.output.snapshot().values())
    finally: await h.close()

@pytest.mark.asyncio
async def test_spoken_repeat_hint_waits_for_owned_prompt_before_flashing(sequence_game):
    app,clock,send,choose,frame,tick=sequence_game
    await choose(up=True)
    owner=dict(app._active_audio['sequence'])
    await send('AudioCompleted',**owner,response_id='PHRASE_040')
    for _ in range(4): await tick()
    owner=dict(app._active_audio['sequence'])
    await send('AudioCompleted',**owner,response_id='PHRASE_042')
    await tick()
    state=app.canonical_runtime.sessions['sequence']
    assert state.game_engine_state.phase=='instruction'
    assert 'sequence' not in app._game_timer_scopes
    frame([])
    owner=dict(app._active_audio['sequence'])
    assert owner['content_id']=='PHRASE_041'
    await send('AudioCompleted',**owner,response_id='PHRASE_041')
    assert state.game_engine_state.phase=='demonstrate'
    frame([1])
