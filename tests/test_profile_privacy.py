from __future__ import annotations

import asyncio
import pytest

from app.foundation.canonical_contract import CanonicalEvent
from test_profile_software_chain import attach_client, build_chain, send


@pytest.mark.parametrize("ending", ["ClientProfileCleared", "AnonymousStart", "SessionCancel", "SessionReset"])
def test_profile_end_scrubs_private_and_derived_fields(ending):
    async def run():
        app, _, led, _ = build_chain()
        client, boundary = attach_client(app)
        try:
            client.ready()
            assert (await boundary.step())["reason"] == "profile_ready"
            state = app.canonical_runtime.sessions["software"]
            assert state.display_name == "PRIVATE_SYNTHETIC_NAME"
            assert state.age_years == 6 and state.age_band_source == "data14_age_years"
            # Seed private and derived state accumulated during a scenario.
            state.schedule_ref = "PRIVATE_SYNTHETIC_SCHEDULE"
            state.accessibility_policy = {"communication": (), "sensory": ("quiet",),
                                          "motor": (), "content_constraints": ()}
            state.dialogue_memory.bounded_turns.append("PRIVATE_SYNTHETIC_TURN")
            state.history_tail.append("PRIVATE_SYNTHETIC_HISTORY")
            if ending == "ClientProfileCleared":
                client.profile_cleared()
                assert (await boundary.step())["reason"] == "profile_cleared"
            else:
                await send(app, ending, 1)
            state = app.canonical_runtime.sessions["software"]
            assert state.profile_ref is None and state.profile_status == "anonymous"
            assert state.display_name is None and state.schedule_ref is None
            assert state.age_years is None and state.age_band_source == "anonymous_default"
            assert state.accessibility_policy == app.canonical_runtime._new_state("fresh", None).accessibility_policy
            assert not state.dialogue_memory.bounded_turns and not state.history_tail
            assert not app._canonical_profiles and not app._canonical_schedule_refs
            assert client.patient() is None and boundary.facts is None
        finally:
            await app.shutdown_identity()
            await app._cleanup_session_resources("software")
            led.cancel_effects(clear=True)
    asyncio.run(run())


def test_guest_onboarding_cannot_replace_verified_age():
    async def run():
        app, _, led, _ = build_chain()
        client, boundary = attach_client(app)
        try:
            client.ready()
            assert (await boundary.step())["reason"] == "profile_ready"
            await send(app, "HandsetLifted", 1)
            state = app.canonical_runtime.sessions["software"]
            state.mode = "exhibition_age_selection"
            expected = (state.age_years, state.age_band, state.age_band_source, dict(state.age_policy))
            result = app.canonical_runtime.handle(CanonicalEvent(
                "ButtonPressed", "software", 2, state.state_version, "software-test",
                {"button_id": 2}, "age-button", 1.0, app.monotonic_now()))
            assert result["reason"] == "exhibition_age_selected"
            assert (state.age_years, state.age_band, state.age_band_source, state.age_policy) == expected
            assert state.mode == "exhibition_age_complete"
        finally:
            await app.shutdown_identity()
            await app._cleanup_session_resources("software")
            led.cancel_effects(clear=True)
    asyncio.run(run())


def test_anonymous_start_drops_private_cache_and_next_voice_context():
    async def run():
        app, _, led, _ = build_chain()
        client, boundary = attach_client(app)
        try:
            client.ready()
            assert (await boundary.step())["reason"] == "profile_ready"
            assert app._canonical_profiles["software"]["display_name"] == "PRIVATE_SYNTHETIC_NAME"
            assert app._canonical_audio_context("software").profile.display_name == "PRIVATE_SYNTHETIC_NAME"
            await send(app, "AnonymousStart", 1)
            assert "software" not in app._canonical_profiles
            context = app._canonical_audio_context("software")
            assert context.profile.display_name is None
            assert "PRIVATE_SYNTHETIC" not in context.system_prompt
            assert client.patient() is None and boundary.facts is None
        finally:
            await app.shutdown_identity()
            await app._cleanup_session_resources("software")
            led.cancel_effects(clear=True)
    asyncio.run(run())
