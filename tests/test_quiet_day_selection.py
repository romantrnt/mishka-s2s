"""Quiet button choices resume through real typed rendering; synthetic Max/PCM only."""
import json

import httpx
import pytest

from app.foundation.canonical_services import TypedInfoRenderer
from app.foundation.schedule_weather import ProductionScheduleWeatherService, YandexWeatherProvider
from test_card_doctors_profile import NAME, doctor_card
from test_panel_session_actions import PanelStand
from test_weather_config import weather_http, weather_payload


async def choose_quiet(h, kind):
    if h.client is not None:
        await h.ready()
    await h.control('start')
    await h.key(7)
    assert h.state.mode == 'schedule_weather' and h.state.handset == 'down'
    assert h.state.silent_mode and not h.app._active_audio
    item = None
    for _ in range(h.state.scene_menu_descriptor['page_count']):
        item = next((item for item in h.state.scene_menu_descriptor['items'] if item['id'] == kind), None)
        if item is not None:
            break
        await h.key(6)
    assert item is not None
    await h.key(item['slot'])
    assert h.state.pending_tool == kind
    assert h.stand.panel_snapshot()['canonical_state']['pending_information_kind'] == kind
    assert not h.app._active_audio and not h.app.bounded_s2s.tts.texts


async def enable(h):
    return await h.control('enable_audio', {**h.scope(), 'state_version': h.state.state_version})


def capture_pcm(h):
    played = []
    original = h.app.audio.play_pcm_owned
    def play(handle, audio):
        played.append((handle, audio))
        return original(handle, audio)
    h.app.audio.play_pcm_owned = play
    return played


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['date', 'card_doctors'])
@pytest.mark.parametrize('join', ['sound', 'lift'])
async def test_quiet_day_choice_resumes_exact_typed_answer_without_menu_substitution(tmp_path, monkeypatch, kind, join):
    h = PanelStand(tmp_path, identity=kind == 'card_doctors')
    rendered = []
    original = TypedInfoRenderer.render
    async def render(self, selected, **kwargs):
        rendered.append(selected)
        return await original(self, selected, **kwargs)
    monkeypatch.setattr(TypedInfoRenderer, 'render', render)
    played = capture_pcm(h)
    try:
        await choose_quiet(h, kind)
        assert not rendered and not played
        public = json.dumps(h.stand.panel_snapshot(), ensure_ascii=False)
        assert NAME not in public and 'card_doctor_names' not in public
        if join == 'sound':
            await enable(h)
            assert h.state.handset == 'down' and not h.app.sessions.get('stand').handset_up
        else:
            await h.simulator.action({'action': 'handset', 'state': 'up'})
            assert h.state.handset == 'up' and h.app.sessions.get('stand').handset_up
        expected = 'TYPED_' + kind.upper()
        assert h.state.mode == 'schedule_weather' and h.state.pending_tool is None
        assert h.stand.panel_snapshot()['canonical_state'].get('pending_information_kind') is None
        assert rendered == [kind]
        assert h.state.active_audio_id == expected and h.app._active_audio['stand']['content_id'] == expected
        assert [handle.response_id for handle, _ in played] == [expected]
        assert played[0][1].samples
        texts = h.app.bounded_s2s.tts.texts
        assert len(texts) == 1
        if kind == 'date':
            assert texts[0].startswith('Сегодня ') and texts[0].endswith('года.')
        else:
            assert NAME in texts[0] and 'Точного плана, времени и кабинета у меня нет' in texts[0]
        assert NAME not in json.dumps(h.stand.panel_snapshot(), ensure_ascii=False)
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('join', ['sound', 'lift'])
async def test_quiet_weather_button_resumes_configured_adapter_only_after_audio_join(tmp_path, monkeypatch, join):
    h = PanelStand(tmp_path)
    h.app.schedule_weather = ProductionScheduleWeatherService(YandexWeatherProvider('synthetic-quiet-key', 1.25, -2.5))
    calls, rendered = [], []
    def handle(request):
        calls.append(request)
        assert request.method == 'GET' and request.url.host == 'api.weather.yandex.ru'
        assert request.url.path == '/v2/forecast'
        assert dict(request.url.params) == {'lat': '1.25', 'lon': '-2.5', 'lang': 'ru_RU', 'limit': '1', 'hours': 'false'}
        assert request.headers['X-Yandex-Weather-Key'] == 'synthetic-quiet-key'
        assert request.content == b''
        return httpx.Response(200, json=weather_payload())
    weather_http(monkeypatch, handle)
    original = TypedInfoRenderer.render
    async def render(self, selected, **kwargs):
        rendered.append((selected, kwargs.get('weather_scope')))
        return await original(self, selected, **kwargs)
    monkeypatch.setattr(TypedInfoRenderer, 'render', render)
    played = capture_pcm(h)
    try:
        await choose_quiet(h, 'weather')
        assert not calls and not rendered and not played
        if join == 'sound':
            await enable(h)
            assert h.state.handset == 'down'
        else:
            await h.simulator.action({'action': 'handset', 'state': 'up'})
            assert h.state.handset == 'up'
        assert len(calls) == 1 and rendered == [('weather', 'configured_point')]
        assert h.state.mode == 'schedule_weather' and h.state.pending_tool is None
        assert h.state.interaction_route == 'panel'
        assert h.stand.panel_snapshot()['canonical_state'].get('pending_information_kind') is None
        assert h.state.active_audio_id == 'TYPED_WEATHER'
        assert h.app._active_audio['stand']['content_id'] == 'TYPED_WEATHER'
        assert [handle.response_id for handle, _ in played] == ['TYPED_WEATHER']
        assert played[0][1].samples
        assert len(h.app.bounded_s2s.tts.texts) == 1
        assert '+3 °C' in h.app.bounded_s2s.tts.texts[0]
        assert 'куртка' in h.app.bounded_s2s.tts.texts[0]
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_quiet_unsupported_place_revokes_previous_local_weather_selection(tmp_path, monkeypatch):
    h = PanelStand(tmp_path)
    h.app.schedule_weather = ProductionScheduleWeatherService(YandexWeatherProvider('synthetic-quiet-key', 1.25, -2.5))
    calls = []
    weather_http(monkeypatch, lambda request: calls.append(request) or httpx.Response(200, json=weather_payload()))
    played = capture_pcm(h)
    try:
        await choose_quiet(h, 'weather')
        await h.stand.inject('text', {'text': 'Какая погода в Синтетикограде?'})
        assert h.state.mode == 'schedule_weather' and h.state.pending_tool is None
        assert h.stand.panel_snapshot()['canonical_state'].get('pending_information_kind') is None
        assert not calls and not played and not h.app.bounded_s2s.tts.texts
        await enable(h)
        assert not calls
        assert all(handle.response_id != 'TYPED_WEATHER' for handle, _ in played)
        assert all('°C' not in text and 'Синтетикоград' not in text for text in h.app.bounded_s2s.tts.texts)
        assert h.state.pending_tool is None
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_back_clears_pending_private_day_choice_before_audio_enable(tmp_path):
    h = PanelStand(tmp_path, identity=True)
    played = capture_pcm(h)
    try:
        await choose_quiet(h, 'card_doctors')
        await h.key(7)
        assert h.state.mode == 'root_menu' and h.state.pending_tool is None
        assert h.stand.panel_snapshot()['canonical_state'].get('pending_information_kind') is None
        await enable(h)
        assert h.state.mode == 'root_menu' and h.state.pending_tool is None
        assert not played and not h.app.bounded_s2s.tts.texts
        assert not h.app._active_audio
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_fresh_max_context_and_next_guest_cannot_replay_old_private_choice(tmp_path):
    h = PanelStand(tmp_path, identity=True)
    played = capture_pcm(h)
    try:
        await choose_quiet(h, 'card_doctors')
        old_scope = {**h.scope(), 'state_version': h.state.state_version}
        old_request = h.state.identity_request_id
        h.client.value = doctor_card(['NEW SYNTHETIC SPECIALIST'])
        h.client.events.append({'kind': 'patient_ready'})
        assert (await h.boundary.step())['reason'] == 'profile_ready'
        assert h.state.identity_request_id != old_request
        assert h.state.pending_tool is None and not h.app._active_audio
        assert h.stand.panel_snapshot()['canonical_state'].get('pending_information_kind') is None
        assert (await h.control('enable_audio', old_scope))['error']['code'] == 'stale_simulator_epoch'
        assert not played and not h.app.bounded_s2s.tts.texts
        await h.control('end')
        await h.control('start')
        await h.key(7)
        assert h.state.profile_status == 'anonymous' and h.state.pending_tool is None
        assert 'card_doctors' not in [item['id'] for item in h.state.scene_menu_descriptor['items']]
        await enable(h)
        assert all(handle.response_id != 'TYPED_CARD_DOCTORS' for handle, _ in played)
        assert all(NAME not in text and 'NEW SYNTHETIC SPECIALIST' not in text for text in h.app.bounded_s2s.tts.texts)
    finally:
        await h.close()
