"""Young plot grammar is request-local; legacy providers and older stories keep their path."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import re
import time

import pytest

from app.foundation.personal_story import PersonalStoryService
from app.foundation.s2s import OllamaLLMProvider
from app.foundation.story_context import StoryContextError
from test_ollama_story_response_schema import capture_http
from test_personal_story_service import engine
from test_story_backend_context import URL, MODEL, contract


def test_young_schema_is_closed_request_copy_with_exact_eight_word_subset():
    original=deepcopy(OllamaLLMProvider.STORY_RESPONSE_SCHEMA)
    schema=OllamaLLMProvider.story_response_schema_for_age('1-3')
    patterns=[item.pop('pattern') for item in schema['properties']['plot']['properties'].values()]
    assert len(set(patterns))==1 and schema==original
    pattern=patterns[0]
    for text in ['Лес.', 'Ёжик принёс листик, веточку; камушек: шишку и цветок!',
                 'Заяц-беляк нашёл семечко. Он принёс его домой?']:
        assert re.fullmatch(pattern,text), text
    for text in ['Один два три четыре пять шесть семь восемь девять.', 'Лес..', 'Лес.\nДом.',
                 'Лес.\u00a0Дом.', 'Лес  дом.', 'Лес1.', '«Лес».', r'Лес\дом.', '']:
        assert not re.fullmatch(pattern,text), text
    schema['properties']['title']['minLength']=999
    assert OllamaLLMProvider.STORY_RESPONSE_SCHEMA==original
    assert OllamaLLMProvider.story_response_schema_for_age('1-3')['properties']['title']['minLength']==1
    assert OllamaLLMProvider.story_response_schema_for_age('6-7')==original


@pytest.mark.asyncio
async def test_actual_http_young_first_repair_and_concurrent_older_keep_separate_formats(monkeypatch):
    sent=capture_http(monkeypatch)
    provider=OllamaLLMProvider(URL,MODEL,1,num_ctx=16384,story_backend_contract=await contract())
    messages=[{'role':'system','content':'Synthetic bounded story.'}]
    async def collect(band):
        return [part async for part in provider.stream_story_for_age(messages,'synthetic',lambda:False,age_band=band)]
    assert await asyncio.gather(collect('1-3'),collect('6-7'))==[['{}'],['{}']]
    assert await collect('1-3')==['{}']
    assert sent[0]['format']==sent[2]['format']==provider.story_response_schema_for_age('1-3')
    assert sent[1]['format']==provider.STORY_RESPONSE_SCHEMA
    assert sent[0]['messages']==sent[1]['messages']==sent[2]['messages']==messages
    assert all(p['shift'] is False and p['truncate'] is False and p['options']['num_ctx']==16384 for p in sent)
    assert all('pattern' not in p for p in provider.STORY_RESPONSE_SCHEMA['properties']['plot']['properties'].values())
    _=[part async for part in provider.stream_story(messages,'legacy',lambda:False)]
    assert sent[-1]['format']==provider.STORY_RESPONSE_SCHEMA
    _=[part async for part in provider.stream(messages,'ordinary',lambda:False)]
    assert 'format' not in sent[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize('rejection',['unknown_age','expired','changed','budget','cancel'])
async def test_age_path_retains_no_io_gates_and_cancellation(monkeypatch,rejection):
    sent=capture_http(monkeypatch);c=await contract()
    provider=OllamaLLMProvider(URL,MODEL,1,num_ctx=16384,story_backend_contract=c)
    band='unknown' if rejection=='unknown_age' else '1-3'
    messages=[{'role':'system','content':'Ж'*7000 if rejection=='budget' else 'Synthetic story.'}]
    if rejection=='expired':c.receipt=replace(c.receipt,expires_at=time.time()-1)
    if rejection=='changed':
        metadata=c._metadata
        async def mismatch(path):
            value=await metadata(path)
            if path=='/api/ps':value['models'][0]['digest']='b'*64
            return value
        c._metadata=mismatch
    with pytest.raises(asyncio.CancelledError if rejection=='cancel' else (ValueError,StoryContextError)):
        _=[part async for part in provider.stream_story_for_age(messages,'synthetic',lambda:rejection=='cancel',age_band=band)]
    assert len(sent)==(1 if rejection=='cancel' else 0)


@pytest.mark.asyncio
async def test_service_selects_explicit_age_method_and_legacy_three_argument_fallback():
    e=engine('1-3');legacy=e.llm.stream_story;seen=[]
    async def aged(messages,request_id,cancelled,*,age_band):
        seen.append(age_band)
        async for part in legacy(messages,request_id,cancelled):yield part
    e.llm.stream_story_for_age=aged
    assert await PersonalStoryService(e).render('лес','1-3',(),1,lambda:True)
    assert seen==['1-3']
    del e.llm.stream_story_for_age
    assert await PersonalStoryService(e).render('лес','1-3',(),1,lambda:True)
    assert len(e.llm.calls)==2
