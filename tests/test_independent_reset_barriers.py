"""Real application routing with synthetic HID and a held reset completion."""
import asyncio

import pytest

from app.foundation.config import Settings
from app.foundation.input_adapters import NormalizedInputEvent
from app.foundation.stand import HardwareIntegrationStand


@pytest.mark.asyncio
async def test_cancelled_overflow_waiters_cannot_cancel_shared_reset_or_hangup():
    stand = HardwareIntegrationStand(Settings(profile="mock"), background_scheduler=False)
    stand._physical_loop = asyncio.get_running_loop()
    stand._hid_generation = 1
    stand._admits_physical_users = lambda: True
    barrier = asyncio.get_running_loop().create_future()
    try:
        await stand.inject_physical({"type": "SpaceReleased", "_source": "physical_hid", "_hid_generation": 1})
        sid = stand.active_session_id
        assert stand.application.canonical_runtime.sessions[sid].handset == "up"
        stand.application._session_reset_waiters[sid] = (asyncio.current_task(), barrier)
        button = NormalizedInputEvent("ButtonPressed", {"button_id": 1})
        for _ in range(64):
            stand._on_physical_input(button, 1)
        for _ in range(5):
            await asyncio.sleep(0)
        stand._on_physical_input(button, 1)
        stand._on_physical_input(NormalizedInputEvent("SpacePressed", {}), 1)
        for _ in range(30):
            await asyncio.sleep(0)
        assert not barrier.cancelled()
        loss = stand._hid_loss_future
        assert loss is not None and not loss.cancelled()
        barrier.set_result(None)
        await asyncio.wait_for(asyncio.wrap_future(loss), 2)
        for _ in range(40):
            if not stand._physical_pending_count:
                break
            await asyncio.sleep(.01)
        assert stand._physical_pending_count == 0
        state = stand.application.canonical_runtime.sessions.get(sid)
        assert state is None or state.handset == "down"
    finally:
        if not barrier.done():
            barrier.set_result(None)
        stand.application._session_reset_waiters.clear()
        await stand.aclose()
