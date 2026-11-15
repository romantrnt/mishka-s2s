"""Incident probes use the production POSIX transport and real pseudo-tty I/O.
Only the selected write syscall is fault-injected; no LED/application method is mocked.
"""
import errno
import os
import time
import pytest
from app.foundation.hardware import PosixSerialTransport
from app.foundation.lighting import LightingCommand, PhysicalLightingOutput, SerialLightingAdapter

class NativeTTY:
    def __init__(self):
        self.master, self.slave = os.openpty()
        os.set_blocking(self.master, False)
        self.path = os.ttyname(self.slave)
        self.buffer = b''
        self.state = {i: 0 for i in range(18)}
        self.lines = []
    def receive(self):
        while True:
            try:
                b = os.read(self.master, 4096)
            except BlockingIOError:
                break
            if not b: break
            self.buffer += b
        while b'\n' in self.buffer:
            line, self.buffer = self.buffer.split(b'\n', 1)
            self.lines.append(line)
            try:
                c, value, duration = map(int, line.split(b','))
                if c in self.state and value in (0,1) and duration >= 0:
                    self.state[c] = value
            except ValueError:
                pass
    def close(self):
        os.close(self.master); os.close(self.slave)

@pytest.mark.parametrize('prefix', [0, 3])
@pytest.mark.parametrize('failure', ['eio', 'timeout'])
def test_native_write_failure_latches_until_new_dark_generation(monkeypatch, prefix, failure):
    tty = NativeTTY()
    serial = SerialLightingAdapter(tty.path)
    serial.open()
    assert type(serial.transport) is PosixSerialTransport
    out = PhysicalLightingOutput(serial)
    write = os.write
    fd = serial.transport.fd
    phase = [0]
    def fault(selected_fd, payload):
        if selected_fd != fd:
            return write(selected_fd, payload)
        if prefix and phase[0] == 0:
            phase[0] = 1
            return write(selected_fd, payload[:prefix])
        phase[0] = 2
        if failure == 'timeout': raise TimeoutError('synthetic device stalls')
        raise OSError(errno.EIO, 'synthetic native I/O error')
    try:
        out.set_elements((LightingCommand(10,1), LightingCommand(11,1)))
        out.flush(); tty.receive()
        assert tty.state[10] == tty.state[11] == 1
        monkeypatch.setattr(os,'write',fault)
        out.clear_all()
        with pytest.raises(ConnectionError): out.flush()
        monkeypatch.setattr(os,'write',write)
        tty.receive()
        diag = out.diagnostics()
        assert diag['pending_batches']==0 and not diag['in_flight']
        assert diag['generation_failed'] and not diag['ready'] and not diag['output_ready']
        assert diag['failed_batches']==1 and diag['overflow_batches']==0
        assert diag['framing_recovery_pending'] and diag['framing_recoveries']==0
        assert diag['last_error']['exception_type']=='_IncompleteLightingLineError'
        assert diag['drain_alive']
        # Desired dark was committed before the asynchronous native failure,
        # while the device remains lit. This reproduces the live mismatch.
        assert not any(out.snapshot().values())
        assert tty.state[10] == tty.state[11] == 1
        sent = diag['sent_commands']; time.sleep(.04)
        assert out.diagnostics()['sent_commands']==sent
        # A host reopen alone does not repair the failed generation or notify MCU.
        serial.reconnect()
        assert serial.is_ready() and out.diagnostics()['generation_failed']
        with pytest.raises(ConnectionError): out.set_element(14,1)
        # Safe explicit generation reconciliation repairs the partial-line fence
        # and sends dark via the same native writer, without any process restart.
        out.reset_dark(); tty.receive()
        diag=out.diagnostics()
        assert not any(tty.state.values()) and not tty.buffer
        assert diag['output_ready'] and not diag['generation_failed']
        assert diag['framing_recoveries']==1 and not diag['framing_recovery_pending']
    finally:
        monkeypatch.setattr(os,'write',write)
        out.close(); serial.close(); tty.close()

def test_real_tty_disconnect_replacement_and_dark_recovery(tmp_path):
    first = NativeTTY(); second = None
    link = tmp_path/'lighting-device'
    link.symlink_to(first.path)
    serial=SerialLightingAdapter(str(link)); serial.open()
    out=PhysicalLightingOutput(serial)
    try:
        out.set_elements((LightingCommand(10,1),LightingCommand(11,1)))
        out.flush();first.receive()
        assert first.state[10]==first.state[11]==1
        # Real PTY peer disappearance causes the production os.write to fail.
        # No syscall or adapter method is replaced in this test.
        os.close(first.master);first.master=None
        out.clear_all()
        with pytest.raises(ConnectionError):out.flush()
        assert out.diagnostics()['generation_failed']
        assert out.diagnostics()['last_error']['exception_type']=='_IncompleteLightingLineError'
        second=NativeTTY();second.state=dict(first.state)
        link.unlink();link.symlink_to(second.path)
        out.reset_dark();second.receive()
        assert not any(second.state.values())
        assert not out.diagnostics()['generation_failed']
        assert out.diagnostics()['framing_recoveries']==1
    finally:
        if second is None:
            second=NativeTTY();link.unlink();link.symlink_to(second.path)
        out.close();serial.close()
        if first.master is not None:os.close(first.master)
        os.close(first.slave)
        second.close()

def test_new_dark_generation_survives_inflight_native_failure(monkeypatch):
    import threading
    tty=NativeTTY();serial=SerialLightingAdapter(tty.path);serial.open()
    out=PhysicalLightingOutput(serial);write=os.write;fd=serial.transport.fd
    entered,release=threading.Event(),threading.Event()
    phase=[0]
    def interrupted(selected_fd,payload):
        if selected_fd!=fd or phase[0]>=2:return write(selected_fd,payload)
        if phase[0]==0:
            phase[0]=1;return write(selected_fd,payload[:3])
        entered.set()
        assert release.wait(2)
        phase[0]=2
        raise OSError(errno.EIO,'synthetic interrupted old owner')
    try:
        out.set_elements((LightingCommand(10,1),LightingCommand(11,1)));out.flush();tty.receive()
        monkeypatch.setattr(os,'write',interrupted)
        out.set_element(13,1)
        assert entered.wait(1)
        # Same as a physical lifecycle owner scheduling terminal dark before
        # the older worker has reported its syscall error.
        out.clear_all()
        release.set();out.flush();tty.receive()
        assert not any(tty.state.values()) and not tty.buffer
        diag=out.diagnostics()
        assert diag['failed_batches']==1 and diag['framing_recoveries']==1
        assert diag['output_ready'] and not diag['generation_failed']
    finally:
        release.set();monkeypatch.setattr(os,'write',write)
        out.close();serial.close();tty.close()

@pytest.mark.parametrize('prefix',[0,3])
@pytest.mark.parametrize('failure',['eio','timeout'])
def test_single_automatic_dark_is_not_admission_or_firmware_ack(monkeypatch,prefix,failure):
    tty=NativeTTY();serial=SerialLightingAdapter(tty.path);serial.open()
    out=PhysicalLightingOutput(serial,auto_dark_recovery=True)
    write=os.write;fd=serial.transport.fd;phase=[0]
    def transient(selected_fd,payload):
        if selected_fd!=fd or phase[0]>=2:return write(selected_fd,payload)
        if prefix and phase[0]==0:
            phase[0]=1;return write(selected_fd,payload[:prefix])
        phase[0]=2
        if failure=='timeout':raise TimeoutError('PRIVATE_SYNTHETIC_DETAIL')
        raise OSError(errno.EIO,'PRIVATE_SYNTHETIC_DETAIL')
    try:
        out.set_elements((LightingCommand(10,1),LightingCommand(11,1)));out.flush();tty.receive()
        monkeypatch.setattr(os,'write',transient)
        out.set_element(13,1)
        with pytest.raises(ConnectionError):out.flush()
        tty.receive();diag=out.diagnostics()
        assert not any(tty.state.values()) and not tty.buffer
        assert diag['generation_failed'] and not diag['output_ready']
        assert diag['dark_recovery_attempts']==1
        assert diag['dark_recovery']['status']=='host_dark_written'
        assert diag['dark_recovery']['host_write_completed']
        assert not diag['dark_recovery']['firmware_acknowledged']
        assert not diag['firmware_acknowledged']
        assert diag['pending_batches']==0 and not diag['in_flight']
        assert diag['last_failure']['cause_type']==('OSError' if failure=='eio' else 'TimeoutError')
        assert diag['last_failure']['errno']==(errno.EIO if failure=='eio' else None)
        assert diag['last_failure']['at_monotonic']<=diag['dark_recovery']['at_monotonic']
        assert 'PRIVATE_SYNTHETIC_DETAIL' not in str(diag)
        with pytest.raises(ConnectionError):out.set_element(14,1)
        sent=diag['sent_commands'];time.sleep(.03)
        assert out.diagnostics()['sent_commands']==sent
        assert out.diagnostics()['dark_recovery_attempts']==1
        out.reset_dark();assert not out.diagnostics()['output_ready']
        assert out.diagnostics()['fault_latched']
    finally:
        monkeypatch.setattr(os,'write',write);out.close();serial.close();tty.close()


def test_persistent_native_failure_is_bounded_and_does_not_queue_retry(monkeypatch):
    tty=NativeTTY();serial=SerialLightingAdapter(tty.path);serial.open()
    out=PhysicalLightingOutput(serial,auto_dark_recovery=True)
    write=os.write;fd=serial.transport.fd;calls=[]
    def failed(selected_fd,payload):
        if selected_fd!=fd:return write(selected_fd,payload)
        calls.append(bytes(payload));raise OSError(errno.EIO,'PRIVATE_ALWAYS_FAILED')
    try:
        out.set_elements((LightingCommand(10,1),LightingCommand(11,1)));out.flush();tty.receive()
        monkeypatch.setattr(os,'write',failed)
        out.clear_all()
        with pytest.raises(ConnectionError):out.flush()
        diag=out.diagnostics()
        assert len(calls)==2 and diag['dark_recovery_attempts']==1
        assert diag['dark_recovery']['status']=='failed'
        assert not diag['dark_recovery']['host_write_completed']
        assert not diag['output_ready'] and diag['pending_batches']==0
        assert 'PRIVATE_ALWAYS_FAILED' not in str(diag)
        time.sleep(.03);assert len(calls)==2
        with pytest.raises(ConnectionError):out.set_element(14,1)
        assert len(calls)==2
        # Failed physical writes cannot be reported as achieved dark.
        tty.receive();assert tty.state[10]==tty.state[11]==1
    finally:
        monkeypatch.setattr(os,'write',write);out.close();serial.close();tty.close()


@pytest.mark.parametrize('replacement',['new_owner','close'])
def test_inflight_dark_compensation_obeys_new_owner_and_close(monkeypatch,replacement):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    tty=NativeTTY();serial=SerialLightingAdapter(tty.path);serial.open()
    out=PhysicalLightingOutput(serial,auto_dark_recovery=True)
    write=os.write;fd=serial.transport.fd;phase=[0]
    entered,release=threading.Event(),threading.Event()
    def interrupted(selected_fd,payload):
        if selected_fd!=fd or phase[0]>=2:return write(selected_fd,payload)
        if phase[0]==0:
            phase[0]=1;raise OSError(errno.EIO,'PRIVATE_OLD_FAILURE')
        entered.set();assert release.wait(2);phase[0]=2
        return write(selected_fd,payload)
    executor=ThreadPoolExecutor(max_workers=1);closing=None
    try:
        out.set_elements((LightingCommand(10,1),LightingCommand(11,1)));out.flush();tty.receive()
        monkeypatch.setattr(os,'write',interrupted)
        out.set_element(13,1);assert entered.wait(1)
        if replacement=='new_owner':
            out.invalidate_pending()
            with pytest.raises(ConnectionError):
                out.set_elements(tuple(LightingCommand(i,int(i==14)) for i in range(18)))
        else:
            closing=executor.submit(out.close)
            for _ in range(100):
                if out._closing:break
                time.sleep(.001)
            assert out._closing
        release.set()
        if closing:closing.result(timeout=2)
        else:
            with pytest.raises(ConnectionError):out.flush()
        tty.receive();diag=out.diagnostics()
        assert diag['dark_recovery_attempts']==1
        assert diag['dark_recovery']['status']==('host_dark_written' if replacement=='new_owner' else 'superseded_or_timed_out')
        assert diag['dark_recovery']['host_write_completed']==(replacement=='new_owner')
        assert not diag['firmware_acknowledged']
        assert diag['pending_batches']==0 and not diag['in_flight']
        assert not any(tty.state.values())
        assert not tty.buffer
    finally:
        release.set();monkeypatch.setattr(os,'write',write)
        if closing:closing.result(timeout=2)
        out.close();executor.shutdown();serial.close();tty.close()

def test_native_recovery_budget_stops_before_new_target_lines(monkeypatch):
    tty=NativeTTY();serial=SerialLightingAdapter(tty.path);serial.open()
    out=PhysicalLightingOutput(serial,auto_dark_recovery=True)
    write=os.write;fd=serial.transport.fd;calls=[]
    def delayed(selected_fd,payload):
        if selected_fd!=fd:return write(selected_fd,payload)
        calls.append(bytes(payload))
        if len(calls)==1:raise OSError(errno.EIO,'synthetic first fault')
        if len(calls)==2:time.sleep(1.05)
        return write(selected_fd,payload)
    try:
        out.set_elements((LightingCommand(10,1),LightingCommand(11,1)));out.flush();tty.receive()
        monkeypatch.setattr(os,'write',delayed)
        out.set_element(13,1)
        before=time.monotonic()
        with pytest.raises(ConnectionError):out.flush()
        assert time.monotonic()-before<1.9
        assert len(calls)==2 and calls[1]==b'\n'
        diag=out.diagnostics()
        assert diag['dark_recovery']['status']=='superseded_or_timed_out'
        assert not diag['dark_recovery']['host_write_completed']
        assert not diag['output_ready'] and diag['pending_batches']==0
        tty.receive();assert tty.state[10]==tty.state[11]==1
    finally:
        monkeypatch.setattr(os,'write',write);out.close();serial.close();tty.close()


@pytest.mark.asyncio
async def test_recovered_dark_keeps_real_stand_admission_closed_and_safe_evidence(monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace
    from app.foundation.config import Settings
    from app.foundation.stand import HardwareIntegrationStand
    stand=HardwareIntegrationStand(Settings(profile='mock'),background_scheduler=False)
    tty=NativeTTY();serial=SerialLightingAdapter(tty.path);serial.open()
    out=PhysicalLightingOutput(serial,auto_dark_recovery=True)
    original_output=stand.lighting_output
    stand.lighting_serial=serial;stand.lighting_output=out
    stand.hardware_mode='physical';stand._lighting_status={'state':'available'}
    stand.settings=replace(stand.settings,max_client_source='local_demo')
    stand.input_adapter=SimpleNamespace(diagnostics=lambda:{'physical_ready':True,'semantic_input_accepted':True,'initial_level_sync':{'status':'synced'}},close=lambda:None)
    stand._hid_status={'state':'active'}
    stand.audio_input=SimpleNamespace(is_ready=lambda:True,stop=lambda:None,close=lambda:None)
    stand._headless_audio_status={'state':'active'}
    stand._admits_physical_users=stand.admission_dependencies_ready
    write=os.write;fd=serial.transport.fd;once=[False]
    def failed(selected_fd,payload):
        if selected_fd==fd and not once[0]:
            once[0]=True;raise OSError(errno.EIO,'PRIVATE_CREDENTIAL_SHOULD_NOT_APPEAR')
        return write(selected_fd,payload)
    try:
        assert stand.admission_dependencies_ready()
        before=dict(stand.application.canonical_runtime.sessions)
        monkeypatch.setattr(os,'write',failed)
        out.set_element(10,1)
        with pytest.raises(ConnectionError):out.flush()
        projection=stand.diagnostics_snapshot()['lighting_hardware']
        assert projection['dark_recovery']['host_write_completed']
        assert projection['last_failure']['cause_type']=='OSError'
        assert projection['last_failure']['errno']==errno.EIO
        assert projection['last_write_failure']['at_monotonic']>0
        assert 'PRIVATE_CREDENTIAL_SHOULD_NOT_APPEAR' not in str(projection)
        assert not projection['firmware_acknowledged']
        assert not stand.admission_dependencies_ready()
        assert stand.application.canonical_runtime.sessions==before
        assert stand.health_snapshot()['lighting']['status']=='unavailable'
    finally:
        monkeypatch.setattr(os,'write',write)
        stand.hardware_mode='simulator';stand.lighting_output=original_output;stand.lighting_serial=None
        stand.input_adapter=None;stand.audio_input=None
        out.close();serial.close();tty.close();await stand.aclose()
