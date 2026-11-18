"""Receipt-bound shared-model requests retain identical Ollama runner options."""
import asyncio
from dataclasses import replace
import json
import time

import httpx
import pytest

from app.foundation.s2s import OllamaLLMProvider
from app.foundation.story_context import StoryContextError
from app.foundation.validation import OllamaMiniLLMValidator
from test_story_backend_context import URL, MODEL, contract, response


def capture(monkeypatch):
    sent=[];client=httpx.AsyncClient
    class Body(httpx.AsyncByteStream):
        def __init__(self,raw):self.raw=raw
        async def __aiter__(self):yield self.raw
    def reply(request):
        value=json.loads(request.content);sent.append(value)
        if value['stream']:
            return httpx.Response(200,stream=Body('\n'.join(map(json.dumps,[
                {'message':{'content':'{}'}},{'done':True,'done_reason':'stop'}])).encode()))
        return httpx.Response(200,stream=Body(response()))
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:client(transport=httpx.MockTransport(reply),**kw))
    return sent


@pytest.mark.asyncio
async def test_fixed_runner_sequence_warmup_ordinary_story_validator_cancel_reentry(monkeypatch):
    sent=capture(monkeypatch)
    generator,validator=await contract(),await contract()
    llm=OllamaLLMProvider(URL,MODEL,1,num_ctx=16384)
    mini=OllamaMiniLLMValidator(URL,MODEL,num_ctx=16384)
    # The actual engine attaches validated receipts after constructing providers.
    llm.story_backend_contract=generator;mini.story_backend_contract=validator
    assert (await llm.warmup())['ok']
    assert [x async for x in llm.stream([], 'ordinary',lambda:False)]==['{}']
    assert [x async for x in llm.stream_story([], 'story',lambda:False)]==['{}']
    assert (await mini.validate_story('Безопасная сказка.')).allowed
    assert (await mini.validate('Привет.')).allowed
    cancelled=False
    with pytest.raises(asyncio.CancelledError):
        async for _ in llm.stream_story([], 'cancel',lambda:cancelled):cancelled=True
    assert [x async for x in llm.stream_story([], 'reentry',lambda:False)]==['{}']
    assert [x async for x in llm.stream([], 'ordinary-again',lambda:False)]==['{}']
    # These are all fields in the pinned Ollama api.Runner; sampling/schema
    # fields do not participate in sched.go needsReload's runner comparison.
    runner_keys={'num_ctx','num_batch','num_gpu','main_gpu','use_mmap','num_thread','draft_num_predict'}
    runners=[{key:value for key,value in item['options'].items() if key in runner_keys} for item in sent]
    assert len(sent)==8 and all(value=={'num_ctx':16384} for value in runners)
    assert all(item.get('shift') is False and item['keep_alive']==-1 for item in sent)
    for index,item in enumerate(sent):
        assert item.get('truncate') is False if index in {2,3,5,6} else 'truncate' not in item
    assert generator.ready(URL,MODEL) and validator.ready(URL,MODEL)


@pytest.mark.asyncio
async def test_expired_receipt_blocks_story_but_does_not_flip_configured_runner(monkeypatch):
    sent=capture(monkeypatch);c=await contract()
    llm=OllamaLLMProvider(URL,MODEL,1,num_ctx=16384,story_backend_contract=c)
    mini=OllamaMiniLLMValidator(URL,MODEL,num_ctx=16384,story_backend_contract=c)
    c.receipt=replace(c.receipt,expires_at=time.time()-1)
    with pytest.raises(StoryContextError):_=[x async for x in llm.stream_story([], 'expired',lambda:False)]
    assert not (await mini.validate_story('Безопасная сказка.')).allowed
    assert not sent
    _=[x async for x in llm.stream([], 'ordinary',lambda:False)]
    assert (await mini.validate('Привет.')).allowed
    assert all(item.get('shift') is False and 'truncate' not in item for item in sent)


@pytest.mark.asyncio
@pytest.mark.parametrize('binding',['absent','other_model','other_endpoint'])
async def test_unbound_ordinary_runner_payload_remains_unchanged(monkeypatch,binding):
    sent=capture(monkeypatch);c=None if binding=='absent' else await contract()
    model='other' if binding=='other_model' else MODEL
    url='http://localhost:11434' if binding=='other_endpoint' else URL
    llm=OllamaLLMProvider(url,model,1,story_backend_contract=c)
    mini=OllamaMiniLLMValidator(url,model,story_backend_contract=c)
    assert (await llm.warmup())['ok']
    _=[x async for x in llm.stream([], 'ordinary',lambda:False)]
    assert (await mini.validate('Привет.')).allowed
    assert all('shift' not in item and 'truncate' not in item for item in sent)
