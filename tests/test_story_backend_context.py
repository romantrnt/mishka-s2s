import json
import time
from dataclasses import replace
import pytest
from app.foundation.story_context import StoryContextReceipt,StoryBackendContract,StoryContextError
from app.foundation.validation import OllamaMiniLLMValidator
from app.foundation.s2s import OllamaLLMProvider

URL='http://127.0.0.1:11434';MODEL='synthetic-model';DIGEST='a'*64
async def contract(**changes):
    receipt=StoryContextReceipt(URL,MODEL,DIGEST,'0.40.11',16384,2048,time.time()+300,
                                truncate_false_verified=True,shift_false_verified=True)
    receipt=replace(receipt,**changes)
    c=StoryBackendContract(receipt)
    c.calls=[]
    async def metadata(path):
        c.calls.append(path)
        return {'version':'0.40.11'} if path=='/api/version' else {'models':[{'name':MODEL,'digest':DIGEST,'runner':'llamacpp','context_length':1024}]}
    c._metadata=metadata
    await c.refresh()
    return c

def response():
    return json.dumps({'done':True,'done_reason':'stop','message':{'content':json.dumps({'decision':'allow','confidence':.99,'reason':'safe'})}}).encode()

@pytest.mark.asyncio
async def test_default_no_receipt_not_full_story_even_when_method_exists():
    mini=OllamaMiniLLMValidator(num_ctx=1)
    assert not mini.supports_full_story
    mini._request=lambda payload:pytest.fail('must not send child candidate')
    decision=await mini.validate_story('safe')
    assert not decision.allowed and decision.reason=='story_backend_unverified'
    llm=OllamaLLMProvider(URL,MODEL,1)
    assert not llm.supports_full_story
    with pytest.raises(StoryContextError,match='^story_backend_unverified$'):
        _=[v async for v in llm.stream_story([], 'x',lambda:False)]

@pytest.mark.asyncio
async def test_validation_full_payload_dynamic_context_and_no_shared_mutation():
    c=await contract();mini=OllamaMiniLLMValidator(URL,MODEL,num_ctx=1024,story_backend_contract=c)
    sent=[]
    def request(payload):sent.append(json.loads(payload));return response()
    mini._request=request
    text='Сказка. '*700+'КОНЕЦ'
    assert (await mini.validate_story(text)).allowed
    value=sent[-1]
    assert json.loads(value['messages'][1]['content'])['candidate']==text
    assert value['truncate'] is False and value['shift'] is False
    assert 1024<value['options']['num_ctx']<=16384
    assert mini.num_ctx==1024
    await mini.validate('Привет')
    assert sent[-1]['options']['num_ctx']==1024
    assert 'truncate' not in sent[-1] and 'shift' not in sent[-1]

@pytest.mark.asyncio
async def test_large_unicode_fails_budget_before_even_metadata_io():
    c=await contract();mini=OllamaMiniLLMValidator(URL,MODEL,story_backend_contract=c)
    c.calls.clear();mini._request=lambda payload:pytest.fail('oversize candidate sent')
    decision=await mini.validate_story('🦖'*6000)
    assert not decision.allowed and decision.reason=='story_context_budget'
    assert c.calls==[]

@pytest.mark.asyncio
@pytest.mark.parametrize('change',['digest','version','runner','unloaded','expired'])
async def test_serving_change_invalidates_before_candidate_sent(change):
    c=await contract();mini=OllamaMiniLLMValidator(URL,MODEL,story_backend_contract=c)
    old=c._metadata
    async def metadata(path):
        value=await old(path)
        if change=='version' and path=='/api/version':value['version']='0.40.12'
        if path=='/api/ps':
            if change=='runner':value['models'][0]['runner']='other'
            if change=='digest':value['models'][0]['digest']='b'*64
            if change=='unloaded':value['models']=[]
        return value
    c._metadata=metadata
    if change=='expired':c.receipt=replace(c.receipt,expires_at=time.time()-1)
    mini._request=lambda payload:pytest.fail('candidate sent after change')
    assert not (await mini.validate_story('safe')).allowed
    assert not mini.supports_full_story

@pytest.mark.asyncio
async def test_mismatched_model_and_tiny_receipt_not_capabilities():
    c=await contract()
    assert not OllamaMiniLLMValidator(URL,'other',story_backend_contract=c).supports_full_story
    c.receipt=replace(c.receipt,context_ceiling=1)
    assert not c.ready(URL,MODEL)
    with pytest.raises(StoryContextError):c.budget([],96,1)

@pytest.mark.asyncio
async def test_generator_actual_payload_bounded_and_dialogue_unchanged(monkeypatch):
    import httpx
    c=await contract();sent=[]
    class Response:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def raise_for_status(self):pass
        async def aiter_lines(self):
            yield json.dumps({'message':{'content':'{}'}})
            yield json.dumps({'done':True,'done_reason':'stop'})
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def stream(self,method,url,**kwargs):sent.append(kwargs['json']);return Response()
    monkeypatch.setattr(httpx,'AsyncClient',Client)
    llm=OllamaLLMProvider(URL,MODEL,1,num_ctx=4096,max_tokens=96,story_backend_contract=c)
    messages=[{'role':'system','content':'Complete original story. '+ 'Тема лес. '*100}]
    assert [v async for v in llm.stream_story(messages,'story',lambda:False)]==['{}']
    assert sent[-1]['messages']==messages and sent[-1]['options']['num_predict']==6000
    assert 6000<sent[-1]['options']['num_ctx']<=16384
    assert sent[-1]['truncate'] is False and sent[-1]['shift'] is False
    _=[v async for v in llm.stream(messages,'dialogue',lambda:False)]
    assert sent[-1]['options']['num_ctx']==4096 and sent[-1]['options']['num_predict']==96
    assert 'truncate' not in sent[-1] and 'shift' not in sent[-1]
    assert llm.num_ctx==4096 and llm.max_tokens==96

@pytest.mark.asyncio
@pytest.mark.parametrize('terminal',[{}, {'done':True}, {'done':True,'done_reason':'length'}, {'done':False,'done_reason':'stop'}])
async def test_validator_rejects_nonclean_terminal_even_with_allow(terminal):
    c=await contract();mini=OllamaMiniLLMValidator(URL,MODEL,story_backend_contract=c)
    value=json.loads(response());value.pop('done');value.pop('done_reason');value.update(terminal)
    mini._request=lambda payload:json.dumps(value).encode()
    result=await mini.validate_story('Safe complete text.')
    assert not result.allowed and result.reason=='story_incomplete_terminal'

@pytest.mark.asyncio
@pytest.mark.parametrize('terminal',[None, {'done':True}, {'done':True,'done_reason':'length'}])
async def test_generator_rejects_nonclean_terminal(monkeypatch,terminal):
    import httpx
    c=await contract()
    class Response:
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def raise_for_status(self):pass
        async def aiter_lines(self):
            yield json.dumps({'message':{'content':'{}'}})
            if terminal is not None:yield json.dumps(terminal)
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def stream(self,*args,**kwargs):return Response()
    monkeypatch.setattr(httpx,'AsyncClient',Client)
    llm=OllamaLLMProvider(URL,MODEL,1,story_backend_contract=c)
    with pytest.raises(RuntimeError):
        _=[v async for v in llm.stream_story([], 'x',lambda:False)]
