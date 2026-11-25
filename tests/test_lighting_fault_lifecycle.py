import asyncio
import os
import sys
from types import SimpleNamespace
import pytest
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.lighting import PhysicalLightingOutput,SerialLightingAdapter
from app.foundation.s2s import SoundDevicePlaybackController,AudioChunk
from app.foundation.stand import HardwareIntegrationStand
from test_astra_native_lighting_incident import NativeTTY

async def setup():
    stand=HardwareIntegrationStand(Settings(profile='mock',runtime_mode='clinical'),background_scheduler=False)
    stand._admits_physical_users=lambda:True
    await stand.inject_physical({'type':'SpaceReleased'})
    await stand.inject_physical({'type':'ButtonPressed','button_id':6})
    tty=NativeTTY();serial=SerialLightingAdapter(tty.path);serial.open()
    out=PhysicalLightingOutput(serial,auto_dark_recovery=True,on_fault=stand._on_lighting_fault)
    original=stand.lighting_output
    stand.lighting_output=out;stand.lighting_serial=serial;stand.hardware_mode='physical'
    stand.application.led.output=out;stand.application.led.exhibition_effects.output=out
    return stand,tty,serial,out,original

async def finish(parts):
    stand,tty,serial,out,original=parts
    pending=stand._lighting_fault_future
    if pending is not None:await asyncio.wrap_future(pending)
    stand.hardware_mode='simulator';stand.lighting_output=original;stand.lighting_serial=None
    stand.application.led.output=original;stand.application.led.exhibition_effects.output=original
    out.close();serial.close();tty.close();await stand.aclose()

async def fault(parts,monkeypatch):
    stand,tty,serial,out,_=parts;fd=serial.transport.fd;write=os.write;once=[False]
    def native(selected_fd,payload):
        if selected_fd==fd and not once[0]:
            once[0]=True;raise TimeoutError('PRIVATE_NATIVE_FAULT')
        return write(selected_fd,payload)
    with monkeypatch.context() as patch:
        patch.setattr(os,'write',native)
        out.set_element(10,1)
        with pytest.raises(ConnectionError):await asyncio.to_thread(out.flush)

@pytest.mark.asyncio
async def test_fault_preserves_urgent_and_never_fakes_hid_or_handset(monkeypatch):
    parts=await setup();stand,tty,serial,out,_=parts;sid=stand.active_session_id
    try:
        await stand.application.receive_async(Event('SpeechReceived',sid,1,1.,'urgent-test',{},'urgent'),'не хочу жить')
        state=stand.application.canonical_runtime.sessions[sid]
        assert state.mode=='safety_stop' and state.safety_severity=='urgent'
        assert 'incident_id' not in state.snapshot()
        handset=state.handset
        guard=stand._audio_owner_guard(sid,physical=True)
        await fault(parts,monkeypatch)
        assert not guard() and not stand._headless_audio_accepting()
        await asyncio.wrap_future(stand._lighting_fault_future)
        state=stand.application.canonical_runtime.sessions[sid]
        assert state.mode=='safety_stop' and state.safety_severity=='urgent'
        assert 'incident_id' not in state.snapshot()
        assert state.handset==handset=='up'
        assert getattr(stand,'_capture_hid_fault',None) is None
        assert stand.application.last_canonical_event['source']=='physical_lighting_loss'
        assert 'PRIVATE_NATIVE_FAULT' not in str(stand.diagnostics_snapshot())
        result=await stand.inject_physical({'type':'ButtonPressed','button_id':1})
        assert result['reason']=='lighting_unavailable'
        # Real hangup may update lifecycle, but must not silently reconcile fault.
        result=await stand.inject_physical({'type':'SpacePressed'})
        closed=result['canonical_state']
        assert closed['handset']=='down' and closed['mode']=='idle'
        assert closed['safety_severity']=='none'
        assert sid not in stand.application.canonical_runtime.sessions
        assert out.diagnostics()['fault_latched'] and not guard()
    finally:await finish(parts)

@pytest.mark.asyncio
async def test_startup_fault_without_loop_is_latched_and_blocks_start(monkeypatch):
    parts=await setup();stand,tty,serial,out,_=parts
    try:
        stand._physical_loop=None;stand._lighting_owner_loop=None
        await fault(parts,monkeypatch)
        assert stand._lighting_fault is not None and stand._lighting_fault_future is None
        assert not stand._headless_audio_accepting()
        with pytest.raises(RuntimeError,match='new hardware lifecycle'):await stand.start_hardware()
    finally:await finish(parts)

@pytest.mark.asyncio
async def test_old_output_failure_cannot_revoke_fresh_hardware_owner(monkeypatch):
    parts=await setup();stand,tty,serial,out,original=parts
    other_tty=NativeTTY();other_serial=SerialLightingAdapter(other_tty.path);other_serial.open()
    other=PhysicalLightingOutput(other_serial,auto_dark_recovery=True,on_fault=stand._on_lighting_fault)
    try:
        stand.lighting_output=other;stand.lighting_serial=other_serial
        await fault(parts,monkeypatch)
        assert stand._lighting_fault is None and stand._lighting_fault_future is None
        assert not stand._physical_output_faulted()
        other.set_element(14,1);other.flush();other_tty.receive()
        assert other_tty.state[14]==1
    finally:
        stand.lighting_output=out;stand.lighting_serial=serial
        other.close();other_serial.close();other_tty.close();await finish(parts)

@pytest.mark.asyncio
async def test_terminal_close_failure_does_not_schedule_new_cleanup(monkeypatch):
    parts=await setup();stand,tty,serial,out,_=parts;write=os.write;fd=serial.transport.fd
    def failed(selected_fd,payload):
        if selected_fd==fd:raise TimeoutError('PRIVATE_CLOSE')
        return write(selected_fd,payload)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(os,'write',failed)
            with pytest.raises(ConnectionError):await asyncio.to_thread(out.close)
        assert stand._lighting_fault is None and stand._lighting_fault_future is None
        assert out.diagnostics()['closed']
    finally:
        # Closed failed output cannot physically reconcile; do not misreport it.
        stand.hardware_mode='simulator';stand.lighting_output=parts[4];stand.lighting_serial=None
        stand.application.led.output=parts[4];stand.application.led.exhibition_effects.output=parts[4]
        serial.close();tty.close();await stand.aclose()

@pytest.mark.asyncio
@pytest.mark.parametrize('boundary',['queued','native_write','success'])
async def test_generic_native_playback_hardware_fence_covers_bounded_dialogue(monkeypatch,boundary):
    parts=await setup();stand,tty,serial,out,_=parts
    playback=SoundDevicePlaybackController();playback.active_token=1
    playback.hardware_current=lambda:not stand._physical_output_faulted()
    writes=[];entered=asyncio.Event();release=asyncio.Event()
    class Stream:
        def __init__(self,**kwargs):pass
        def start(self):pass
        def write(self,chunk):writes.append(len(chunk))
        def stop(self): self.drained = True
        def abort(self):pass
        def close(self):pass
    monkeypatch.setitem(sys.modules,'sounddevice',SimpleNamespace(
        OutputStream=Stream,query_devices=lambda *_:{'max_output_channels':1}))
    original=playback._run_io
    async def delayed(operation):
        if boundary=='native_write' and operation.__name__=='guarded_write':
            entered.set();await release.wait()
        return await original(operation)
    playback._run_io=delayed;task=None
    try:
        if boundary=='queued':await playback._play_lock.acquire()
        task=asyncio.create_task(playback.play(AudioChunk((.1,)*4096,16000,1,'stand')))
        if boundary=='native_write':await asyncio.wait_for(entered.wait(),1)
        elif boundary=='queued':await asyncio.sleep(0)
        if boundary!='success':await fault(parts,monkeypatch)
        release.set()
        if playback._play_lock.locked() and boundary=='queued':playback._play_lock.release()
        await task
        if boundary == 'success':
            assert sum(writes) == 4096
        else:
            assert not writes
    finally:
        release.set()
        if task:await asyncio.gather(task,return_exceptions=True)
        await playback.stop();await finish(parts)
