"""Synthetic cards through the actual Max JSON boundary and application router."""
import asyncio
import json
from datetime import datetime, timezone

import pytest

from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.max_json import MaxJsonBoundary


def card(name="Синтетик", age=6):
    return {"schema_version": 1, "record": {"state": "active"},
            "rfid": {"state": "active", "valid_until": "2030-01-01T00:00:00Z"},
            "visit": {"status": "open"}, "identity": {"first_name": name},
            "birth": {"age_years": age, "birth_data_state": "complete" if age is not None else "unknown"},
            "acquaintance": {"kind": "first"},
            "adaptation": {"wheelchair_access": False, "participation_mode": "standing"}}


class Client:
    def __init__(self, value): self.value, self.events = value, []
    def start(self): return True
    def poll(self): return self.events.pop(0) if self.events else None
    def patient(self): return self.value
    def clear(self): self.value = None
    def close(self): return True


class Harness:
    def __init__(self, value):
        self.clock = [10.]
        self.app, *_ = build_application(settings=Settings(profile="mock", identity_mode="max_json"),
            background_scheduler=False, monotonic_now=lambda: self.clock[0])
        self.client = Client(value)
        self.boundary = MaxJsonBoundary(self.app, self.client, now=lambda: datetime(2029, 1, 1, tzinfo=timezone.utc))
        self.boundary.start(background=False)
        self.sequence = 0
        self.played = []
        original = self.app.audio.play_owned
        self.app.audio.play_owned = lambda handle, asset, **kw: (self.played.append(handle.response_id), original(handle, asset, **kw))[-1]
    @property
    def state(self): return self.app.canonical_runtime.sessions[self.boundary.session_id]
    async def send(self, kind, payload=None):
        self.sequence += 1
        return await self.app.receive_async(Event(kind, self.boundary.session_id, self.sequence, 1., "test", payload or {},
            f"test:{self.sequence}", self.clock[0]))
    async def ready(self):
        self.client.events.append({"kind": "patient_ready"})
        return await self.boundary.step()
    async def lift(self):
        await self.send("HandsetLifted")
        self.clock[0] += 5
        await self.send("WelcomeTimerFired")
    def callback(self):
        lease = self.app._active_audio[self.boundary.session_id]
        return {**{key: lease[key] for key in ("token", "generation", "playback_lease", "playback_id")},
                "response_id": lease["content_id"]}
    async def complete(self): return await self.send("AudioCompleted", self.callback())
    async def close(self):
        await self.boundary.close()
        await self.app.shutdown_identity()


@pytest.mark.parametrize("name,age", [("Синтетик", 6), (None, 6), ("Синтетик", None), (None, None)])
def test_authorized_json_skips_interview_even_when_optional_fields_missing(name, age):
    async def run():
        h = Harness(card(name, age))
        try:
            assert (await h.ready())["reason"] == "profile_ready"
            await h.lift()
            assert (await h.complete())["reason"] == "authorized_profile_menu_started"
            assert h.state.mode == "exhibition_menu_tutorial_prompt"
            assert h.state.display_name == name and h.state.age_years == age
            if age is None:
                assert h.state.age_band == "3-5" and h.state.age_band_source == "profile_missing_default"
            else: assert h.state.age_band_source == "data14_age_years"
            assert not h.state.onboarding_name_collected
            assert (await h.complete())["reason"] == "exhibition_menu_buttons_started"
            assert h.played[-2:] == ["PHRASE_007", "PHRASE_008"]
            assert not set(h.played) & {"PHRASE_078", "PHRASE_099", "PHRASE_108", "PHRASE_075"}
            context = h.app._canonical_audio_context(h.boundary.session_id)
            assert context.profile.display_name == name
            assert context.profile.age == age
            public = json.dumps(h.state.snapshot(), ensure_ascii=False)
            assert "Синтетик" not in public
        finally: await h.close()
    asyncio.run(run())


def test_guest_keeps_existing_onboarding_and_no_borrowed_profile():
    async def run():
        h = Harness(None)
        try:
            await h.lift()
            assert (await h.complete())["reason"] == "exhibition_introduction_started"
            assert h.state.profile_status == "anonymous" and h.state.display_name is None
            assert h.state.age_band == "3-5" and h.state.age_years is None
            assert "PHRASE_078" in h.played
        finally: await h.close()
    asyncio.run(run())


@pytest.mark.parametrize("cause", ["revoke", "expire", "unknown", "service_error"])
def test_lost_authorization_before_welcome_completion_cannot_skip_with_old_data(cause):
    async def run():
        h = Harness(card())
        try:
            await h.ready(); await h.lift()
            callback = h.callback()
            if cause == "expire":
                h.clock[0] = h.state.identity_expires_monotonic + 1
                await h.app.expire_identity_leases()
            else:
                h.client.value = None
                reason = {"revoke": "scan_started", "unknown": "no_personalization", "service_error": "api_error"}[cause]
                h.client.events.append({"kind": "clear", "reason_code": reason})
                await h.boundary.step()
                if cause == "unknown": assert h.boundary._healthy
                if cause == "service_error": assert not h.boundary._healthy
            await h.send("AudioCompleted", callback)
            assert h.state.mode == "session_closed" and h.state.profile_ref is None
            assert h.state.display_name is None and not h.app._canonical_profiles
            assert "PHRASE_007" not in h.played
        finally: await h.close()
    asyncio.run(run())


def test_hangup_resets_profile_and_stale_welcome_cannot_personalize_next_guest():
    async def run():
        h = Harness(card())
        try:
            await h.ready(); await h.lift()
            old = h.callback()
            await h.send("HandsetPlaced")
            assert h.client.patient() is None and not h.app._canonical_profiles
            await h.lift()
            await h.send("AudioCompleted", old)
            assert h.state.mode == "voice_welcome" and h.state.profile_status == "anonymous"
            assert (await h.complete())["reason"] == "exhibition_introduction_started"
            assert "PHRASE_007" not in h.played
        finally: await h.close()
    asyncio.run(run())


@pytest.mark.parametrize("section,field,bad", [
    ("identity", "first_name", 42),
    ("birth", "age_years", -1),
    ("record", "state", "archived"),
    ("rfid", "valid_until", "2020-01-01T00:00:00Z"),
])
def test_invalid_card_never_authorizes_interview_bypass(section, field, bad):
    async def run():
        value = card(); value[section][field] = bad
        h = Harness(value)
        try:
            assert (await h.ready())["reason"] == "invalid_client_profile"
            await h.lift()
            assert (await h.complete())["reason"] == "exhibition_introduction_started"
            assert h.state.profile_ref is None and h.state.display_name is None
        finally: await h.close()
    asyncio.run(run())
