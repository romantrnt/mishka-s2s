"""Actual application/service TTS inputs, synthetic IPC peer; no acoustic claim."""
import json
import re
from datetime import datetime, timezone

import pytest

from test_experimental_voice import configured
from test_stage1_nested_game_integrity import SyntheticVoice

@pytest.fixture
def worker_spy(configured):
    provider,_,worker=configured('vox555')
    source=worker.read_text()
    original='    Path(__file__).with_suffix(".active").write_text(str(os.getpid())+":"+text)'
    source=source.replace(original,original+'\n    with Path(__file__).with_suffix(".requests").open("a") as log: log.write(json.dumps({"text":text,"op":cmd["op"]})+"\\n")')
    # Tiny valid CPU protocol frames; no retained long synthetic audio.
    source=source.replace('12000','240').replace('144001','2881').replace('144000','2880')
    worker.write_text(source)
    def texts():
        path=worker.with_suffix('.requests')
        return [json.loads(line)['text'] for line in path.read_text().splitlines()] if path.exists() else []
    return provider,texts


def install(app,provider):
    app.bounded_s2s.tts=provider
    app.bounded_s2s.voice=SyntheticVoice()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind,button,response',[('weather',2,'TYPED_WEATHER'),('date',1,'TYPED_DATE')])
async def test_actual_public_typed_request_reaches_normalized_worker(worker_spy,kind,button,response):
    from test_product_typed_speech import Harness
    h=Harness();provider,texts=worker_spy;install(h.app,provider)
    weather={'summary':'Ясно, −2.50 °C','temperature_c':-2.5,'source':'demo'}
    snapshot=dict(weather)
    async def current(*args,**kwargs):return weather
    h.app.schedule_weather.weather=current
    try:
        await h.start();await h.button(button)
        assert h.state.active_audio_id==response
        assert texts() and not re.search(r'\d|[°℃=]',texts()[-1])
        if kind=='weather':assert 'минус две целых пятьдесят сотых градуса по Цельсию' in texts()[-1]
        else:assert texts()[-1].startswith('Сегодня ')
        assert weather==snapshot
    finally:
        await h.close();await provider.close()


@pytest.mark.asyncio
async def test_actual_menu_and_game_prompt_use_normalized_provider(worker_spy):
    from test_scene_menu_runtime import Harness
    h=Harness();provider,texts=worker_spy;install(h.app,provider)
    try:
        await h.button(1);await h.release(1)
        await h.complete()  # Actual recorded invitation terminal opens owned menu.
        assert h.state.active_audio_id=='MENU_GAME' and texts()
        menu_text=texts()[-1]
        assert 'один' in menu_text.lower() and not re.search(r'\d',menu_text)
        await h.button(2);await h.release(2)
        assert h.state.active_game_id=='quiz'
        await h.complete()
        assert h.state.active_audio_id=='GAME_PROMPT'
        assert texts()[-1]!=menu_text and not re.search(r'\d|[°℃=]',texts()[-1])
    finally:
        await h.close();await provider.close()


@pytest.mark.asyncio
async def test_typed_schedule_through_actual_app_speak_text_preserves_source(worker_spy):
    """Notification path only; does not claim schedule authorization coverage."""
    from app.foundation.api import build_application
    from app.foundation.config import Settings
    from app.foundation.canonical_services import TypedInfoRenderer
    app,*_=build_application(settings=Settings(profile='mock'),background_scheduler=False)
    provider,texts=worker_spy;install(app,provider)
    item={'status':'scheduled','start_at':'2026-11-25T09:05:00+03:00','title':'Занятие','room':'12','floor':2}
    source=dict(item)
    class Schedule:
        async def schedule(self,*args):return [item]
    from zoneinfo import ZoneInfo
    renderer=TypedInfoRenderer(Schedule(),{'timezone':'Europe/Moscow'},'synthetic',now=lambda:datetime(2026,11,24,tzinfo=ZoneInfo('Europe/Moscow')))
    try:
        typed=await renderer.render('what_next')
        turn=await app.bounded_s2s.speak_text('spoken-schedule',typed['text'])
        assert not turn.fallback_reason
        assert 'двадцать пят' in texts()[-1] and 'девять ноль пять' in texts()[-1]
        assert not re.search(r'\d',texts()[-1]) and item==source
    finally:
        await app.shutdown_identity();await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('raw,expected',[
 ('2+3','два плюс три'),
 ('2+3=5','два плюс три равно пять'),('2-3=-1','два минус три равно минус один'),
 ('2×3=6','два умножить на три равно шесть'),('6/2=3','шесть делить на два равно три'),
])
async def test_actual_stand_notification_formula_worker_input(worker_spy,raw,expected):
    from app.foundation.simulator import SimulatorAcceptanceHarness
    from app.foundation.s2s import MockPlaybackController
    h=SimulatorAcceptanceHarness();provider,texts=worker_spy;install(h.stand.application,provider)
    h.stand.application.bounded_s2s.playback=MockPlaybackController()
    try:
        await h.stand._play_response_audio('stand',raw)
        assert texts()==[expected+'.']
        assert not h.stand.errors
    finally:
        await h.stand.aclose();await provider.close()


@pytest.mark.asyncio
async def test_actual_story_service_keeps_decimal_in_one_tts_request(worker_spy):
    from app.foundation.personal_story import PersonalStoryService
    from test_personal_story_service import engine, PLOT_SENTENCES, DURATION_BOUNDS
    provider,texts=worker_spy
    e=engine()
    plot={key:' '.join(part[:7]) for key,part in zip(('setting','problem','attempt','resolution'),PLOT_SENTENCES)}
    plot['setting']=plot['setting'].replace('Зайчик нашёл семечко.','Зайчик отмерил 2.5 литра воды.',1)
    async def numeric_story(*args):
        yield json.dumps({'title':'Цветок','plot':plot,'age_band':'3-5','estimated_duration_sec':60,'safety_tags':['safe'],'ending_state':'safe_resolved'},ensure_ascii=False)
    e.llm.stream_story=numeric_story
    e.tts=provider;e.voice=SyntheticVoice()
    try:
        # Full semantic/structure path stays real. Tiny fixture PCM is expected
        # to fail the 60-second duration floor after its TTS input was observed.
        assert await PersonalStoryService(e).render('лес','3-5',(),7,lambda:True) is None
        assert texts() and texts()[0]=='Зайчик отмерил две целых пять десятых литра воды.'
        assert not any(re.search(r'\d',text) for text in texts())
    finally:await provider.close()
