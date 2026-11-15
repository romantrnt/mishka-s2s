import asyncio
from types import SimpleNamespace
import pytest
from test_personal_welcome_stand_integration import wired
from test_private_welcome import start,deadline,speech
from app.foundation.stand import UARTLEDController
from app.foundation.lighting import FakeLightingOutput, BUTTON_FILL_ELEMENTS, BUTTON_DIGIT_ELEMENTS, PICTOGRAM_ELEMENTS

@pytest.mark.asyncio
async def test_actual_private_welcome_pcm_completion_lighting_trace():
    h,render,stand=wired('wait')
    output=FakeLightingOutput()
    led=UARTLEDController(SimpleNamespace(send=lambda message:None),output=output,monotonic_now=lambda:h.clock[0],background_scheduler=False)
    h.app.led=led
    lit=lambda:tuple(sorted(int(k) for k,v in output.snapshot().items() if v))
    try:
        await start(h)
        print('lift',lit())
        h.clock[0]=15.;led.advance_effects(15.)
        before=lit();print('wave_finished',before)
        result=await deadline(h)
        await stand.playback.started.wait()
        print('private_pcm_started',lit(),result['reason'])
        assert lit()==before
        lease=dict(h.app._active_audio[h.boundary.session_id])
        batch_start=len(output.batches)
        stand.playback.release.set()
        await asyncio.gather(*tuple(stand._response_audio_tasks))
        print('private_pcm_completed',lit(),h.app.last_canonical_effects)
        assert h.state.mode=='personal_welcome_listen'
        expected=tuple(sorted((*BUTTON_FILL_ELEMENTS.values(),PICTOGRAM_ELEMENTS[6],BUTTON_DIGIT_ELEMENTS[7][1])))
        assert lit()==expected
        assert led.effect_snapshot()['effect']=='breathing'
        assert all(any(command.state for command in batch) for batch in output.batches[batch_start:])
        generation=led.effect_snapshot()['generation']
        assert await h.app._set_dialogue_lighting_phase(h.boundary.session_id,'processing') is None
        assert led.effect_snapshot()['generation']==generation
        await h.send('AudioCompleted',{**lease,'response_id':lease['content_id']})
        assert lit()==expected and led.effect_snapshot()['generation']==generation
        result=await speech(h,'Хорошо')
        print('answer',result['reason'],lit())
        assert result['reason']=='personal_welcome_answer_received'
        await h.send('HandsetPlaced')
        led.advance_effects(h.clock[0]+20)
        assert not lit()
    finally:
        stand.playback.release.set();stand._cancel_response_audio();led.cancel_effects(clear=True);await h.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('interrupt',['hangup','revoke'])
async def test_revoked_private_completion_cannot_relight_listening(interrupt):
    h,render,stand=wired('wait');output=FakeLightingOutput()
    led=UARTLEDController(SimpleNamespace(send=lambda message:None),output=output,monotonic_now=lambda:h.clock[0],background_scheduler=False)
    h.app.led=led
    try:
        await start(h);h.clock[0]=15.;led.advance_effects(15.)
        await deadline(h);await stand.playback.started.wait()
        lease=dict(h.app._active_audio[h.boundary.session_id])
        tasks=tuple(stand._response_audio_tasks)
        if interrupt=='hangup':await h.send('HandsetPlaced')
        else:
            h.client.value=None;h.client.events.append({'kind':'clear','reason_code':'scan_started'})
            await h.boundary.step()
        stand.playback.release.set();await asyncio.gather(*tasks,return_exceptions=True)
        await h.send('AudioCompleted',{**lease,'response_id':lease['content_id']})
        led.advance_effects(h.clock[0]+30)
        assert not any(output.snapshot().values())
        assert led.effect_snapshot()['effect']=='idle'
    finally:
        stand.playback.release.set();stand._cancel_response_audio();led.cancel_effects(clear=True);await h.close()

@pytest.mark.asyncio
async def test_static_listening_adapter_uses_same_logical_frame():
    from test_private_welcome import setup
    from app.foundation.canonical_services import LED_PRESETS,DIALOGUE_FILL_ELEMENTS,DIALOGUE_ANCHOR_ELEMENTS
    h,render=setup()
    try:
        await start(h);await deadline(h);await h.complete()
        expected=set(DIALOGUE_FILL_ELEMENTS+DIALOGUE_ANCHOR_ELEMENTS)
        assert {key for key,value in LED_PRESETS['LISTENING'] if value}==expected
        assert {int(key) for key,value in h.app.led.output.snapshot().items() if value}==expected
    finally:await h.close()
