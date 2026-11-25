"""Capture cancellation is scoped before mutation and after provider awaits."""
import asyncio,json,time
from types import SimpleNamespace
import pytest
from app.foundation.s2s import AudioIngressPipeline,EnergyVADAdapter,LocalS2SPipeline,S2SConfig,SileroVADAdapter

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


class StatefulVAD:
    def __init__(self):self.calls=0;self.state=0;self.resets=0;self.fail_reset=False
    def __call__(self,samples,rate):
        assert rate==16000 and len(samples)==512
        self.calls+=1;self.state+=1
        return .95 if any(samples) else 0.
    def reset_states(self):
        self.resets+=1
        if self.fail_reset:raise RuntimeError('test_reset_failed')
        self.state=0


def loaded_vad():
    model=StatefulVAD();vad=SileroVADAdapter(lambda:model);vad._model=model
    return vad,model


def test_api_loader_exposes_reset_on_the_same_probability_callable(monkeypatch):
    import sys
    from app.foundation.api import _silero_loader
    class Model(StatefulVAD):
        def __call__(self,samples,rate):
            value=super().__call__(samples,rate)
            return SimpleNamespace(item=lambda:value)
    model=Model();loads=[]
    def load():loads.append(True);return model
    fake_torch=SimpleNamespace(float32='float32',tensor=lambda x,**kwargs:x)
    monkeypatch.setitem(sys.modules,'torch',fake_torch)
    monkeypatch.setitem(sys.modules,'silero_vad',SimpleNamespace(load_silero_vad=load))
    probability=_silero_loader(SimpleNamespace(vad_device='cpu'))
    assert probability((.2,)*512,16000)==.95
    probability.reset_states()
    assert model.resets==1 and model.calls==1 and loads==[True]


def test_same_capture_buffer_and_speech_end_resets_keep_vad_continuity():
    vad,model=loaded_vad();ingress=AudioIngressPipeline(vad,S2SConfig())
    ingress.feed((.2,)*256,1)
    assert model.resets==1 and model.calls==0
    ingress.feed((.2,)*256,1)
    ingress.reset()  # Explicit buffer reset is not a new capture owner.
    for _ in range(10):ingress.feed((.2,)*512,1)
    for _ in range(7):status,chunk=ingress.feed((0.,)*512,1)
    assert status=='speech_end' and chunk is not None
    assert model.resets==1 and model.state==18
    ingress.feed((.2,)*512,1)
    assert model.resets==1 and model.state==19


def test_interleaved_live_capture_cannot_reset_or_feed_the_current_owner():
    vad,model=loaded_vad();first=AudioIngressPipeline(vad,S2SConfig());second=AudioIngressPipeline(vad,S2SConfig())
    first.feed((.2,)*512,1);before=(model.resets,model.calls,model.state)
    with pytest.raises(RuntimeError,match='capture'):
        second.feed((.3,)*512,2)
    assert (model.resets,model.calls,model.state)==before
    assert not second._vad_pending
    first.feed((.2,)*512,1)
    assert model.state==2 and model.resets==1


def test_closed_capture_and_late_close_cannot_touch_the_new_owner():
    vad,model=loaded_vad();old=AudioIngressPipeline(vad,S2SConfig())
    old.feed((.2,)*512,1);old.close_capture()
    fresh=AudioIngressPipeline(vad,S2SConfig());fresh.feed((.3,)*512,2)
    before=(model.resets,model.calls,model.state)
    old.close_capture()
    with pytest.raises(RuntimeError,match='closed'):
        old.feed((.2,)*512,1)
    assert (model.resets,model.calls,model.state)==before
    fresh.feed((.3,)*512,2)
    assert model.state==2


def test_late_pipeline_token_cannot_close_replacement_same_sid():
    vad,model=loaded_vad();config=S2SConfig()
    owner=LocalS2SPipeline(vad,None,SimpleNamespace(config=config),config)
    old=AudioIngressPipeline(vad,config);owner.ingress['sid']=old;old.feed((.2,)*512,1)
    owner.close_capture('sid',expected_ingress=old)
    fresh=AudioIngressPipeline(vad,config);owner.ingress['sid']=fresh;fresh.feed((.3,)*512,2)
    before=(model.resets,model.calls,model.state,owner.generations['sid'])
    owner.close_capture('sid',expected_ingress=old)
    assert owner.ingress['sid'] is fresh
    assert (model.resets,model.calls,model.state,owner.generations['sid'])==before


def test_capture_close_before_first_vad_window_never_loads_model():
    loads=[]
    def loader():loads.append(True);return StatefulVAD()
    vad=SileroVADAdapter(loader);old=AudioIngressPipeline(vad,S2SConfig())
    old.feed((.2,)*128,1);old.close_capture()
    assert not loads
    fresh=AudioIngressPipeline(vad,S2SConfig());fresh.feed((.2,)*512,2)
    assert loads==[True] and vad._model.resets==1 and vad._model.state==1


def test_reset_failure_revokes_old_capture_and_next_owner_retries_reset():
    vad,model=loaded_vad();old=AudioIngressPipeline(vad,S2SConfig());old.feed((.2,)*512,1)
    model.fail_reset=True
    with pytest.raises(RuntimeError,match='test_reset_failed'):old.close_capture()
    assert not old.buffer._samples
    model.fail_reset=False
    fresh=AudioIngressPipeline(vad,S2SConfig());fresh.feed((.3,)*512,2)
    assert model.state==1


@pytest.mark.asyncio
async def test_capture_reset_failure_still_cancels_pending_and_conversation():
    vad,model=loaded_vad();config=S2SConfig();conversation_calls=[]
    async def cancel_conversation(sid,*,ownership_guard=None):
        if ownership_guard is None or ownership_guard():conversation_calls.append(sid)
    owner=LocalS2SPipeline(vad,None,SimpleNamespace(config=config,cancel_session=cancel_conversation),config)
    ingress=AudioIngressPipeline(vad,config);owner.ingress['sid']=ingress;ingress.feed((.2,)*512,1)
    pending=asyncio.create_task(asyncio.Event().wait());owner.pending_tasks['sid']=pending
    model.fail_reset=True
    try:
        with pytest.raises(RuntimeError,match='test_reset_failed'):
            await owner.cancel_session('sid')
        assert 'sid' not in owner.ingress and not ingress.buffer._samples
        assert pending.cancelled() and 'sid' not in owner.pending_tasks
        assert conversation_calls==['sid']
        model.fail_reset=False
        fresh=AudioIngressPipeline(vad,config);fresh.feed((.3,)*512,2)
        assert model.state==1
    finally:
        pending.cancel();await asyncio.gather(pending,return_exceptions=True)
