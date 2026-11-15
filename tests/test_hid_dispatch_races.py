import asyncio
import pytest
from app.foundation.stand import HardwareIntegrationStand
from app.foundation.config import Settings


async def settle_physical_inputs(stand):
    async def settled():
        # Reservation precedes the loop callback that creates the owned task.
        await asyncio.sleep(0)
        while stand._physical_pending_count:
            tasks = tuple(stand._physical_pending)
            if tasks:
                await asyncio.gather(*tasks)
            await asyncio.sleep(0)
        pending_loss = getattr(stand, '_hid_loss_future', None)
        if pending_loss is not None:
            await asyncio.wrap_future(pending_loss)
    await asyncio.wait_for(settled(), 2)
    assert not stand._physical_pending


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel_cleanup',[False,True])
async def test_capture_rejected_during_identity_hangup_cleanup(cancel_cleanup):
    from test_private_welcome import setup,start,deadline
    h,_=setup();stand=HardwareIntegrationStand(Settings(profile='mock'),background_scheduler=False)
    original_app=stand.application;stand.application=h.app;stand.active_session_id=h.boundary.session_id
    entered=asyncio.Event();release=asyncio.Event();task=None
    original=h.app._cleanup_session_resources
    async def blocked(*args,**kwargs):
        entered.set();await release.wait();return await original(*args,**kwargs)
    try:
        await start(h);await deadline(h);await h.complete()
        assert stand._headless_audio_accepting()
        h.app._cleanup_session_resources=blocked
        task=asyncio.create_task(h.send('HandsetPlaced'))
        await asyncio.wait_for(entered.wait(),2)
        assert h.state.handset=='down'
        if cancel_cleanup:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        stand._headless_audio_queue=asyncio.Queue()
        stand._enqueue_headless_audio_frame((.1,.2),stand._headless_frame_owner())
        assert stand._headless_audio_queue.empty()
        assert not stand._headless_audio_accepting()
        assert not stand._headless_frame_owner()[1].lifecycle_current()
    finally:
        release.set()
        if task: await asyncio.gather(task,return_exceptions=True)
        await h.close();stand.application=original_app;await stand.aclose()

@pytest.mark.asyncio
async def test_queued_lift_rechecks_hid_fault_before_dispatch():
    from app.foundation.input_adapters import NormalizedInputEvent
    stand=HardwareIntegrationStand(Settings(profile='mock'),background_scheduler=False)
    stand._physical_loop=asyncio.get_running_loop();stand._hid_generation=1
    stand._admits_physical_users=lambda:True
    called=[]
    receive=stand.application.receive_async
    async def observed(event,*args,**kwargs):
        called.append(event.event_type);return await receive(event,*args,**kwargs)
    stand.application.receive_async=observed
    try:
        stand._on_physical_input(NormalizedInputEvent('SpaceReleased',{}),generation=1)
        stand._on_hid_error({'kind':'device_disconnected'},1)
        await settle_physical_inputs(stand)
        assert 'HandsetLifted' not in called
        assert 'HardwareDisconnected' in called
    finally: await stand.aclose()

@pytest.mark.asyncio
@pytest.mark.parametrize('ordering',list(__import__('itertools').permutations([(57,0),(6,1),(8,1),(57,1)])))
async def test_queued_simultaneous_edges_end_matching_physical_level(ordering):
    from app.foundation.input_adapters import KeyboardHIDInputAdapter
    stand=HardwareIntegrationStand(Settings(profile='mock'),background_scheduler=False)
    stand._physical_loop=asyncio.get_running_loop();stand._hid_generation=1
    stand._admits_physical_users=lambda:True
    adapter=KeyboardHIDInputAdapter('/dev/null',lambda e:stand._on_physical_input(e,1))
    adapter._known_levels.add(57);adapter._pressed.add(57)
    try:
        # Every permutation in one reader batch, with the default 35ms debounce.
        for code,value in ordering: adapter.feed_key(code,value,timestamp=1.)
        await settle_physical_inputs(stand)
        record=stand.application.sessions.get(stand.active_session_id)
        actual_up=bool(record and record.handset_up)
        assert actual_up is (57 not in adapter._pressed)
        if not actual_up: assert not stand._headless_audio_accepting()
    finally: await stand.aclose()

@pytest.mark.asyncio
async def test_queued_old_reader_event_rejected_after_reconnect():
    from app.foundation.input_adapters import NormalizedInputEvent
    stand=HardwareIntegrationStand(Settings(profile='mock'),background_scheduler=False)
    stand._physical_loop=asyncio.get_running_loop();stand._hid_generation=1
    stand._admits_physical_users=lambda:True
    dispatched=[];inject=stand.inject_physical
    async def observed(payload):
        dispatched.append(payload)
        return await inject(payload)
    stand.inject_physical=observed
    try:
        stand._on_physical_input(NormalizedInputEvent('SpaceReleased',{}),1)
        stand._hid_generation=2
        await settle_physical_inputs(stand)
        assert dispatched == []
        # The callback rejects the old owner before creating a task; the
        # coroutine boundary independently rejects a stale generation too.
        result=await inject({'type':'SpaceReleased','_hid_generation':1})
        assert result['reason']=='stale_hid_generation'
        assert stand.application.sessions.get(stand.active_session_id) is None
    finally: await stand.aclose()
