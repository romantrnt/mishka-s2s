"""Personal stories use the existing owned wave and its lifecycle cleanup."""
import asyncio
import pytest

from app.foundation.application import STORY_PLAYBACK_WAVE_GROUPS
from app.foundation.domain import Event
from app.foundation.stand import UARTLEDController
from app.foundation.api import build_application
from app.foundation.config import Settings
from test_personal_story_service import engine


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal',['HandsetPlaced','SessionReset'])
async def test_actual_personal_wave_frames_pause_resume_exit_and_new_owner_cleanup(terminal):
    app,*_=build_application(settings=Settings(profile='mock',runtime_mode='exhibition'),background_scheduler=False)
    app.bounded_s2s=engine();app._owned_menu_voice_capable=lambda:False
    sid='personal-wave';clock=[100.];sequence=0
    app.monotonic_now=lambda:clock[0]
    app.led=led=UARTLEDController(None,monotonic_now=app.monotonic_now,background_scheduler=False)
    async def send(kind,payload=None):
        nonlocal sequence
        sequence+=1;clock[0]+=.01
        return await app.receive_async(Event(kind,sid,sequence,float(sequence),'wave-test',
            payload or {},f'wave:{sequence}',clock[0]))
    async def button(number):
        result=await send('ButtonPressed',{'button_id':number})
        await send('ButtonReleased',{'button_id':number})
        return result
    async def complete(response_id):
        lease=dict(app._active_audio[sid])
        return await send('AudioCompleted',{**lease,'response_id':response_id})
    async def story():
        await button(3)
        result=await send('ButtonPressed',{'button_id':1})
        effects=list(app.last_canonical_effects)
        await send('ButtonReleased',{'button_id':1})
        assert result['reason']=='personal_story_ready'
        return result,effects
    try:
        await send('HandsetLifted')
        # Begin at the already-admitted menu; onboarding is a separate contract.
        state=app.canonical_runtime.sessions[sid]
        state.mode='menu_ready';state.welcome_status='completed';state.welcome_deadline_monotonic=None
        result,effects=await story()
        assert led.effect_snapshot()['effect']=='wave' and led.effect_snapshot()['wave_loop']
        assert led.effect_snapshot()['wave_stage_ms']==620
        assert [e['type'] for e in effects].index('lighting_wave_intent')<[e['type'] for e in effects].index('play_personal_story')
        first=led.output.snapshot();clock[0]+=.63;led.advance_effects(clock[0])
        assert led.output.snapshot()!=first
        old=dict(app._active_audio[sid]);generation=led.effect_snapshot()['generation']
        async def pause():return {'story_id':'PERSONAL_STORY','offset_ms':1000}
        app.audio.pause=pause
        assert (await button(6))['reason']=='story_pause_requested'
        assert led.effect_snapshot()['effect']=='wave_paused'
        paused=led.output.snapshot();clock[0]+=10;led.advance_effects(clock[0])
        assert led.output.snapshot()==paused
        await complete('PHRASE_062');await button(6);await complete('PHRASE_064')
        assert led.effect_snapshot()['effect']=='wave'
        assert app._active_audio[sid]['playback_lease']!=old['playback_lease']
        before=led.output.snapshot();clock[0]+=.63;led.advance_effects(clock[0])
        assert led.output.snapshot()!=before
        assert (await button(7))['reason']=='personal_story_back'
        assert not led.effect_snapshot()['active']
        root=led.output.snapshot();clock[0]+=10;led.advance_effects(clock[0])
        assert led.output.snapshot()==root
        await story()
        assert led.effect_snapshot()['generation']>generation and led.effect_snapshot()['effect']=='wave'
        current_generation=led.effect_snapshot()['generation']
        await send('AudioCompleted',{**old,'response_id':'PERSONAL_STORY'})
        assert led.effect_snapshot()['generation']==current_generation
        assert app.canonical_runtime.sessions[sid].mode=='personal_story_playing'
        await send(terminal)
        assert not led.effect_snapshot()['active']
        terminal_frame=led.output.snapshot()
        if terminal=='HandsetPlaced':assert not any(terminal_frame.values())
        else:assert {int(key) for key,value in terminal_frame.items() if value}<={14}  # Existing lift-handset cue.
        clock[0]+=10;led.advance_effects(clock[0])
        assert led.output.snapshot()==terminal_frame
    finally:await app.shutdown_identity()


@pytest.mark.asyncio
@pytest.mark.parametrize('settle_cancelled_owner',[False,True])
async def test_personal_wave_background_scheduler_resumes_across_cancelled_owner(monkeypatch,settle_cancelled_owner):
    clock=[100.];wakeups=asyncio.Queue();frames=[]
    real_sleep=asyncio.sleep
    async def wakeup(delay):
        task=asyncio.current_task()
        if not task.get_name().startswith('mishka-led-effect-'):
            return await real_sleep(delay)
        release=asyncio.Event()
        await wakeups.put((task,release))
        await release.wait()
    async def frame_hook(frame):
        frames.append((frame,led.output.snapshot()))
    monkeypatch.setattr(asyncio,'sleep',wakeup)
    led=UARTLEDController(None,monotonic_now=lambda:clock[0],effect_hook=frame_hook)
    engine=led.exhibition_effects
    async def sleeping_owner():
        task,release=await asyncio.wait_for(wakeups.get(),1)
        assert task is engine._task and engine.task_active
        await real_sleep(0)
        return task,release
    try:
        led.start_wave(groups=STORY_PLAYBACK_WAVE_GROUPS,loop=True,stage_duration_ms=620)
        owner,release=await sleeping_owner()
        generation=engine.generation
        for _ in range(2):
            before=led.output.snapshot()
            clock[0]+=.63;release.set()
            owner,release=await sleeping_owner()
            assert led.output.snapshot()!=before
            engine.pause_effect()
            held=led.output.snapshot();count=engine.frame_count
            clock[0]+=10
            if settle_cancelled_owner:
                await asyncio.gather(owner,return_exceptions=True)
            assert led.output.snapshot()==held and engine.frame_count==count
            engine.resume_effect()
            resumed=engine._task
            assert resumed is not owner
            owner,release=await sleeping_owner()
            assert owner is resumed and engine.generation==generation
            # More than a full 7 * 620 ms cycle, with only async wakeups:
            # no manual advance() can hide a stopped background owner.
            for _ in range(9):
                before=led.output.snapshot();count=engine.frame_count
                clock[0]+=.63;release.set()
                owner,release=await sleeping_owner()
                assert led.output.snapshot()!=before and engine.frame_count>count
                assert frames[-1][1]==led.output.snapshot()
                assert engine.last_error is None
        led.cancel_effects()
        await asyncio.gather(owner,return_exceptions=True)
        assert not engine.task_active and not any(led.output.snapshot().values())
    finally:
        task=engine._task
        led.cancel_effects()
        if task is not None:await asyncio.gather(task,return_exceptions=True)
