"""Lost evdev events revoke physical authority until explicit reader recovery."""
from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import patch

import pytest

from app.foundation.input_adapters import KeyboardHIDInputAdapter as Adapter


def event(kind, code, value=0):
    return Adapter.EVENT_STRUCT.pack(10, 0, kind, code, value)


DROPPED = event(0, 3)
REPORT = event(0, 0)


def synced_adapter(callback=None, on_error=None):
    events = []
    errors = []
    adapter = Adapter("/dev/input/probe", callback or events.append,
                      on_error=on_error or errors.append, poll_s=.005)
    adapter._fd = 42
    adapter._ready = True
    adapter._status = "active"
    adapter._initial_level_sync = {"status": "synced"}
    adapter._known_levels = set(adapter.SUPPORTED_KEY_CODES)
    adapter._pressed.add(adapter.KEY_SPACE)
    return adapter, events, errors


@pytest.mark.parametrize("split", range(1, Adapter.EVENT_STRUCT.size))
def test_partial_syn_dropped_faults_before_batch_tail(split):
    adapter, events, errors = synced_adapter()
    # The pre-marker edge is valid; no event after the loss marker is trusted,
    # including a release after SYN_REPORT and another SYN_DROPPED.
    chunks = [event(1, 57, 0) + DROPPED[:split],
              DROPPED[split:] + event(1, 57, 1) + REPORT +
              event(1, 57, 0) + event(1, 6, 1) + DROPPED]
    with patch("app.foundation.input_adapters.os.read", side_effect=chunks) as read:
        adapter._run()
    assert read.call_count == 2
    assert [item.type for item in events] == ["SpaceReleased"]
    assert errors == [{"kind": "input_desynchronized"}]
    diag = adapter.diagnostics()
    assert diag["status"] == "input_desynchronized"
    assert diag["ready"] is False
    assert diag["semantic_input_accepted"] is False
    assert diag["physical_ready"] is False
    assert diag["input_desynchronized"] is True
    assert not adapter._pressed and not adapter._known_levels
    # Deterministic injection cannot accidentally resurrect this reader.
    adapter.feed_key(57, 0)
    adapter.feed_key(6, 1)
    assert [item.type for item in events] == ["SpaceReleased"]
    assert adapter.diagnostics()["last_error"] == errors[-1]
    assert adapter.diagnostics()["unverified_events"] == 2


@pytest.mark.parametrize("prefix", [b"", DROPPED[:1], DROPPED[:-1]])
def test_empty_or_incomplete_abi_eof_is_disconnect_not_synthetic_lift(prefix):
    adapter, events, errors = synced_adapter()
    chunks = ([prefix] if prefix else []) + [b""]
    with patch("app.foundation.input_adapters.os.read", side_effect=chunks):
        adapter._run()
    assert events == []
    assert errors == [{"kind": "device_disconnected"}]
    assert not adapter.is_ready()
    assert not adapter.diagnostics()["semantic_input_accepted"]


def test_ordinary_syn_report_does_not_change_edge_semantics():
    adapter, events, errors = synced_adapter()
    payload = REPORT + event(1, 57, 0) + REPORT + event(1, 57, 1) + REPORT
    with patch("app.foundation.input_adapters.os.read", side_effect=[payload, b""]):
        adapter._run()
    assert [item.type for item in events] == ["SpaceReleased", "SpacePressed"]
    assert errors == [{"kind": "device_disconnected"}]


def test_error_callback_sees_revocation_and_cannot_continue_tail():
    observed = []
    adapter, events, _ = synced_adapter()
    def failed(diagnostic):
        observed.append((diagnostic, adapter.is_ready(), adapter._stop.is_set()))
        raise RuntimeError("consumer failed")
    adapter.on_error = failed
    with patch("app.foundation.input_adapters.os.read",
               side_effect=[DROPPED + event(1, 57, 0), AssertionError("read after fault")]):
        adapter._run()
    assert observed == [({"kind": "input_desynchronized"}, False, True)]
    assert events == []


def test_repeated_explicit_close_open_rebaselines_without_fake_lift():
    events, errors = [], []
    adapter = Adapter("/dev/input/probe", events.append, on_error=errors.append, poll_s=.005)
    opened_fds = []
    def ioctl(fd, request, bitmap, mutate):
        # Every recovery starts physically on-hook. ioctl itself emits no edge.
        bitmap[57 // 8] |= 1 << (57 % 8)
    try:
        for cycle in range(3):
            read_fd, write_fd = os.pipe()
            os.set_blocking(read_fd, False)
            opened_fds.append(read_fd)
            with patch("app.foundation.input_adapters.os.open", return_value=read_fd), \
                 patch("app.foundation.input_adapters.fcntl.ioctl", side_effect=ioctl):
                adapter.open()
            assert adapter.diagnostics()["physical_ready"]
            assert not adapter.diagnostics()["input_desynchronized"]
            before = len(events)
            os.write(write_fd, event(1, 57, 1) + REPORT)
            time.sleep(.015)
            assert len(events) == before
            # Real release is admitted, then overflow faults and its tail dies.
            os.write(write_fd, event(1, 57, 0) + DROPPED +
                     event(1, 57, 1) + REPORT + event(1, 57, 0))
            adapter._thread.join(timeout=1)
            assert not adapter._thread.is_alive()
            assert [item.type for item in events[before:]] == ["SpaceReleased"]
            assert not adapter.is_ready()
            assert adapter.diagnostics()["last_error"] == {"kind": "input_desynchronized"}
            adapter.close()
            os.close(write_fd)
            with pytest.raises(OSError):
                os.fstat(read_fd)
        assert len(errors) == 3
    finally:
        adapter.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("already_active", [False, True])
async def test_raw_fault_revokes_stand_and_queued_lift_before_dispatch(already_active):
    from app.foundation.stand import HardwareIntegrationStand
    from app.foundation.config import Settings
    stand = HardwareIntegrationStand(Settings(profile="mock", hardware_mode="simulator"),
                                     background_scheduler=False)
    stand._admits_physical_users = lambda: True
    stand._physical_loop = asyncio.get_running_loop()
    adapter, _, _ = synced_adapter(
        callback=lambda e: stand._on_physical_input(e, 0),
        on_error=lambda e: stand._on_hid_error(e, 0))
    seen = []
    original = stand.application.receive_async
    async def observe(incoming, *args, **kwargs):
        seen.append(incoming.event_type)
        return await original(incoming, *args, **kwargs)
    stand.application.receive_async = observe
    try:
        if already_active:
            await stand.inject_physical({"type": "SpaceReleased"})
            seen.clear()
        # Execute reader synchronously so lift is enqueued, but not yet
        # dispatched on the asyncio loop, when the fault revokes authority.
        payload = event(1, 57, 0) + DROPPED + REPORT + event(1, 57, 1)
        with patch("app.foundation.input_adapters.os.read", side_effect=[payload]):
            adapter._run()
        assert stand._capture_hid_fault is not None
        assert stand._hid_status["kind"] == "input_desynchronized"
        assert not stand._headless_audio_accepting()
        await asyncio.wrap_future(stand._hid_loss_future)
        await asyncio.sleep(.01)
        assert seen == ["HardwareDisconnected"]
        state = stand.application.canonical_runtime.sessions[stand.active_session_id]
        assert state.mode == "safety_stop"
        assert not stand._headless_audio_accepting()
        # Later direct ordinary dispatch is blocked by the same latch.
        await stand.inject_physical({"type": "ButtonPressed", "button_id": 7,
                                     "_hid_generation": 0})
        assert seen == ["HardwareDisconnected"]
    finally:
        adapter._fd = None
        stand._physical_loop = None
        await stand.aclose()
