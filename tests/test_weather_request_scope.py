"""Request-local weather scope; no live provider or model calls."""
import asyncio
import json
import time

import pytest

from app.foundation import canonical_services
from app.foundation.menu_content import build_menu_page
from app.foundation.schedule_weather import (
    ProductionScheduleWeatherService, ScheduleWeatherProviderError, YandexWeatherProvider,
)


@pytest.fixture
def current_weather():
    provider = YandexWeatherProvider('synthetic', 55., 37.)
    calls = []

    async def fetch():
        calls.append(True)
        return provider._normalize({'info': {}, 'forecasts': [], 'fact': {
            'condition': 'clear', 'temp': 3, 'obs_time': time.time(),
        }})

    provider._fetch = fetch
    return ProductionScheduleWeatherService(provider), calls


@pytest.mark.parametrize('text,expected', [
    ('Какая погода?', 'configured_point'),
    ('Какая сейчас погода?', 'configured_point'),
    ('Какая сегодня погода?', 'configured_point'),
    ('Какая погода на сегодня?', 'configured_point'),
    ('Что мне надеть на прогулку?', 'configured_point'),
    ('Какая температура на улице?', 'configured_point'),
    ('Что надеть в такую погоду?', 'configured_point'),
    ('Что надеть в дождь?', 'configured_point'),
    ('Что надеть в прохладную погоду?', 'configured_point'),
    ('Какая погода в прохладную погоду?', 'unresolved_request'),
    ('Что надеть для прогулки?', 'configured_point'),
    ('Какая сегодня погода для прогулки?', 'configured_point'),
    ('Что надеть в дождь в Москве?', 'unresolved_request'),
    ('Какая погода в данный момент?', 'configured_point'),
    ('Какая погода в Москве?', 'unresolved_request'),
    ('В Санкт-Петербурге какая погода?', 'unresolved_request'),
    ('Какая погода в санкт-петербурге?', 'unresolved_request'),
    ('Какая погода на даче?', 'unresolved_request'),
    ('Какая погода у бабушки?', 'unresolved_request'),
    ('Какая погода для Москвы?', 'unresolved_request'),
    ('Какая погода для «Москвы»?', 'unresolved_request'),
    ('Какая погода для нашего города?', 'unresolved_request'),
    ('Расскажи погоду для Сочи', 'unresolved_request'),
    ('Какая там погода?', 'unresolved_request'),
    ('Для какого города эта погода?', 'source_place_question'),
    ('Для какого города у тебя погода?', 'source_place_question'),
    ('Погоду какого города ты знаешь?', 'source_place_question'),
    ('Какая погода завтра?', 'unresolved_request'),
    ('Какая погода вчера?', 'unresolved_request'),
    ('Какая погода на выходных?', 'unresolved_request'),
    ('Какая погода в понедельник?', 'unresolved_request'),
    ('Какая погода сегодня вечером?', 'unresolved_request'),
    ('Какая погода через час?', 'unresolved_request'),
    ('Какая погода через пять часов?', 'unresolved_request'),
    ('Какая погода через двадцать пять минут?', 'unresolved_request'),
    ('Какая погода через 5 часов?', 'unresolved_request'),
    ('Какая погода спустя полчаса?', 'unresolved_request'),
    ('Какая будет погода?', 'unresolved_request'),
    ('Какая погода будет сегодня?', 'unresolved_request'),
    ('Какая ожидается погода?', 'unresolved_request'),
    ('Какой прогноз погоды на сегодня?', 'unresolved_request'),
    ('Расскажи прогноз погоды', 'unresolved_request'),
    ('Какая была погода?', 'unresolved_request'),
    ('Какая погода 25 ноября?', 'unresolved_request'),
    ('Какая погода в Москве завтра?', 'unresolved_request'),
    ('Мишка, где ты живёшь?', None),
    ('Какая погода в твоём лесу?', None),
    ('Расскажи сказку про погоду в Москве', None),
    ('Почему меняется погода?', None),
    ('Не рассказывай про погоду в Москве', None),
    ('Я не знаю, какая погода?', 'configured_point'),
    ('', None),
    (None, None),
])
def test_only_admitted_factual_weather_has_a_closed_scope(text, expected):
    assert canonical_services.weather_request_scope(text) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['Какая погода?', 'Какая сейчас погода?', 'Какая сегодня погода?', 'Какая погода на сегодня?'])
async def test_current_requests_keep_real_typed_current_fact_path(current_weather, text):
    service, calls = current_weather
    result = await canonical_services.TypedInfoRenderer(service, None, None).render(
        'weather', weather_scope=canonical_services.weather_request_scope(text))
    assert len(calls) == 1
    assert result['source'] == 'yandex_weather'
    assert '+3 °C' in result['text'] and 'куртка' in result['text']
    assert not any(word in result['text'] for word in ('город', 'телефон', 'Москв'))


@pytest.mark.asyncio
@pytest.mark.parametrize('scope,expected', [
    ('requested_place', 'в этом месте'),
    ('source_place_question', 'Название города'),
    ('unsupported_time', 'на этот день'),
])
async def test_unsupported_scope_never_fetches_unrelated_current_fact(current_weather, scope, expected):
    service, calls = current_weather
    result = await canonical_services.TypedInfoRenderer(service, None, None).render(
        'weather', weather_scope=scope)
    assert not calls
    assert result['kind'] == 'weather' and expected in result['text']
    assert set(result) == {'kind', 'text'}
    assert '+3' not in result['text']


@pytest.mark.asyncio
@pytest.mark.parametrize('scope', [None, 'Москва', 'configured_point; tomorrow', []])
async def test_absent_or_open_scope_fails_closed_before_provider(current_weather, scope):
    service, calls = current_weather
    with pytest.raises(ScheduleWeatherProviderError, match='^weather_request_scope_unavailable$'):
        await canonical_services.TypedInfoRenderer(service, None, None).render('weather', weather_scope=scope)
    assert not calls


@pytest.mark.parametrize('band', ['1-3', '4-5', '6-7'])
def test_missing_day_weather_is_explained_without_fake_choice(band):
    page = build_menu_page('schedule_weather', band, day_items=(('date', 'Дата'),))
    assert page.buttons == (1, 5, 7)
    assert [(item.id, item.title) for item in page.items] == [('date', 'Дата')]
    assert 'Погоду сейчас не могу проверить.' in page.spoken_text
    assert page.spoken_text.index('Погоду сейчас') < page.spoken_text.index('Кнопка один')
    assert page.action_for(5) == ('help', None) and page.action_for(7) == ('back', None)
    assert len(page.spoken_text) <= 320


def test_weather_on_another_page_is_not_announced_as_unavailable():
    items = (('date', 'Дата'), ('weather', 'Погода'))
    first = build_menu_page('schedule_weather', '1-3', day_items=items)
    second = build_menu_page('schedule_weather', '1-3', 1, day_items=items)
    assert first.items[0].id == 'date' and second.items[0].id == 'weather'
    for page in (first, second):
        assert 'Погоду сейчас не могу проверить.' not in page.spoken_text


async def weather_app(route, service, *, band='4-5'):
    from test_contextual_game_flow import Harness
    from test_scene_menu_runtime import Render
    h = Harness(band=band, mode='bear_dialogue' if route == 'bear' else 'root_menu')
    render = Render()
    h.app.bounded_s2s.tts = h.app.bounded_s2s.voice = render
    h.app.schedule_weather = service
    if route == 'day':
        await h.key(7)
        await h.done()
        await h.done()
        await h.send('SpeechReceived', text='Нет')
        assert h.state.mode == 'schedule_weather' and h.state.scene_menu_phase == 'ready'
    render.texts.clear()
    return h, render


@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['bear', 'day'])
async def test_actual_admitted_current_weather_reaches_owned_audio(current_weather, route):
    service, calls = current_weather
    h, render = await weather_app(route, service)
    try:
        result = await h.send('SpeechReceived', text='Какая сейчас погода?')
        assert result['reason'] == 'typed_info_requested'
        assert h.state.active_audio_id == 'TYPED_WEATHER' and len(calls) == 1
        assert len(render.texts) == 1 and '+3 °C' in render.texts[0]
        assert not h.model.calls
        await h.done()
        assert h.state.mode == ('bear_dialogue' if route == 'bear' else 'schedule_weather')
        assert h.state.active_audio_id is None
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_actual_current_day_button_has_scope_from_accepted_descriptor(current_weather):
    service, calls = current_weather
    h, render = await weather_app('day', service)
    try:
        button = next(item['slot'] for item in h.state.scene_menu_descriptor['items'] if item['id'] == 'weather')
        await h.key(button)
        assert h.state.active_audio_id == 'TYPED_WEATHER' and len(calls) == 1
        assert len(render.texts) == 1 and '+3 °C' in render.texts[0]
        assert not h.model.calls
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['bear', 'day'])
@pytest.mark.parametrize('text,fragment', [
    ('Какая погода в Синтетикограде?', 'вопрос о погоде'),
    ('Для какого города у тебя погода?', 'Название города'),
    ('Какая погода завтра?', 'вопрос о погоде'),
    ('Какая погода вчера?', 'вопрос о погоде'),
    ('Какая погода на выходных?', 'вопрос о погоде'),
    ('Какая погода через пять часов?', 'вопрос о погоде'),
    ('Какая погода через 5 часов?', 'вопрос о погоде'),
    ('Какая будет погода?', 'вопрос о погоде'),
    ('Какой прогноз погоды на сегодня?', 'вопрос о погоде'),
    ('Какая погода для Москвы?', 'вопрос о погоде'),
    ('Какая погода для «Москвы»?', 'вопрос о погоде'),
    ('Какая погода для нашего города?', 'вопрос о погоде'),
])
async def test_actual_routes_keep_unsupported_scope_and_private_utterance_local(current_weather, route, text, fragment):
    service, calls = current_weather
    h, render = await weather_app(route, service)
    try:
        result = await h.send('SpeechReceived', text=text)
        assert h.state.active_audio_id == 'TYPED_WEATHER' and not calls and not h.model.calls
        assert len(render.texts) == 1 and fragment in render.texts[0]
        assert '+3' not in render.texts[0]
        public = json.dumps((result, h.app.audio.commands, h.app.last_canonical_event), ensure_ascii=False)
        assert text not in public and 'Синтетикоград' not in public
        assert 'weather_scope' not in public and 'requested_place' not in public
        assert not h.app.conversation_contexts
        await h.done()
        assert h.state.mode == ('bear_dialogue' if route == 'bear' else 'schedule_weather')
        assert h.state.active_audio_id is None
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['Что надеть в дождь?', 'Что надеть в прохладную погоду?',
    'Что надеть для прогулки?', 'Какая сегодня погода для прогулки?'])
async def test_actual_clothes_context_uses_current_facts_without_forced_city(current_weather, text):
    service, calls = current_weather
    h, render = await weather_app('bear', service)
    try:
        await h.send('SpeechReceived', text=text)
        assert h.state.active_audio_id == 'TYPED_WEATHER' and len(calls) == 1
        assert 'куртка' in render.texts[-1] and 'месте' not in render.texts[-1]
        assert not h.model.calls
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['Мишка, где ты живёшь?', 'Какая погода в твоём волшебном лесу?',
    'Почему меняется погода?', 'Не рассказывай про погоду'])
async def test_model_cannot_borrow_prior_weather_request_for_nonfactual_input(current_weather, text):
    service, calls = current_weather
    h, render = await weather_app('bear', service)
    try:
        await h.send('SpeechReceived', text='Какая сейчас погода?')
        await h.done()
        render.texts.clear()
        h.model.answers = ['<action type="request_information" kind="weather"/>']
        await h.send('SpeechReceived', text=text)
        assert len(h.model.calls) == 1 and len(calls) == 1
        assert h.state.active_audio_id != 'TYPED_WEATHER'
        assert not any('+3' in value for value in render.texts)
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_authorized_followup_without_private_origin_cannot_take_scope_from_payload(current_weather):
    from app.foundation.domain import Event
    service, calls = current_weather
    h, render = await weather_app('bear', service)
    try:
        await h.app._receive_canonical_event(Event('DialogueFallback', h.state.session_id, 1, 1.,
            'bounded_catalogue', {'reason': 'catalogue_request', 'action': 'request_information',
                'target': 'weather', 'weather_scope': 'configured_point'}, 'synthetic'),
            dialogue_fallback_authorized=True)
        assert not calls and not any('+3' in value for value in render.texts)
        assert h.state.active_audio_id == 'PHRASE_100'
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['untrusted', 'unverified', 'danger'])
async def test_weather_scope_never_bypasses_input_safety(current_weather, case):
    from app.foundation.input_safety import InputSafetyDecision
    service, calls = current_weather
    h, render = await weather_app('bear', service)
    try:
        if case == 'unverified':
            async def classify(*args, **kwargs):
                return InputSafetyDecision('none', 'synthetic_unavailable', False)
            h.app.safety_classifier.classify = classify
        await h.send('SpeechReceived', {'_stt_untrusted': True} if case == 'untrusted' else {},
            text='Меня бьют, какая погода?' if case == 'danger' else 'Какая погода в Москве?')
        assert not calls and not h.model.calls
        assert h.state.active_audio_id != 'TYPED_WEATHER'
        if case == 'danger':
            assert h.state.mode == 'safety_stop'
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['HandsetPlaced', 'SessionReset', 'back', 'new_input'])
async def test_late_provider_cannot_outlive_the_request_owner(current_weather, action):
    service, calls = current_weather
    h, render = await weather_app('bear', service)
    entered, release = asyncio.Event(), asyncio.Event()
    original, task = service.weather, None
    async def blocked(*args, **kwargs):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return await original(*args, **kwargs)
    service.weather = blocked
    try:
        task = asyncio.create_task(h.send('SpeechReceived', text='Какая сейчас погода?'))
        await asyncio.wait_for(entered.wait(), 2)
        if action == 'back':
            await h.key(7)
        elif action == 'new_input':
            await h.send('SpeechReceived', text='Какая погода завтра?')
        else:
            await h.send(action)
        release.set()
        await task
        assert not any('+3' in value for value in render.texts)
        if action == 'new_input':
            assert len(render.texts) == 1 and 'вопрос о погоде' in render.texts[0]
        else:
            assert not any(c.get('response_id') == 'TYPED_WEATHER' for c in h.app.audio.commands)
    finally:
        release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_actual_max_identity_expiry_fences_pending_public_weather(current_weather):
    from app.foundation.domain import Event
    from test_profile_onboarding import Harness, card
    from test_scene_menu_runtime import Render
    service, calls = current_weather
    h = Harness(card())
    render = Render()
    h.app.bounded_s2s.tts = h.app.bounded_s2s.voice = render
    h.app.schedule_weather = service
    entered, release, task = asyncio.Event(), asyncio.Event(), None
    original = service.weather
    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)
    service.weather = blocked
    try:
        await h.ready()
        h.state.mode, h.state.handset = 'bear_dialogue', 'up'
        epoch = h.app._session_epochs.get(h.boundary.session_id, 0)
        h.sequence += 1
        task = asyncio.create_task(h.app.receive_async(Event('SpeechReceived', h.boundary.session_id,
            h.sequence, 1., 'test', {}, 'weather-expiry', h.clock[0]), 'Какая сейчас погода?'))
        await asyncio.wait_for(entered.wait(), 2)
        h.clock[0] = h.state.identity_expires_monotonic
        await h.app.expire_identity_leases()
        assert h.app._session_epochs[h.boundary.session_id] > epoch
        release.set()
        await task
        assert not h.app._canonical_profiles and not render.texts
        assert not any(c.get('response_id') == 'TYPED_WEATHER' for c in h.app.audio.commands)
    finally:
        release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_private_followup_origin_is_rechecked_after_identity_expiry_await(current_weather):
    from app.foundation.domain import Event
    from test_profile_onboarding import Harness, card
    service, calls = current_weather
    h = Harness(card())
    h.app.schedule_weather = service
    entered, release, task = asyncio.Event(), asyncio.Event(), None
    original_expire = h.app.expire_identity_leases
    async def delayed_expire():
        if asyncio.current_task() is task:
            entered.set()
            await release.wait()
        return await original_expire()
    try:
        await h.ready()
        state = h.state
        state.mode, state.handset = 'bear_dialogue', 'up'
        session_id = h.boundary.session_id
        epoch = h.app._session_epochs.get(session_id, 0)
        revision = h.app._session_revisions.get(session_id, 0)
        def origin_current():
            return (h.app.canonical_runtime.sessions.get(session_id) is state and state.handset == 'up'
                and h.app._session_epochs.get(session_id, 0) == epoch
                and h.app._session_revisions.get(session_id, 0) == revision)
        h.app.expire_identity_leases = delayed_expire
        task = asyncio.create_task(h.app._receive_canonical_event(Event('DialogueFallback', h.boundary.session_id,
            1, 1., 'bounded_catalogue', {'reason':'catalogue_request', 'action':'request_information',
                'target':'weather'}, 'origin-await'), dialogue_fallback_authorized=True,
            _weather_scope=canonical_services.weather_request_scope('Какая погода?'),
            _weather_origin_current=origin_current))
        await asyncio.wait_for(entered.wait(), 2)
        await h.send('HandsetPlaced')
        release.set()
        result = await task
        assert result['reason'] == 'stale_session_generation' and not calls
        owner = h.app.canonical_runtime.sessions.get(session_id)
        assert owner is None or (owner.handset == 'down' and owner.active_audio_id != 'TYPED_WEATHER')
    finally:
        release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_young_day_voice_can_request_configured_weather_on_another_page(current_weather):
    service, calls = current_weather
    h, render = await weather_app('day', service, band='1-3')
    try:
        descriptor = h.state.scene_menu_descriptor
        assert [item['id'] for item in descriptor['items']] == ['date']
        assert descriptor['page_count'] == 2
        await h.send('SpeechReceived', text='Какая сейчас погода?')
        assert len(calls) == 1 and h.state.active_audio_id == 'TYPED_WEATHER'
        assert '+3 °C' in render.texts[-1]
        assert h.state.scene_menu_descriptor == descriptor and not h.model.calls
        await h.done()
        assert h.state.mode == 'schedule_weather' and h.state.scene_menu_phase == 'ready'
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_young_day_missing_configuration_remains_unavailable(current_weather):
    service, calls = current_weather
    service.weather_provider.api_key = None
    h, render = await weather_app('day', service, band='1-3')
    try:
        assert [item['id'] for item in h.state.scene_menu_descriptor['items']] == ['date']
        result = await h.send('SpeechReceived', text='Какая сейчас погода?')
        assert result['reason'] == 'typed_information_unavailable'
        assert h.state.active_audio_id == 'PHRASE_100' and not calls and not render.texts
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_young_day_numeric_buttons_still_use_the_current_page_only(current_weather):
    service, calls = current_weather
    h, render = await weather_app('day', service, band='1-3')
    try:
        descriptor = h.state.scene_menu_descriptor
        await h.key(2)  # The second logical capability is not button 2 on this page.
        assert not calls and h.state.active_audio_id is None
        assert h.state.scene_menu_descriptor == descriptor
        await h.key(6)
        assert [item['id'] for item in h.state.scene_menu_descriptor['items']] == ['weather']
        await h.key(1)
        assert len(calls) == 1 and h.state.active_audio_id == 'TYPED_WEATHER'
        assert '+3 °C' in render.texts[-1]
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['bear', 'day'])
@pytest.mark.parametrize('text', ['Погода Москва', 'Какая погода к вечеру?',
    'Какая погода пять часов назад?', 'Температура Сочи', 'Погода после обеда',
    'Какая погода в прохладную погоду?'])
async def test_unconsumed_qualifier_never_authorizes_current_facts(current_weather, route, text):
    service, calls = current_weather
    h, render = await weather_app(route, service)
    try:
        result = await h.send('SpeechReceived', text=text)
        assert not calls and not h.model.calls
        assert h.state.active_audio_id == 'TYPED_WEATHER'
        assert render.texts == ['На этот вопрос о погоде ответить не могу.']
        assert text not in json.dumps(result, ensure_ascii=False)
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['Погода', 'Мишка, какая сегодня погода?',
    'Пожалуйста, скажи, какая сейчас погода на улице?', 'Сегодня какая погода?',
    'Как погода сейчас?', 'Расскажи мне о погоде, пожалуйста.',
    'Какая сейчас температура воздуха?', 'Я не знаю, какая сейчас погода?',
    'Как мне одеться сегодня?', 'Что мне нужно надеть на прогулку?'])
async def test_complete_supported_current_requests_retain_natural_forms(current_weather, text):
    service, calls = current_weather
    result = await canonical_services.TypedInfoRenderer(service, None, None).render(
        'weather', weather_scope=canonical_services.weather_request_scope(text))
    assert len(calls) == 1 and '+3 °C' in result['text']
