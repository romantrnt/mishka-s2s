import asyncio
import errno
import threading
import time

import pytest

from app.foundation.lighting import (
    ExhibitionLightingEngine, LightingFaultLatchedError, PhysicalLightingOutput,
    SerialLightingAdapter,
)


class GatedWire:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.writes = []
        self.state = {i: 1 for i in range(18)}
        self.read_failure = threading.Event()
        self.opens = 0

    def open(self):
        self.opens += 1

    def close(self):
        pass

    def write(self, raw):
        self.entered.set()
        assert self.release.wait(3)
        self.writes.append(raw)
        channel, state, _ = map(int, raw.strip().split(b","))
        self.state[channel] = state

    def read_available(self):
        if self.read_failure.is_set():
            raise OSError(errno.EIO, "PRIVATE_RX_DEVICE_PATH")
        return b""


def wait_until(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(.005)


def test_queue_overflow_stops_producer_fences_backlog_and_writes_terminal_dark():
    async def scenario():
        wire = GatedWire()
        serial = SerialLightingAdapter("synthetic", transport=wire)
        serial.open()
        faults = []
        output = PhysicalLightingOutput(serial, auto_dark_recovery=True,
                                        on_fault=lambda *args: faults.append(args))
        try:
            output.set_element(0, 1)
            assert wire.entered.wait(1)
            for _ in range(31):
                output.set_element(0, 1)
            engine = ExhibitionLightingEngine(output, background_scheduler=False)
            engine.effect = "breathing"
            engine.started_at_monotonic = 0
            await engine._run(engine.generation)
            assert output.diagnostics()["fault_latched"]
            assert not output.diagnostics()["output_ready"]
            assert len(faults) == 1
            assert output.diagnostics()["pending_batches"] == 0
            with pytest.raises(LightingFaultLatchedError):
                output.set_element(1, 1)
            wire.release.set()
            with pytest.raises(ConnectionError):
                await asyncio.to_thread(output.flush)
            assert not any(wire.state.values())
            assert len(wire.writes) == 19  # one uninterruptible line, then dark
            assert output.diagnostics()["dark_recovery_attempts"] == 1
        finally:
            wire.release.set()
            output.close()
            serial.close()
    asyncio.run(scenario())


def test_rx_failure_is_visible_and_latches_owner_without_unbounded_retries():
    wire = GatedWire()
    wire.release.set()
    serial = SerialLightingAdapter("synthetic", transport=wire)
    serial.open()
    faults = []
    output = PhysicalLightingOutput(serial, auto_dark_recovery=True,
                                    on_fault=lambda *args: faults.append(args))
    try:
        wire.read_failure.set()
        wait_until(lambda: bool(faults))
        wait_until(lambda: output.diagnostics()["dark_recovery_attempts"] == 1)
        with pytest.raises(ConnectionError):
            output.flush()
        diagnostic = output.diagnostics()
        assert not diagnostic["output_ready"]
        assert diagnostic["fault_latched"]
        assert diagnostic["first_failure"]["stage"] == "serial_read"
        assert diagnostic["first_failure"]["errno"] == errno.EIO
        assert len(faults) == 1
        assert wire.opens <= 2
        assert "PRIVATE_RX_DEVICE_PATH" not in str(diagnostic)
    finally:
        wire.read_failure.clear()
        output.close()
        serial.close()


def test_closed_reader_cannot_report_a_fault_against_reopened_adapter():
    class DelayedReader(GatedWire):
        def __init__(self):
            super().__init__()
            self.reader_entered = threading.Event()
            self.reader_release = threading.Event()
            self.old_reader = None

        def read_available(self):
            if self.old_reader is None:
                self.old_reader = threading.current_thread()
                self.reader_entered.set()
                assert self.reader_release.wait(3)
                raise OSError(errno.EIO, "old descriptor")
            return b""

    wire = DelayedReader()
    serial = SerialLightingAdapter("synthetic", transport=wire)
    serial.open()
    assert wire.reader_entered.wait(1)
    old_thread = serial._drain_thread
    try:
        serial.close()
        serial.open()
        wire.reader_release.set()
        old_thread.join(1)
        assert not old_thread.is_alive()
        assert serial.is_ready()
        assert serial.diagnostics()["last_error"] is None
    finally:
        wire.reader_release.set()
        serial.close()


def test_native_pty_peer_hangup_is_not_mistaken_for_idle_rx():
    import os
    import pty
    from app.foundation.hardware import PosixSerialTransport
    master, slave = pty.openpty()
    serial = PosixSerialTransport(os.ttyname(slave))
    try:
        serial.open()
        assert serial.read_available() == b""
        os.close(master)
        master = None
        with pytest.raises(ConnectionError, match="disconnected"):
            serial.read_available()
    finally:
        serial.close()
        if master is not None:
            os.close(master)
        os.close(slave)


def test_rx_failure_before_owner_registration_is_replayed_and_cannot_relight():
    wire = GatedWire()
    wire.release.set()
    wire.read_failure.set()
    serial = SerialLightingAdapter("synthetic", transport=wire)
    serial.open()
    wait_until(lambda: serial.diagnostics()["read_failures"] > 0)
    wire.read_failure.clear()
    faults = []
    output = PhysicalLightingOutput(serial, auto_dark_recovery=True,
                                    on_fault=lambda *args: faults.append(args))
    try:
        with pytest.raises(LightingFaultLatchedError):
            output.set_element(0, 1)
        with pytest.raises(ConnectionError):
            output.flush()
        assert len(faults) == 1
        assert not any(wire.state.values())
        assert output.diagnostics()["fault_latched"]
    finally:
        output.close()
        serial.close()


def test_rx_fault_fences_owner_while_a_tx_syscall_is_blocked():
    wire = GatedWire()
    serial = SerialLightingAdapter("synthetic", transport=wire)
    serial.open()
    faults = []
    output = PhysicalLightingOutput(serial, auto_dark_recovery=True,
                                    on_fault=lambda *args: faults.append(args))
    try:
        output.set_element(0, 1)
        assert wire.entered.wait(1)
        wire.read_failure.set()
        wait_until(lambda: bool(faults))
        assert not wire.release.is_set()
        assert not serial.is_ready()
        assert not output.diagnostics()["output_ready"]
        wire.read_failure.clear()
        wire.release.set()
        with pytest.raises(ConnectionError):
            output.flush()
        assert not any(wire.state.values())
    finally:
        wire.read_failure.clear()
        wire.release.set()
        output.close()
        serial.close()
