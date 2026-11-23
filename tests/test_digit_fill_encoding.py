"""Synthetic serial bytes only; no firmware, RGB or physical colour claims."""
import threading

import pytest

from app.foundation.config import Settings, SettingsError
from app.foundation.lighting import (
    BUTTON_DIGIT_ELEMENTS, BUTTON_FILL_ELEMENTS, LIGHTING_ELEMENT_IDS,
    LightingCommand, PhysicalLightingOutput, SerialLightingAdapter,
)


class Wire:
    def __init__(self):
        self.lines = []
        self.failures = 0
    def open(self): pass
    def close(self): pass
    def write(self, data):
        if self.failures:
            self.failures -= 1
            raise OSError('synthetic reconnect')
        self.lines.append(bytes(data))
        return len(data)


def opened(mode='fill_color'):
    wire = Wire()
    adapter = SerialLightingAdapter('synthetic', transport=wire, digit_mode=mode)
    adapter.open()
    return adapter, wire


def targets(wire):
    return {int(channel): int(state) for channel, state, _ in
            (line.decode().strip().split(',') for line in wire.lines)}


@pytest.mark.parametrize('button', range(1, 8))
@pytest.mark.parametrize('digit,fill,expected', [(0,0,0),(1,0,1),(1,1,1),(0,1,2)])
@pytest.mark.parametrize('reverse', [False, True])
def test_final_pair_is_projected_before_writing_regardless_of_batch_order(button, digit, fill, expected, reverse):
    adapter, wire = opened()
    d, f = BUTTON_DIGIT_ELEMENTS[button][1], BUTTON_FILL_ELEMENTS[button]
    commands = (LightingCommand(d,digit,100), LightingCommand(f,fill,200))
    try:
        adapter.send_batch(commands[::-1] if reverse else commands)
        assert targets(wire) == {f:fill, d:expected}
        assert wire.lines[-1] == f'{d},{expected},100\n'.encode()
    finally: adapter.close()


def test_fill_only_changes_recompute_digit_and_reset_keeps_logical_state_binary():
    adapter, wire = opened()
    output = PhysicalLightingOutput(adapter)
    try:
        output.set_element(14,1); output.flush()
        assert targets(wire) == {14:1,15:2}
        assert output.snapshot()['14'] == 1 and output.snapshot()['15'] == 0
        output.set_element(15,1); output.flush()
        assert targets(wire)[15] == 1
        output.set_element(14,0); output.flush()
        assert targets(wire)[15] == 1
        output.set_element(15,0); output.flush()
        assert targets(wire)[15] == 0
        output.clear_all(); output.flush()
        assert targets(wire) == dict.fromkeys(LIGHTING_ELEMENT_IDS,0)
        assert set(output.snapshot().values()) == {0}
    finally: output.close(); adapter.close()


def test_animation_frames_and_reconnect_keep_pair_state_and_channel_contract():
    adapter, wire = opened()
    try:
        for active in range(1,8):
            frame = tuple(LightingCommand(i,int(i==BUTTON_FILL_ELEMENTS[active]),120) for i in LIGHTING_ELEMENT_IDS)
            adapter.send_batch(frame)
            result = targets(wire)
            assert set(result) == set(LIGHTING_ELEMENT_IDS)
            for button in range(1,8):
                assert result[BUTTON_DIGIT_ELEMENTS[button][1]] == (2 if button==active else 0)
            assert all(result[i] in (0,1) for i in (4,5,16,17))
        adapter.reconnect()
        wire.lines.clear()
        adapter.send(LightingCommand(BUTTON_FILL_ELEMENTS[7],0))
        assert targets(wire) == {1:0,0:0}
        wire.failures=1
        adapter.send(LightingCommand(14,1))
        assert targets(wire)[15] == 2
    finally: adapter.close()


def test_superseded_partial_batch_cannot_commit_future_pair_state():
    adapter, wire = opened()
    checks = 0
    def current():
        nonlocal checks
        checks += 1
        return checks < 3
    try:
        assert not adapter.send_batch((LightingCommand(14,1),LightingCommand(15,1)),is_current=current)
        adapter.send(LightingCommand(15,0))
        assert targets(wire) == {14:0,15:0}
    finally: adapter.close()


def test_legacy_default_and_binary_logical_validation_remain_unchanged():
    wire=Wire(); adapter=SerialLightingAdapter('synthetic',transport=wire);adapter.open()
    try:
        adapter.send(LightingCommand(14,1))
        assert wire.lines == [b'14,1,0\n']
        assert adapter.diagnostics()['digit_mode']=='legacy'
        with pytest.raises(ValueError): LightingCommand(15,2)
        with pytest.raises(ValueError): LightingCommand(18,1)
        with pytest.raises(ValueError): SerialLightingAdapter('synthetic',digit_mode='automatic')
    finally: adapter.close()


def test_generation_reset_supersedes_inflight_pair_and_finishes_all_eighteen_dark():
    entered, release = threading.Event(), threading.Event()
    class BlockingWire(Wire):
        def write(self, data):
            if not self.lines:
                entered.set()
                if not release.wait(1):raise TimeoutError('synthetic bounded write')
            return super().write(data)
    wire=BlockingWire();adapter=SerialLightingAdapter('synthetic',transport=wire,digit_mode='fill_color')
    adapter.open();output=PhysicalLightingOutput(adapter)
    try:
        output.set_element(14,1)
        assert entered.wait(1)
        output.clear_all()
        release.set();output.flush()
        assert targets(wire)==dict.fromkeys(LIGHTING_ELEMENT_IDS,0)
        assert set(output.snapshot().values())=={0}
        assert output.diagnostics()['dropped_batches']>=1
    finally:release.set();output.close();adapter.close()


def test_rapid_fill_updates_and_failed_retry_do_not_change_logical_digit():
    adapter,wire=opened()
    try:
        for index in range(30):
            fill=index%2
            adapter.send(LightingCommand(14,fill,60))
            assert targets(wire)=={14:fill,15:2 if fill else 0}
        wire.failures=2
        with pytest.raises(ConnectionError):adapter.send(LightingCommand(15,1))
        adapter.reconnect()
        adapter.send(LightingCommand(14,0))
        assert targets(wire)[15]==0
    finally:adapter.close()


def test_mode_is_explicit_validated_config_and_journal_defaults_off(monkeypatch):
    monkeypatch.delenv('LIGHTING_DIGIT_MODE',raising=False)
    monkeypatch.delenv('DIALOGUE_JOURNAL_ENABLED',raising=False)
    monkeypatch.delenv('DIALOGUE_JOURNAL_DIR',raising=False)
    settings=Settings.from_env()
    assert settings.lighting_digit_mode=='legacy' and not settings.dialogue_journal_enabled
    assert settings.dialogue_journal_dir=='runtime/private/session-journal'
    monkeypatch.setenv('LIGHTING_DIGIT_MODE','fill_color')
    monkeypatch.setenv('DIALOGUE_JOURNAL_ENABLED','true')
    monkeypatch.setenv('DIALOGUE_JOURNAL_DIR','runtime/private/synthetic-journal')
    settings=Settings.from_env()
    assert settings.lighting_digit_mode=='fill_color' and settings.dialogue_journal_enabled
    assert 'dialogue_journal_dir' not in settings.redacted_snapshot()
    monkeypatch.setenv('LIGHTING_DIGIT_MODE','automatic')
    with pytest.raises(SettingsError):Settings.from_env()
    monkeypatch.setenv('LIGHTING_DIGIT_MODE','legacy')
    monkeypatch.setenv('DIALOGUE_JOURNAL_ENABLED','perhaps')
    with pytest.raises(SettingsError):Settings.from_env()
