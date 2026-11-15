"""Real CSV host path against a newline-framed device parser, without hardware."""
import re
import pytest
from app.foundation.lighting import LIGHTING_ELEMENT_IDS, LightingCommand, PhysicalLightingOutput, SerialLightingAdapter


class StatefulLineTransport:
    """Host close/reopen does not reset an independently powered UART parser."""
    def __init__(self, parser='strict', cut=3):
        self.parser, self.cut = parser, cut
        self.pending = b''
        self.state = {i: 0 for i in LIGHTING_ELEMENT_IDS}
        self.lines = []
        self.fail_stage = 0
    def open(self): pass
    def close(self): pass
    def write(self, data):
        if self.fail_stage == 1:
            self.pending += data[:self.cut]
            self.fail_stage = 2
            return self.cut
        if self.fail_stage == 2:
            self.fail_stage = 0
            raise OSError('injected write interruption')
        self.pending += data
        while b'\n' in self.pending:
            line, self.pending = self.pending.split(b'\n', 1)
            self.lines.append(line)
            try:
                parts = line.decode('ascii').split(',')
                if self.parser == 'historical_firmware':
                    # sketch_oct12a uses first two commas and Arduino toInt,
                    # which accepts a numeric prefix in the duration field.
                    if len(parts) < 3: continue
                    def to_int(value):
                        match = re.match(r'\s*([+-]?\d+)', value)
                        return int(match[1]) if match else 0
                    channel, state, duration = map(to_int, parts[:3])
                else:
                    if len(parts) != 3: continue
                    channel, state, duration = map(int, parts)
                if channel in self.state and state in (0, 1) and duration >= 0:
                    self.state[channel] = state
            except (UnicodeError, ValueError): pass
        return len(data)


@pytest.mark.parametrize('parser', ['strict', 'historical_firmware'])
@pytest.mark.parametrize('cut', range(1, len(b'13,1,0\n')))
def test_terminal_dark_recovers_unfinished_previous_csv_line(parser, cut):
    wire = StatefulLineTransport(parser, cut)
    adapter = SerialLightingAdapter('/dev/mock', transport=wire)
    adapter.open()
    output = PhysicalLightingOutput(adapter)
    try:
        output.set_elements(tuple(LightingCommand(i, 1) for i in LIGHTING_ELEMENT_IDS))
        output.flush()
        assert all(wire.state.values())
        wire.fail_stage = 1
        output.set_element(13, 1)
        with pytest.raises(ConnectionError): output.flush()
        assert wire.pending == b'13,1,0\n'[:cut]
        output.clear_all()
        output.flush()
        # Assert device parser state, not the optimistic desired target snapshot.
        assert not any(wire.state.values()), (wire.state, wire.lines[-18:])
        assert not wire.pending
    finally:
        output.close()
        adapter.close()


def test_failed_boundary_recovery_cannot_send_new_targets():
    wire = StatefulLineTransport()
    adapter = SerialLightingAdapter('/dev/mock', transport=wire)
    adapter.open()
    output = PhysicalLightingOutput(adapter)
    try:
        wire.fail_stage = 1
        output.set_element(13, 1)
        with pytest.raises(ConnectionError): output.flush()
        assert adapter.diagnostics()['framing_recovery_pending']
        wire.fail_stage = 2
        output.clear_all()
        with pytest.raises(ConnectionError): output.flush()
        assert wire.pending == b'13,' and not wire.lines
        assert adapter.diagnostics()['framing_recovery_pending']
        output.clear_all()
        output.flush()
        assert not any(wire.state.values())
        assert not adapter.diagnostics()['framing_recovery_pending']
    finally:
        output.close(); adapter.close()


def test_empty_serial_read_yields_instead_of_busy_spinning():
    class Wire(StatefulLineTransport):
        reads = 0
        def read_available(self):
            self.reads += 1
            return b''
    wire = Wire()
    adapter = SerialLightingAdapter('/dev/mock', transport=wire)
    class StopAfterWait:
        stopped = False
        waits = []
        def is_set(self): return self.stopped
        def wait(self, delay): self.waits.append(delay); self.stopped = True
    stop = StopAfterWait()
    adapter._drain_stop = stop
    # No actual thread/time measurement: require an explicit bounded wait
    # on the empty-read branch, preventing an unbounded spin on production tty.
    original = wire.read_available
    def bounded_read():
        if wire.reads > 2: raise AssertionError('empty read spun without yielding')
        return original()
    wire.read_available = bounded_read
    adapter._drain_loop()
    assert wire.reads == 1 and stop.waits == [0.02]

@pytest.mark.asyncio
async def test_operational_projection_shows_queue_failure_not_firmware_receipt():
    from app.foundation.stand import HardwareIntegrationStand
    from app.foundation.config import Settings
    stand = HardwareIntegrationStand(Settings(profile='mock'), background_scheduler=False)
    wire = StatefulLineTransport()
    adapter = SerialLightingAdapter('/dev/mock', transport=wire)
    adapter.open()
    output = PhysicalLightingOutput(adapter)
    stand.lighting_serial, stand.lighting_output = adapter, output
    try:
        wire.fail_stage = 1
        output.set_element(13, 1)
        with pytest.raises(ConnectionError): output.flush()
        diag = stand.diagnostics_snapshot()['lighting_hardware']
        assert diag['generation_failed'] and not diag['output_ready']
        assert diag['state_kind'] == 'desired_targets'
        assert diag['firmware_acknowledged'] is False
        assert diag['failed_batches'] == 1 and diag['framing_recovery_pending']
        assert 'async_error' not in diag  # arbitrary exception text is not projected
        output.clear_all(); output.flush()
        diag = stand.panel_snapshot()['lighting_hardware']
        assert not diag['generation_failed'] and diag['output_ready']
        assert diag['written_batches'] == 1 and diag['framing_recoveries'] == 1
        assert diag['firmware_acknowledged'] is False
    finally:
        await stand.aclose()
