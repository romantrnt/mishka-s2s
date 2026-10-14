from __future__ import annotations

import asyncio

import pytest

from app.foundation.config import Settings
from app.foundation.input_adapters import KeyboardHIDInputAdapter
from app.foundation.lighting import LIGHTING_ELEMENT_IDS
from app.foundation.stand import HardwareIntegrationStand


CHILD_TAG = "mock-child-001"


def test_hid_number_press_release_and_space_autorepeat_are_normalized_once():
    events = []
    adapter = KeyboardHIDInputAdapter("/dev/null", lambda event: events.append((event.type, event.payload)), debounce_ms=0)
    adapter.feed_key(2, 1, timestamp=1.0)
    adapter.feed_key(2, 2, timestamp=1.1)
    adapter.feed_key(2, 0, timestamp=1.2)
    adapter.feed_key(57, 1, timestamp=2.0)
    adapter.feed_key(57, 2, timestamp=2.1)
    adapter.feed_key(57, 0, timestamp=2.2)
    assert events == [
        ("ButtonPressed", {"button_id": 1}),
        ("ButtonReleased", {"button_id": 1}),
        ("SpacePressed", {}),
        ("SpaceReleased", {}),
    ]


@pytest.mark.xfail(strict=True, reason="stale/contradictory: exhibition runtime intentionally rejects clinical RFID with exhibition_rfid_ignored; AUD-02 does not alter exhibition isolation")
def test_canonical_p0_wave_space_and_contextual_story_flow():
    async def scenario():
        stand = HardwareIntegrationStand(settings=Settings(profile="mock", hardware_mode="fake"))
        try:
            first = await stand.inject("rfid", {"tag_id": CHILD_TAG}, "completion")
            assert first["reason"] == "rfid_identified"
            assert stand.snapshot()["lighting"] == {str(element_id): 1 for element_id in LIGHTING_ELEMENT_IDS}
            repeated = await stand.inject("rfid", {"tag_id": CHILD_TAG}, "completion")
            assert repeated["reason"] == "rfid_repeated"
            assert repeated["outputs"] == []

            stand._physical_sequence += 1
            await stand.inject_physical({"type": "SpacePressed"})
            assert stand.snapshot()["session"]["node"] == "automatic_mood_check"
            await stand.inject("button", {"button_id": 4}, "completion")
            assert stand.snapshot()["session"]["node"] == "root_menu"
            await stand.inject("button", {"button_id": 2}, "completion")
            assert stand.snapshot()["session"]["node"] == "fixed_story_menu"
            started = await stand.inject("button", {"button_id": 1}, "completion")
            assert started["state"] == "fixed_story_playing"
            paused = await stand.inject("button", {"button_id": 6}, "completion")
            assert paused["state"] == "story_paused"
            resumed = await stand.inject("button", {"button_id": 6}, "completion")
            assert resumed["state"] == "fixed_story_playing"
            exited = await stand.inject("button", {"button_id": 7}, "completion")
            assert exited["state"] == "root_menu"

            await stand.reset("completion")
            assert all(value == 0 for value in stand.snapshot()["lighting"].values())
        finally:
            stand.close()

    asyncio.run(scenario())
