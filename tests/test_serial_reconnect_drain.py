"""Use the native PTY path; inject only CDC drain/write syscall failures."""
import errno
import os
import termios
import time
import tty
from types import SimpleNamespace

import pytest

from app.foundation.lighting import LightingCommand, PhysicalLightingOutput, SerialLightingAdapter
from app.foundation.stand import HardwareIntegrationStand
from test_astra_native_lighting_incident import NativeTTY


def test_stalled_old_output_drain_does_not_exhaust_automatic_dark_budget(monkeypatch):
    device = NativeTTY()
    serial = SerialLightingAdapter(device.path)
    serial.open()
    output = PhysicalLightingOutput(serial, auto_dark_recovery=True)
    native_write = os.write
    native_tcsetattr = tty.tcsetattr
    serial_fd = serial.transport.fd
    failed = False

    def fail_once(fd, payload):
        nonlocal failed
        if fd == serial_fd and not failed:
            failed = True
            raise TimeoutError("synthetic CDC write stall")
        return native_write(fd, payload)

    def stalled_drain(fd, when, attributes):
        # PTYs do not emulate a CDC driver's stalled output drain. Model it
        # at this one syscall boundary, preserving all remaining native I/O.
        if when in (termios.TCSADRAIN, termios.TCSAFLUSH):
            time.sleep(1.1)
        return native_tcsetattr(fd, when, attributes)

    try:
        output.set_elements((LightingCommand(10, 1), LightingCommand(11, 1)))
        output.flush()
        device.receive()
        with monkeypatch.context() as patch:
            patch.setattr(os, "write", fail_once)
            patch.setattr(tty, "tcsetattr", stalled_drain)
            output.set_element(13, 1)
            started = time.monotonic()
            with pytest.raises(ConnectionError):
                output.flush()
            elapsed = time.monotonic() - started
        device.receive()
        diagnostics = output.diagnostics()
        assert diagnostics["dark_recovery"]["status"] == "host_dark_written"
        assert elapsed < 1.0
        assert not any(device.state.values()) and not device.buffer
        assert diagnostics["framing_recoveries"] == 1
        assert not diagnostics["framing_recovery_pending"]
        assert diagnostics["dark_recovery_attempts"] == 1
        assert diagnostics["fault_latched"] and not diagnostics["output_ready"]
        assert not diagnostics["firmware_acknowledged"]
        assert diagnostics["pending_batches"] == 0 and not diagnostics["in_flight"]
        with pytest.raises(ConnectionError):
            output.set_element(14, 1)
    finally:
        output.close()
        serial.close()
        device.close()


def test_first_failure_survives_dark_recovery_and_later_cleanup_failures(monkeypatch):
    device = NativeTTY()
    serial = SerialLightingAdapter(device.path)
    serial.open()
    notifications = []
    output = PhysicalLightingOutput(
        serial, auto_dark_recovery=True,
        on_fault=lambda owner, failure: notifications.append(failure),
    )
    native_write = os.write
    serial_fd = serial.transport.fd
    calls = 0

    def failing_device(fd, payload):
        nonlocal calls
        if fd != serial_fd:
            return native_write(fd, payload)
        calls += 1
        if calls == 1:
            raise OSError(errno.EIO, "PRIVATE_INITIAL_DEVICE_FAILURE")
        raise TimeoutError("PRIVATE_LATER_CLEANUP_FAILURE")

    def projection():
        owner = SimpleNamespace(lighting_serial=serial, lighting_output=output)
        return HardwareIntegrationStand._lighting_diagnostics(owner)

    try:
        assert projection()["first_failure"] is None
        with monkeypatch.context() as patch:
            patch.setattr(os, "write", failing_device)
            output.set_element(10, 1)
            with pytest.raises(ConnectionError):
                output.flush()
            first = projection()["first_failure"]
            assert first["cause_type"] == "OSError" and first["errno"] == errno.EIO
            assert first["generation"] == 0
            assert first["stage"] == "output_batch_write"
            assert first["at_monotonic"] > 0
            assert projection()["dark_recovery"]["status"] == "failed"
            for _ in range(2):
                output.clear_all()
                with pytest.raises(ConnectionError):
                    output.flush()
                diagnostics = projection()
                assert diagnostics["first_failure"] == first
                assert diagnostics["last_failure"]["cause_type"] == "TimeoutError"
                assert diagnostics["last_failure"]["generation"] > first["generation"]
                assert diagnostics["fault_latched"] and not diagnostics["output_ready"]
            assert len(notifications) == 1
            assert diagnostics["dark_recovery_attempts"] == 1
            assert "PRIVATE_" not in str(diagnostics)
            first["cause_type"] = "mutated observer copy"
            assert projection()["first_failure"]["cause_type"] == "OSError"
    finally:
        output.close()
        serial.close()
        device.close()
