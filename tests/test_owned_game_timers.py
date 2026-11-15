import asyncio
import pytest
import pytest_asyncio
from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.age_policy import POLICIES
from app.foundation.game_engine import start, timer_step, next_deadline

@pytest.mark.parametrize('band',tuple(POLICIES))
def test_existing_policy_wait_windows_and_bounded_repeats(band):
    p=POLICIES[band]
    state=start('catch_light',sequence=0,now=10.,selection_button=1,run_id='test',
        response_window_s=p.game_response_window_ms/1000,first_hint_s=p.first_hint_after_seconds,hint_limit=p.repeat_count)
    assert state.response_deadline==10+p.game_response_window_ms/1000
    assert state.hint_deadline==10+p.first_hint_after_seconds
    due=next_deadline(state)
    assert timer_step(state,due-.001).state==state
    for count in range(p.repeat_count):
        result=timer_step(state,next_deadline(state))
        state=result.state
        assert state.hints_emitted==count+1
        assert state.round==0 and state.target==1
    assert next_deadline(state) is None

@pytest_asyncio.fixture
async def game():
    clock=[100.]
    app,*_=build_application(settings=Settings(profile='mock',runtime_mode='clinical'),
                            monotonic_now=lambda:clock[0],background_scheduler=False)
    sequence=0
    async def send(kind,button=None,**payload):
        nonlocal sequence
        sequence+=1
        if button is not None: payload['button_id']=button
        return await app.receive_async(Event(kind,'timer-game',sequence,clock[0],'test',payload,f't:{sequence}',clock[0]))
    await send('ButtonPressed',1)
    await send('ButtonReleased',1)
    await send('ButtonPressed',1)
    await send('ButtonReleased',1)
    try:
        yield app,clock,send
    finally:
        await app._cleanup_session_resources('timer-game')

@pytest.mark.asyncio
async def test_actual_boundary_silent_onhook_and_no_penalty(game):
    app,clock,send=game
    state=app.canonical_runtime.sessions['timer-game']
    before=dict(app.led.output.snapshot())
    original=state.game_engine_state
    scope=app._game_timer_scopes['timer-game']
    assert scope[-1]==118.
    clock[0]=117.999
    assert await app.flush_game_timers()==[]
    clock[0]=118.
    result=(await app.flush_game_timers())[0]
    assert result['reason']=='game_wait_hint'
    assert state.game_engine_state.hints_emitted==1
    assert state.game_engine_state.round==original.round
    assert state.game_level==0
    assert app.led.output.snapshot()==before
    assert 'timer-game' not in app._active_audio
    assert await app._fire_game_timer('timer-game',scope) is None
    clock[0]=136.
    assert len(await app.flush_game_timers())==1
    assert state.game_engine_state.hints_emitted==2
    assert 'timer-game' not in app._game_timer_scopes
    assert (await send('ButtonPressed',1))['reason']=='game_progress'
    assert state.game_engine_state.hints_emitted==0
    assert app._game_timer_scopes['timer-game'][-1]==154.

@pytest.mark.asyncio
async def test_external_timer_cannot_spoof_source_time_or_ownership(game):
    app,clock,send=game
    scope=app._game_timer_scopes['timer-game']
    event=Event('GameTimer','timer-game',99,9999.,'canonical_game_timer',
        {'run_id':scope[1],'round_generation':scope[2],'timer_generation':scope[3],'deadline':scope[4]},'spoof',9999.)
    result=await app.receive_async(event)
    assert result['reason']=='game_timer_untrusted'
    assert app.canonical_runtime.sessions['timer-game'].game_engine_state.hints_emitted==0
    clock[0]=118.
    assert (await app.flush_game_timers())[0]['reason']=='game_wait_hint'

@pytest.mark.asyncio
@pytest.mark.parametrize('action',['help','exit','reset','disconnect'])
async def test_late_timer_revoked_by_actual_navigation(game,action):
    app,clock,send=game
    scope=app._game_timer_scopes['timer-game']
    if action=='help': await send('ButtonPressed',5)
    elif action=='exit': await send('ButtonPressed',7)
    elif action=='reset': await send('SessionReset')
    else: await send('HardwareDisconnected')
    frame=dict(app.led.output.snapshot())
    clock[0]=1000.
    assert await app._fire_game_timer('timer-game',scope) is None
    assert 'timer-game' not in app._game_timer_scopes
    assert app.led.output.snapshot()==frame

@pytest.mark.asyncio
async def test_scope_rechecked_after_awaited_ingress_race(game):
    app,clock,send=game
    scope=app._game_timer_scopes['timer-game']
    original=app._receive_canonical_event
    entered,release=asyncio.Event(),asyncio.Event()
    async def delayed(event,*args,**kwargs):
        if event.event_type=='GameTimer':
            entered.set()
            await release.wait()
        return await original(event,*args,**kwargs)
    app._receive_canonical_event=delayed
    clock[0]=118.
    task=asyncio.create_task(app._fire_game_timer('timer-game',scope))
    try:
        await entered.wait()
        await send('ButtonPressed',5)
        frame=dict(app.led.output.snapshot())
        release.set()
        assert (await task)['reason']=='game_timer_stale'
        assert app.led.output.snapshot()==frame
    finally:
        release.set()
        await task
        app._receive_canonical_event=original

@pytest.mark.asyncio
async def test_clinical_hangup_rearms_epoch_without_sound(game):
    app,clock,send=game
    await send('HandsetLifted')
    old=app._game_timer_scopes['timer-game']
    await send('HandsetPlaced')
    current=app._game_timer_scopes['timer-game']
    assert current[0]!=old[0]
    assert current[1:]==old[1:]
    clock[0]=118.
    assert await app._fire_game_timer('timer-game',old) is None
    assert (await app.flush_game_timers())[0]['reason']=='game_wait_hint'
    assert 'timer-game' not in app._active_audio

@pytest.mark.asyncio
@pytest.mark.parametrize('age,seconds,repeats',[(2,20,2),(4,18,2),(6,15,1)])
async def test_profile_to_game_timer_and_exhibition_reset_without_state_seeding(age,seconds,repeats):
    from test_profile_onboarding import Harness,card
    h=Harness(card(age=age))
    try:
        await h.ready()
        await h.lift()
        await h.complete()  # verified profile bypasses name/age interview
        await h.complete()  # approved root menu recording
        await h.send('ButtonPressed',{'button_id':1})  # interrupt only
        await h.send('ButtonReleased',{'button_id':1})
        await h.send('ButtonPressed',{'button_id':1})
        await h.send('ButtonReleased',{'button_id':1})
        # First game-chooser press only interrupts its spoken menu.
        assert (await h.send('ButtonPressed',{'button_id':1}))['reason']=='scene_menu_skipped'
        await h.send('ButtonReleased',{'button_id':1})
        assert (await h.send('ButtonPressed',{'button_id':1}))['reason']=='game_started'
        await h.send('ButtonReleased',{'button_id':1})
        await h.complete()  # finish optional game instruction
        sid=h.boundary.session_id
        scope=h.app._game_timer_scopes[sid]
        assert scope[-1]==h.clock[0]+seconds
        assert h.state.game_engine_state.hint_limit==repeats
        h.clock[0]+=seconds
        assert (await h.app.flush_game_timers())[0]['reason']=='game_wait_hint'
        assert h.app._active_audio[sid]['content_id']=='PHRASE_030'
        scope=h.app._game_timer_scopes.get(sid)
        await h.send('HandsetPlaced')
        assert sid not in h.app._game_timer_scopes
        assert sid not in h.app._active_audio
        assert not any(h.app.led.output.snapshot().values())
        if scope is not None:
            h.clock[0]+=seconds
            assert await h.app._fire_game_timer(sid,scope) is None
    finally:
        await h.close()

@pytest.mark.asyncio
async def test_background_task_is_cancelled_on_help_and_cleanup():
    clock=[10.]
    app,*_=build_application(settings=Settings(profile='mock',runtime_mode='clinical'),
                            monotonic_now=lambda:clock[0],background_scheduler=True)
    sequence=0
    async def send(kind,button):
        nonlocal sequence
        sequence+=1
        return await app.receive_async(Event(kind,'background-game',sequence,clock[0],'test',{'button_id':button},f'b:{sequence}',clock[0]))
    try:
        for kind in ['ButtonPressed','ButtonReleased','ButtonPressed','ButtonReleased']:
            await send(kind,1)
        task=app._game_timer_tasks['background-game']
        assert not task.done()
        await send('ButtonPressed',5)
        await asyncio.sleep(0)
        assert task.done()
        assert 'background-game' not in app._game_timer_tasks
        assert 'background-game' not in app._game_timer_scopes
    finally:
        await app._cleanup_session_resources('background-game')
