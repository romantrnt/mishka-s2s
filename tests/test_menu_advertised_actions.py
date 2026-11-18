"""Current chooser actions own an edge while optional narration is active."""
import pytest

from test_scene_menu_runtime import Harness
from test_personal_story_service import engine


def story_providers(h):
    providers = engine()
    for name in ('llm', 'tts', 'voice', 'response_validator', 'config'):
        setattr(h.app.bounded_s2s, name, getattr(providers, name))
    return providers


async def enter(h, button, phase):
    await h.button(button)
    await h.release(button)
    for _ in range(3):
        if h.state.scene_menu_phase == phase:
            return
        await h.complete()
    assert h.state.scene_menu_phase == phase


@pytest.mark.asyncio
@pytest.mark.parametrize('button,phase', [(button, phase) for button in (1, 2, 4, 7)
    for phase in ('narrating', 'repeat_prompt', 'repeat_listen')] + [(1, 'invite')])
async def test_back_is_immediate_in_every_owned_chooser_phase(button, phase):
    h = Harness()
    try:
        await enter(h, button, phase)
        old = dict(h.app._active_audio.get(h.sid) or {})
        result = await h.button(7)
        assert h.state.mode == 'root_menu' and h.state.active_audio_id is None
        assert h.sid not in h.app._active_audio
        effects = [item['type'] for item in result['outputs']]
        assert effects.count('audio_stop') == effects.count('capture_close') == 1
        if old:
            await h.send('AudioCompleted', {**old, 'response_id': old['content_id']})
            assert h.state.mode == 'root_menu' and h.state.active_audio_id is None
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('button,target', [(1, 'game_active'), (2, 'fixed_story_playing'),
    (4, 'personal_story_playing'), (7, 'schedule_weather')])
async def test_current_selection_interrupts_narration_without_hidden_skip(button, target):
    h = Harness()
    if button == 4:
        story_providers(h)
    try:
        await enter(h, button, 'narrating')
        old = dict(h.app._active_audio[h.sid])
        result = await h.button(1)
        assert h.state.mode == target
        assert result['reason'] != 'scene_menu_skipped'
        effects = [item['type'] for item in result['outputs']]
        assert effects.count('audio_stop') == effects.count('capture_close') == 1
        current = (h.state.active_audio_id, h.state.playback_lease)
        assert current[0] and current[1] != old['playback_lease']
        await h.send('AudioCompleted', {**old, 'response_id': old['content_id']})
        assert (h.state.active_audio_id, h.state.playback_lease) == current
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['narrating', 'repeat_prompt', 'repeat_listen'])
async def test_current_next_page_owns_fresh_press_and_old_audio_cannot_finish_it(phase):
    h = Harness('1-3')
    try:
        await enter(h, 2, phase)
        old = dict(h.app._active_audio.get(h.sid) or {})
        assert 6 in h.state.scene_menu_descriptor['buttons']
        result = await h.button(6)
        assert result['reason'] == 'scene_menu_page'
        assert h.state.scene_menu_descriptor['page'] == 1
        assert h.state.scene_menu_phase == 'narrating'
        lease = h.state.playback_lease
        await h.button(6)
        assert h.state.scene_menu_descriptor['page'] == 1 and h.state.playback_lease == lease
        if old:
            await h.send('AudioCompleted', {**old, 'response_id': old['content_id']})
            assert h.state.scene_menu_phase == 'narrating' and h.state.playback_lease == lease
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_inherited_back_hold_and_unknown_key_do_not_activate_actions():
    h = Harness('1-3')
    try:
        await h.button(7)
        assert (await h.button(7))['reason'] == 'scene_menu_release_required'
        assert h.state.mode == 'schedule_weather'
        await h.release(7)
        await h.button(4)  # Not offered on this one-choice page.
        assert h.state.mode == 'schedule_weather' and h.state.scene_menu_phase == 'ready'
        assert (await h.button(4))['reason'] == 'scene_menu_release_required'
        await h.button(7)
        assert h.state.mode == 'root_menu'
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('text,content', [('Какое сегодня число?', 'TYPED_DATE'),
    ('Какая сегодня погода?', 'TYPED_WEATHER'), ('Что будет дальше?', 'PHRASE_101')])
async def test_existing_day_request_supersedes_repeat_question(text, content):
    h = Harness()
    try:
        await enter(h, 7, 'repeat_listen')
        result = await h.send('SpeechReceived', text=text)
        assert result['reason'] != 'scene_menu_answer_unknown'
        assert h.state.mode == 'schedule_weather' and h.state.scene_menu_phase == 'ready'
        assert h.state.active_audio_id == content
        old = dict(h.app._active_audio[h.sid])
        assert (await h.button(7))['reason'] == 'info_back'
        assert h.state.mode == 'root_menu' and h.state.active_audio_id is None
        await h.send('AudioCompleted', {**old, 'response_id': old['content_id']})
        assert h.state.mode == 'root_menu' and h.sid not in h.app._active_audio
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_existing_supported_voice_theme_supersedes_repeat_question():
    h = Harness()
    providers = story_providers(h)
    try:
        await enter(h, 4, 'repeat_listen')
        assert h.app.personal_story_available()
        assert any(item['id'] == 'космос' for item in h.state.scene_menu_descriptor['items'])
        result = await h.send('SpeechReceived', text='Расскажи сказку про космос')
        assert result['reason'] == 'personal_story_ready'
        assert h.state.mode == 'personal_story_playing' and len(providers.llm.calls) == 1
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['Неизвестная тема', 'Расскажи сказку про космос'])
async def test_repeat_gate_does_not_invent_unavailable_themes_or_fixed_story_voice(text):
    h = Harness()
    try:
        await enter(h, 2, 'repeat_listen')
        result = await h.send('SpeechReceived', text=text)
        assert result['reason'] == 'scene_menu_choice_clarification'
        assert h.state.mode == 'fixed_story_menu' and h.state.scene_menu_phase == 'ready'
        assert h.state.active_audio_id == 'CATALOGUE_CLARIFY'
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_back_during_capability_remap_uses_stable_back_without_selecting_new_slot():
    from app.foundation.schedule_weather import ProductionScheduleWeatherService, YandexWeatherProvider
    h = Harness('1-3')
    service = ProductionScheduleWeatherService(YandexWeatherProvider('synthetic', 55., 37.))
    h.app.schedule_weather = service
    try:
        await enter(h, 7, 'narrating')
        await h.button(6)
        await h.release(6)
        assert h.state.scene_menu_descriptor['items'][0]['id'] == 'weather'
        service.weather_provider = None
        result = await h.button(7)
        assert result['reason'] == 'info_back' and h.state.mode == 'root_menu'
        assert h.state.active_audio_id is None and h.sid not in h.app._active_audio
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_back_cancels_pending_menu_render_without_late_pcm():
    import asyncio
    h = Harness()
    gate = asyncio.Event()
    h.render.block = gate
    pending = None
    try:
        pending = asyncio.create_task(h.button(2))
        await h.render.entered.wait()
        result = await h.button(7)
        assert result['reason'] == 'story_menu_back' and h.state.mode == 'root_menu'
        gate.set()
        await pending
        assert not h.played and h.sid not in h.app._active_audio
    finally:
        gate.set()
        if pending:
            await asyncio.gather(pending, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_untrusted_day_request_cannot_dismiss_repeat_question_or_authorize_facts():
    h = Harness()
    try:
        await enter(h, 7, 'repeat_listen')
        await h.send('SpeechReceived', {'_stt_untrusted': True}, text='Какое сегодня число?')
        assert h.state.scene_menu_phase == 'repeat_listen'
        assert h.state.active_audio_id is None
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['Позови маму', 'Позови взрослого', 'Нужен взрослый'])
async def test_explicit_help_in_theme_repeat_listen_keeps_safety_priority(text):
    h = Harness()
    try:
        await enter(h, 4, 'repeat_listen')
        result = await h.send('SpeechReceived', text=text)
        assert result['reason'] == 'help_requested'
        assert h.state.mode == 'safety_stop' and h.state.active_audio_id == 'PHRASE_091'
    finally:
        await h.close()
