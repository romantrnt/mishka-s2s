import asyncio
import concurrent.futures
from dataclasses import replace
import pytest
from app.foundation.stand import HardwareIntegrationStand
from app.foundation.config import Settings

@pytest.mark.asyncio
async def test_two_explicit_recovery_callers_do_not_cleanup_new_generation(monkeypatch):
    from app.foundation import stand as module
    stand=HardwareIntegrationStand(Settings(profile='mock'),background_scheduler=False)
    stand.hardware_mode='physical';stand._hid_generation=1;stand._capture_hid_fault=1
    stand.settings=replace(stand.settings,hid_device='/synthetic')
    pending=concurrent.futures.Future();pending.set_exception(RuntimeError('old loss failed'))
    stand._hid_loss_future=pending
    events=[];opened=[]
    class HID:
        def __init__(self,*a,**kw): pass
        def is_ready(self): return True
        def open(self): opened.append(stand._hid_generation)
        def close(self): pass
        def diagnostics(self): return {'physical_ready':True,'semantic_input_accepted':True,'initial_level_sync':{'status':'synced'}}
    monkeypatch.setattr(module,'KeyboardHIDInputAdapter',HID)
    async def receive(event,*args):
        events.append((event.event_type,stand._hid_generation));return {'outputs':[]}
    stand.application.receive_async=receive
    stand._start_headless_audio_capture=lambda:None
    try:
        await asyncio.gather(stand.start_hardware(),stand.start_hardware())
        assert opened==[2]
        assert events==[('HardwareDisconnected',1)]
    finally:
        stand.hardware_mode='simulator';await stand.aclose()

@pytest.mark.asyncio
@pytest.mark.parametrize('operations',[('start','start'),('start','stop'),('stop','start')])
async def test_held_cleanup_serializes_explicit_lifecycle_operations(monkeypatch,operations):
    from app.foundation import stand as module
    stand=HardwareIntegrationStand(Settings(profile='mock'),background_scheduler=False)
    stand.hardware_mode='physical';stand._hid_generation=1;stand._capture_hid_fault=1
    stand.settings=replace(stand.settings,hid_device='/synthetic')
    pending=concurrent.futures.Future();pending.set_exception(RuntimeError('old loss failed'))
    stand._hid_loss_future=pending
    entered=asyncio.Event();release=asyncio.Event();events=[];opened=[];closed=[]
    class HID:
        def __init__(self,*a,**kw): self.ready=False
        def is_ready(self): return self.ready
        def open(self): self.ready=True;opened.append(stand._hid_generation)
        def close(self): self.ready=False;closed.append(stand._hid_generation)
        def diagnostics(self): return {'physical_ready':self.ready,'semantic_input_accepted':self.ready,'initial_level_sync':{'status':'synced'}}
    monkeypatch.setattr(module,'KeyboardHIDInputAdapter',HID)
    async def receive(event,*args):
        events.append((event.event_type,stand._hid_generation))
        entered.set();await release.wait();return {'outputs':[]}
    stand.application.receive_async=receive
    stand._start_headless_audio_capture=lambda:None
    tasks=[]
    try:
        tasks.append(asyncio.create_task(getattr(stand,operations[0]+'_hardware')()))
        await asyncio.wait_for(entered.wait(),1)
        tasks.append(asyncio.create_task(getattr(stand,operations[1]+'_hardware')()))
        for _ in range(10): await asyncio.sleep(0)
        # A second explicit operation must not start its own teardown of the
        # same generation while the first teardown is still in progress.
        assert events==[('HardwareDisconnected',1)]
        assert not opened and not tasks[1].done()
        release.set();await asyncio.wait_for(asyncio.gather(*tasks),2)
        assert events==[('HardwareDisconnected',1)]
        assert len(opened)==1
        if operations[-1]=='stop':
            assert stand.input_adapter is None
            assert stand._hid_status['state']=='stopped'
            assert closed==[3]
        else:
            assert stand.input_adapter.is_ready()
            assert stand._capture_hid_fault is None
        assert stand._hid_loss_future is None
    finally:
        release.set()
        await asyncio.gather(*tasks,return_exceptions=True)
        stand.hardware_mode='simulator';await stand.aclose()
