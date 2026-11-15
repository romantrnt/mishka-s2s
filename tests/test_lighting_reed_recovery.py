"""A host reopen does not reset the firmware's unterminated CSV buffer."""
import re
import pytest
from app.foundation.lighting import LightingCommand, PhysicalLightingOutput, SerialLightingAdapter


class PersistentParserTransport:
    def __init__(self, cut=3, historical=True):
        self.cut = cut
        self.historical = historical
        self.buffer = bytearray()
        self.led = {i: 1 for i in range(18)}
        self.writes = []
        self.phase = 0
        self.fail_fence = False
    def open(self): pass
    def close(self): pass
    def feed(self, raw):
        for byte in raw:
            if byte in (10,13):
                fields = self.buffer.decode().split(',')
                self.buffer.clear()
                if len(fields) < 3 or (not self.historical and len(fields) != 3):
                    continue
                def to_int(value):
                    match = re.match(r'\s*([+-]?\d+)',value)
                    return int(match.group(1)) if match else 0
                try:
                    index, state, _ = map(to_int if self.historical else int, fields[:3])
                    if index in self.led and state in (0,1): self.led[index] = state
                except ValueError: pass
            else:
                self.buffer.append(byte)
    def write(self, raw):
        self.writes.append(bytes(raw))
        if self.phase == 0:
            self.phase = 1
            self.feed(raw[:self.cut])
            return self.cut
        if self.phase == 1:
            self.phase = 2
            raise OSError('simulated partial UART disconnect')
        if self.fail_fence and raw == b'\n':
            raise OSError('simulated failed boundary fence')
        self.feed(raw)
        return len(raw)


@pytest.mark.parametrize('historical',[False,True])
@pytest.mark.parametrize('cut',range(1,7))
def test_terminal_dark_recovers_every_interrupted_csv_prefix(cut,historical):
    wire = PersistentParserTransport(cut,historical)
    adapter = SerialLightingAdapter('/isolated-fake',transport=wire)
    adapter.open()
    output = PhysicalLightingOutput(adapter)
    try:
        output.set_element(13,1)
        with pytest.raises(ConnectionError): output.flush()
        assert wire.buffer
        output.clear_all()
        output.flush()
        assert not any(wire.led.values()), (cut,wire.led,wire.writes)
        assert not wire.buffer
        assert wire.writes[2] == b'\n'
    finally:
        output.close()
        adapter.close()


def test_failed_boundary_fence_never_allows_a_new_target_line():
    wire = PersistentParserTransport()
    adapter = SerialLightingAdapter('/isolated-fake',transport=wire)
    adapter.open()
    output = PhysicalLightingOutput(adapter)
    try:
        output.set_element(13,1)
        with pytest.raises(ConnectionError): output.flush()
        wire.fail_fence = True
        start = len(wire.writes)
        output.clear_all()
        with pytest.raises(ConnectionError): output.flush()
        assert wire.writes[start:] == [b'\n']
        assert adapter.diagnostics()['framing_recovery_pending']
        wire.fail_fence = False
        output.clear_all()
        output.flush()
        assert not any(wire.led.values())
        assert adapter.diagnostics()['framing_recoveries'] == 1
        assert not adapter.diagnostics()['framing_recovery_pending']
    finally:
        wire.fail_fence = False
        output.close()
        adapter.close()


def test_empty_nonblocking_uart_read_yields_until_stop():
    class Stop:
        stopped = False
        waits = []
        def is_set(self): return self.stopped
        def wait(self, timeout):
            self.waits.append(timeout)
            self.stopped = True
    class Empty:
        reads = 0
        def read_available(self):
            self.reads += 1
            # Avoid a runaway baseline while still exposing the missing wait.
            if self.reads > 1: adapter._drain_stop.stopped = True
            return b''
    transport = Empty()
    adapter = SerialLightingAdapter('/isolated-fake',transport=transport)
    adapter._drain_stop = Stop()
    adapter._drain_loop(adapter._drain_stop, transport)
    assert adapter._drain_stop.waits == [0.02]
    assert transport.reads == 1


@pytest.mark.asyncio
async def test_public_lighting_diagnostics_distinguish_desired_from_written_and_acknowledged():
    from app.foundation.stand import HardwareIntegrationStand
    from app.foundation.config import Settings
    stand = HardwareIntegrationStand(Settings(profile='mock'), background_scheduler=False)
    wire = PersistentParserTransport()
    adapter = SerialLightingAdapter('/isolated-fake',transport=wire)
    adapter.open()
    output = PhysicalLightingOutput(adapter)
    old_output = stand.lighting_output
    stand.lighting_output = output
    stand.lighting_serial = adapter
    stand.hardware_mode = 'physical'
    stand._lighting_status = {'state':'active'}
    try:
        output.set_element(13,1)
        with pytest.raises(ConnectionError): output.flush()
        # Reopening the serial port does not repair the failed output owner.
        adapter.reconnect()
        assert adapter.is_ready()
        snapshot = stand.diagnostics_snapshot()
        info = snapshot['lighting_hardware']
        assert info['state_kind'] == 'desired_targets'
        assert info['firmware_acknowledged'] is False
        assert info['generation_failed'] and not info['output_ready']
        assert info['framing_recovery_pending']
        assert snapshot['health']['lighting']['status'] == 'unavailable'
        assert 'async_error' not in info
        output.clear_all()
        output.flush()
        info = stand.snapshot()['hardware']['lighting']
        assert info['output_ready'] and not info['generation_failed']
        assert info['written_batches'] >= 1 and info['framing_recoveries'] == 1
        assert info['firmware_acknowledged'] is False
        assert set(output.snapshot().values()) == {0}
    finally:
        stand.hardware_mode = 'simulator'
        stand.lighting_output = old_output
        stand.lighting_serial = None
        output.close()
        adapter.close()
        await stand.aclose()
