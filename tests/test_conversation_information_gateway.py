"""Explicit personal/current facts use typed authority before ordinary generation."""
import asyncio
from dataclasses import replace

import pytest

from test_contextual_game_flow import Harness
from test_owned_private_pcm import setup, install, HANDLE
from test_product_typed_speech import Harness as DayHarness


@pytest.mark.asyncio
@pytest.mark.parametrize('text,content', [
    ('Какая сейчас погода?', 'TYPED_WEATHER'),
    ('Мишка, какая погода на улице?', 'TYPED_WEATHER'),
    ('Какое сегодня число?', 'TYPED_DATE'),
    ('Когда мой следующий приём?', 'PHRASE_101'),
    ('Куда мне сейчас идти?', 'PHRASE_101'),
    ('К какому врачу мне идти?', 'PHRASE_101'),
    ('Как зовут моего врача?', 'PHRASE_101'),
    ('Я не помню, кто мой врач?', 'PHRASE_101'),
    ('Я не знаю, какая сейчас погода?', 'TYPED_WEATHER'),
])
async def test_explicit_facts_cannot_be_answered_by_model_say(text, content):
    h = Harness('<say>Сейчас солнечно, двадцать градусов.</say>')
    try:
        result = await h.send('SpeechReceived', text=text)
        assert h.state.mode == 'bear_dialogue'
        assert not h.model.calls
        assert any(e.get('type') == 'typed_info_request' for e in result['outputs'])
        assert h.state.active_audio_id == content
        assert not h.app.conversation_contexts
        await h.done()
        assert h.state.mode == 'bear_dialogue' and h.state.dialogue_information_kind is None
        await h.send('SpeechReceived', text='Почему облака белые?')
        assert len(h.model.calls) == 1 and h.state.mode == 'bear_dialogue'
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['Где ты живёшь?', 'Где мой игрушечный телефон?', 'Почему меняется погода?',
    'Расскажи сказку про погоду', 'Мишка, какая погода в твоём волшебном лесу?', 'Я хочу викторину про погоду',
    'Ты врач?', 'Что делает врач?', 'Не говори моё расписание'])
async def test_persona_and_general_questions_keep_normal_dialogue(text):
    h = Harness('<say>Давай представим маленький домик в лесу.</say>')
    try:
        result = await h.send('SpeechReceived', text=text)
        assert len(h.model.calls) == 1 and h.state.dialogue_information_kind is None
        assert not any(e.get('type') == 'typed_info_request' for e in result['outputs'])
        assert h.state.mode == 'bear_dialogue'
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('text,kind', [
    ('Кто меня сегодня принимает?', 'card_doctors'),
    ('Кто будет меня принимать?', 'card_doctors'),
    ('Кто примет меня сегодня?', 'card_doctors'),
    ('Кто со мной занимается?', 'card_doctors'),
    ('Кто будет заниматься со мной?', 'card_doctors'),
    ('Я не знаю, кто со мной занимается', 'card_doctors'),
    ('Кто мой специалист?', 'card_doctors'),
    ('Какой специалист занимается со мной?', 'card_doctors'),
    ('С кем у меня занятие?', 'card_doctors'),
    ('Во сколько мне на приём?', 'what_next'),
    ('Во сколько меня примет врач?', 'what_next'),
    ('Кто мой следующий врач?', 'what_next'),
    ('Куда мне на процедуру?', 'where_now'),
    ('Какая у меня процедура?', 'where_now'),
    ('Какое у меня занятие?', 'where_now'),
])
async def test_personal_clinical_question_family_cannot_use_model_fact_guesses(text, kind):
    h = Harness('<say>Сегодня тебя принимает педиатр.</say>')
    try:
        result = await h.send('SpeechReceived', text=text)
        assert not h.model.calls
        effect = next(e for e in result['outputs'] if e.get('type') == 'typed_info_request')
        assert effect['kind'] == kind
        assert h.state.mode == 'bear_dialogue' and h.state.active_audio_id == 'PHRASE_101'
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('text', [
    'Кто принимает гостей в сказке?', 'Мишка, ты принимаешь гостей?',
    'Кто меня принимает в гости?', 'Кто со мной играет?',
    'Что делает специалист?', 'Ты врач?', 'Что делает врач?',
    'Не говори, кто меня сегодня принимает', 'Не рассказывай моё расписание',
])
async def test_clinical_question_family_preserves_hosting_play_professions_and_refusal(text):
    h = Harness('<say>Давай поговорим о добрых героях.</say>')
    try:
        result = await h.send('SpeechReceived', text=text)
        assert len(h.model.calls) == 1
        assert not any(e.get('type') == 'typed_info_request' for e in result['outputs'])
        assert h.state.dialogue_information_kind is None and h.state.mode == 'bear_dialogue'
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_same_personal_doctor_question_uses_day_typed_privacy_fallback():
    h = DayHarness()
    try:
        await h.start()
        from app.foundation.domain import Event
        h.seq += 1
        await h.app.receive_async(Event('SpeechReceived', 'typed', h.seq, 1., 'test', {}, str(h.seq), 1.),
            'К какому врачу мне идти?')
        assert h.state.mode == 'schedule_weather' and h.state.active_audio_id == 'PHRASE_101'
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', ['ordinary', 'HandsetPlaced', 'SessionReset', 'help'])
async def test_pending_typed_provider_cannot_cross_new_input_or_scene(cancel):
    h = Harness()
    entered, release = asyncio.Event(), asyncio.Event()
    task = None
    original = h.app.schedule_weather.weather
    async def delayed(*args, **kwargs):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return await original(*args, **kwargs)
    h.app.schedule_weather.weather = delayed
    try:
        task = asyncio.create_task(h.send('SpeechReceived', text='Какая сейчас погода?'))
        await asyncio.wait_for(entered.wait(), 2)
        if cancel == 'ordinary':
            await h.send('SpeechReceived', text='Расскажи про облака')
        elif cancel == 'help':
            await h.key(5)
        else:
            await h.send(cancel)
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert not any(c.get('response_id') == 'TYPED_WEATHER' for c in h.app.audio.commands)
        current = h.app.canonical_runtime.sessions.get(h.state.session_id)
        assert current is None or current.dialogue_information_kind is None
    finally:
        release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['date', 'weather', 'what_next', 'where_now'])
async def test_stand_typed_pcm_requires_current_exact_bear_request_scope(kind):
    stand = await setup()
    try:
        state = stand.application.canonical_runtime.sessions[HANDLE.session_id]
        state.mode = 'bear_dialogue'
        state.dialogue_information_kind = kind
        state.identity_capabilities = ['schedule']
        handle = replace(HANDLE, response_id='TYPED_'+kind.upper())
        install(stand, handle)
        identity = (state.profile_ref, state.identity_request_id)
        assert stand._pcm_owner_current(handle, identity)
        state.dialogue_information_kind = 'weather' if kind != 'weather' else 'date'
        assert not stand._pcm_owner_current(handle, identity)
        state.dialogue_information_kind = kind
        state.mode = 'root_menu'
        assert not stand._pcm_owner_current(handle, identity)
        state.mode = 'bear_dialogue'
        if kind in {'what_next', 'where_now'}:
            state.identity_capabilities = []
            assert not stand._pcm_owner_current(handle, identity)
    finally:
        await stand.aclose()
