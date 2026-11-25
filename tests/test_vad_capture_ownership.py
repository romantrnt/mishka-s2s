"""Capture cancellation is scoped before mutation and after provider awaits."""
import asyncio,json,time
from types import SimpleNamespace
import pytest
from app.foundation.s2s import AudioIngressPipeline,EnergyVADAdapter,LocalS2SPipeline,S2SConfig

@pytest.mark.asyncio
@pytest.mark.parametrize('authorized',[False,True])
@pytest.mark.parametrize('operation',['cancel_session','cancel_output'])
async def test_stale_cancel_guard_preserves_fresh_capture_and_work(authorized,operation):
    config=S2SConfig()
    async def cancel_conversation(sid,*,ownership_guard=None):
        if ownership_guard is not None and not ownership_guard():return
    conversation=SimpleNamespace(config=config,cancel_session=cancel_conversation)
    pipeline=LocalS2SPipeline(EnergyVADAdapter(),None,conversation,config)
    fresh=AudioIngressPipeline(pipeline.vad,config)
    fresh.buffer.append((.1,)*512);fresh.active=True
    pipeline.ingress['same-sid']=fresh;pipeline.generations['same-sid']=9
    pending=asyncio.create_task(asyncio.Event().wait())
    pipeline.pending_tasks['same-sid']=pending;pipeline.pending_started_at['same-sid']=time.monotonic()
    try:
        await getattr(pipeline,operation)('same-sid',ownership_guard=lambda:authorized)
        fact={'operation':operation,'authorized':authorized,'fresh_capture_present':pipeline.ingress.get('same-sid') is fresh,
              'fresh_buffer_samples':len(fresh.buffer._samples),'pending_cancelled':pending.cancelled(),
              'generation':pipeline.generations.get('same-sid')}
        print(json.dumps(fact))
        if not authorized:
            assert pipeline.ingress.get('same-sid') is fresh
            assert len(fresh.buffer._samples)==512 and fresh.active
            assert not pending.cancelled()
            assert pipeline.generations['same-sid']==9
        elif operation=='cancel_session':
            assert 'same-sid' not in pipeline.ingress
            assert not fresh.buffer._samples and pending.cancelled()
        else:
            assert pipeline.ingress.get('same-sid') is fresh and fresh.active
            assert len(fresh.buffer._samples)==512 and pending.cancelled()
            assert pipeline.generations['same-sid']==10
    finally:
        pending.cancel();await asyncio.gather(pending,return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation',['cancel_session','cancel_output'])
async def test_old_cancel_after_provider_await_cannot_clear_replacement(operation):
    config=S2SConfig();entered=asyncio.Event();finish=asyncio.Event();mutations=[]
    async def cancel_conversation(sid,*,ownership_guard=None):
        entered.set();await finish.wait()
        if ownership_guard is None or ownership_guard():mutations.append(sid)
    conversation=SimpleNamespace(config=config,cancel_session=cancel_conversation)
    owner=LocalS2SPipeline(EnergyVADAdapter(),None,conversation,config)
    old=AudioIngressPipeline(owner.vad,config);owner.ingress['same-sid']=old
    task=asyncio.create_task(getattr(owner,operation)('same-sid',ownership_guard=lambda:True))
    try:
        await asyncio.wait_for(entered.wait(),1)
        fresh=AudioIngressPipeline(owner.vad,config);fresh.buffer.append((.2,)*512)
        owner.ingress['same-sid']=fresh;owner.generations['same-sid']=99
        finish.set();await asyncio.wait_for(task,1)
        assert owner.ingress['same-sid'] is fresh
        assert len(fresh.buffer._samples)==512 and owner.generations['same-sid']==99
        assert not mutations
    finally:
        finish.set();task.cancel();await asyncio.gather(task,return_exceptions=True)
