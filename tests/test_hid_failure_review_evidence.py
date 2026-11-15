"""Regression tests for fail-closed HID faults and independent teardown."""
import asyncio
from types import SimpleNamespace

import pytest
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.stand import HardwareIntegrationStand


async def active_stand():
    stand = HardwareIntegrationStand(settings=Settings(profile='mock').validate())
    app = stand.application
    sid = stand.active_session_id
    await app.receive_async(Event('HandsetLifted',sid,1,1.,'test',{},'lift',0.))
    app._cancel_welcome_timer(sid)
    state = app.canonical_runtime.sessions[sid]
    record = app.sessions.get(sid)
    state.mode='bear_dialogue'; state.handset='up'
    record.current_node='bear_dialogue'; record.handset_up=True
    app.led.clear_all(); app.led.set_element(1,1)
    stand._physical_loop=asyncio.get_running_loop()
    stand._hid_generation=1
    stand._hid_status={'state':'active','generation':1}
    stand._admits_physical_users=lambda: True
    return stand


def test_dispatch_hangup_exception_revokes_capture_and_clears_stale_lighting(caplog):
    async def run():
        stand=await active_stand()
        original=stand.application.receive_async
        try:
            async def failing(event,*args,**kwargs):
                if event.event_type=='HandsetPlaced': raise OSError('SYNTHETIC_PRIVATE_DETAIL')
                return await original(event,*args,**kwargs)
            stand.application.receive_async=failing
            owner=stand._headless_frame_owner()
            stand._headless_audio_queue=asyncio.Queue(maxsize=2)
            stand._on_physical_input(SimpleNamespace(type='SpacePressed',payload={}),generation=1)
            for _ in range(100):
                if stand._hid_status.get('state')=='dispatch_failure': break
                await asyncio.sleep(.005)
            assert stand._hid_status['state']=='dispatch_failure'
            assert stand._capture_hid_fault==1
            await asyncio.wrap_future(stand._hid_loss_future)
            assert stand.application.canonical_runtime.sessions[stand.active_session_id].mode=='safety_handoff'
            assert not stand._headless_audio_accepting() and not owner[1]()
            stand._headless_audio_queue=asyncio.Queue(maxsize=2)
            stand._enqueue_headless_audio_frame((.01,.02),owner)
            assert stand._headless_audio_queue.qsize()==0
            assert stand.lighting_output.snapshot()['1']==0
            assert stand.lighting_output.snapshot()['14']==1
            assert 'SYNTHETIC_PRIVATE_DETAIL' not in caplog.text

        finally:
            stand.application.receive_async=original
            stand._headless_audio_queue=None
            await stand.aclose()
    asyncio.run(run())


def test_audio_cancel_exception_preserves_independent_disconnect_safe_lighting():
    async def run():
        stand=await active_stand()
        app=stand.application
        original=app.audio.cancel
        try:
            def bad_cancel(*args): raise OSError('synthetic adapter cancel failure')
            app.audio.cancel=bad_cancel
            result=await app.receive_async(Event('HardwareDisconnected',stand.active_session_id,2,2.,'physical_hid_loss',{},'loss',0.))
            state=app.canonical_runtime.sessions[stand.active_session_id]
            assert state.mode=='safety_handoff'
            lighting=stand.lighting_output.snapshot()
            assert lighting['1']==0 and lighting['14']==1
            outputs=app.last_canonical_effects
            assert [effect['type'] for effect in outputs]==['audio_stop','capture_close','lighting_preset']
            assert outputs[0]['sync_cleanup']['status']=='failed'
            assert outputs[1]['resolution']=='application_boundary_acknowledged'
            assert outputs[2]['preset']=='SAFE_STOP'
        finally:
            app.audio.cancel=original
            await stand.aclose()
    asyncio.run(run())


def test_old_dispatch_completion_cannot_overwrite_new_reader_health():
    async def run():
        stand=await active_stand()
        original=stand.application.receive_async
        entered, release=asyncio.Event(),asyncio.Event()
        try:
            async def delayed_failure(event,*args,**kwargs):
                entered.set()
                await release.wait()
                raise OSError('synthetic old-generation failure')
            stand.application.receive_async=delayed_failure
            stand._on_physical_input(SimpleNamespace(type='SpacePressed',payload={}),generation=1)
            await asyncio.wait_for(entered.wait(),1)
            stand._hid_generation=2
            stand._hid_status={'state':'active','generation':2}
            release.set()
            for _ in range(100):
                if stand._hid_status.get('state')=='dispatch_failure': break
                await asyncio.sleep(.005)
            assert stand._hid_status['state']=='active' and stand._hid_status['generation']==2
            assert getattr(stand,'_capture_hid_fault',None) is None
        finally:
            release.set()
            stand.application.receive_async=original
            await stand.aclose()
    asyncio.run(run())


def test_failed_hid_loss_future_recovers_only_after_successful_teardown(monkeypatch):
    async def run():
        stand=await active_stand()
        app=stand.application
        original=app.audio.cancel
        try:
            def bad_cancel(*args): raise OSError('synthetic transient cancel failure')
            app.audio.cancel=bad_cancel
            stand._on_hid_error({'kind':'device_disconnected'},1)
            with pytest.raises(RuntimeError,match='hid_cleanup_failed'): await asyncio.wrap_future(stand._hid_loss_future)
            pending=stand._hid_loss_future
            with pytest.raises(RuntimeError,match='hid_cleanup_failed'):
                await stand._settle_hid_loss(pending)
            assert stand._hid_loss_future is pending
            from dataclasses import replace
            from app.foundation import stand as stand_module
            opened=[]
            class FakeHID:
                def __init__(self,*args,**kwargs): pass
                def open(self): opened.append(True)
                def close(self): pass
                def is_ready(self): return True
                def diagnostics(self): return {"physical_ready":True,"semantic_input_accepted":True,"initial_level_sync":{"status":"synced"}}
            monkeypatch.setattr(stand_module,"KeyboardHIDInputAdapter",FakeHID)
            stand.settings=replace(stand.settings,hid_device="/synthetic/no-device-opened")
            stand.hardware_mode='physical'
            stand._start_headless_audio_capture=lambda: setattr(stand,'_headless_audio_status',{'state':'active'})
            with pytest.raises(RuntimeError,match='hid_cleanup_failed'):
                await stand.start_hardware()
            assert opened==[] and stand._capture_hid_fault==1
            app.audio.cancel=original
            await stand.start_hardware()
            assert opened==[True] and stand._hid_loss_future is None
            assert stand._capture_hid_fault is None and stand._hid_generation==2
            assert stand.application.canonical_runtime.sessions[stand.active_session_id].mode=='safety_handoff'
            assert stand.lighting_output.snapshot()['14']==1
            await stand.stop_hardware()
            assert stand._hid_generation==3
        finally:
            stand.hardware_mode='simulator'
            app.audio.cancel=original
            await stand.aclose()
    asyncio.run(run())


def test_capture_stop_failure_does_not_block_other_device_teardown():
    async def run():
        stand=await active_stand()
        try:
            def fail(): raise OSError('synthetic capture stop failure')
            stand.audio_input=SimpleNamespace(stop=fail)
            stand._headless_audio_queue=asyncio.Queue(maxsize=2)
            stand._headless_audio_task=asyncio.create_task(asyncio.sleep(60))
            task=stand._headless_audio_task
            stand._on_hid_error({'kind':'device_disconnected'},1)
            with pytest.raises(OSError): await asyncio.wrap_future(stand._hid_loss_future)
            assert stand.lighting_output.snapshot()['14']==1
            assert stand._headless_audio_queue is None and stand._headless_audio_task is None
            assert task.done() and not stand._headless_audio_accepting()
        finally:
            stand.audio_input=None
            await stand.aclose()
    asyncio.run(run())


def test_timed_out_audio_owner_does_not_count_as_completed_hid_cleanup():
    async def run():
        stand=await active_stand()
        app=stand.application
        original=app._cancel_audio_owner
        old_owner=app.bounded_s2s
        try:
            async def timed_out(*args,**kwargs): return {'status':'timed_out'}
            app._cancel_audio_owner=timed_out
            app.bounded_s2s=SimpleNamespace()
            stand._on_hid_error({'kind':'device_disconnected'},1)
            with pytest.raises(RuntimeError,match='hid_cleanup_failed'):
                await asyncio.wrap_future(stand._hid_loss_future)
            assert stand._capture_hid_fault==1
            assert stand.lighting_output.snapshot()['14']==1
        finally:
            app._cancel_audio_owner=original
            app.bounded_s2s=old_owner
            await stand.aclose()
    asyncio.run(run())
