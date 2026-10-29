from __future__ import annotations

import asyncio
import json

import pytest

from app.foundation.config import Settings
from app.foundation.identity import IdentityDecision, SyntheticIdentityResolver
from app.foundation.lighting import LIGHTING_ELEMENT_IDS
from app.foundation.simulator import SimulatorAcceptanceHarness


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


def test_synthetic_card_helpers_project_backend_ready_and_dark_removal():
    async def scenario():
        uid = "001-SYNTHETIC-PRIVATE-CARD"
        private_name = "SYNTHETIC-PRIVATE-NAME"
        resolver = SyntheticIdentityResolver({uid: IdentityDecision(
            "authorized", {"profile_ref": "fixture-subject", "display_name": private_name},
            frozenset({"personalize"}), 60.0,
        )}, clock=lambda: 0.0)
        harness = SimulatorAcceptanceHarness(identity_resolver=resolver)
        try:
            assert harness.stand.application.identity_resolver is resolver
            dark = {str(element_id): 0 for element_id in LIGHTING_ELEMENT_IDS}
            assert harness.snapshot()["physical_lighting"] == dark
            await harness.present_card(uid, wait_for_lookup=True)
            assert harness.snapshot()["canonical_state"]["mode"] == "rfid_ready"
            public = json.dumps(harness.stand.panel_snapshot(), ensure_ascii=False)
            assert uid not in public and private_name not in public
            await harness.remove_card()
            assert harness.snapshot()["canonical_state"]["mode"] == "idle"
            assert harness.snapshot()["physical_lighting"] == dark
        finally:
            harness.close()
    asyncio.run(scenario())


def test_pending_synthetic_lookup_does_not_hold_hangup():
    class GatedResolver:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def resolve(self, uid, installation_id, request_id):
            self.started.set()
            await self.release.wait()
            return IdentityDecision("authorized", {"profile_ref": "late-subject"},
                                    frozenset({"personalize"}), 60.0)

        def readiness(self):
            return {"provider": "synthetic", "ready": True, "synthetic": True, "production_ready": False}

    async def scenario():
        resolver = GatedResolver()
        harness = SimulatorAcceptanceHarness(identity_resolver=resolver)
        try:
            await asyncio.wait_for(harness.present_card("fixture-pending"), timeout=1.0)
            await asyncio.wait_for(resolver.started.wait(), timeout=1.0)
            assert harness.snapshot()["canonical_state"]["mode"] == "rfid_lookup"
            await asyncio.wait_for(harness.space_released(), timeout=1.0)
            await asyncio.wait_for(harness.space_pressed(), timeout=1.0)
            resolver.release.set()
            await asyncio.sleep(0)
            assert harness.snapshot()["canonical_state"]["mode"] == "idle"
            assert all(value == 0 for value in harness.snapshot()["physical_lighting"].values())
        finally:
            harness.close()
    asyncio.run(scenario())
