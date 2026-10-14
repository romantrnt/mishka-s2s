import asyncio

import pytest

from app.foundation.lighting import (BUTTON_OUTPUT_ELEMENTS, EXHIBITION_BREATHING_PHASE_MS, EXHIBITION_DIGIT_WAVE_GROUPS, LIGHTING_ELEMENT_IDS, ExhibitionLightingEngine, FakeLightingOutput, LightingCommand, LightingController, LightingState, PhysicalLightingOutput, SerialLightingAdapter, show_digit)
from app.foundation.stand import UARTLEDController


def test_registry_has_exactly_sixteen_unique_elements():
    assert len(LIGHTING_ELEMENT_IDS) == 16
    assert len(set(LIGHTING_ELEMENT_IDS)) == 16
    assert set(LIGHTING_ELEMENT_IDS) == {10, 11, 20, 21, 30, 31, 40, 41, 50, 51, 60, 61, 62, 70, 71, 72}


def test_command_state_is_binary_and_wire_is_csv_lf():
    assert LightingCommand(21, 1, 500).to_wire() == b"21,1,500\n"
    with pytest.raises(ValueError):
        LightingCommand(21, 2)
    with pytest.raises(ValueError):
        LightingCommand(999, 1)
    with pytest.raises(ValueError):
        LightingCommand(21, 1, -1)
    assert set(LightingCommand(21, 1).as_dict()) == {"element_id", "state", "transition_ms"}
    with pytest.raises(ValueError):
        LightingCommand(True, 1)
    with pytest.raises(ValueError):
        LightingCommand(21, True)
    with pytest.raises(ValueError):
        LightingCommand(21, 1, True)


class _RecordingLightingTransport:
    def __init__(self):
        self.writes = []
        self.opened = False

    def open(self):
        self.opened = True

    def close(self):
        self.opened = False

    def write(self, payload):
        self.writes.append(payload)


class _FailOnceLightingTransport(_RecordingLightingTransport):
    def __init__(self):
        super().__init__()
        self.failures_left = 1

    def write(self, payload):
        if self.failures_left:
            self.failures_left -= 1
            raise TimeoutError("UART write timed out")
        super().write(payload)


def test_physical_lighting_serial_writes_only_canonical_csv_lines():
    transport = _RecordingLightingTransport()
    adapter = SerialLightingAdapter("/dev/test-lighting", transport=transport)
    adapter.open()
    try:
        adapter.send_batch((LightingCommand(21, 1, 500), LightingCommand(62, 0)))
        assert transport.writes == [b"21,1,500\n", b"62,0,0\n"]
        assert all(b"{" not in payload and b"crc" not in payload.lower() for payload in transport.writes)
    finally:
        adapter.close()


def test_physical_lighting_protocol_emits_exact_ordered_csv_lf_frames():
    transport = _RecordingLightingTransport()
    adapter = SerialLightingAdapter("/dev/test-lighting", transport=transport)
    adapter.open()
    try:
        adapter.send_batch((
            LightingCommand(10, 1, 120),
            LightingCommand(62, 0, 0),
            LightingCommand(72, 1, 120),
        ))
        assert b"".join(transport.writes) == b"10,1,120\n62,0,0\n72,1,120\n"
    finally:
        adapter.close()


def test_physical_lighting_reconnects_before_next_command_after_transport_loss():
    transport = _RecordingLightingTransport()
    adapter = SerialLightingAdapter("/dev/test-lighting", transport=transport)
    adapter.open()
    output = PhysicalLightingOutput(adapter)
    adapter._ready = False
    output.set_element(10, 1, 100)
    assert transport.writes == [b"10,1,100\n"]
    adapter.close()


def test_physical_lighting_retries_idempotent_batch_after_transient_transport_loss():
    transport = _FailOnceLightingTransport()
    adapter = SerialLightingAdapter("/dev/test-lighting", transport=transport)
    adapter.open()
    try:
        adapter.send_batch((LightingCommand(10, 1, 100),))
        assert transport.writes == [b"10,1,100\n"]
        assert adapter.is_ready()
        assert adapter.diagnostics()["last_error"] is None
        assert adapter.diagnostics()["failed_commands"] == 1
    finally:
        adapter.close()


def test_background_lighting_failure_is_recorded_without_unhandled_task_exception():
    class FailingOutput(FakeLightingOutput):
        def set_elements(self, _commands):
            raise ConnectionError("UART write timed out")

    async def scenario():
        output = FakeLightingOutput()
        clock = [0.0]
        engine = ExhibitionLightingEngine(output, monotonic_now=lambda: clock[0])
        engine.start_breathing(now=0.0)
        engine.output = FailingOutput()
        clock[0] = 1.0
        await asyncio.sleep(0.15)
        assert not engine.task_active
        assert engine.last_error["exception_type"] == "ConnectionError"
        assert "timed out" in engine.last_error["detail"]

    asyncio.run(scenario())


def test_canonical_element_api_is_independent_and_preserves_transition():
    output = FakeLightingOutput()
    controller = LightingController(output)
    controller.set_element(61, 1, 500)
    assert output.snapshot()["61"] == 1
    assert output.snapshot()["60"] == 0
    assert output.snapshot()["62"] == 0
    assert output.commands[-1] == LightingCommand(61, 1, 500)
    controller.set_element(62, 1)
    controller.set_element(72, 0, 300)
    assert output.snapshot()["62"] == 1
    assert output.snapshot()["60"] == 0
    assert output.snapshot()["61"] == 1
    assert output.snapshot()["70"] == 0
    assert output.snapshot()["71"] == 0
    assert output.snapshot()["72"] == 0
    assert output.commands[-1].transition_ms == 300


def test_show_digit_two_targets_20_and_21():
    output = FakeLightingOutput()
    assert show_digit(output, 2, 1, 500) == (LightingCommand(20, 1, 500), LightingCommand(21, 1, 500))
    assert output.snapshot()["20"] == 1
    assert output.snapshot()["21"] == 1


def test_clear_all_turns_off_all_sixteen_elements():
    output = FakeLightingOutput()
    output.set_elements(tuple(LightingCommand(element_id, 1) for element_id in LIGHTING_ELEMENT_IDS))
    output.clear_all(250)
    assert output.snapshot() == {str(element_id): 0 for element_id in LIGHTING_ELEMENT_IDS}


def test_symbols_are_mapped_to_buttons_six_and_seven():
    assert 62 in BUTTON_OUTPUT_ELEMENTS[6]
    assert 72 in BUTTON_OUTPUT_ELEMENTS[7]


def test_state_rejects_unknown_or_partial_mappings():
    with pytest.raises(ValueError):
        LightingState.from_mapping({10: 0, 999: 1})
    with pytest.raises(ValueError):
        LightingState.from_mapping({10: 0})

def test_standby_breathing_uses_one_second_binary_transition_serial_batches():
    transport = _RecordingLightingTransport()
    adapter = SerialLightingAdapter("/dev/test-lighting", transport=transport)
    adapter.open()
    try:
        output = PhysicalLightingOutput(adapter)
        engine = ExhibitionLightingEngine(output, monotonic_now=lambda: 0.0, background_scheduler=False)
        engine.start_breathing(now=0.0)
        on_wire = transport.writes[-16:]
        assert on_wire == [
            f"{element_id},1,{EXHIBITION_BREATHING_PHASE_MS}\n".encode("ascii")
            for element_id in LIGHTING_ELEMENT_IDS
        ]
        engine.advance(1.0)
        off_wire = transport.writes[-16:]
        assert off_wire == [
            f"{element_id},0,{EXHIBITION_BREATHING_PHASE_MS}\n".encode("ascii")
            for element_id in LIGHTING_ELEMENT_IDS
        ]
    finally:
        adapter.close()


def test_effect_frame_hook_receives_binary_breathing_and_wave_frames():
    output = FakeLightingOutput()
    frames = []
    engine = ExhibitionLightingEngine(
        output, monotonic_now=lambda: 0.0, background_scheduler=False,
        frame_hook=frames.append,
    )
    engine.start_breathing(now=0.0)
    engine.advance(now=1.0)
    assert [frame["effect"] for frame in frames] == ["breathing", "breathing"]
    assert [frame["state"] for frame in frames] == [1, 0]

    engine.start_wave(now=2.0)
    engine.advance(now=3.0, force=True)
    assert frames[-1]["effect"] == "wave_complete"


def test_uart_controller_forwards_digit_wave_groups_and_holds_fourteen_elements():
    controller = UARTLEDController(
        adapter=None,
        output=FakeLightingOutput(),
        monotonic_now=lambda: 0.0,
        background_scheduler=False,
    )
    controller.start_wave(now=0.0, groups=EXHIBITION_DIGIT_WAVE_GROUPS, hold_on_complete=True)
    controller.advance_effects(now=8.0, force=True)
    frame = controller.effect_snapshot()["last_frame"]
    assert controller.effect_snapshot()["effect"] == "wave_hold"
    assert frame["held"] is True
    assert frame["active_elements"] == [10, 11, 20, 21, 30, 31, 40, 41, 50, 51, 60, 61, 70, 71]
