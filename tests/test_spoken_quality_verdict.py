"""Closed scoped quality verdicts in the same mandatory semantic request."""
import json

import pytest

from app.foundation.validation import AsyncResponseValidator, OllamaMiniLLMValidator, SpokenResponseRequirement


def validation(reply):
    requests=[]
    mini=OllamaMiniLLMValidator(model='qwen3:14b',num_ctx=16384,keep_alive='-1')
    mini._story_runner_pinned=True
    async def request(raw):
        requests.append(json.loads(raw))
        return json.dumps({'message':{'content':reply if isinstance(reply,str) else json.dumps(reply)}}).encode()
    mini._request=request
    return AsyncResponseValidator(mini),requests


@pytest.mark.asyncio
async def test_old_allow_cannot_override_explicit_invalid_quality():
    validator,requests=validation({'quality':'invalid','decision':'allow','confidence':1.})
    result=await validator.validate('Я живу в лесу.',spoken_requirement=SpokenResponseRequirement('1-3','bear_dialogue'))
    assert result.blocked
    assert len(requests)==1


@pytest.mark.asyncio
@pytest.mark.parametrize('reply,allowed', [
    ({'quality':'valid','decision':'allow','confidence':1.},True),
    ({'quality':'valid','decision':'allow','confidence':.75},True),
    ({'quality':'valid','decision':'allow','confidence':.74},False),
    ({'quality':'valid','decision':'block','confidence':1.},False),
    ({'quality':'valid','decision':'review','confidence':1.},False),
    ({'quality':'invalid','decision':'allow','confidence':1.},False),
    ({'quality':'review','decision':'allow','confidence':1.},False),
    ({'quality':'unknown','decision':'allow','confidence':1.},False),
    ({'quality':False,'decision':'allow','confidence':1.},False),
    ({'quality':1,'decision':'allow','confidence':1.},False),
    ({'quality':None,'decision':'allow','confidence':1.},False),
    ({'quality':[],'decision':'allow','confidence':1.},False),
    ({'quality':{},'decision':'allow','confidence':1.},False),
    ({'decision':'allow','confidence':1.},False),
    ({'quality':'valid','confidence':1.},False),
    ({'quality':'valid','decision':'allow'},False),
    ({'quality':'valid','decision':'allow','confidence':1.,'reason':'safe'},False),
    ({'quality':'valid','decision':'allow','confidence':True},False),
    ('{"quality":"invalid","quality":"valid","decision":"allow","confidence":1}',False),
    ('{"quality":"valid","decision":"allow","confidence":1} ignored',False),
])
async def test_closed_quality_verdict_requires_both_quality_and_old_safety(reply,allowed):
    validator,requests=validation(reply)
    result=await validator.validate('Я живу в лесу.',spoken_requirement=SpokenResponseRequirement('1-3','bear_dialogue'))
    assert result.allowed is allowed
    assert len(requests)==1
    schema=requests[0]['format']
    assert list(schema['properties'])==['quality','decision','confidence']
    assert schema['required']==['quality','decision','confidence'] and schema['additionalProperties'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['bear_dialogue','support_intake','game_active','warmup_speech'])
@pytest.mark.parametrize('band,words', [('1-3',8),('3-5',18),('4-5',18),('5-7',30),('6-7',30)])
async def test_scoped_request_retains_age_mode_final_turn_and_one_mini_call(mode,band,words):
    validator,requests=validation({'quality':'valid','decision':'allow','confidence':.99})
    requirement=SpokenResponseRequirement(band,mode,final_word_turn=mode=='game_active')
    text='Вернулись к разговору, я слушаю.' if mode=='game_active' else 'Я живу в добром лесу.'
    result=await validator.validate(text,context=('Контекст',),spoken_requirement=requirement)
    assert result.allowed and len(requests)==1
    request=requests[0]
    assert request['options']=={'temperature':0,'num_predict':96,'num_ctx':16384}
    assert request['think'] is False and request['stream'] is False and request['shift'] is False
    assert request['model']=='qwen3:14b' and request['keep_alive']==-1
    system=request['messages'][0]['content']
    assert f'The total word limit is {words}.' in system
    assert ('final word-game turn' in system)==(mode=='game_active')
    assert ('Require one complete thought in one sentence' in system)==(band=='1-3')
    assert system.endswith(OllamaMiniLLMValidator.SPOKEN_QUALITY_INSTRUCTION)
    assert json.loads(request['messages'][1]['content'])=={'candidate':text,'context':"('Контекст',)"}


@pytest.mark.asyncio
async def test_legacy_unscoped_verdict_still_works_but_is_not_a_scoped_quality_verdict():
    validator,requests=validation({'decision':'allow','confidence':1.})
    assert (await validator.validate('Я живу в лесу.')).allowed
    assert requests[0]['format']==OllamaMiniLLMValidator.DECISION_SCHEMA
    assert not requests[0]['messages'][0]['content'].endswith(OllamaMiniLLMValidator.SPOKEN_QUALITY_INSTRUCTION)
    assert (await validator.validate('Я живу в лесу.',spoken_requirement=SpokenResponseRequirement('1-3','bear_dialogue'))).blocked
    assert len(requests)==2 and requests[1]['format']==OllamaMiniLLMValidator.SPOKEN_DECISION_SCHEMA


@pytest.mark.asyncio
@pytest.mark.parametrize('reply', [
    {'quality':'invalid','decision':'allow','confidence':1.},
    {'quality':'review','decision':'allow','confidence':1.},
    {'decision':'allow','confidence':1.},
    {'quality':'valid','decision':'block','confidence':1.},
])
async def test_actual_generator_wire_cannot_emit_pcm_or_return_with_failed_quality(monkeypatch,reply):
    from test_dialogue_json_contract import context, engine, wire
    wire(monkeypatch,'{"type":"say_return","text":"Я слушаю тебя.","target":"root_menu"}')
    e=engine()
    e.response_validator,requests=validation(reply)
    result=await e.run_text_turn('Привет',context())
    assert result.fallback_reason and not result.chunks and not result.actions
    assert not e.playback.played and len(requests)==1
    assert not e.active_tasks and not e.active_queues
