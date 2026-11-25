"""Native capture queues cannot reinterpret NAME samples as an AGE answer."""
import asyncio

import pytest

from app.foundation.config import Settings
from app.foundation.stand import HardwareIntegrationStand
from test_child_button_access import ChildHarness


@pytest.mark.asyncio
async def test_queued_name_frame_is_revoked_but_fresh_age_frame_is_dispatched():
    h = ChildHarness()
    stand = HardwareIntegrationStand(Settings(profile="mock", headless_turn_taking=True),
                                     background_scheduler=False)
    original_app = stand.application
    dispatched = []
    try:
        await h.name()
        await h.complete()
        assert h.state.mode == "exhibition_name_capture" and not h.state.active_audio_id
        stand.application = h.app
        stand.active_session_id = h.sid
        stand.hardware_mode = "physical"
        stand._headless_audio_queue = asyncio.Queue(maxsize=64)
        old_owner = stand._headless_frame_owner()
        stand._enqueue_headless_audio_frame((0.1, 0.2), old_owner)
        assert stand._headless_audio_queue.qsize() == 1

        assert (await h.button(7))["reason"] == "exhibition_name_skipped_to_age"
        await h.send("ButtonReleased", {"button_id": 7})
        await h.complete()
        assert h.state.mode == "exhibition_age_selection" and h.state.age_years is None
        assert not old_owner[1]() and old_owner[1].lifecycle_current()

        async def capture(samples, rate, sid, **kwargs):
            dispatched.append((tuple(samples), h.state.mode, kwargs["_ingress_guard"]()))
            return {}

        stand.inject_pcm = capture
        fresh_owner = stand._headless_frame_owner()
        assert fresh_owner[1]()
        stand._enqueue_headless_audio_frame((0.3, 0.4), fresh_owner)
        await stand._headless_audio_queue.put(None)
        await stand._headless_audio_worker()
        assert len(dispatched) == 1
        assert dispatched[0][0] == pytest.approx((0.3, 0.4))
        assert dispatched[0][1:] == ("exhibition_age_selection", True)
        assert h.state.age_years is None  # This is a transport test, not an ASR substitute.
        await h.send("HandsetPlaced")
        assert not fresh_owner[1].lifecycle_current()
    finally:
        stand.hardware_mode = "simulator"
        stand.application = original_app
        await h.close()
        await stand.aclose()
