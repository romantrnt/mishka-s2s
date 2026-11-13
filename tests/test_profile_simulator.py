from __future__ import annotations

import asyncio
import json

import pytest

from app.foundation.config import Settings
from app.foundation.lighting import LIGHTING_ELEMENT_IDS
from app.foundation.simulator import SimulatorAcceptanceHarness
from test_profile_software_chain import attach_client


@pytest.mark.parametrize("settings", [
    Settings(profile="production", hardware_mode="simulator"),
    Settings(profile="mock", hardware_mode="physical"),
])
def test_acceptance_harness_rejects_real_providers_and_devices(settings):
    with pytest.raises(ValueError, match="requires mock providers and simulator hardware"):
        SimulatorAcceptanceHarness(settings)


def test_simulator_button_sequence_reaches_canonical_deduplication():
    async def scenario():
        harness = SimulatorAcceptanceHarness()
        # Test-only control admission: this unit harness has no lifecycle warmup.
        harness.stand._admits_physical_users = lambda: True
        try:
            await harness.space_released(sequence=40)
            duplicate = await harness.button(1, sequence=40)
            assert duplicate["reason"] == "stale_sequence"
            assert duplicate["outputs"] == []
            await harness.button(1, sequence=41)
            await harness.button(1)
            assert harness.stand.application.canonical_runtime.sessions["stand"].last_sequence_by_source["simulator"] == 42
        finally:
            harness.close()
    asyncio.run(scenario())


def test_simulator_audio_completion_carries_lease_and_rejects_old_playback():
    async def scenario():
        harness = SimulatorAcceptanceHarness()
        try:
            await harness.space_released()
            await harness.advance(5.0)
            old = harness.audio_identity("PHRASE_001")
            assert set(old) == {"response_id", "token", "playback_id", "playback_lease", "generation"}
            rejected = await harness.audio_completed("PHRASE_001", token=old["token"] + 1)
            assert rejected["reason"] == "stale_exhibition_audio_completion"
            accepted = await harness.audio_completed("PHRASE_001", identity=old)
            assert accepted["reason"] == "exhibition_introduction_started"
            await harness.space_pressed()
            await harness.space_released()
            await harness.advance(5.0)
            current = harness.audio_identity("PHRASE_001")
            assert current["playback_id"] != old["playback_id"]
            rejected = await harness.audio_completed("PHRASE_001", identity=old)
            assert rejected["reason"] == "stale_exhibition_audio_completion"
            assert harness.snapshot()["canonical_state"]["mode"] == "voice_welcome"
            accepted = await harness.audio_completed("PHRASE_001", identity=current)
            assert accepted["reason"] == "exhibition_introduction_started"
        finally:
            harness.close()
    asyncio.run(scenario())


def test_ready_json_projects_backend_ready_private_panel_and_dark_clear():
    async def scenario():
        private_name = "SYNTHETIC-PRIVATE-NAME"
        harness = SimulatorAcceptanceHarness(Settings(profile="mock", runtime_mode="exhibition",
            hardware_mode="simulator", identity_mode="max_json"))
        client, boundary = attach_client(harness.stand.application, session_id="stand")
        try:
            dark = {str(element_id): 0 for element_id in LIGHTING_ELEMENT_IDS}
            assert harness.snapshot()["physical_lighting"] == dark
            client.ready(first_name=private_name)
            ready = await boundary.step()
            assert ready["reason"] == "profile_ready"
            assert harness.snapshot()["canonical_state"]["mode"] == "profile_ready"
            assert harness.stand.application._canonical_profiles["stand"]["display_name"] == private_name
            public = json.dumps(harness.stand.panel_snapshot(), ensure_ascii=False)
            assert private_name not in public
            assert "patient_profile" not in public
            client.profile_cleared()
            cleared = await boundary.step()
            assert cleared["reason"] == "profile_cleared"
            assert harness.snapshot()["canonical_state"]["mode"] == "idle"
            assert harness.snapshot()["physical_lighting"] == dark
            assert not harness.stand.application._canonical_profiles
            assert boundary.facts is None
        finally:
            await harness.stand.application.shutdown_identity()
            harness.close()
    asyncio.run(scenario())
