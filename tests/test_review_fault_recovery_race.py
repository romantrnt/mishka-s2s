import asyncio
import concurrent.futures
from dataclasses import replace
import pytest
from app.foundation.stand import HardwareIntegrationStand
from app.foundation.config import Settings

@pytest.mark.asyncio
@pytest.mark.parametrize("barrier", [False, True])
async def test_two_explicit_recovery_callers_do_not_cleanup_new_generation(monkeypatch, barrier):
    from app.foundation import stand as module
    stand=HardwareIntegrationStand(Settings(profile='mock'),background_scheduler=False)
    stand.hardware_mode='physical';stand._hid_generation=1;stand._capture_hid_fault=1
    stand.settings=replace(stand.settings,hid_device='/synthetic')
    pending=concurrent.futures.Future();pending.set_exception(RuntimeError('old loss failed'))
    stand._hid_loss_future=pending
    events=[];opened=[]
    entered=asyncio.Event();release=asyncio.Event()
    class HID:
        def __init__(self,*a,**kw): pass
        def is_ready(self): return True
        def open(self): opened.append(stand._hid_generation)
        def close(self): pass
        def diagnostics(self): return {'physical_ready':True,'semantic_input_accepted':True,'initial_level_sync':{'status':'synced'}}
    monkeypatch.setattr(module,'KeyboardHIDInputAdapter',HID)
    async def receive(event,*args):
        events.append((event.event_type,stand._hid_generation))
        entered.set()
        if barrier:
            await release.wait()
        return {'outputs':[]}
    stand.application.receive_async=receive
    def start_capture():
        stand._headless_audio_task = asyncio.create_task(asyncio.Event().wait())
        stand._headless_audio_status = {"state": "active"}
    stand._start_headless_audio_capture = start_capture
    try:
        first=asyncio.create_task(stand.start_hardware())
        await asyncio.wait_for(entered.wait(), 2)
        second=asyncio.create_task(stand.start_hardware())
        if barrier:
            for _ in range(5):
                await asyncio.sleep(0)
            assert events == [("HardwareDisconnected", 1)]
            assert opened == []
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 2)
        assert opened==[2]
        assert events==[('HardwareDisconnected',1)]
    finally:
        stand.hardware_mode='simulator';await stand.aclose()

@pytest.mark.asyncio
async def test_settle_does_not_retry_replaced_owner():
    stand = HardwareIntegrationStand(Settings(profile="mock"), background_scheduler=False)
    stand._hid_generation = 1
    pending = concurrent.futures.Future()
    stand._hid_loss_future = pending
    calls = []
    async def cleanup(sid, generation):
        calls.append(generation)
    stand._handle_hid_loss = cleanup
    waiter = asyncio.create_task(stand._settle_hid_loss(pending))
    await asyncio.sleep(0)
    replacement = concurrent.futures.Future()
    stand._hid_loss_future = replacement
    stand._hid_generation = 2
    pending.set_exception(RuntimeError("old"))
    await waiter
    assert calls == []
    assert stand._hid_loss_future is replacement
    stand._hid_loss_future = None
    await stand.aclose()
