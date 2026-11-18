import asyncio
import pytest
from test_scene_menu_runtime import Harness
from app.foundation.schedule_weather import ProductionScheduleWeatherService,YandexWeatherProvider

async def weather_page(h):
    service=ProductionScheduleWeatherService(YandexWeatherProvider('synthetic',55.,37.))
    service.menu_test_calls=[]
    async def forbidden(*args,**kwargs):
        service.menu_test_calls.append(True)
        raise AssertionError('menu must not query provider')
    service.weather_provider.current=forbidden
    h.app.schedule_weather=service
    await h.button(7);await h.release(7)
    await h.button(6);await h.release(6)
    assert h.state.scene_menu_descriptor['items'][0]['id']=='weather'
    return service

@pytest.mark.asyncio
@pytest.mark.parametrize('outcome',['complete','skip','failed','unwired','onhook'])
async def test_changed_spoken_slot_requires_new_narration_not_second_fresh_press(outcome):
    h=Harness('1-3')
    try:
        service=await weather_page(h)
        count=len(h.render.texts)
        service.weather_provider=None
        if outcome=='unwired':h.app.audio.private_pcm_supported=False
        if outcome=='onhook':
            h.state.handset='down';h.app.sessions.get(h.sid).handset_up=False
        await h.button(1);await h.release(1)
        assert h.state.scene_menu_refresh_required
        assert h.state.scene_menu_descriptor['items'][0]['id']=='date'
        if outcome in {'unwired','onhook'}:
            assert len(h.render.texts)==count
        else:
            assert len(h.render.texts)==count+1
            assert 'Дата' in h.render.texts[-1] and 'Погода' not in h.render.texts[-1]
            if outcome in {'complete','failed'}:await h.complete(failed=outcome=='failed')
        await h.button(1);await h.release(1)
        if outcome=='complete':
            assert not h.state.scene_menu_refresh_required
            assert h.played[-1].response_id=='TYPED_DATE'
        else:
            assert not any(x.response_id=='TYPED_DATE' for x in h.played)
            await h.button(1);await h.release(1)
            assert not any(x.response_id=='TYPED_DATE' for x in h.played)
        if outcome=='onhook':
            await h.send('HandsetLifted')
            assert h.state.scene_menu_phase=='narrating'
            await h.complete()
            assert not h.state.scene_menu_refresh_required
        assert not service.menu_test_calls
        assert not h.state.identity_capabilities
        assert not h.app._canonical_profiles
        assert not h.app._canonical_schedule_refs.get(h.sid)
    finally:await h.close()

@pytest.mark.asyncio
async def test_change_during_pending_narration_revokes_old_render_and_completion():
    h=Harness('1-3');pending=None;gate=asyncio.Event()
    try:
        service=await weather_page(h)
        h.render.entered.clear();h.render.block=gate
        pending=asyncio.create_task(h.button(6)) # next page currently date
        await asyncio.wait_for(h.render.entered.wait(),2)
        old=dict(h.app._active_audio.get(h.sid,{ }))
        service.weather_provider=None;h.render.block=None
        result=await h.release(6)
        assert h.state.scene_menu_phase=='narrating'
        assert h.state.scene_menu_refresh_required
        current=dict(h.app._active_audio[h.sid])
        gate.set();await pending
        assert h.app._active_audio[h.sid]==current
        assert 'Дата' in h.render.texts[-1]
        await h.complete()
        assert not h.state.scene_menu_refresh_required
        assert h.state.scene_menu_phase=='ready'
    finally:
        gate.set()
        if pending:await asyncio.gather(pending,return_exceptions=True)
        await h.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('button',[5,7])
async def test_refresh_failure_keeps_help_and_back_available(button):
    h=Harness('1-3')
    try:
        service=await weather_page(h);service.weather_provider=None
        await h.button(1);await h.release(1);await h.complete(failed=True)
        await h.button(button)
        assert h.state.mode==('wellbeing_check' if button==5 else 'root_menu')
        assert not h.state.scene_menu_refresh_required
    finally:await h.close()
