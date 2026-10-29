"""Local-only synthetic HTTP -> PTY -> application acceptance.

These tests need normal local socketpair IPC for TestClient's worker portal and
asyncio wakeups. They do not open a network listener or a physical device.
Positive cases explicitly assume control-plane readiness after proving the real
mock startup is DEGRADED. They do not claim model/production warmup acceptance.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time

from fastapi.testclient import TestClient

from app.foundation.config import Settings
from app.foundation.identity import IdentityDecision, SyntheticIdentityResolver
from app.foundation.lifecycle import RuntimeState
from app.foundation.stand import create_stand_app

TOKEN = "synthetic-http-operator"
UID = "0001-SYNTHETIC-PRIVATE-UID"
NAME = "SYNTHETIC-PRIVATE-NAME"
HEADERS = {"x-stand-token": TOKEN}


def make_api(resolver):
    api = create_stand_app(Settings(profile="mock", hardware_mode="simulator",
        identity_mode="optional_rfid", identity_provider="synthetic", stand_access_token=TOKEN))
    api.state.stand.application.identity_resolver = resolver
    return api


async def await_mock_startup(api):
    lifecycle = api.state.lifecycle
    result = await asyncio.wait_for(asyncio.shield(lifecycle._warmup), 2.0)
    assert result.get("mock") is True
    assert lifecycle.snapshot()["state"] == "DEGRADED"
    assert lifecycle.snapshot()["reason"] == "full_e2e_warmup_required"
    assert not lifecycle.admits_users()


async def assume_control_plane_ready(api):
    # Test input at the readiness boundary, not a forged nonmock E2E result or
    # a replacement admits_users/auth/owner/privacy predicate. The real mock
    # startup policy is tested separately below and remains unchanged.
    await await_mock_startup(api)
    api.state.lifecycle._state = RuntimeState.READY
    api.state.lifecycle._reason = "test_only_assumed_control_plane_readiness"
    assert api.state.lifecycle.admits_users()


async def finish_lookup(application):
    task = application._identity_lookup_tasks.get("stand")
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), 2.0)


def assert_public_private_free(client):
    response = client.get("/stand/state", headers=HEADERS)
    assert response.status_code == 200
    public = response.json()
    encoded = json.dumps(public)
    assert UID not in encoded and NAME not in encoded
    return public


def test_given_ready_fixture_real_http_pty_authorized_lift_hangup_and_lifespan_close():
    resolver = SyntheticIdentityResolver({UID: IdentityDecision("authorized",
        {"profile_ref": "synthetic-http-subject", "display_name": NAME},
        frozenset({"personalize"}), time.monotonic() + 60.0)})
    api = make_api(resolver)
    stand = api.state.stand
    started = time.monotonic()
    with TestClient(api) as client:
        client.portal.call(assume_control_plane_ready, api)
        readiness = client.get("/ready")
        assert readiness.status_code == 503 and readiness.json()["production_ready"] is False
        denied = client.post("/stand/rfid", json={"tag_id": UID, "session_id": "stand"})
        assert denied.status_code == 401
        response = client.post("/stand/rfid", json={"tag_id": UID, "session_id": "stand"}, headers=HEADERS)
        assert response.status_code == 200
        client.portal.call(finish_lookup, stand.application)
        assert assert_public_private_free(client)["canonical_state"]["mode"] == "rfid_ready"
        lifted = client.post("/stand/handset", json={"state": "up", "session_id": "stand"}, headers=HEADERS)
        assert lifted.status_code == 200
        assert lifted.json()["reason"] == "exhibition_handset_lifted"
        state = stand.application.canonical_runtime.sessions["stand"]
        assert state.mode == "voice_welcome" and state.profile_status == "identified"
        placed = client.post("/stand/handset", json={"state": "down", "session_id": "stand"}, headers=HEADERS)
        assert placed.status_code == 200 and placed.json()["reason"] == "exhibition_reset"
        public = assert_public_private_free(client)
        assert public["canonical_state"]["mode"] == "idle"
        assert set(public["physical_lighting"].values()) == {0}
        assert not stand.application._canonical_profiles
        close_started = time.monotonic()
    assert time.monotonic() - close_started < 3.0
    assert time.monotonic() - started < 10.0
    assert stand._closed
    assert not stand.application._identity_lookup_tasks
    assert not stand.application._identity_provider_tasks
    assert not stand.application._identity_deadline_tasks
    assert stand.application._idle_monitor_task is None
    assert stand.application._watchdog_task is None


class GatedSyntheticResolver:
    def __init__(self):
        self.started = threading.Event()
        self.gate = asyncio.Event()
        self.calls = 0

    async def resolve(self, uid, installation_id, request_id):
        self.calls += 1
        self.started.set()
        # A provider may finish even after caller cancellation. It must never
        # upgrade the guest session or restore the ended identified context.
        while not self.gate.is_set():
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                continue
        return IdentityDecision("authorized", {"profile_ref": "synthetic-late-subject", "display_name": NAME},
                                frozenset({"personalize"}), time.monotonic() + 60.0)

    def readiness(self):
        return {"ready": True, "synthetic": True, "provider": "synthetic", "production_ready": False}


def test_given_ready_fixture_http_pty_lift_and_hangup_reject_late_allow():
    resolver = GatedSyntheticResolver()
    api = make_api(resolver)
    stand = api.state.stand
    with TestClient(api) as client:
        client.portal.call(assume_control_plane_ready, api)
        readiness = client.get("/ready")
        assert readiness.status_code == 503 and readiness.json()["production_ready"] is False
        try:
            response = client.post("/stand/rfid", json={"tag_id": UID, "session_id": "stand"}, headers=HEADERS)
            assert response.status_code == 200
            assert resolver.started.wait(timeout=1.0)
            assert stand.application.canonical_runtime.sessions["stand"].mode == "rfid_lookup"
            started = time.monotonic()
            lifted = client.post("/stand/handset", json={"state": "up", "session_id": "stand"}, headers=HEADERS)
            assert lifted.status_code == 200 and time.monotonic() - started < 1.0
            state = stand.application.canonical_runtime.sessions["stand"]
            assert state.mode == "voice_welcome" and state.profile_status == "anonymous"
            placed = client.post("/stand/handset", json={"state": "down", "session_id": "stand"}, headers=HEADERS)
            assert placed.status_code == 200
            async def release_and_drain():
                resolver.gate.set()
                tasks = tuple(stand.application._identity_provider_tasks)
                if tasks:
                    _, pending = await asyncio.wait(tasks, timeout=2.0)
                    assert not pending
            client.portal.call(release_and_drain)
            public = assert_public_private_free(client)
            assert public["canonical_state"]["mode"] == "idle"
            assert set(public["physical_lighting"].values()) == {0}
            assert not stand.application._canonical_profiles
            guest = client.post("/stand/handset", json={"state": "up", "session_id": "stand"}, headers=HEADERS)
            assert guest.status_code == 200
            assert stand.application.canonical_runtime.sessions["stand"].profile_status == "anonymous"
            client.post("/stand/handset", json={"state": "down", "session_id": "stand"}, headers=HEADERS).raise_for_status()
            # These are actual encoded/CRC-checked device-to-host PTY frames.
            incoming = [frame["decoded"]["type"] for frame in stand.raw_frames
                        if frame.get("direction") == "device_to_host" and isinstance(frame.get("decoded"), dict)]
            assert incoming.count("rfid") == 1 and incoming.count("handset") == 4
            close_started = time.monotonic()
        finally:
            async def release_fixture():
                resolver.gate.set()
                tasks = tuple(stand.application._identity_provider_tasks)
                if tasks:
                    _, pending = await asyncio.wait(tasks, timeout=2.0)
                    assert not pending
            client.portal.call(release_fixture)
    assert time.monotonic() - close_started < 3.0
    assert stand._closed and resolver.calls == 1
    assert not stand.application._identity_lookup_tasks
    assert not stand.application._identity_deadline_tasks
    assert not stand.application._identity_provider_tasks


def test_default_mock_http_remains_unavailable_and_never_bypasses_admission():
    class CountingSyntheticResolver(SyntheticIdentityResolver):
        def __init__(self):
            super().__init__({})
            self.calls = 0
        async def resolve(self, uid, installation_id, request_id):
            self.calls += 1
            return await super().resolve(uid, installation_id, request_id)
    resolver = CountingSyntheticResolver()
    api = make_api(resolver)
    stand = api.state.stand
    with TestClient(api) as client:
        client.portal.call(await_mock_startup, api)
        denied = client.post("/stand/rfid", json={"tag_id": UID, "session_id": "stand"})
        assert denied.status_code == 401
        unavailable = client.post("/stand/rfid", json={"tag_id": UID, "session_id": "stand"}, headers=HEADERS)
        assert unavailable.status_code == 503
        assert unavailable.json()["error"]["code"] == "runtime_unavailable"
        assert unavailable.json()["error"]["state"] == "DEGRADED"
        assert resolver.calls == 0
        assert not stand.application.canonical_runtime.sessions
        assert set(stand.lighting_output.snapshot().values()) == {0}
    assert stand._closed
