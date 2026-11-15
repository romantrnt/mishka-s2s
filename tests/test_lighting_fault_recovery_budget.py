import asyncio
import os
import threading
import pytest
from app.foundation.lighting import PhysicalLightingOutput,SerialLightingAdapter
from test_astra_native_lighting_incident import NativeTTY


def test_repeated_dark_and_close_do_not_repeat_automatic_compensation(monkeypatch):
    tty=NativeTTY();serial=SerialLightingAdapter(tty.path);serial.open()
    out=PhysicalLightingOutput(serial,auto_dark_recovery=True)
    write=os.write;fd=serial.transport.fd;calls=[]
    def failed(selected_fd,payload):
        if selected_fd==fd:
            calls.append(bytes(payload));raise TimeoutError('PRIVATE_PERMANENT_FAILURE')
        return write(selected_fd,payload)
    try:
        monkeypatch.setattr(os,'write',failed)
        out.set_element(10,1)
        with pytest.raises(ConnectionError):out.flush()
        assert len(calls)==2 and out.diagnostics()['dark_recovery_attempts']==1
        for index in range(3):
            out.clear_all()
            with pytest.raises(ConnectionError):out.flush()
            assert len(calls)==3+index
            assert out.diagnostics()['dark_recovery_attempts']==1
            assert not out.diagnostics()['pending_batches']
        with pytest.raises(ConnectionError):out.close()
        assert len(calls)==6 and out.diagnostics()['dark_recovery_attempts']==1
    finally:
        monkeypatch.setattr(os,'write',write)
        serial.close();tty.close()


def test_first_failure_during_close_has_no_automatic_compensation(monkeypatch):
    tty=NativeTTY();serial=SerialLightingAdapter(tty.path);serial.open()
    out=PhysicalLightingOutput(serial,auto_dark_recovery=True)
    write=os.write;fd=serial.transport.fd;calls=[]
    def failed(selected_fd,payload):
        if selected_fd==fd:
            calls.append(bytes(payload));raise TimeoutError('PRIVATE_CLOSE_FAILURE')
        return write(selected_fd,payload)
    try:
        monkeypatch.setattr(os,'write',failed)
        with pytest.raises(ConnectionError):out.close()
        assert len(calls)==1
        assert out.diagnostics()['dark_recovery_attempts']==0
        assert out.diagnostics()['closed']
    finally:
        monkeypatch.setattr(os,'write',write);serial.close();tty.close()


def test_old_generation_failure_does_not_consume_new_owners_compensation_budget(monkeypatch):
    tty=NativeTTY();serial=SerialLightingAdapter(tty.path);serial.open()
    out=PhysicalLightingOutput(serial,auto_dark_recovery=True)
    write=os.write;fd=serial.transport.fd;entered,release=threading.Event(),threading.Event();once=[False]
    def interrupted(selected_fd,payload):
        if selected_fd==fd and not once[0]:
            once[0]=True;entered.set();assert release.wait(2)
            raise TimeoutError('PRIVATE_SUPERSEDED_FAILURE')
        return write(selected_fd,payload)
    try:
        monkeypatch.setattr(os,'write',interrupted)
        out.set_element(10,1);assert entered.wait(1)
        out.clear_all();release.set();out.flush();tty.receive()
        assert out.diagnostics()['dark_recovery_attempts']==0
        assert out.diagnostics()['output_ready']
        assert not any(tty.state.values())
    finally:
        release.set();monkeypatch.setattr(os,'write',write);out.close();serial.close();tty.close()
