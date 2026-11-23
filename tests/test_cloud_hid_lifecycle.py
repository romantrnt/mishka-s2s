"""Cloud reconstruction regressions; synthetic inputs, never physical hardware."""
import asyncio
from unittest.mock import patch
import pytest
from app.foundation.input_adapters import KeyboardHIDInputAdapter as Adapter, NormalizedInputEvent
from app.foundation.stand import HardwareIntegrationStand
from app.foundation.config import Settings

@pytest.mark.parametrize('delay',[0,.0175,.0344,.1])
def test_fast_onhook_is_not_dropped(delay):
    events=[];a=Adapter('/dev/null',events.append)
    a._known_levels.add(57);a._pressed.add(57)
    a.feed_key(57,0,timestamp=1.)
    a.feed_key(57,1,timestamp=1.+delay)
    assert [e.type for e in events]==['SpaceReleased','SpacePressed']
    assert 57 in a._pressed


def test_reader_batch_uses_processing_clock_without_losing_onhook():
    events=[];a=Adapter('/dev/null',events.append)
    a._known_levels.add(57);a._pressed.add(57);a._fd=42
    data=a.EVENT_STRUCT.pack(100,0,1,57,0)+a.EVENT_STRUCT.pack(101,0,1,57,1)
    with patch('app.foundation.input_adapters.os.read',side_effect=[data,b'']), patch('app.foundation.input_adapters.time.monotonic',return_value=500):
        a._run()
    assert [e.type for e in events]==['SpaceReleased','SpacePressed']
    assert 57 in a._pressed


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_cleanup',[False,True])
async def test_canonical_onhook_blocks_fresh_capture_during_cleanup(cancel_cleanup):
    from test_private_welcome import setup,start,deadline
    h,_=setup();stand=HardwareIntegrationStand(Settings(profile='mock'),background_scheduler=False)
    old_app=stand.application;stand.application=h.app;stand.active_session_id=h.boundary.session_id
    entered=asyncio.Event();release=asyncio.Event();task=None
    original=h.app._cleanup_session_resources
    async def blocked(*args,**kwargs):
        entered.set();await release.wait();return await original(*args,**kwargs)
    try:
        await start(h);await deadline(h);await h.complete()
        await h.complete()
        for _ in range(2):
            await h.send('ButtonPressed', {'button_id':6})
            await h.send('ButtonReleased', {'button_id':6})
        assert h.state.mode=='bear_dialogue'
        assert stand._headless_audio_accepting()
        h.app._cleanup_session_resources=blocked
        task=asyncio.create_task(h.send('HandsetPlaced'))
        await asyncio.wait_for(entered.wait(),2)
        assert h.state.handset=='down'
        if cancel_cleanup:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
        stand._headless_audio_queue=asyncio.Queue()
        owner=stand._headless_frame_owner()
        stand._enqueue_headless_audio_frame((.1,.2),owner)
        assert stand._headless_audio_queue.empty()
        assert not stand._headless_audio_accepting()
        assert not owner[1].lifecycle_current()
    finally:
        release.set()
        if task: await asyncio.gather(task,return_exceptions=True)
        await h.close();stand.application=old_app;await stand.aclose()


@pytest.mark.asyncio
async def test_queued_lift_is_rejected_after_reader_fault(monkeypatch):
    stand=HardwareIntegrationStand(Settings(profile='mock'),background_scheduler=False)
    stand._physical_loop=asyncio.get_running_loop();stand._hid_generation=1
    stand._admits_physical_users=lambda:True
    calls=[];futures=[];schedule=asyncio.run_coroutine_threadsafe
    def tracked(coro,loop):
        future=schedule(coro,loop);futures.append(future);return future
    monkeypatch.setattr(asyncio,'run_coroutine_threadsafe',tracked)
    receive=stand.application.receive_async
    async def observed(event,*args,**kwargs):
        calls.append(event.event_type);return await receive(event,*args,**kwargs)
    stand.application.receive_async=observed
    try:
        stand._on_physical_input(NormalizedInputEvent('SpaceReleased',{}),1)
        stand._on_hid_error({'kind':'device_disconnected'},1)
        await asyncio.gather(*(asyncio.wrap_future(f) for f in futures))
        assert calls==['HardwareDisconnected']
    finally: await stand.aclose()
