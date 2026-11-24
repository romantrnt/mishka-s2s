import pytest
from test_scene_menu_runtime import Harness
from test_demo_story_offline_contract import UnavailableModels, TrustedValidation
from app.foundation.demo_story_catalog import load_catalog


def capable(h):
    unavailable = UnavailableModels()
    h.app.bounded_s2s.llm = unavailable
    h.app.bounded_s2s.response_validator = TrustedValidation(unavailable)
    return unavailable

@pytest.mark.asyncio
@pytest.mark.parametrize('index',range(5))
@pytest.mark.parametrize('band',['1-3','3-5','5-7'])
async def test_actual_all_five_authored_stories_use_current_age_text_without_models(index, band):
    h=Harness(band);unavailable=capable(h)
    try:
        await h.button(3);await h.release(3)
        assert h.state.scene_menu_phase=='narrating'
        page=h.state.scene_menu_descriptor
        assert page['buttons']==[1,2,3,4,5,7] and len(page['items'])==5
        selected=page['items'][index]
        assert selected['slot']==index+1
        public=next(entry for entry in load_catalog() if entry['id']==selected['id'])
        entry=h.state.personal_story_selection[index]
        assert entry['id']==selected['id'] and entry['title']==public['title']
        assert entry['text']==public['young_text' if band=='1-3' else 'text']
        result=await h.button(index+1)
        assert result['reason']=='personal_story_ready'
        assert h.state.mode=='personal_story_playing'
        assert h.played[-1].response_id=='PERSONAL_STORY'
        validator=h.app.bounded_s2s.response_validator
        assert validator.trusted_decisions and all(d.allowed for d in validator.trusted_decisions)
        assert not unavailable.calls
    finally:await h.close()

@pytest.mark.asyncio
async def test_unavailable_story_shows_no_theme_choices_and_never_calls_generation():
    h=Harness();unavailable=capable(h)
    # Local authored playback still needs the real private PCM output contract.
    h.app.audio.private_pcm_supported=False
    try:
        await h.button(3);await h.release(3)
        assert h.state.scene_menu_descriptor['items']==[]
        assert h.state.scene_menu_descriptor['buttons']==[7]
        await h.button(1);await h.release(1);await h.button(1)
        assert h.state.mode=='personal_story_theme'
        assert not h.app._personal_stories
        assert not h.app._personal_story_jobs and not unavailable.calls
    finally:await h.close()

@pytest.mark.asyncio
async def test_theme_repeat_no_is_not_interpreted_as_story_theme():
    h=Harness();unavailable=capable(h)
    try:
        await h.button(3);await h.release(3);await h.complete();await h.complete()
        assert h.state.scene_menu_phase=='repeat_listen'
        await h.send('SpeechReceived',text='Нет')
        assert h.state.scene_menu_answers['personal_story_theme']=='no'
        assert h.state.mode=='personal_story_theme'
        assert h.state.scene_menu_phase=='ready'
        assert not h.app._personal_stories and not unavailable.calls
    finally:await h.close()

@pytest.mark.asyncio
async def test_day_menu_uses_actual_capabilities_and_does_not_read_on_reentry():
    from app.foundation.schedule_weather import ProductionScheduleWeatherService
    h=Harness();h.app.schedule_weather=ProductionScheduleWeatherService(None)
    try:
        await h.button(7);await h.release(7)
        assert h.state.scene_menu_descriptor['items']==[{'slot':1,'id':'date','title':'Дата'}]
        assert h.played[-1].response_id=='MENU_DAY'
        await h.button(1);await h.release(1)
        assert h.played[-1].response_id=='TYPED_DATE'
        await h.complete()
        await h.button(7);await h.release(7)
        h.state.root_menu_back_guard_until=None
        await h.button(7)
        assert h.state.mode=='schedule_weather'
        assert h.state.scene_menu_phase=='ready'
        assert [x.response_id for x in h.played].count('MENU_DAY')==1
    finally:await h.close()

@pytest.mark.asyncio
async def test_day_first_age_page_next_selects_weather_and_failure_is_honest():
    from app.foundation.schedule_weather import ProductionScheduleWeatherService,YandexWeatherProvider
    h=Harness('1-3')
    provider=YandexWeatherProvider('synthetic-key',55.,37.)
    async def offline(*args,**kwargs):raise RuntimeError('synthetic outage')
    provider.current=offline
    h.app.schedule_weather=ProductionScheduleWeatherService(provider)
    try:
        await h.button(7);await h.release(7)
        assert [i['id'] for i in h.state.scene_menu_descriptor['items']]==['date']
        await h.button(1);await h.release(1)
        await h.button(6);await h.release(6)
        assert [i['id'] for i in h.state.scene_menu_descriptor['items']]==['weather']
        await h.button(1);await h.release(1)
        result=await h.button(1)
        assert not any(x.response_id=='TYPED_WEATHER' for x in h.played)
        assert h.state.mode=='schedule_weather'
        assert h.app._active_audio[h.sid]['content_id']=='PHRASE_100'
        assert 'synthetic outage' not in repr(result)
    finally:await h.close()


@pytest.mark.asyncio
async def test_capability_disappearing_never_remaps_old_slot_to_different_action():
    from app.foundation.schedule_weather import ProductionScheduleWeatherService,YandexWeatherProvider
    h=Harness('1-3');service=ProductionScheduleWeatherService(YandexWeatherProvider('synthetic',55.,37.))
    h.app.schedule_weather=service
    try:
        await h.button(7);await h.release(7)
        await h.button(6);await h.release(6)
        assert h.state.scene_menu_descriptor['items'][0]['id']=='weather'
        service.weather_provider=None
        result=await h.button(1)
        assert not any(x.response_id in {'TYPED_DATE','TYPED_WEATHER'} for x in h.played)
        assert h.state.scene_menu_descriptor['items'][0]['id']=='date'
        assert any(e.get('type')=='scene_menu_page' for e in result['outputs'])
    finally:await h.close()
