"""Synthetic serial bytes only; no firmware, RGB or physical colour claims."""
import threading

import pytest

from app.foundation.config import Settings, SettingsError
from app.foundation.lighting import (
    BUTTON_DIGIT_ELEMENTS, BUTTON_FILL_ELEMENTS, LIGHTING_ELEMENT_IDS, PICTOGRAM_ELEMENTS,
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
        expected_targets = {f:fill, d:expected}
        if button in PICTOGRAM_ELEMENTS:
            expected_targets[PICTOGRAM_ELEMENTS[button]] = 2 if fill else 0
        assert targets(wire) == expected_targets
        assert f'{d},{expected},100\n'.encode() in wire.lines
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
            assert all(result[i] in (0,1) for i in (5,16))
            assert result[4] == (2 if active == 6 else 0)
            assert result[17] == (2 if active == 7 else 0)
        adapter.reconnect()
        wire.lines.clear()
        adapter.send(LightingCommand(BUTTON_FILL_ELEMENTS[7],0))
        assert targets(wire) == {1:0,0:0,17:0}
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


@pytest.mark.parametrize('button,fill,emblem,digit', [(6,3,4,2),(7,1,17,0)])
@pytest.mark.parametrize('on,filled,expected', [(0,0,0),(1,0,1),(1,1,1),(0,1,2)])
@pytest.mark.parametrize('reverse', [False,True])
def test_emblem_matches_digit_fill_colour_contract(button,fill,emblem,digit,on,filled,expected,reverse):
    assert PICTOGRAM_ELEMENTS[button] == emblem
    assert BUTTON_FILL_ELEMENTS[button] == fill
    adapter,wire=opened()
    commands=(LightingCommand(emblem,on,100),LightingCommand(fill,filled,200))
    try:
        adapter.send_batch(commands[::-1] if reverse else commands)
        assert targets(wire) == {fill:filled,emblem:expected,digit:2 if filled else 0}
        assert f'{emblem},{expected},100\n'.encode() in wire.lines
        assert set(adapter._logical_targets.values()) <= {0,1}
    finally:adapter.close()


@pytest.mark.parametrize('fill,emblem,digit', [(3,4,2),(1,17,0)])
@pytest.mark.parametrize('cutoff', [2,3,4,5])
def test_emblem_only_recovers_whole_cancelled_group(fill,emblem,digit,cutoff):
    adapter,wire=opened()
    checks=0
    def current():
        nonlocal checks
        checks+=1
        return checks<cutoff
    try:
        assert not adapter.send_batch((LightingCommand(fill,1),LightingCommand(digit,1),LightingCommand(emblem,1)),is_current=current)
        adapter.send(LightingCommand(emblem,0))
        assert targets(wire) == {fill:0,digit:0,emblem:0}
        assert set(adapter._logical_targets.values()) == {0}
    finally:adapter.close()


def test_legacy_keeps_all_eighteen_channel_bytes_including_emblems():
    adapter,wire=opened('legacy')
    commands=tuple(LightingCommand(i,int(i%2==0),60+i) for i in LIGHTING_ELEMENT_IDS)
    try:
        adapter.send_batch(commands)
        assert wire.lines == [f'{c.element_id},{c.state},{c.transition_ms}\n'.encode() for c in commands]
    finally:adapter.close()


def test_fill_colour_keeps_compensation_channels_binary_and_independent():
    adapter,wire=opened()
    try:
        adapter.send_batch((LightingCommand(5,1,80),LightingCommand(16,1,90),
                            LightingCommand(3,1,100),LightingCommand(1,1,120)))
        assert targets(wire) == {5:1,16:1,3:1,2:2,4:2,1:1,0:2,17:2}
        assert b'5,1,80\n' in wire.lines and b'16,1,90\n' in wire.lines
        wire.lines.clear()
        adapter.send_batch((LightingCommand(5,0,30),LightingCommand(16,0,40)))
        assert wire.lines == [b'5,0,30\n',b'16,0,40\n']
    finally:adapter.close()


@pytest.mark.parametrize('fill,emblem,digit', [(3,4,2),(1,17,0)])
def test_emblem_retry_and_reconnect_keep_the_completed_group_baseline(fill,emblem,digit):
    adapter,wire=opened()
    try:
        adapter.send(LightingCommand(fill,1,120))
        adapter.send(LightingCommand(emblem,1,80))
        assert targets(wire) == {fill:1,digit:2,emblem:1}
        wire.failures=1
        adapter.send(LightingCommand(emblem,0,60))
        assert targets(wire) == {fill:1,digit:2,emblem:2}
        assert adapter._logical_targets[emblem] == 0
        adapter.reconnect()
        wire.lines.clear()
        adapter.send(LightingCommand(fill,0,40))
        assert targets(wire) == {fill:0,digit:0,emblem:0}
        assert set(adapter._logical_targets.values()) == {0}
    finally:adapter.close()


@pytest.mark.parametrize('fill,emblem,digit', [(3,4,2),(1,17,0)])
def test_group_writes_final_values_in_order_and_keeps_each_explicit_transition(fill,emblem,digit):
    adapter,wire=opened()
    try:
        adapter.send_batch((LightingCommand(emblem,1,25),LightingCommand(digit,0,10),
                            LightingCommand(fill,1,200),LightingCommand(emblem,0,60),
                            LightingCommand(digit,1,90)))
        assert wire.lines == [f'{fill},1,200\n'.encode(),f'{digit},1,90\n'.encode(),
                              f'{emblem},2,60\n'.encode()]
        wire.lines.clear()
        adapter.send_batch((LightingCommand(emblem,1,50),LightingCommand(digit,0,70)))
        assert wire.lines == [f'{fill},1,70\n'.encode(),f'{digit},2,70\n'.encode(),
                              f'{emblem},1,50\n'.encode()]
    finally:adapter.close()
