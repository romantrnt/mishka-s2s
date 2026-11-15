import asyncio
from types import SimpleNamespace
import pytest
from test_lighting_fault_lifecycle import setup,finish,fault

@pytest.mark.asyncio
async def test_native_fault_after_button_admission_is_not_reclassified_as_hid_failure(monkeypatch):
    parts=await setup();stand=parts[0];app=stand.application
    original_receive=app.receive_async;original_cleanup=stand._handle_lighting_fault
    original_observer=stand._observe_physical_input_future
    admitted,resume,cleanup_release,observed=asyncio.Event(),asyncio.Event(),asyncio.Event(),asyncio.Event()
    outcome={}
    async def delayed_receive(event,*args,**kwargs):
        if event.event_type=='ButtonPressed' and event.payload.get('button_id')==7:
            admitted.set();await resume.wait()
        return await original_receive(event,*args,**kwargs)
    async def delayed_cleanup(*args,**kwargs):
        await cleanup_release.wait();return await original_cleanup(*args,**kwargs)
    def observed_done(done,*args,**kwargs):
        outcome['error']=done.exception()
        if outcome['error'] is None:outcome['result']=done.result()
        original_observer(done,*args,**kwargs);observed.set()
    app.receive_async=delayed_receive;stand._handle_lighting_fault=delayed_cleanup
    stand._observe_physical_input_future=observed_done
    stand._physical_loop=asyncio.get_running_loop();stand._hid_generation=1
    stand._hid_status={'state':'active','generation':1}
    try:
        stand._on_physical_input(SimpleNamespace(type='ButtonPressed',payload={'button_id':7}),generation=1)
        await asyncio.wait_for(admitted.wait(),1)
        # Real POSIX write failure occurs strictly after inject_physical's gate,
        # before the admitted application's real Button7 lighting effect.
        await fault(parts,monkeypatch)
        assert stand._lighting_fault is not None
        resume.set();await asyncio.wait_for(observed.wait(),2)
        assert outcome['error'] is None, type(outcome['error']).__name__
        assert outcome['result']['reason']=='lighting_unavailable'
        assert outcome['result']['canonical_state']['handset']=='up'
        assert not outcome['result']['host_write_completed']
        assert not outcome['result']['firmware_acknowledged']
        assert getattr(stand,'_capture_hid_fault',None) is None
        assert stand._hid_status['state']=='active'
        cleanup_release.set();await asyncio.wrap_future(stand._lighting_fault_future)
        assert app.canonical_runtime.sessions[stand.active_session_id].mode=='safety_handoff'
        assert getattr(stand,'_capture_hid_fault',None) is None
    finally:
        resume.set();cleanup_release.set()
        app.receive_async=original_receive;stand._handle_lighting_fault=original_cleanup
        stand._observe_physical_input_future=original_observer
        pending=getattr(stand,'_hid_loss_future',None)
        if pending is not None:await asyncio.gather(asyncio.wrap_future(pending),return_exceptions=True)
        await finish(parts)
