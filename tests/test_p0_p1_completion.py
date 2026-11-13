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
