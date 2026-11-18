"""Receipt-bound shared-model requests retain identical Ollama runner options."""
import asyncio
from dataclasses import asdict, replace
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
        raw=(json.dumps({'message':{'content':json.dumps({'severity':'none','confidence':.99})}}).encode()
             if value.get('format')=='json' else response())
        return httpx.Response(200,stream=Body(raw))
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:client(transport=httpx.MockTransport(reply),**kw))
    return sent


async def actual_application(tmp_path, *, binding='bound'):
    from app.foundation.adapters import RecordingAudio, RecordingLED, RuleBasedIntentClassifier
    from app.foundation.application import ScenarioApplication
    from app.foundation.config import Settings
    from app.foundation.observability import StructuredEventPublisher
    from app.foundation.repositories import InMemorySessionRepository
    from app.foundation.s2s import (BoundedConversationEngine, RetryingLLMProvider,
                                   MockTTSProvider, RVCProvider, MockPlaybackController)
    generator,validator=await contract(),await contract()
    path=tmp_path/'receipt.json'
    path.write_text(json.dumps({'generator':asdict(generator.receipt),'validator':asdict(validator.receipt)}))
    settings=Settings(profile='local_cpu',llm_model=MODEL,validation_provider='ollama',
        validation_model=MODEL,llm_num_ctx=16384,validation_num_ctx=16384,
        story_backend_receipt_path='' if binding=='absent' else str(path))
    llm=OllamaLLMProvider(URL,MODEL,1,num_ctx=settings.llm_num_ctx)
    engine=BoundedConversationEngine(RetryingLLMProvider(llm),MockTTSProvider(),RVCProvider(),
                                    MockPlaybackController(),settings=settings)
    # Engine construction loads/attaches the actual operator receipt. Only its
    # metadata transport is synthetic; Application constructs its own classifier.
    for c in engine.story_backend_contracts.values():
        c._metadata=generator._metadata
        await c.refresh()
    if binding=='foreign_model':settings=replace(settings,validation_model='other')
    if binding=='foreign_endpoint':settings=replace(settings,ollama_base_url='http://localhost:11434')
    app=ScenarioApplication(InMemorySessionRepository(),None,RuleBasedIntentClassifier(),
        RecordingAudio(),RecordingLED(),StructuredEventPublisher(),bounded_s2s=engine,
        settings=settings,background_scheduler=False)
    return app,llm,engine.response_validator.mini


@pytest.mark.asyncio
async def test_actual_application_input_classifier_keeps_shared_runner_through_cancel_reentry(monkeypatch,tmp_path):
    sent=capture(monkeypatch)
    app,llm,mini=await actual_application(tmp_path)
    try:
        assert (await llm.warmup())['ok']
        assert await app.safety_classifier.semantic.classify('Синтетическая реплика.')==('none',.99)
        assert [x async for x in llm.stream([], 'ordinary',lambda:False)]==['{}']
        assert [x async for x in llm.stream_story([], 'story',lambda:False)]==['{}']
        assert (await mini.validate_story('Безопасная сказка.')).allowed
        cancelled=False
        with pytest.raises(asyncio.CancelledError):
            async for _ in llm.stream_story([], 'cancel',lambda:cancelled):cancelled=True
        decision=await app.safety_classifier.classify('Обычная синтетическая реплика.')
        assert decision.verified and decision.severity=='none'
        assert [x async for x in llm.stream_story([], 'reentry',lambda:False)]==['{}']
        assert (await mini.validate('Привет.')).allowed
        assert len(sent)==9
        runner_keys={'num_ctx','num_batch','num_gpu','main_gpu','use_mmap','num_thread','draft_num_predict'}
        assert all({k:v for k,v in item['options'].items() if k in runner_keys}=={'num_ctx':16384}
                   and item.get('shift') is False
                   and item['keep_alive']==-1 for item in sent)
        assert app.safety_classifier.semantic.story_backend_contract is app.bounded_s2s.story_backend_contracts['validator']
        assert all('truncate' not in sent[index] for index in (0,1,2,6,8))
    finally:await app.shutdown_identity()


@pytest.mark.asyncio
async def test_actual_input_classifier_pin_survives_expiry_without_restoring_story_capability(monkeypatch,tmp_path):
    sent=capture(monkeypatch)
    app,llm,mini=await actual_application(tmp_path)
    try:
        for c in app.bounded_s2s.story_backend_contracts.values():
            c.receipt=replace(c.receipt,expires_at=time.time()-1)
            c._observed=None
        with pytest.raises(StoryContextError):
            _=[x async for x in llm.stream_story([], 'expired',lambda:False)]
        assert not (await mini.validate_story('Безопасная сказка.')).allowed and not sent
        decision=await app.safety_classifier.classify('Обычная синтетическая реплика.')
        assert decision.verified and decision.severity=='none'
        assert len(sent)==1 and sent[0]['shift'] is False and 'truncate' not in sent[0]
        assert not llm.supports_full_story and not mini.supports_full_story
    finally:await app.shutdown_identity()


def test_input_gate_keeps_custom_semantic_and_mock_compatibility():
    from app.foundation.config import Settings
    from app.foundation.input_safety import InputSafetyGate
    custom=object()
    assert InputSafetyGate(Settings(profile='local_cpu'),custom,
                           story_backend_contract=object()).semantic is custom
    assert InputSafetyGate(Settings(profile='mock'),story_backend_contract=object()).semantic is None


@pytest.mark.asyncio
@pytest.mark.parametrize('binding',['absent','foreign_model','foreign_endpoint'])
async def test_actual_application_unbound_input_classifier_preserves_defaults(monkeypatch,tmp_path,binding):
    sent=capture(monkeypatch)
    app,_,_=await actual_application(tmp_path,binding=binding)
    try:
        assert await app.safety_classifier.semantic.classify('Синтетическая реплика.')==('none',.99)
        assert len(sent)==1 and 'shift' not in sent[0] and 'truncate' not in sent[0]
    finally:await app.shutdown_identity()


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
