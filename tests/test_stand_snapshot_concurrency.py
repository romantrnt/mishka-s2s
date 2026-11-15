"""A hardware writer must not invalidate a diagnostic projection mid-event."""
import threading

import pytest

from app.foundation.config import Settings
from app.foundation.hardware import encode_frame
from app.foundation.stand import HardwareIntegrationStand


@pytest.mark.parametrize("buffer_name,projector", [
    ("raw_frames", "_panel_payload"),
    ("commands", "_panel_payload"),
    ("errors", "_safe_diagnostic_item"),
    ("canonical_trace", "_safe_trace_item"),
    ("canonical_diagnostics", "_safe_diagnostic_item"),
])
def test_snapshot_projects_stable_copy_during_threaded_append(monkeypatch, buffer_name, projector):
    stand = HardwareIntegrationStand(settings=Settings(profile="mock").validate())
    marker = {"type": "test_snapshot_marker", "sequence": 91}
    if buffer_name == "raw_frames":
        target = stand.raw_frames
        entry = {"decoded": {"payload": marker}}
    elif buffer_name == "commands":
        target = stand.commands
        entry = {"payload": marker}
    else:
        owner = stand.application if buffer_name.startswith("canonical_") else stand
        target = getattr(owner, buffer_name)
        entry = marker
    target.clear()
    target.append(entry)
    projection_started = threading.Event()
    appended = threading.Event()
    original = getattr(stand, projector)
    thread_errors = []

    def project(value):
        if value is marker:
            projection_started.set()
            assert appended.wait(2), "writer failed to append during projection"
        return original(value)

    def writer():
        try:
            assert projection_started.wait(2), "projection never reached test entry"
            if buffer_name == "raw_frames":
                # Same callback used by the real UART reader/writer threads.
                stand._observe_raw("host_to_device", encode_frame("led_pattern", 92, {}))
            else:
                target.append({"type": "newer_diagnostic", "sequence": 92})
        except BaseException as exc:
            thread_errors.append(exc)
        finally:
            appended.set()

    monkeypatch.setattr(stand, projector, project)
    worker = threading.Thread(target=writer)
    worker.start()
    try:
        result = stand.snapshot()
        assert len(result[buffer_name]) == 1
        assert len(target) == 2
    finally:
        projection_started.set()
        worker.join(3)
        stand.close()
    assert not worker.is_alive()
    assert not thread_errors
