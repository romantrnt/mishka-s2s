"""Unchanged Max TLS/SQLite + real Stand, with controlled capture timing.

Set MISHKA_TEST_MAX_BUNDLE to the supplied, unchanged Max source directory.
Speech providers are mock; these tests make no GPU or real-reader claim.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import threading

import pytest

from scripts.run_max_json_demo import demo_session


MAX_SOURCE = os.environ.get("MISHKA_TEST_MAX_BUNDLE")
pytestmark = pytest.mark.skipif(not MAX_SOURCE, reason="requires explicit unchanged Max source directory")


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", ["hangup", "reset"])
@pytest.mark.parametrize("new_lift", [False, True])
async def test_scan_submitted_before_cleanup_cannot_adopt_child_start_epoch(cleanup, new_lift):
    async with demo_session(Path(MAX_SOURCE), assume_control_ready=True) as runtime:
        simulator = runtime.api.state.remote_devices
        captures = 0
        async def capture(*args):
            nonlocal captures
            captures += 1
            return await runtime.capture.scan(*args)
        simulator.scan_callback = capture
        submitted_epoch = simulator.epoch
        async def cleanup_then_lift():
            await simulator.action({"action": "reset"} if cleanup == "reset" else
                                   {"action": "handset", "state": "down"})
            if new_lift:
                await simulator.action({"action": "handset", "state": "up"})
        # Both parent coroutines are already runnable. The scan parent creates
        # its child, then the queued cleanup parent advances the epoch before
        # that child starts; capture must never observe this stale submission.
        scan = asyncio.create_task(simulator.action({"action": "rfid", "card": "demo-1", "epoch": submitted_epoch}))
        cleanup_task = asyncio.create_task(cleanup_then_lift())
        results = await asyncio.gather(scan, cleanup_task)
        assert results[0].get("error", {}).get("code") == "stale_simulator_epoch"
        assert captures == 0
        await asyncio.sleep(0.12)
        state = runtime.stand.application.canonical_runtime.sessions.get("stand")
        # Cleanup of a never-started session may leave no canonical record.
        # A stale scan must not be the action that manufactures that record.
        assert state is None or state.handset == ("up" if new_lift else "down")
        assert state is None or state.profile_status == "anonymous"
        assert not runtime.stand.application._canonical_profiles
        assert runtime.boundary.client.patient() is None
        assert runtime.boundary.facts is None
        assert simulator.scan_admits_profile() is True
        if new_lift:
            assert state is not None
            assert state.mode == "voice_welcome"
            await simulator.action({"action": "handset", "state": "down"})
        # Real Max TLS/SQLite remains usable for a fresh submission.
        fresh = await simulator.action({"action": "rfid", "card": "demo-2"})
        assert fresh["reason"] == "synthetic_scan_processed"
        assert captures == 1
        assert runtime.stand.application.canonical_runtime.sessions["stand"].mode == "profile_ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,cleanup", [("queued", "hangup"), ("queued", "reset"), ("in_handler", "hangup"), ("in_handler", "reset")])
async def test_old_scan_cannot_attach_after_cleanup_and_new_lift(phase, cleanup, monkeypatch):
    async with demo_session(Path(MAX_SOURCE), assume_control_ready=True) as runtime:
        simulator = runtime.api.state.remote_devices
        simulator.scan_callback = runtime.capture.scan
        entered = threading.Event()
        release = threading.Event()
        if phase == "queued":
            original = runtime.capture._frame_factory
            def delayed(*args):
                entered.set()
                assert release.wait(5)
                return original(*args)
            monkeypatch.setattr(runtime.capture, "_frame_factory", delayed)
        else:
            coordinator = runtime.boundary.client._bridge._coordinator
            original = coordinator.resolve
            def delayed(*args):
                entered.set()
                assert release.wait(5)
                return original(*args)
            monkeypatch.setattr(coordinator, "resolve", delayed)
        request = asyncio.create_task(simulator.action({"action": "rfid", "card": "demo-1"}))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            # Request cancellation must not discard the owned scan/quarantine.
            cancelled = phase == "queued" and cleanup == "reset"
            if cancelled:
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
                assert simulator._scan_task is not None and not simulator._scan_task.done()
            await simulator.action({"action": "reset"} if cleanup == "reset" else {"action": "handset", "state": "down"})
            assert simulator.scan_admits_profile() is False
            await simulator.action({"action": "handset", "state": "up"})
            release.set()
            result = await simulator._scan_task if cancelled else await request
            assert result["error"]["code"] == "stale_simulator_epoch"
            # Let the boundary's sole owner pump any late metadata/ready event.
            await asyncio.sleep(0.12)
            state = runtime.stand.application.canonical_runtime.sessions["stand"]
            assert state.handset == "up"
            assert state.profile_status == "anonymous"
            assert state.mode == "voice_welcome"
            assert not runtime.stand.application._canonical_profiles
            assert runtime.boundary.client.patient() is None
            assert runtime.boundary.facts is None
            assert simulator.scan_admits_profile() is True
            # The next actual TLS/SQLite scan still works; no upstream duplicate
            # workaround or manufactured patient_ready is used.
            await simulator.action({"action": "handset", "state": "down"})
            fresh = await simulator.action({"action": "rfid", "card": "demo-2"})
            assert fresh["reason"] == "synthetic_scan_processed"
            assert runtime.stand.application.canonical_runtime.sessions["stand"].mode == "profile_ready"
        finally:
            release.set()
            if not request.done():
                await asyncio.gather(request, return_exceptions=True)
