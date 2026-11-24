"""Cancellation preserves availability; every model request still rechecks."""
import asyncio
from dataclasses import replace
import time

import pytest

from app.foundation.personal_story import PersonalStoryService, story_messages
from app.foundation.s2s import OllamaLLMProvider
from app.foundation.story_context import StoryContextError
from app.foundation.validation import AsyncResponseValidator, OllamaMiniLLMValidator
from test_personal_story_app import start_app, enter, send
from test_personal_story_service import LLM
from test_story_backend_context import URL, MODEL, contract, response


async def cancel_refresh(c):
    metadata=c._metadata;entered=asyncio.Event();observed=c._observed
    async def blocked(path):
        entered.set()
        await asyncio.Future()
    c._metadata=blocked
    pending=asyncio.create_task(c.refresh())
    try:
        await asyncio.wait_for(entered.wait(),1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):await pending
    finally:
        c._metadata=metadata
    assert c.matches(URL,MODEL) and c._observed==observed, 'cancellation cannot publish a new observation'


async def providers():
    generator,validator=await contract(),await contract()
    llm=OllamaLLMProvider(URL,MODEL,1,story_backend_contract=generator)
    mini=OllamaMiniLLMValidator(URL,MODEL,story_backend_contract=validator)
    calls=[]
    async def stream(messages,request_id,cancelled,**kwargs):
        assert generator.ready(URL,MODEL), 'no model request before fresh metadata verification'
        calls.append('generate')
        async for raw in LLM().stream_story(messages,request_id,cancelled):yield raw
    async def validate(payload):
        assert validator.ready(URL,MODEL), 'no candidate sent before fresh metadata verification'
        calls.append('validate')
        return response()
    llm._stream_bounded=stream;mini._request_bytes=validate
    return llm,mini,generator,validator,calls


@pytest.mark.asyncio
async def test_repeated_cancel_recovers_actual_model_story_service_and_fresh_checks():
    app=await start_app()
    llm,mini,generator,validator,calls=await providers()
    app.bounded_s2s.llm=llm
    app.bounded_s2s.response_validator=AsyncResponseValidator(mini=mini)
    service=PersonalStoryService(app.bounded_s2s)
    try:
        # Retained model service seam only: root3 now uses the local catalogue.
        for c in (generator,validator):
            metadata=c._metadata;entered=asyncio.Event()
            async def blocked(path):
                entered.set()
                await asyncio.Future()
            c._metadata=blocked
            pending=asyncio.create_task(service.render('лес','3-5',(),1,lambda:True))
            try:
                await asyncio.wait_for(entered.wait(),1)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(pending,1)
            finally:
                c._metadata=metadata
                pending.cancel()
                await asyncio.wait_for(asyncio.gather(pending,return_exceptions=True),1)
            assert llm.supports_full_story and mini.supports_full_story
            assert llm.supports_story_request(story_messages('лес','3-5',()))
            assert service.capable()
            counts=[len(c.calls) for c in (generator,validator)]
            audio=await asyncio.wait_for(service.render('лес','3-5',(),2,lambda:True),5)
            assert audio is not None and audio.token==2 and audio.samples
            assert calls[-2:]==['generate','validate']
            assert [len(c.calls)-n for c,n in zip((generator,validator),counts)]==[4,4]
            assert all(c.ready(URL,MODEL) for c in (generator,validator))
    finally:
        await app.shutdown_identity()


@pytest.mark.asyncio
async def test_cancelled_authored_story_reenters_current_chooser_without_model_io():
    from app.foundation.personal_story import STORY_ID
    app=await start_app()
    llm,mini,generator,validator,calls=await providers()
    app.bounded_s2s.llm=llm
    app.bounded_s2s.response_validator=AsyncResponseValidator(mini=mini)
    synthesize=app.bounded_s2s.tts.synthesize
    entered=asyncio.Event();pending=None
    async def blocked(*args):
        entered.set()
        await asyncio.Future()
    try:
        for c in (generator,validator):
            await cancel_refresh(c)
        await enter(app)
        app.bounded_s2s.tts.synthesize=blocked
        pending=asyncio.create_task(send(app,'ButtonPressed',{'button_id':1}))
        await asyncio.wait_for(entered.wait(),1)
        owner=app._personal_stories['story-test']
        await send(app,'ButtonReleased',{'button_id':1})
        assert (await send(app,'ButtonPressed',{'button_id':7}))['reason']=='activity_back'
        await send(app,'ButtonReleased',{'button_id':7})
        assert (await asyncio.wait_for(pending,1))['reason']=='stale_session_generation'
        assert owner.closed and owner.audio is None
        assert not app._personal_stories and not app._personal_story_jobs
        assert not any(c.get('response_id')==STORY_ID for c in app.audio.commands)
        app.bounded_s2s.tts.synthesize=synthesize
        assert PersonalStoryService(app.bounded_s2s).capable() and app.personal_story_available()
        await send(app,'ButtonPressed',{'button_id':3})
        await send(app,'ButtonReleased',{'button_id':3})
        state=app.canonical_runtime.sessions['story-test']
        assert state.mode=='personal_story_theme' and len(state.scene_menu_descriptor['items'])==5
        counts=[len(c.calls) for c in (generator,validator)]
        result=await asyncio.wait_for(send(app,'ButtonPressed',{'button_id':1}),5)
        assert result['reason']=='personal_story_ready' and state.mode=='personal_story_playing'
        assert app._personal_stories['story-test'] is not owner
        assert sum(c.get('response_id')==STORY_ID for c in app.audio.commands)==1
        assert not calls, 'authored recovery does not need model generation or semantic validation'
        assert [len(c.calls)-n for c,n in zip((generator,validator),counts)]==[0,0]
        assert all(c.ready(URL,MODEL) for c in (generator,validator))
        await send(app,'ButtonReleased',{'button_id':1})
        await send(app,'ButtonPressed',{'button_id':7})
        await send(app,'ButtonReleased',{'button_id':7})
        assert state.mode=='root_menu' and not app._personal_stories
    finally:
        app.bounded_s2s.tts.synthesize=synthesize
        if pending:
            pending.cancel()
            await asyncio.wait_for(asyncio.gather(pending,return_exceptions=True),1)
        await app.shutdown_identity()


@pytest.mark.asyncio
@pytest.mark.parametrize('role',['generator','validator'])
@pytest.mark.parametrize('phase',['before_request','after_request'])
async def test_cancel_inside_actual_provider_rechecks_before_next_request(role,phase):
    llm,mini,generator,validator,calls=await providers()
    c=generator if role=='generator' else validator
    metadata=c._metadata;entered=asyncio.Event();seen=0
    async def blocked(path):
        nonlocal seen
        seen+=1
        if seen==(1 if phase=='before_request' else 3):
            entered.set()
            await asyncio.Future()
        return await metadata(path)
    async def invoke():
        if role=='generator':return [v async for v in llm.stream_story([], 'next',lambda:False)]
        return await mini.validate_story('Безопасная сказка.')
    c._metadata=blocked;pending=asyncio.create_task(invoke())
    try:
        await asyncio.wait_for(entered.wait(),1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):await pending
        c._metadata=metadata;count=len(c.calls)
        result=await invoke()
        assert result if role=='generator' else result.allowed
        assert len(c.calls)-count==4, 'the next model request has fresh pre/post metadata, not stale readiness'
    finally:
        c._metadata=metadata;pending.cancel();await asyncio.gather(pending,return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_cannot_create_initial_unverified_capability():
    from app.foundation.story_context import StoryBackendContract
    verified=await contract();c=StoryBackendContract(verified.receipt)
    await cancel_refresh(c)
    assert not c.ready(URL,MODEL)
    assert not OllamaLLMProvider(URL,MODEL,1,story_backend_contract=c).supports_full_story
    assert not OllamaMiniLLMValidator(URL,MODEL,story_backend_contract=c).supports_full_story


@pytest.mark.asyncio
@pytest.mark.parametrize('role',['generator','validator'])
@pytest.mark.parametrize('change',['digest','version','expired'])
async def test_recovery_still_rejects_changed_or_expired_backend_before_model_io(role,change):
    llm,mini,generator,validator,calls=await providers()
    c=generator if role=='generator' else validator
    await cancel_refresh(c)
    original=c._metadata
    async def changed(path):
        value=await original(path)
        if change=='version' and path=='/api/version':value['version']='0.40.12'
        if change=='digest' and path=='/api/ps':value['models'][0]['digest']='b'*64
        return value
    c._metadata=changed
    if change=='expired':c.receipt=replace(c.receipt,expires_at=time.time()-1)
    if role=='generator':
        with pytest.raises(StoryContextError):
            _=[part async for part in llm.stream_story([], 'next',lambda:False)]
    else:
        assert not (await AsyncResponseValidator(mini=mini).validate_story('Безопасная сказка.')).allowed
    assert not calls and not c.ready(URL,MODEL)
    assert not (llm if role=='generator' else mini).supports_full_story


@pytest.mark.asyncio
@pytest.mark.parametrize('old_result',['cancel','mismatch','error','valid'])
@pytest.mark.parametrize('new_result',['failure','success'])
async def test_old_refresh_cannot_restore_or_clear_newer_verification(old_result,new_result):
    c=await contract();metadata=c._metadata;entered=asyncio.Event();release=asyncio.Event()
    async def overlapping(path):
        old=asyncio.current_task().get_name()=='old-refresh'
        if old:
            entered.set()
            await release.wait()
            if old_result=='error':raise OSError('synthetic metadata failure')
        value=await metadata(path)
        if path=='/api/ps' and ((old and old_result=='mismatch') or (not old and new_result=='failure')):
            value['models'][0]['digest']='b'*64
        return value
    c._metadata=overlapping
    pending=asyncio.create_task(c.refresh(),name='old-refresh')
    await asyncio.wait_for(entered.wait(),1)
    try:
        if new_result=='failure':
            with pytest.raises(StoryContextError):await c.refresh()
        else:await c.refresh()
        if old_result=='cancel':pending.cancel()
        else:release.set()
        await asyncio.gather(pending,return_exceptions=True)
        provider=OllamaLLMProvider(URL,MODEL,1,story_backend_contract=c)
        available=new_result=='success' and old_result not in {'mismatch','error'}
        assert c.ready(URL,MODEL) is available
        assert provider.supports_full_story is available
    finally:
        pending.cancel();await asyncio.gather(pending,return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['mismatch','error'])
async def test_old_real_failure_revokes_availability_even_when_newer_refresh_was_cancelled(failure):
    c=await contract();metadata=c._metadata;entered=asyncio.Event();release=asyncio.Event()
    async def blocked(path):
        entered.set()
        await release.wait()
        if failure=='error':raise OSError('synthetic metadata failure')
        value=await metadata(path)
        if path=='/api/ps':value['models'][0]['digest']='b'*64
        return value
    c._metadata=blocked
    pending=asyncio.create_task(c.refresh())
    await asyncio.wait_for(entered.wait(),1)
    try:
        await cancel_refresh(c)
        release.set()
        with pytest.raises(StoryContextError):await pending
        assert not c.ready(URL,MODEL)
        assert not OllamaLLMProvider(URL,MODEL,1,story_backend_contract=c).supports_full_story
    finally:
        pending.cancel();await asyncio.gather(pending,return_exceptions=True)
