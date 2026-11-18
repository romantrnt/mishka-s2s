"""Presentation snapshots advance even when background lights emit no input."""
from fastapi import FastAPI
import pytest
from concurrent.futures import ThreadPoolExecutor
import threading

from app.foundation.remote_devices import install_remote_devices
from app.foundation.simulator import SimulatorAcceptanceHarness


@pytest.mark.asyncio
async def test_background_frames_and_http_have_one_capture_order():
    harness = SimulatorAcceptanceHarness()
    stand = harness.stand
    remote = install_remote_devices(FastAPI(), stand, admits_users=lambda: True)
    frames = []

    async def publish(frame):
        frames.append(frame["state"])

    stand.hub.clients.add(object())
    stand.hub.publish = publish
    try:
        http_before = stand.panel_snapshot()
        cursor = remote.journal()["cursor"]
        await stand._publish_lighting_effect_frame({})
        await stand._publish_lighting_effect_frame({})
        http_after = stand.panel_snapshot()
        snapshots = [http_before, *frames, http_after]
        assert len(snapshots) == 4
        assert all(s["simulator"]["journal_cursor"] == cursor for s in snapshots)
        revisions = [s["simulator"]["snapshot_revision"] for s in snapshots]
        assert all(a < b for a, b in zip(revisions, revisions[1:]))
        remote.invalidate("reset")
        reset = stand.panel_snapshot()["simulator"]
        assert reset["epoch"] > http_after["simulator"]["epoch"]
        assert reset["snapshot_revision"] > revisions[-1]
        assert reset["instance_id"] == http_before["simulator"]["instance_id"]
    finally:
        stand.hub.clients.clear()
        await stand.aclose()


@pytest.mark.asyncio
async def test_concurrent_http_capture_is_serialized_with_projection():
    harness = SimulatorAcceptanceHarness()
    stand = harness.stand
    install_remote_devices(FastAPI(), stand, admits_users=lambda: True)
    entered = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()
    original = stand.snapshot
    calls = 0

    def capture():
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            assert release.wait(2)
        else:
            second_entered.set()
        return original()

    stand.snapshot = capture
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(stand.panel_snapshot)
            assert entered.wait(1)
            second = pool.submit(stand.panel_snapshot)
            try:
                assert not second_entered.wait(.05), 'counter and capture must share the same lock'
            finally:
                release.set()
            a, b = first.result(2), second.result(2)
        assert a["simulator"]["snapshot_revision"] < b["simulator"]["snapshot_revision"]
        assert second_entered.is_set()
    finally:
        release.set()
        await stand.aclose()
