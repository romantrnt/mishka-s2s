"""A positively classified named request owns an offer before free generation."""
import asyncio

import pytest

from app.foundation.activity_requests import ActivityRequestGate, activity_request_matches, named_game_candidate
from app.foundation.s2s import ConversationContext, ProfileContext
from test_activity_request_gate import engine, classified
from test_contextual_game_flow import Harness, Model
from test_conversation_catalogue_gateway import quick_public_audio


@pytest.mark.asyncio
async def test_named_request_cannot_degrade_to_a_spoken_promise():
    e, context, model, tts = engine('<say>Отлично! Давай сыграем в квиз.</say>')
    e.activity_request_gate, requests = classified()
    result = await e.run_text_turn('Хочу сыграть в квиз.', context)
    assert [action.params for action in result.actions] == [{'game_id': 'quiz'}]
    assert not result.chunks and not tts.texts and not model.calls
    assert len(requests) == 1 and context.turns == 0
    assert context.history == [('Хочу сыграть в квиз.', '')]
    assert activity_request_matches(result.activity_request, 'Хочу сыграть в квиз.', result.actions[0], context)


@pytest.mark.asyncio
@pytest.mark.parametrize('text,target', [
    ('Хочу сыграть в квиз.', 'quiz'), ('Давай сыграем в Вопросы Мишки.', 'quiz'),
    ('Хочу поиграть в викторину.', 'quiz'), ('Квиз.', 'quiz'),
    ('Не хочу сказки, хочу квиз.', 'quiz'), ('Хочу не квиз, а слова.', 'word_game'),
    ('Давай сыграем в «Повтори огоньки».', 'repeat_pattern'),
    ('Можно поиграть в живой свет?', 'free_lights'), ('Хочу игру найди лишнее.', 'odd_one_out'),
])
@pytest.mark.parametrize('structured', [False,True])
async def test_varied_named_requests_have_one_semantic_receipt_and_no_free_generation(text,target,structured):
    e, context, model, tts = engine(structured=structured)
    e.activity_request_gate, requests = classified()
    context.history = [('Я не хочу играть.', 'Хорошо, поговорим.')]
    context.turns = 2
    result = await e.run_text_turn(text, context)
    assert not result.fallback_reason and not result.chunks
    assert [action.params for action in result.actions] == [{'game_id':target}]
    assert activity_request_matches(result.activity_request,text,result.actions[0],context)
    assert not activity_request_matches(result.activity_request,text,result.actions[0],object())
    assert not model.calls and not tts.texts and len(requests) == 1
    assert context.turns == 2 and context.history[-1] == (text,'')
    assert sum(item[0] == text for item in context.history) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('text', [
    'Мне нравятся облака.', 'Квиз мне нравится.', 'Что такое квиз?',
    'Я прочитал фразу «давай сыграем в квиз».', 'Мама сказала «хочу квиз».',
    'Не хочу играть в квиз.', 'Хочу квиз или игру в слова.', 'Хочу поиграть.', 'Да.',
])
async def test_non_candidates_get_no_extra_semantic_hop_or_history_consent(text):
    assert named_game_candidate(text) is None
    e, context, model, tts = engine('<say>Я тебя слушаю.</say>')
    e.activity_request_gate, requests = classified()
    context.history = [('Хочу квиз.', 'Предложение из старого разговора.')]
    result = await e.run_text_turn(text, context)
    assert result.chunks == ('Я тебя слушаю.',) and not result.actions
    assert result.activity_request is None and not requests and len(model.calls) == 1
    assert context.turns == 1


@pytest.mark.asyncio
async def test_semantic_denial_repairs_original_once_without_second_classifier():
    e, context, model, tts = engine('<say>Уточни, во что ты хочешь поиграть?</say>')
    e.activity_request_gate, requests = classified('review', .7)
    result = await e.run_text_turn('Хочу квиз.', context)
    assert not result.actions and result.activity_request is None
    assert len(requests) == len(model.calls) == 1
    assert model.calls[0][-1]['content'] == 'Хочу квиз.'
    assert 'SPEECH_FORM only' in model.calls[0][0]['content']
    assert context.history == [('Хочу квиз.', 'Уточни, во что ты хочешь поиграть?')]
    assert context.turns == 1 and tts.texts == ['Уточни, во что ты хочешь поиграть?']


@pytest.mark.asyncio
async def test_denied_repair_cannot_reintroduce_navigation_or_classification():
    e, context, model, tts = engine('<action type="offer_game" game_id="quiz"/>')
    e.activity_request_gate, requests = classified('block')
    result = await e.run_text_turn('Квиз.', context)
    assert not result.actions and result.activity_request is None
    assert len(requests) == len(model.calls) == 1
    assert context.turns == 1 and len(context.history) == 1
    assert tts.texts == ['О чём поговорим?']


@pytest.mark.asyncio
async def test_pre_generation_timeout_is_bounded_and_never_emits_a_game_promise():
    calls=[]
    class Slow:
        async def classify(self,*args):
            calls.append(args)
            await asyncio.sleep(10)
    e, context, model, tts = engine('<say>Уточни название игры.</say>')
    e.activity_request_gate = ActivityRequestGate(Slow(),timeout_s=.02)
    async with asyncio.timeout(1):
        result = await e.run_text_turn('Хочу квиз.',context)
    assert not result.actions and result.activity_request is None
    assert len(calls) == len(model.calls) == 1
    assert tts.texts == ['Уточни название игры.']


@pytest.mark.asyncio
async def test_cancelled_named_admission_has_no_pcm_history_or_late_receipt():
    entered=asyncio.Event()
    class Waiting:
        async def classify(self,*args):
            entered.set()
            await asyncio.Event().wait()
    e, context, model, tts = engine()
    e.activity_request_gate = ActivityRequestGate(Waiting())
    task=asyncio.create_task(e.run_text_turn('Хочу квиз.',context))
    await asyncio.wait_for(entered.wait(),1)
    task.cancel()
    result,=await asyncio.gather(task,return_exceptions=True)
    assert isinstance(result,asyncio.CancelledError)
    assert not context.history and context.turns == 0
    assert not tts.texts and not model.calls and not e.resource_snapshot()['active_tasks']


@pytest.mark.asyncio
async def test_cancel_resistant_old_classifier_cannot_take_the_new_owner():
    entered,cancelled,release=asyncio.Event(),asyncio.Event(),asyncio.Event()
    class Resistant:
        async def classify(self,*args):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return True
    e, old, model, tts = engine('<say>Облака плывут по небу.</say>')
    e.activity_request_gate = ActivityRequestGate(Resistant())
    previous=asyncio.create_task(e.run_text_turn('Хочу квиз.',old))
    current=None
    try:
        await asyncio.wait_for(entered.wait(),1)
        fresh=ConversationContext(old.session_id,'canonical_product','bear_dialogue',('__all__',),(),
            8,120,4,profile=ProfileContext(age_band='6-7'))
        current=asyncio.create_task(e.run_text_turn('Расскажи об облаках.',fresh))
        await asyncio.wait_for(cancelled.wait(),1)
        release.set()
        old_result,new_result=await asyncio.wait_for(asyncio.gather(previous,current,return_exceptions=True),2)
        assert isinstance(old_result,asyncio.CancelledError)
        assert new_result.chunks == ('Облака плывут по небу.',) and not new_result.actions
        assert not old.history and old.turns == 0
        assert fresh.history == [('Расскажи об облаках.','Облака плывут по небу.')]
        assert len(model.calls) == 1 and tts.texts == ['Облака плывут по небу.']
        assert not e.resource_snapshot()['active_tasks']
    finally:
        release.set()
        await asyncio.gather(*(task for task in (previous,current) if task is not None),return_exceptions=True)


@pytest.mark.asyncio
async def test_application_heard_offer_and_fresh_consent_remain_the_only_game_entry():
    h=Harness()
    quick_public_audio(h)
    h.app.bounded_s2s.activity_request_gate,requests=classified()
    try:
        result=await h.send('SpeechReceived',text='Хочу сыграть в квиз.')
        assert result['reason'] == 'game_offered'
        assert h.state.pending_game_offer == 'quiz' and h.state.active_audio_id == 'DIALOGUE_OFFER'
        assert not h.model.calls and len(requests) == 1
        context=h.app.conversation_contexts[h.state.session_id]
        assert context.turns == 0 and context.history == [('Хочу сыграть в квиз.','')]
        await h.key(1)
        assert h.state.mode == 'bear_dialogue'
        await h.done()
        await h.key(1)
        assert h.state.active_game_id == 'quiz' and h.state.game_return_mode == 'bear_dialogue'
        await h.key(7,duration=6.)
        assert h.state.mode == 'bear_dialogue' and h.state.active_game_id is None
    finally:
        await h.close()
