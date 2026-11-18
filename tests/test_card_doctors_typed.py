"""DATA14 card names are useful partial private facts, never a fabricated plan."""
import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.foundation.canonical_services import TypedInfoRenderer
from app.foundation.domain import Event
from app.foundation.schedule_weather import ScheduleWeatherProviderError
from test_card_doctors_profile import doctor_card, mapped, NAME
from test_contextual_game_flow import Model
from test_profile_onboarding import Harness
from test_owned_private_pcm import setup, install, HANDLE
from app.foundation.validation import AsyncResponseValidator, OllamaMiniLLMValidator


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['Кто мой врач?', 'Какие у меня сегодня врачи?'])
async def test_actual_card_resolution_uses_personalize_only_for_qualified_partial_reply(text):
    h = Harness(doctor_card())
    spoken = []
    semantic_requests = []
    mini = OllamaMiniLLMValidator()
    async def request(payload):
        semantic_requests.append(json.loads(payload))
        return json.dumps({'message':{'content':json.dumps({'decision':'allow','confidence':1.})}}).encode()
    mini._request_bytes = request
    h.app.bounded_s2s.response_validator = AsyncResponseValidator(mini)
    try:
        await h.ready()
        h.state.mode, h.state.handset = 'bear_dialogue', 'up'
        model = Model()
        h.app.bounded_s2s.llm = model
        synthesize = h.app.bounded_s2s.tts.delegate.synthesize
        async def record(value, *args):
            spoken.append(value)
            return await synthesize(value, *args)
        h.app.bounded_s2s.tts.delegate.synthesize = record
        h.sequence += 1
        result = await h.app.receive_async(Event('SpeechReceived', h.boundary.session_id, h.sequence,
            1., 'test', {}, f'card:{h.sequence}', h.clock[0]), text)
        assert h.state.identity_capabilities == ['personalize']
        assert h.state.active_audio_id == 'TYPED_CARD_DOCTORS' and h.state.mode == 'bear_dialogue'
        assert NAME in spoken[-1] and '01.01.2029' in spoken[-1]
        assert 'Точного плана, времени и кабинета у меня нет' in spoken[-1]
        assert not model.calls
        assert not semantic_requests  # Private names cannot enter either model boundary.
        assert await h.app._render_typed_audio(h.boundary.session_id, 'Произвольный чужой текст.',
            {'content_id':'TYPED_CARD_DOCTORS'}, 0, lambda:True) is None
        assert not semantic_requests  # A content ID alone cannot grant trusted text.
        public = json.dumps((result, h.app.audio.commands, h.app._canonical_audio_context(h.boundary.session_id).system_prompt), ensure_ascii=False)
        assert NAME not in public and 'card_doctor_names' not in public
        await h.complete()
        assert h.state.mode == 'bear_dialogue' and h.state.dialogue_information_kind is None
        h.sequence += 1
        await h.app.receive_async(Event('SpeechReceived', h.boundary.session_id, h.sequence,
            1., 'test', {}, f'ordinary:{h.sequence}', h.clock[0]), 'Поговорим про облака')
        assert len(model.calls) == 1 and semantic_requests
        assert NAME not in json.dumps((model.calls, semantic_requests), ensure_ascii=False)
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_day_menu_advertises_only_current_card_facts_without_schedule_grant():
    h = Harness(doctor_card())
    try:
        await h.ready()
        h.state.mode, h.state.handset = 'root_menu', 'up'
        await h.send('ButtonPressed', {'button_id':7})
        items = h.state.scene_menu_descriptor['items']
        selected = next(item for item in items if item['id'] == 'card_doctors')
        assert not {'what_next','where_now'}.intersection(item['id'] for item in items)
        await h.send('ButtonReleased', {'button_id':selected['slot']})
        await h.send('ButtonPressed', {'button_id':selected['slot']})
        assert h.state.active_audio_id == 'TYPED_CARD_DOCTORS'
        old = h.callback()
        await h.send('ButtonReleased', {'button_id':7})
        await h.send('ButtonPressed', {'button_id':7})
        assert h.state.mode == 'root_menu' and not h.state.active_audio_id
        await h.send('AudioCompleted', old)
        assert h.state.mode == 'root_menu' and not h.state.active_audio_id
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('visit,expected', [('2029-01-01','сегодня'), ('2028-12-30','30.12.2028')])
async def test_card_date_and_preview_are_explicit_without_next_appointment_claim(visit, expected):
    profile, *_ = mapped(doctor_card(['Первый специалист','Второй специалист','Третий специалист'], visit))
    renderer = TypedInfoRenderer(None, profile, None, now=lambda: datetime(2029,1,1,tzinfo=timezone.utc))
    text = (await renderer.render('card_doctors'))['text']
    assert expected in text and 'Это часть списка' in text
    assert 'Третий специалист' not in text and 'следующий' not in text
    if visit != '2029-01-01':
        assert 'сегодня' not in text


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['empty','no_source','invalid_date'])
async def test_no_facts_never_means_no_appointments(mutation):
    profile, *_ = mapped(doctor_card())
    if mutation == 'empty': profile['card_doctor_names'] = []
    elif mutation == 'no_source': profile.pop('card_doctors_source')
    else: profile['card_visit_date'] = '2029-02-30'
    with pytest.raises(ScheduleWeatherProviderError):
        await TypedInfoRenderer(None, profile, None).render('card_doctors')


@pytest.mark.asyncio
async def test_card_pcm_requires_personalize_exact_scene_kind_identity_and_expiry():
    stand = await setup()
    try:
        state = stand.application.canonical_runtime.sessions[HANDLE.session_id]
        state.mode, state.dialogue_information_kind = 'bear_dialogue', 'card_doctors'
        handle = replace(HANDLE, response_id='TYPED_CARD_DOCTORS')
        install(stand, handle)
        identity = (state.profile_ref, state.identity_request_id)
        assert stand._pcm_owner_current(handle, identity)
        state.identity_capabilities = ['schedule']
        assert not stand._pcm_owner_current(handle, identity)
        state.identity_capabilities = ['personalize']
        state.dialogue_information_kind = 'weather'
        assert not stand._pcm_owner_current(handle, identity)
        state.dialogue_information_kind = 'card_doctors'
        assert not stand._pcm_owner_current(handle, ('other-child',identity[1]))
        stand.clock[0] = state.identity_expires_monotonic
        assert not stand._pcm_owner_current(handle, identity)
    finally:
        await stand.aclose()


@pytest.mark.asyncio
async def test_card_expiry_during_render_prevents_late_private_pcm():
    h = Harness(doctor_card())
    entered, release = asyncio.Event(), asyncio.Event()
    task = None
    try:
        await h.ready()
        h.state.mode, h.state.handset = 'bear_dialogue', 'up'
        synthesize = h.app.bounded_s2s.tts.delegate.synthesize
        async def blocked(value, *args):
            entered.set()
            try: await release.wait()
            except asyncio.CancelledError: await release.wait()
            return await synthesize(value, *args)
        h.app.bounded_s2s.tts.delegate.synthesize = blocked
        h.sequence += 1
        task = asyncio.create_task(h.app.receive_async(Event('SpeechReceived', h.boundary.session_id, h.sequence,
            1., 'test', {}, 'card-await', h.clock[0]), 'Кто мой врач?'))
        await asyncio.wait_for(entered.wait(), 2)
        h.clock[0] = h.state.identity_expires_monotonic
        await h.app.expire_identity_leases()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert not any(c.get('response_id') == 'TYPED_CARD_DOCTORS' for c in h.app.audio.commands)
        assert not h.app._canonical_profiles
    finally:
        release.set()
        if task: await asyncio.gather(task, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['Телефон: 1234567', 'Выпей таблетку'])
async def test_source_card_template_still_blocks_unsafe_names_without_any_model_hop(name):
    h = Harness(doctor_card([name]))
    model = Model()
    from test_persona_validation import RecordingAllow
    mini = RecordingAllow()
    h.app.bounded_s2s.response_validator = AsyncResponseValidator(mini)
    h.app.bounded_s2s.llm = model
    try:
        await h.ready()
        h.state.mode, h.state.handset = 'bear_dialogue', 'up'
        h.sequence += 1
        await h.app.receive_async(Event('SpeechReceived', h.boundary.session_id, h.sequence,
            1., 'test', {}, 'unsafe-card', h.clock[0]), 'Кто мой врач?')
        assert h.state.active_audio_id == 'PHRASE_101'
        assert not model.calls and not mini.calls
        assert not any(c.get('response_id') == 'TYPED_CARD_DOCTORS' for c in h.app.audio.commands)
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('temperature,condition,clothes', [(-2,'snow','шапка'), (10,'rain','дождевик'), (24,'clear','лёгкая одежда')])
async def test_weather_clothes_are_deterministic_from_returned_facts(temperature, condition, clothes):
    class Service:
        async def weather(self, *args, **kwargs):
            return {'summary':'Фактическая погода', 'temperature_c':temperature, 'condition':condition, 'source':'test'}
    info = await TypedInfoRenderer(Service(), None, None).render('weather', weather_scope='configured_point')
    assert clothes in info['text'] and info['source'] == 'test'


def test_day_page_does_not_claim_schedule_missing_when_available_on_another_page():
    from app.foundation.menu_content import build_menu_page
    page = build_menu_page('schedule_weather', '1-3', day_items=(
        ('date','Дата'), ('weather','Погода'), ('what_next','Следующее занятие')))
    assert page.items[0].id == 'date' and page.page_count > 1
    assert 'Расписания сейчас не вижу' not in page.spoken_text
