from __future__ import annotations

import asyncio
import pytest

from app.foundation.canonical_contract import CanonicalEvent, CanonicalRuntime
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.api import build_application

PROFILE = {"child_id": "synthetic-child", "display_name": "PRIVATE_SYNTHETIC_NAME",
           "date_of_birth": "2020-01-01", "timezone": "Europe/Moscow",
           "sensory_flags": ["quiet"], "schedule_ref": "PRIVATE_SYNTHETIC_SCHEDULE"}


def send(runtime, kind, sequence, payload=None):
    state = runtime.sessions.get("privacy")
    return runtime.handle(CanonicalEvent(kind, "privacy", sequence,
        state.state_version if state else 0, "test", payload or {},
        f"privacy:{sequence}", 1.0, 1.0))


@pytest.mark.parametrize("ending", ["RfidRemoved", "AnonymousStart", "SessionCancel", "SessionReset"])
def test_identity_end_scrubs_private_and_derived_fields(ending):
    runtime = CanonicalRuntime()
    runtime.handle(CanonicalEvent("RfidPresented", "privacy", 1, 0, "test", {}, "first", 1.0), PROFILE)
    state = runtime.sessions["privacy"]
    state.dialogue_memory.bounded_turns.append("PRIVATE_SYNTHETIC_TURN")
    state.history_tail.append("PRIVATE_SYNTHETIC_HISTORY")
    send(runtime, ending, 2)
    state = runtime.sessions["privacy"]
    assert state.profile_ref is None and state.profile_status == "anonymous"
    assert state.display_name is None and state.schedule_ref is None
    assert state.age_years is None and state.age_band_source == "anonymous_default"
    assert state.accessibility_policy == runtime._new_state("fresh", None).accessibility_policy
    assert not state.dialogue_memory.bounded_turns and not state.history_tail


def test_guest_onboarding_cannot_replace_verified_age():
    runtime = CanonicalRuntime(exhibition_mode=True)
    state = runtime._new_state("privacy", PROFILE)
    state.mode = "exhibition_age_selection"
    runtime.sessions["privacy"] = state
    expected = (state.age_years, state.age_band, state.age_band_source, dict(state.age_policy))
    send(runtime, "ButtonPressed", 1, {"button_id": 2})
    assert (state.age_years, state.age_band, state.age_band_source, state.age_policy) == expected
    assert state.mode == "exhibition_age_complete"


def test_anonymous_start_drops_private_cache_and_next_voice_context():
    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"), background_scheduler=False)
        app.profiles.resolve = lambda _uid: dict(PROFILE)
        async def no_wave(*_args):
            return None
        app._run_readiness_wave_async = no_wave
        await app.receive_async(Event("RfidPresented", "privacy", 1, 1.0, "test", {"tag_id": "synthetic-card"}, "one"))
        assert app._canonical_profiles["privacy"]["child_id"] == "synthetic-child"
        await app.receive_async(Event("AnonymousStart", "privacy", 2, 1.0, "test", {}, "two"))
        assert "privacy" not in app._canonical_profiles
        context = app._canonical_audio_context("privacy")
        assert context.profile.display_name is None
        assert "PRIVATE_SYNTHETIC" not in context.system_prompt
        await app._cleanup_session_resources("privacy")
    asyncio.run(run())
