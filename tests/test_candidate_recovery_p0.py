"""Candidate recovery probes: real owners/adapters, synthetic faults, no live devices.

These assertions establish local failure isolation, not physical acceptance.
"""
import asyncio
import os
import pty
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest

from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.hardware import PosixSerialTransport
from app.foundation.name_audio import synthesize_private_text
from app.foundation.stand import HardwareIntegrationStand, build_application, create_stand_app
from test_experimental_voice import configured  # Existing real subprocess protocol fixture.
from test_hid_failure_review_evidence import active_stand
from test_public_story_identity_expiry import Client



async def application(monkeypatch):
    from app.foundation import api
    monkeypatch.setattr(api, "_prepare_runtime_dirs", lambda _: None)
    web = create_stand_app(Settings(profile="mock", hardware_mode="simulator",
        stand_access_token="synthetic-recovery-p0", mutation_auth_policy="token_required"))
    app = web.state.stand.application
    app.background_scheduler = False
    return app, web


async def post(web, path="/internal/s2s/warmup", payload=None):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://local.test",
            headers={"x-stand-token": "synthetic-recovery-p0"}) as client:
        return await client.post(path, json=payload or {})


def event(kind, sequence=1, payload=None, sid="recovery-p0"):
    return Event(kind, sid, sequence, float(sequence), "recovery-p0", payload or {},
                 f"recovery:{sequence}", float(sequence))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "partial", "mock"])
async def test_warmup_failure_blocks_actual_ingress_then_explicit_retry_recovers(monkeypatch, failure):
    app, web = await application(monkeypatch)
    attempts = []

    async def warmup():
        attempts.append(True)
        if len(attempts) > 1:
            return {"ok": True, "e2e": {"ok": True}}
        if failure == "exception":
            raise ConnectionError("SYNTHETIC_PRIVATE_NETWORK_DETAIL")
        return {"ok": True, "e2e": {"ok": failure != "partial"}, "mock": failure == "mock"}

    monkeypatch.setattr(app.runtime_manager, "warmup", warmup)
    try:
        assert (await post(web)).status_code == (503 if failure == "exception" else 200)
        failed = web.state.lifecycle.snapshot()
        assert failed["state"] == "DEGRADED" and not failed["user_available"]
        assert "SYNTHETIC_PRIVATE" not in repr(failed)
        rejected = await post(web, "/stand/handset", {"state": "up"})
        assert rejected.status_code == 503 and rejected.json()["error"]["code"] == "runtime_unavailable"
        assert not app.canonical_runtime.sessions and app._runtime_maintenance is None
        # No loop retry storms: recovery is explicitly requested and coalesced.
        await asyncio.sleep(.02)
        assert len(attempts) == 1
        assert (await post(web)).status_code == 200
        ready = web.state.lifecycle.snapshot()
        assert ready["state"] == "READY" and ready["user_available"]
        assert ready["generation"] == failed["generation"] + 1
        assert ready["last_degraded"] == failed["last_degraded"]
        assert (await app.receive_async(event("HandsetLifted", 2)))["reason"] != "runtime_unavailable"
    finally:
        await web.state.lifecycle.stop()
        await web.state.stand.aclose()


@pytest.mark.asyncio
async def test_cancelled_warmup_waiter_does_not_free_lease_or_start_second_provider(monkeypatch):
    app, web = await application(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    attempts = []

    async def warmup():
        attempts.append(True)
        entered.set()
        await release.wait()
        raise TimeoutError("synthetic timeout")

    monkeypatch.setattr(app.runtime_manager, "warmup", warmup)
    first = asyncio.create_task(post(web))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        generation = web.state.lifecycle.snapshot()["generation"]
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(post(web))
        await asyncio.sleep(.01)
        assert len(attempts) == 1 and app._runtime_maintenance is not None
        assert web.state.lifecycle.snapshot()["generation"] == generation
        assert (await app.receive_async(event("HandsetLifted")))["reason"] == "runtime_unavailable"
        release.set()
        assert (await asyncio.wait_for(second, 2)).status_code == 503
        assert app._runtime_maintenance is None
        assert web.state.lifecycle.snapshot()["state"] == "DEGRADED"
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
        await web.state.lifecycle.stop()
        await web.state.stand.aclose()


@pytest.mark.asyncio
async def test_hid_fault_revokes_old_frame_hangup_and_fresh_entry_are_possible():
    stand = await active_stand()
    app, sid = stand.application, stand.active_session_id
    owner = stand._headless_frame_owner()
    try:
        stand._on_hid_error({"kind": "device_disconnected"}, 1)
        assert not owner[1]()  # Revocation precedes awaited cleanup.
        await asyncio.wait_for(asyncio.wrap_future(stand._hid_loss_future), 2)
        state = app.canonical_runtime.sessions[sid]
        assert state.mode == "safety_stop"
        assert stand.lighting_output.snapshot()["14"] == 1
        assert not app._active_audio
        stand._headless_audio_queue = asyncio.Queue()
        stand._enqueue_headless_audio_frame((.01,), owner)
        assert stand._headless_audio_queue.empty()
        await app.receive_async(event("HandsetPlaced", 10, sid=sid))
        state = app.canonical_runtime.sessions.get(sid)
        assert state is None or (state.mode == "idle" and state.handset == "down")
        # A recovered reader owns the next physical generation. No real device opened.
        stand._hid_generation = 2
        stand._capture_hid_fault = None
        await app.receive_async(event("HandsetLifted", 11, sid=sid))
        state = app.canonical_runtime.sessions[sid]
        assert state.handset == "up" and state.mode != "safety_stop"
        assert not owner[1]() and not app._personal_stories
    finally:
        stand._headless_audio_queue = None
        await stand.aclose()


@pytest.mark.asyncio
async def test_failed_hid_cleanup_blocks_reopen_then_parallel_retry_has_one_owner(monkeypatch):
    from app.foundation import stand as module
    stand = await active_stand()
    app, original = stand.application, stand.application.audio.cancel
    opened, closed = [], []

    class Reader:
        ready = False
        def __init__(self, *args, **kwargs): pass
        def open(self): self.ready = True; opened.append(stand._hid_generation)
        def close(self): self.ready = False; closed.append(stand._hid_generation)
        def is_ready(self): return self.ready
        def diagnostics(self):
            return {"physical_ready": self.ready, "semantic_input_accepted": self.ready,
                    "initial_level_sync": {"status": "synced"}}

    def capture():
        stand._headless_audio_task = asyncio.create_task(asyncio.Event().wait())
        stand._headless_audio_status = {"state": "active"}

    monkeypatch.setattr(module, "KeyboardHIDInputAdapter", Reader)
    stand._start_headless_audio_capture = capture
    stand.settings = replace(stand.settings, hid_device="/synthetic/no-real-device")
    stand.hardware_mode = "physical"
    try:
        def fail(*args): raise OSError("synthetic cancel failure")
        app.audio.cancel = fail
        stand._on_hid_error({"kind": "device_disconnected"}, 1)
        with pytest.raises(RuntimeError, match="hid_cleanup_failed"):
            await asyncio.wrap_future(stand._hid_loss_future)
        with pytest.raises(RuntimeError, match="hid_cleanup_failed"):
            await stand.start_hardware()
        assert not opened and stand._capture_hid_fault == 1
        app.audio.cancel = original
        await asyncio.wait_for(asyncio.gather(stand.start_hardware(), stand.start_hardware()), 2)
        assert opened == [2] and stand._hid_loss_future is None
        assert stand._capture_hid_fault is None
        # Old failure callback cannot revoke the replacement owner.
        stand._on_hid_error({"kind": "device_disconnected"}, 1)
        assert stand._capture_hid_fault is None and stand.input_adapter.is_ready()
    finally:
        app.audio.cancel = original
        stand.hardware_mode = "simulator"
        await stand.aclose()


def test_disconnected_native_serial_close_is_idempotent_and_does_not_leak_fd():
    for _ in range(3):
        master, slave = pty.openpty()
        transport = PosixSerialTransport(os.ttyname(slave))
        try:
            transport.open()
            descriptor = transport.fd
            os.close(master)
            master = None
            with pytest.raises(ConnectionError, match="disconnected"):
                transport.read_available()
            transport.close()
            assert transport.fd is None
            with pytest.raises(OSError): os.fstat(descriptor)
            transport.close()
        finally:
            transport.close()
            if master is not None: os.close(master)
            os.close(slave)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_native_cancelled_drain_successor_keeps_one_worker_and_does_not_replay(configured, cancel_waiter):
    provider, _, worker = configured("voxcpm765")
    pending = None
    try:
        stream = provider.synthesize_audio_stream("stream_close", 1, lambda: False, first_chunk=True)
        await anext(stream)
        pid = provider._process.pid
        await stream.aclose()
        assert provider.readiness()["reason"] == "experimental_voice_draining"
        cancelled = asyncio.Event()
        pending = asyncio.create_task(synthesize_private_text(provider, "stream", 2, cancelled.is_set))
        if cancel_waiter:
            await asyncio.sleep(.01)
            cancelled.set()
            with pytest.raises(asyncio.CancelledError): await asyncio.wait_for(pending, .2)
            assert not provider._recovery_task.cancelled()
            assert provider.jobs_submitted == 1
            await asyncio.wait_for(provider._recovery_task, 2)
            audio = await synthesize_private_text(provider, "stream", 3, lambda: False)
            assert audio.token == 3
        else:
            audio = await asyncio.wait_for(pending, 2)
            assert audio.token == 2
        assert len(audio.samples) == 144000
        assert provider.worker_starts == 1 and provider._process.pid == pid
        assert provider.jobs_submitted == 2 and provider.jobs_completed == 1
        assert worker.with_suffix(".active").read_text().endswith(":stream")
    finally:
        if pending: await asyncio.gather(pending, return_exceptions=True)
        await provider.close()


@pytest.mark.asyncio
async def test_max_poll_network_fault_clears_authority_guest_survives_then_fresh_card_recovers(monkeypatch):
    client = Client()
    card = deepcopy(client.card)
    api = create_stand_app(Settings(profile="mock", hardware_mode="simulator",
        identity_mode="max_json", runtime_mode="exhibition"), phone_client=client)
    stand, app = api.state.stand, api.state.stand.application
    app.background_scheduler = False
    boundary = app._max_client_boundary
    boundary.admits_users = None  # Isolate JSON authority; no real warmup claimed.
    boundary.now = lambda: datetime(2026, 11, 24, tzinfo=timezone.utc)
    boundary.start(background=False)
    sid = boundary.session_id
    try:
        client.events.append({"kind": "patient_ready"})
        assert (await boundary.step())["reason"] == "profile_ready"
        old_request = app.canonical_runtime.sessions[sid].identity_request_id
        def fail(): raise ConnectionError("SYNTHETIC_PRIVATE_NETWORK_DETAIL")
        poll = client.poll
        monkeypatch.setattr(client, "poll", fail)
        assert (await boundary.step())["reason"] == "client_unavailable"
        state = app.canonical_runtime.sessions[sid]
        assert state.profile_ref is None and state.identity_request_id is None
        assert not app._canonical_profiles and boundary.facts is None
        assert app.identity_status()["identity_ready"] is False
        assert stand.admission_dependencies_ready()  # RFID is optional for guest availability.
        result = await app.receive_async(event("HandsetLifted", sid=sid))
        assert result["reason"] != "runtime_unavailable" and state.handset == "up"
        assert state.profile_status != "identified"
        await app.receive_async(event("HandsetPlaced", 2, sid=sid))
        monkeypatch.setattr(client, "poll", poll)
        client.card = card
        client.events.append({"kind": "patient_ready"})
        assert (await boundary.step())["reason"] == "profile_ready"
        state = app.canonical_runtime.sessions[sid]
        assert state.identity_request_id != old_request and state.profile_ref
        assert app.identity_status()["identity_ready"] is True
        assert "SYNTHETIC_PRIVATE" not in repr(stand.health_snapshot())
    finally:
        await stand.aclose()


@pytest.mark.asyncio
async def test_hung_native_tts_has_deadline_reaps_worker_then_new_request_succeeds(configured):
    from app.foundation.experimental_voice import ExperimentalVoiceUnavailable
    provider, _, worker = configured("vox555", inference_timeout_s=.08)
    try:
        chunks = []
        with pytest.raises(ExperimentalVoiceUnavailable, match="timeout"):
            async for chunk in provider.synthesize_audio_stream("stream_slow", 1, lambda: False, first_chunk=True):
                chunks.append(chunk)
        assert not chunks
        await asyncio.wait_for(provider._recovery_task, 2)
        assert provider.worker_starts == 2 and provider.readiness()["ready"]
        assert worker.with_suffix(".active").read_text().endswith(":stream_slow")
        # Recovery never retries the old text. Only an explicit new token submits.
        assert provider.jobs_submitted == 1
        audio = await asyncio.wait_for(synthesize_private_text(provider, "stream", 2, lambda: False), 2)
        assert audio.token == 2 and len(audio.samples) == 144000
        assert provider.jobs_submitted == 2 and provider.worker_starts == 2
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_playback_timeout_stops_adapter_then_hangup_reentry_rejects_stale_completion():
    app, *_ = build_application(settings=Settings(profile="mock", runtime_mode="exhibition",
        playback_timeout_s=.02), background_scheduler=False)
    sid = "recovery-playback-p0"
    clock = [0.]
    app.monotonic_now = app.canonical_runtime.monotonic_now = lambda: clock[0]
    try:
        await app.receive_async(event("HandsetLifted", sid=sid))
        clock[0] = app.canonical_runtime.sessions[sid].welcome_deadline_monotonic
        await app.flush_welcome_timers()
        lease = dict(app._active_audio[sid])
        await asyncio.sleep(.08)
        state = app.canonical_runtime.sessions[sid]
        assert state.audio_status == "failed" and state.audio_failure_code == "audio_playback_timeout"
        assert not app._active_audio and not app.audio.commands
        assert state.expected_inputs == ["handset_down", "reset"]
        assert any(item.get("preset") == "ERROR_RECOVERABLE" for item in app.last_canonical_effects)
        await app.receive_async(event("HandsetPlaced", 2, sid=sid))
        await app.receive_async(event("HandsetLifted", 3, sid=sid))
        fresh = app.canonical_runtime.sessions[sid]
        assert fresh.handset == "up" and fresh.audio_failure_code is None
        result = await app.receive_async(event("AudioCompleted", 4, lease, sid=sid))
        assert result["reason"] == "stale_exhibition_audio_completion"
        assert fresh.handset == "up" and fresh.audio_failure_code is None
    finally:
        await app._cleanup_session_resources(sid)
        await app.shutdown_identity()
