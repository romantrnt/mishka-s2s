from __future__ import annotations

import os
import threading


from app.foundation import input_adapters
from app.foundation.events import parse_event
from app.foundation.input_adapters import KeyboardHIDInputAdapter


def _synced_adapter(monkeypatch, pressed_codes=()):
    events = []
    adapter = KeyboardHIDInputAdapter('/dev/input/test', events.append, debounce_ms=0)
    adapter._fd = 41
    pressed = set(pressed_codes)

    def ioctl(_fd, _request, bitmap, _mutate):
        for code in pressed:
            bitmap[code // 8] |= 1 << (code % 8)

    monkeypatch.setattr(input_adapters.fcntl, 'ioctl', ioctl)
    adapter._sync_initial_levels()
    return adapter, events


def _event_types(events):
    return [event.type for event in events]


def _lift_count(events):
    return sum(
        parse_event({"type": event.type}, "aud02", index, "hid", now=0.0).event_type == "HandsetLifted"
        for index, event in enumerate(events, 1)
    )


def test_initial_down_then_first_real_release_emits_one_lift(monkeypatch):
    adapter, events = _synced_adapter(monkeypatch, {KeyboardHIDInputAdapter.KEY_SPACE})
    adapter.feed_key(adapter.KEY_SPACE, 0, timestamp=1.0)
    adapter.feed_key(adapter.KEY_SPACE, 0, timestamp=2.0)
    assert _event_types(events) == ['SpaceReleased']
    assert _lift_count(events) == 1
    assert adapter.diagnostics()['initial_level_sync']['status'] == 'synced'


def test_initial_up_press_then_release_emits_one_lift(monkeypatch):
    adapter, events = _synced_adapter(monkeypatch)
    adapter.feed_key(adapter.KEY_SPACE, 1, timestamp=1.0)
    adapter.feed_key(adapter.KEY_SPACE, 0, timestamp=2.0)
    assert _event_types(events) == ['SpacePressed', 'SpaceReleased']
    assert _lift_count(events) == 1


def test_unavailable_level_api_rejects_unverified_first_event_without_false_lift(monkeypatch):
    events = []
    adapter = KeyboardHIDInputAdapter('/dev/input/test', events.append, debounce_ms=0)
    adapter._fd = 41

    def unavailable(*_args, **_kwargs):
        raise OSError('EVIOCGKEY unavailable')

    monkeypatch.setattr(input_adapters.fcntl, 'ioctl', unavailable)
    adapter._sync_initial_levels()
    adapter.feed_key(adapter.KEY_SPACE, 0, timestamp=1.0)
    assert events == []
    diagnostics = adapter.diagnostics()
    assert diagnostics['initial_level_sync']['error_code'] == 'initial_level_unavailable'
    assert diagnostics['initial_level_sync']['first_event_policy'] == 'reject_unverified_first_event'
    assert diagnostics['semantic_input_accepted'] is False
    assert diagnostics['physical_ready'] is False
    assert diagnostics['unverified_events'] == 1


def test_reader_replacement_rejects_stale_first_event_from_old_generation():
    from types import SimpleNamespace
    from app.foundation.stand import HardwareIntegrationStand

    stand = HardwareIntegrationStand.__new__(HardwareIntegrationStand)
    stand._hid_generation = 2
    stand._physical_loop = None
    stand._physical_sequence = 0
    stand._on_physical_input(SimpleNamespace(type='SpaceReleased', payload={}), generation=1)
    assert stand._physical_sequence == 0


def test_eof_then_reconnect_does_not_create_fake_lift(monkeypatch):
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    events = []
    adapter = KeyboardHIDInputAdapter('/dev/input/test', events.append, poll_s=0.005)
    adapter._fd = read_fd
    adapter._ready = True
    adapter._status = 'active'
    thread = threading.Thread(target=adapter._run, daemon=True)
    adapter._thread = thread
    thread.start()
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert events == []
    assert adapter.diagnostics()['status'] == 'device_disconnected'
    adapter.close()

    reconnected_events = []
    reconnected = KeyboardHIDInputAdapter('/dev/input/test', reconnected_events.append, debounce_ms=0)
    reconnected._fd = 41
    monkeypatch.setattr(input_adapters.fcntl, 'ioctl', lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError('unavailable')))
    reconnected._sync_initial_levels()
    reconnected.feed_key(reconnected.KEY_SPACE, 0, timestamp=2.0)
    assert reconnected_events == []
