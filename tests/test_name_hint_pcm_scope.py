"""The real Stand admits only the current public onboarding hint's PCM."""
from dataclasses import replace

import pytest

from app.foundation.config import Settings
from app.foundation.stand import HardwareIntegrationStand
from test_child_button_access import ChildHarness


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["skip", "hangup"])
async def test_name_hint_pcm_is_playable_and_revoked_by_real_navigation(ending):
    h = ChildHarness()
    stand = HardwareIntegrationStand(Settings(profile="mock"), background_scheduler=False)
    original_app = stand.application
    try:
        await h.name()
        stand.application = h.app
        stand.active_session_id = h.sid
        handle = h.played[-1]
        assert handle.response_id == "NAME_BUTTON_HINT"
        assert stand._pcm_owner_current(handle, None)
        assert not stand._pcm_owner_current(replace(handle, token=handle.token + 1), None)
        # Even if a same-mode lease survives a concern, it must not grant output.
        h.state.safety_severity = "concern"
        assert not stand._pcm_owner_current(handle, None)
        h.state.safety_severity = "none"
        assert stand._pcm_owner_current(handle, None)
        if ending == "skip":
            await h.button(7)
            assert h.state.mode == "exhibition_age_prompt"
        else:
            await h.send("HandsetPlaced")
        assert not stand._pcm_owner_current(handle, None)
    finally:
        stand.application = original_app
        await h.close()
        await stand.aclose()
