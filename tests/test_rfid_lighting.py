"""Deterministic effect traces and mocked serial ownership boundaries."""

import asyncio
import threading

import pytest

from app.foundation.lighting import (
    EXHIBITION_WAVE_GROUPS,
    LIGHTING_ELEMENT_IDS,
    ExhibitionLightingEngine,
    FakeLightingOutput,
    LightingCommand,
    PhysicalLightingOutput,
    SerialLightingAdapter,
)


def _engine(output=None, **kwargs):
    return ExhibitionLightingEngine(
        output or FakeLightingOutput(), monotonic_now=lambda: 0.0,
        background_scheduler=False, **kwargs,
    )


def _all(state, transition_ms=0):
    return tuple(LightingCommand(element_id, state, transition_ms) for element_id in LIGHTING_ELEMENT_IDS)


def _finish_ready(engine):
    for stage in range(1, 8):
        engine.advance(stage * 0.12)


def test_reset_is_dark_idle_and_does_not_restart_breathing():
    engine = _engine()
    engine.start_breathing(now=0.0)
    snapshot = engine.reset_to_standby(now=0.25)
    assert snapshot["effect"] == "idle"
    assert snapshot["state"] == 0
    assert not snapshot["task_active"]
    assert engine.output.batches[-1] == _all(0)
    count = len(engine.output.commands)
    assert engine.advance(100.0) is None
    assert len(engine.output.commands) == count
    assert set(engine.output.snapshot().values()) == {0}


def test_ready_exact_wave_to_breathing_trace_has_no_inserted_off():
    engine = _engine()
    engine.start_rfid_ready(now=0.0)
    generation = engine.generation
    for stage in range(1, 7):
        engine.advance(stage * 0.12)
    assert engine.advance(0.839) is None
    assert engine.effect == "wave"
    engine.advance(0.84)

    batches = engine.output.batches
    assert batches[0] == _all(0)
    assert len(batches) == 10  # clear, seven travel stages, hold, breathe
    for stage, group in enumerate(EXHIBITION_WAVE_GROUPS):
        assert batches[stage + 1] == tuple(
            LightingCommand(element_id, int(element_id in group), 120)
            for element_id in LIGHTING_ELEMENT_IDS
        )
    assert batches[-2:] == [_all(1, 120), _all(1, 1000)]
    # Inspect the full ordered command stream at this boundary, not just a
    # desired-state snapshot. Travel-stage OFFs above remain intentional.
    assert all(command.state == 1 for batch in batches[-2:] for command in batch)
    assert engine.generation == generation
    assert engine.effect == "breathing"
    assert engine.snapshot()["phase"] == "rfid_ready"
    assert engine.advance(1.839) is None
    engine.advance(1.84)
    assert engine.output.batches[-1] == _all(0, 1000)


def test_delayed_ready_poll_does_not_skip_stages_or_final_dwell():
    frames = []
    engine = _engine(frame_hook=frames.append)
    engine.start_rfid_ready(now=0.0)
    engine.advance(10.0)
    assert engine.wave_stage == 1
    assert engine.advance(10.01, force=True) is None
    for stage in range(2, 7):
        engine.advance(10.0 + (stage - 1) * 0.12)
    assert engine.effect == "wave"
    assert engine.advance(10.719) is None
    engine.advance(10.72)
    assert [frame["wave_stage"] for frame in frames if frame["effect"] == "wave"] == list(range(1, 8))
    assert engine.effect == "breathing"


@pytest.mark.parametrize("at", [0.3, 0.84])
def test_lift_preempts_ready_without_clear_or_late_breathing(at):
    engine = _engine()
    engine.start_rfid_ready(now=0.0)
    if at == 0.84:
        _finish_ready(engine)
    before = len(engine.output.batches)
    old_generation = engine.generation
    engine.start_wave(now=at, preserve_current=True)
    assert engine.generation == old_generation + 1
    assert len(engine.output.batches) == before + 1
    assert any(command.state == 1 for command in engine.output.batches[-1])
    engine.advance(at + 10.0)
    assert engine.effect == "wave_hold"
    assert engine.last_frame["effect"] == "wave_complete"
    assert not any(batch == _all(1, 1000) for batch in engine.output.batches[before:])


def test_breathing_preserve_current_emits_no_startup_clear():
    engine = _engine()
    engine.start_wave(now=0.0)
    before = len(engine.output.batches)
    engine.start_breathing(now=0.2, preserve_current=True)
    assert engine.output.batches[before:] == [_all(1, 1000)]


def test_cancel_during_completion_hook_fences_owned_breathing():
    def on_frame(frame):
        if frame["effect"] == "wave_complete":
            engine.cancel_effects(clear=True)

    engine = _engine(frame_hook=on_frame)
    engine.start_rfid_ready(now=0.0)
    _finish_ready(engine)
    assert engine.effect == "idle"
    assert engine.output.batches[-1] == _all(0)
    assert _all(1, 1000) not in engine.output.batches
    count = len(engine.output.batches)
    assert engine.advance(20.0) is None
    assert len(engine.output.batches) == count


class _Transport:
    def __init__(self):
        self.writes = []
        self.attempts = []

    def open(self):
        pass

    def close(self):
        pass

    def write(self, payload):
        self.attempts.append(payload)
        self.writes.append(payload)
        return len(payload)


class _BlockedTransport(_Transport):
    def __init__(self, *, fail_blocked=False):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fail_blocked = fail_blocked

    def write(self, payload):
        self.attempts.append(payload)
        if not self.entered.is_set():
            self.entered.set()
            if not self.release.wait(2.0):
                raise TimeoutError("mock write not released")
            if self.fail_blocked:
                raise TimeoutError("old owner failed")
        self.writes.append(payload)
        return len(payload)


def _physical(transport, **kwargs):
    adapter = SerialLightingAdapter("/mock-only", transport=transport)
    adapter.open()
    return adapter, PhysicalLightingOutput(adapter, **kwargs)


def test_bounded_queue_reset_drops_stale_lines_then_writes_terminal_dark():
    transport = _BlockedTransport()
    adapter, output = _physical(transport, max_pending_batches=2)
    try:
        output.set_elements((LightingCommand(0, 1), LightingCommand(1, 1)))
        assert transport.entered.wait(1.0)
        output.set_element(2, 1)
        for _ in range(100):
            with pytest.raises(BufferError):
                output.set_element(3, 1)
        assert output.diagnostics()["pending_batches"] == 1
        assert output.diagnostics()["overflow_batches"] == 100
        assert output.snapshot()["3"] == 0  # rejected writes are not desired
        output.clear_all()
        with pytest.raises(TimeoutError):
            output.flush(timeout_s=0.001)
        transport.release.set()
        output.flush()
        assert transport.writes == [b"0,1,0\n"] + [command.to_wire() for command in _all(0)]
        diagnostics = output.diagnostics()
        assert diagnostics["dropped_batches"] == 2
        assert diagnostics["written_batches"] == 1
        assert diagnostics["state_kind"] == "desired_targets"
        assert diagnostics["firmware_acknowledged"] is False
    finally:
        transport.release.set()
        output.close()
        adapter.close()


def test_invalidation_blocks_failed_old_generation_retry():
    transport = _BlockedTransport(fail_blocked=True)
    adapter, output = _physical(transport)
    try:
        output.set_elements((LightingCommand(0, 1), LightingCommand(1, 1)))
        assert transport.entered.wait(1.0)
        output.clear_all()
        transport.release.set()
        output.flush()
        assert transport.attempts.count(b"0,1,0\n") == 1
        assert b"1,1,0\n" not in transport.attempts
        assert transport.writes == [command.to_wire() for command in _all(0)]
    finally:
        transport.release.set()
        output.close()
        adapter.close()


def test_retry_checks_generation_before_each_retried_line():
    current = [True]

    class RetryTransport(_Transport):
        def write(self, payload):
            if not self.attempts:
                self.attempts.append(payload)
                raise TimeoutError("first attempt")
            result = super().write(payload)
            current[0] = False
            return result

    transport = RetryTransport()
    adapter = SerialLightingAdapter("/mock-only", transport=transport)
    adapter.open()
    try:
        assert adapter.send_batch(
            (LightingCommand(0, 1), LightingCommand(1, 1)),
            is_current=lambda: current[0],
        ) is False
        assert transport.attempts == [b"0,1,0\n", b"0,1,0\n"]
        assert transport.writes == [b"0,1,0\n"]
    finally:
        adapter.close()


def test_partial_write_finishes_same_line_without_duplicate_csv_prefix():
    class PartialTransport(_Transport):
        def __init__(self):
            super().__init__()
            self.stream = bytearray()

        def write(self, payload):
            count = len(payload)
            if not self.attempts:
                count -= 1
            self.attempts.append(payload)
            self.stream.extend(payload[:count])
            return count

    transport = PartialTransport()
    adapter = SerialLightingAdapter("/mock-only", transport=transport)
    adapter.open()
    try:
        assert adapter.send_batch((LightingCommand(17, 1, 1000),)) is True
        assert transport.attempts == [b"17,1,1000\n", b"\n"]
        assert transport.stream == b"17,1,1000\n"
        assert adapter.diagnostics()["failed_commands"] == 0
    finally:
        adapter.close()


def test_partial_line_completes_before_observing_generation_invalidation():
    current = [True]

    class PartialTransport(_Transport):
        def __init__(self):
            super().__init__()
            self.stream = bytearray()

        def write(self, payload):
            count = 2 if not self.attempts else len(payload)
            self.attempts.append(payload)
            self.stream.extend(payload[:count])
            current[0] = False
            return count

    transport = PartialTransport()
    adapter = SerialLightingAdapter("/mock-only", transport=transport)
    adapter.open()
    try:
        assert adapter.send_batch(
            (LightingCommand(0, 1), LightingCommand(1, 1)),
            is_current=lambda: current[0],
        ) is False
        assert transport.stream == b"0,1,0\n"
    finally:
        adapter.close()


def test_failed_partial_line_is_exposed_without_retrying_corrupt_prefix():
    class PartialFailingTransport(_Transport):
        def write(self, payload):
            self.attempts.append(payload)
            if len(self.attempts) == 1:
                return 2
            raise TimeoutError("mock interrupted line")

    transport = PartialFailingTransport()
    adapter = SerialLightingAdapter("/mock-only", transport=transport)
    adapter.open()
    try:
        with pytest.raises(ConnectionError, match="incomplete line"):
            adapter.send_batch((LightingCommand(0, 1),))
        assert transport.attempts == [b"0,1,0\n", b"1,0\n"]
        assert adapter.diagnostics()["failed_commands"] == 1
    finally:
        adapter.close()


def test_posix_partial_write_failure_is_not_retried_as_a_complete_line(monkeypatch):
    from app.foundation.hardware import PosixSerialTransport

    transport = PosixSerialTransport("/mock-only")
    attempts = []
    stream = bytearray()

    def write(_fd, payload):
        payload = bytes(payload)
        attempts.append(payload)
        if len(attempts) == 1:
            stream.extend(payload[:2])
            return 2
        if len(attempts) == 2:
            raise OSError("mock interruption after partial os.write")
        stream.extend(payload)
        return len(payload)

    # Exercise the production all-or-raise writer without opening a device.
    monkeypatch.setattr("app.foundation.hardware.os.write", write)
    monkeypatch.setattr(transport, "open", lambda: setattr(transport, "fd", 12345))
    monkeypatch.setattr(transport, "close", lambda: setattr(transport, "fd", None))
    transport.open()
    adapter = SerialLightingAdapter("/mock-only", transport=transport)
    adapter._ready = True
    try:
        with pytest.raises(ConnectionError, match="incomplete line"):
            adapter.send_batch((LightingCommand(0, 1),))
        assert attempts == [b"0,1,0\n", b"1,0\n"]
        assert stream == b"0,"
        assert not adapter.is_ready()
    finally:
        adapter.close()


def test_persistent_write_failure_is_exposed_by_flush_and_close():
    class FailingTransport(_Transport):
        def write(self, payload):
            self.attempts.append(payload)
            raise OSError("mock UART unavailable")

    transport = FailingTransport()
    adapter, output = _physical(transport)
    try:
        output.set_element(0, 1)
        with pytest.raises(ConnectionError, match="during drain"):
            output.flush()
        assert output.diagnostics()["failed_batches"] == 1
        with pytest.raises(ConnectionError, match="new generation"):
            output.set_element(1, 1)
        with pytest.raises(ConnectionError, match="during drain"):
            output.close()
        assert output.diagnostics()["failed_batches"] == 2
        assert output.diagnostics()["async_error"]["exception_type"] == "ConnectionError"
        assert not transport.writes
    finally:
        adapter.close()


def test_close_timeout_is_reported_and_terminal_dark_survives_late_invalidation():
    transport = _BlockedTransport()
    adapter, output = _physical(transport)
    try:
        output.set_elements((LightingCommand(0, 1), LightingCommand(1, 1)))
        assert transport.entered.wait(1.0)
        with pytest.raises(TimeoutError):
            output.close(timeout_s=0.001)
        output.invalidate_pending()
        with pytest.raises(ConnectionError):
            output.set_element(2, 1)
        transport.release.set()
        output.close()  # rechecks the original terminal frame, does not claim an ACK
        assert transport.writes == [b"0,1,0\n"] + [command.to_wire() for command in _all(0)]
    finally:
        transport.release.set()
        output.close()
        adapter.close()


def test_reset_dark_drains_zero_targets_and_queue_is_bounded_per_batch():
    adapter, output = _physical(_Transport())
    try:
        with pytest.raises(ValueError, match="at most eighteen"):
            output.set_elements((LightingCommand(0, 1),) * 19)
        output.set_element(17, 1)
        output.reset_dark()
        assert output.snapshot() == {str(element_id): 0 for element_id in LIGHTING_ELEMENT_IDS}
        assert output.adapter.transport.writes[-18:] == [command.to_wire() for command in _all(0)]
    finally:
        output.close()
        adapter.close()


def test_background_owner_wakes_at_120ms_boundaries_and_continues_same_generation(monkeypatch):
    async def scenario():
        clock = [0.0]
        frames = []
        engine = ExhibitionLightingEngine(
            FakeLightingOutput(), monotonic_now=lambda: clock[0], frame_hook=frames.append,
        )

        async def wakeup(delay):
            clock[0] += delay
            if engine.effect == "breathing":
                engine.cancel_effects(clear=False)

        monkeypatch.setattr(asyncio, "sleep", wakeup)
        engine.start_rfid_ready(now=0.0)
        generation = engine.generation
        task = engine._task
        await task
        assert [frame["at_monotonic"] for frame in frames if frame["effect"] == "wave"] == pytest.approx(
            [stage * 0.12 for stage in range(7)]
        )
        assert [frame["effect"] for frame in frames[-2:]] == ["wave_complete", "breathing"]
        assert frames[-1]["at_monotonic"] == pytest.approx(0.84)
        assert engine.generation == generation + 1  # only our final cancellation

    asyncio.run(scenario())
