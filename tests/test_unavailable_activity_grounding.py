"""Unavailable activity promises use real bounded output, synthetic semantic labels."""
import asyncio
import json

import pytest

from app.foundation.activity_requests import activity_request_matches
from test_activity_request_gate import engine, classified


@pytest.mark.asyncio
async def test_explicit_unavailable_riddles_uses_authorized_chooser_without_fake_quiz():
    e, context, model, tts = engine(json.dumps({'type': 'say', 'text': 'Давай загадки. Я начну.'}), structured=True)
    e.activity_request_gate, requests = classified()
    try:
        result = await e.run_text_turn('Запусти игру загадки', context)
        assert [(a.value, a.params) for a in result.actions] == [('open_catalog', {'catalog': 'games'})]
        assert result.activity_request.notice_code == 'riddles_unavailable'
        assert activity_request_matches(result.activity_request, 'Запусти игру загадки', result.actions[0], context)
        assert not result.chunks and not tts.texts and not e.playback.played
        assert len(requests) == 1 and context.turns == 0
    finally:
        await e.cancel_session(context.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('wire', ['json0', 'json1', 'xml'])
async def test_split_structured_model_proposal_cannot_emit_prefix_before_grounding(monkeypatch, wire):
    monkeypatch.setenv('MISHKA_STREAM_FIRST_JSON_CHUNK', '1' if wire == 'json1' else '0')
    e, context, model, tts = engine(structured=wire != 'xml')
    e.activity_request_gate, requests = classified()
    entered, release = asyncio.Event(), asyncio.Event()
    async def split(_messages, _request, cancelled, **_kwargs):
        yield '<say>Открываю игру.</say>' if wire == 'xml' else '{"type":"say","text":"Открываю игру. '
        entered.set()
        await release.wait()
        yield '<say>С загадками?</say>' if wire == 'xml' else 'С загадками?"}'
    if wire == 'xml':
        model.stream = split
    else:
        model.stream_dialogue = split
    task = asyncio.create_task(e.run_text_turn('Мне нравится лес', context))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(.03)  # Give real queue/TTS first-PCM work a scheduling opportunity.
        assert not tts.texts and not e.playback.played
        release.set()
        result = await task
        assert result.fallback_reason == 'unsupported_activity_offer'
        assert not result.actions and result.activity_request is None
        assert not result.chunks and not tts.texts and not e.playback.played
        assert not requests  # Model words cannot authorize the child's choice.
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await e.cancel_session(context.session_id)


@pytest.mark.asyncio
async def test_bare_agreement_cannot_manufacture_available_catalogue_request():
    e, context, model, tts = engine(
        json.dumps({'type': 'open_catalog', 'catalog': 'games'}),
        json.dumps({'type': 'say', 'text': 'Какую игру выберем?'}), structured=True)
    e.activity_request_gate, requests = classified()  # Even a positive semantic fixture cannot invent a target.
    try:
        result = await e.run_text_turn('Давай', context)
        assert not result.actions and result.activity_request is None
    finally:
        await e.cancel_session(context.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('text', [
    'Мы с бабушкой любим загадки', 'Саша сказал «давай загадки»',
    'Не хочу загадки', 'Расскажи про загадки',
])
async def test_discussion_quotes_and_negation_do_not_navigate(text):
    e, context, model, tts = engine(json.dumps({'type': 'say', 'text': 'Я слушаю тебя.'}), structured=True)
    e.activity_request_gate, requests = classified()
    try:
        result = await e.run_text_turn(text, context)
        assert result.chunks == ('Я слушаю тебя.',)
        assert not result.actions and result.activity_request is None
    finally:
        await e.cancel_session(context.session_id)


@pytest.mark.asyncio
async def test_generic_child_play_request_gets_real_receipt_but_never_model_riddle_pcm():
    e, context, model, tts = engine(json.dumps({'type': 'say', 'text': 'Запускаю игру загадки.'}), structured=True)
    e.activity_request_gate, requests = classified()
    try:
        result = await e.run_text_turn('Давай поиграем', context)
        assert [(a.value, a.params) for a in result.actions] == [('open_catalog', {'catalog': 'games'})]
        assert result.activity_request.notice_code == 'riddles_unavailable'
        assert activity_request_matches(result.activity_request, 'Давай поиграем', result.actions[0], context)
        assert not tts.texts and not e.playback.played and len(requests) == 1
    finally:
        await e.cancel_session(context.session_id)


@pytest.mark.asyncio
async def test_classifier_denial_never_gets_replaced_by_a_local_receipt():
    e, context, model, tts = engine(json.dumps({'type': 'say', 'text': 'Я слушаю тебя.'}), structured=True)
    e.activity_request_gate, requests = classified('block')
    try:
        result = await e.run_text_turn('Запусти игру загадки', context)
        assert not result.actions and result.activity_request is None
        assert len(requests) == 1
    finally:
        await e.cancel_session(context.session_id)


@pytest.mark.parametrize('game_id,title', [('fixture_riddles', 'Загадки'), ('riddles', 'Отгадай-ка')])
def test_availability_and_notice_receipt_follow_the_actual_catalogue(monkeypatch, game_id, title):
    import app.foundation.activity_requests as requests
    from app.foundation.game_engine import GameSpec
    from app.foundation.s2s import ParsedOutput
    context = object()
    action = ParsedOutput('action', 'open_catalog', {'catalog': 'games'})
    receipt = requests.ActivityRequestReceipt('Хочу загадки', 'open_catalog', 'games', context, 'riddles_unavailable')
    assert receipt.matches('Хочу загадки', action, context)
    monkeypatch.setattr(requests, 'IMMERSIVE_GAME_SPECS', (*requests.IMMERSIVE_GAME_SPECS,
        GameSpec(game_id, title, 'FIXTURE', 'voice')))
    assert requests.unavailable_game_request('Хочу загадки') is None
    assert not requests.unsupported_game_offer('Запускаю игру загадки.')
    assert not receipt.matches('Хочу загадки', action, context)


@pytest.mark.parametrize('text', [
    'Я могу рассказать про загадки.', 'Бабушка сказала: «Давай загадки».',
    'Я люблю загадки.', 'Я не буду загадывать загадки.',
    'Хочешь узнать, что такое загадки?',
    'Загадки бывают о животных. Давай поговорим о лесе.',
])
def test_harmless_riddle_output_is_not_a_capability_promise(text):
    from app.foundation.activity_requests import unsupported_game_offer
    assert not unsupported_game_offer(text)


@pytest.mark.asyncio
@pytest.mark.parametrize('text', [
    'Я не умею петь. Запускаю игру загадки!',
    'Я не умею петь, запускаю игру загадки!',
    'Я не умею петь, но запускаю игру загадки!',
])
async def test_unrelated_negative_clause_cannot_hide_later_unavailable_offer(text):
    e, context, model, tts = engine(json.dumps({'type': 'say', 'text': text}), structured=True)
    e.activity_request_gate, requests = classified()
    try:
        result = await e.run_text_turn('Мне нравится лес', context)
        assert result.fallback_reason == 'unsupported_activity_offer'
        assert not tts.texts and not e.playback.played and not result.actions
    finally:
        await e.cancel_session(context.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', ['cancel', 'failure', 'invalid_tail'])
async def test_buffered_proposal_is_discarded_on_cancel_failure_or_late_bad_framing(monkeypatch, ending):
    monkeypatch.setenv('MISHKA_STREAM_FIRST_JSON_CHUNK', '1')
    e, context, model, tts = engine(structured=True)
    entered, release = asyncio.Event(), asyncio.Event()
    async def split(*_args, **_kwargs):
        yield '{"type":"say","text":"Открываю игру. '
        entered.set()
        await release.wait()
        if ending == 'failure':
            raise RuntimeError('synthetic interrupted model')
        yield 'С загадками?"} []'
    model.stream_dialogue = split
    task = asyncio.create_task(e.run_text_turn('Мне нравится лес', context))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(.02)
        assert not tts.texts and not e.playback.played
        if ending == 'cancel':
            task.cancel()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert not tts.texts and not e.playback.played
        assert not e.active_tasks and not e.active_queues
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await e.cancel_session(context.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('action,text', [
    ({'type': 'offer_game', 'game_id': 'quiz'}, 'Хочу викторину'),
    ({'type': 'open_catalog', 'catalog': 'games'}, 'Хочу играть'),
])
async def test_unavailable_target_never_skips_receipt_authorization(monkeypatch, action, text):
    from dataclasses import replace
    import app.foundation.activity_requests as requests_module
    monkeypatch.setattr(requests_module, 'IMMERSIVE_GAME_SPECS', tuple(
        replace(game, available=False) for game in requests_module.IMMERSIVE_GAME_SPECS))
    e, context, model, tts = engine(json.dumps(action), json.dumps({'type': 'say', 'text': 'Я слушаю тебя.'}), structured=True)
    e.activity_request_gate, requests = classified('block')
    try:
        result = await e.run_text_turn(text, context)
        assert not result.actions and result.activity_request is None
        assert not requests  # An unavailable target never reaches semantic authorization.
        assert result.chunks == ('Я слушаю тебя.',)
    finally:
        await e.cancel_session(context.session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize('text', [
    'Запускаю игру загадки.', 'Открываю режим загадок.', 'Включаю игру загадки.',
])
async def test_separate_unavailable_launch_claims_never_reach_tts(monkeypatch, text):
    monkeypatch.setenv('MISHKA_STREAM_FIRST_JSON_CHUNK', '1')
    e, context, model, tts = engine(json.dumps({'type': 'say', 'text': text}), structured=True)
    e.activity_request_gate, requests = classified()
    try:
        result = await e.run_text_turn('Мне нравится лес', context)
        assert result.fallback_reason == 'unsupported_activity_offer'
        assert not result.actions and result.activity_request is None
        assert not tts.texts and not e.playback.played
    finally:
        await e.cancel_session(context.session_id)


@pytest.mark.parametrize('text', [
    'Не предлагаю играть в загадки.', 'Я не задам тебе загадку.',
    'Может, поговорим о загадках?', 'Может, ты любишь загадки?',
    'Мама сказала: «Может, загадки?»',
])
def test_additional_proposal_forms_keep_negation_and_discussion(text):
    from app.foundation.activity_requests import unsupported_game_offer
    assert not unsupported_game_offer(text)
