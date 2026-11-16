"""Serial traffic suppression is based on whole lines actually written by this owner."""
import errno
import threading
import time

import pytest

from app.foundation.lighting import (
    ExhibitionLightingEngine, FakeLightingOutput, LightingCommand,
    PhysicalLightingOutput, SerialLightingAdapter,
)


class Wire:
    def __init__(self):
        self.writes = []
        self.opens = 0
        self.failures = 0
    def open(self):
        self.opens += 1
    def close(self):
        pass
    def write(self, raw):
        if self.failures:
            self.failures -= 1
            raise OSError(errno.EIO, "synthetic serial failure")
        self.writes.append(bytes(raw))
        return len(raw)


def opened(wire=None):
    wire = wire or Wire()
    serial = SerialLightingAdapter("synthetic", transport=wire)
    serial.open()
    return serial, wire


def test_duplicate_positive_transition_is_suppressed_per_written_channel():
    serial, wire = opened()
    try:
        a, b = LightingCommand(0,1,140), LightingCommand(1,0,140)
        assert serial.send_batch((a,b,a,b))
        assert serial.send_batch((a,b))
        assert wire.writes == [a.to_wire(), b.to_wire()]
        assert serial.diagnostics()["sent_commands"] == 2
    finally:
        serial.close()


def test_changed_target_or_transition_is_always_transmitted():
    serial, wire = opened()
    commands = (LightingCommand(0,1,140), LightingCommand(0,0,140),
                LightingCommand(0,0,280), LightingCommand(0,1,280),
                LightingCommand(0,1,0), LightingCommand(0,1,280))
    try:
        serial.send_batch(commands)
        assert wire.writes == [c.to_wire() for c in commands]
    finally:
        serial.close()


def test_zero_transition_always_transmits_complete_root_and_dark_frames():
    serial, wire = opened()
    output = PhysicalLightingOutput(serial)
    try:
        root = tuple(LightingCommand(i,int(i in {1,3,6,9,10,13,14,5,16}),0) for i in range(18))
        output.set_elements(root)
        output.flush()
        output.set_elements(root)
        output.flush()
        output.clear_all()
        output.flush()
        output.clear_all()
        output.flush()
        assert wire.writes == [c.to_wire() for c in root]*2 + [f"{i},0,0\n".encode() for i in range(18)]*2
    finally:
        output.close()
        serial.close()


@pytest.mark.parametrize("action", ["close-open", "reconnect"])
def test_new_transport_lifecycle_cannot_reuse_old_written_targets(action):
    serial, wire = opened()
    command = LightingCommand(0,1,140)
    try:
        serial.send(command)
        serial.send(command)
        assert wire.writes == [command.to_wire()]
        if action == "close-open":
            serial.close()
            serial.open()
        else:
            serial.reconnect()
        serial.send(command)
        assert wire.writes == [command.to_wire()]*2
    finally:
        serial.close()


def test_failed_batch_retry_replays_cached_prefix_after_reopen():
    serial, wire = opened()
    a, b = LightingCommand(0,1,140), LightingCommand(1,1,140)
    try:
        serial.send(a)
        wire.failures = 1
        serial.send_batch((a,b))
        assert wire.opens == 2
        assert wire.writes == [a.to_wire(), a.to_wire(), b.to_wire()]
        assert serial.diagnostics()["failed_commands"] == 1
    finally:
        serial.close()


def test_uncertain_partial_write_forgets_targets_and_restores_csv_boundary():
    class Partial(Wire):
        partial = False
        step = 0
        def write(self, raw):
            if self.partial:
                self.step += 1
                if self.step == 1:
                    self.writes.append(bytes(raw[:2]))
                    return 2
                raise OSError(errno.EIO,"synthetic partial serial failure")
            return super().write(raw)
    serial, wire = opened(Partial())
    a, b = LightingCommand(0,1,140), LightingCommand(1,1,140)
    try:
        serial.send(a)
        wire.partial = True
        with pytest.raises(ConnectionError):
            serial.send(b)
        assert serial._written_targets == {}
        assert serial.diagnostics()["framing_recovery_pending"]
        wire.partial = False
        serial.reconnect()
        serial.send(a)
        assert wire.writes[-2:] == [b"\n", a.to_wire()]
    finally:
        serial.close()


def test_generation_cancel_finishes_only_current_csv_line_and_caches_written_prefix():
    current = [True]
    class PartialCancel(Wire):
        first = True
        def write(self, raw):
            if self.first:
                self.first = False
                self.writes.append(bytes(raw[:2]))
                current[0] = False
                return 2
            return super().write(raw)
    serial, wire = opened(PartialCancel())
    a, b = LightingCommand(0,1,140), LightingCommand(1,1,140)
    try:
        assert not serial.send_batch((a,b), is_current=lambda:current[0])
        assert b"".join(wire.writes) == a.to_wire()
        current[0] = True
        serial.send_batch((a,b), is_current=lambda:current[0])
        assert b"".join(wire.writes) == a.to_wire()+b.to_wire()
    finally:
        serial.close()


def test_rx_fault_during_blocked_tx_invalidates_cache_without_waiting_for_writer():
    class Blocked(Wire):
        def __init__(self):
            super().__init__()
            self.entered, self.release, self.rx_failed = (threading.Event() for _ in range(3))
            self.block = False
        def write(self, raw):
            if self.block:
                self.entered.set()
                assert self.release.wait(2)
            return super().write(raw)
        def read_available(self):
            if self.rx_failed.is_set():
                raise OSError(errno.EIO,"synthetic RX fault")
            return b""
    serial, wire = opened(Blocked())
    reported = threading.Event()
    serial.set_read_fault_handler(lambda diagnostic:reported.set())
    worker = None
    try:
        serial.send(LightingCommand(0,1,140))
        wire.block = True
        worker = threading.Thread(target=lambda:serial.send(LightingCommand(1,1,140)))
        worker.start()
        assert wire.entered.wait(1)
        wire.rx_failed.set()
        assert reported.wait(1), "RX fault must not wait behind the TX lock"
        assert not wire.release.is_set()
        assert serial._written_targets == {}
        wire.release.set()
        worker.join(1)
        assert not worker.is_alive()
        assert serial._written_targets == {}, "late successful TX cannot restore an invalidated epoch"
    finally:
        wire.release.set()
        if worker is not None:
            worker.join(1)
        serial.close()


@pytest.mark.parametrize("origin,baseline", [(1,180),(4,126),(6,162)])
def test_real_ripple_frames_reduce_only_redundant_wire_lines(origin,baseline):
    clock = [0.]
    frames = FakeLightingOutput()
    engine = ExhibitionLightingEngine(frames,monotonic_now=lambda:clock[0],background_scheduler=False)
    engine.start_game_ripple(origin)
    for step in range(1,12):
        clock[0] = step*.14+.001
        engine.advance()
    assert len(frames.commands) == baseline
    serial, wire = opened()
    try:
        for command in frames.commands:
            serial.send(command)
        assert len(wire.writes) == 49
        actual = {i:0 for i in range(18)}
        for raw in wire.writes:
            element,state,transition = map(int,raw.strip().split(b","))
            assert state in {0,1} and transition in {0,140}
            actual[element] = state
        assert not any(actual.values())
    finally:
        serial.close()
