"""The simulator advertises the same onboarding controls as the controller."""
import pytest

from app.foundation.config import Settings
from app.foundation.stand import HardwareIntegrationStand
from test_child_button_access import ChildHarness


@pytest.mark.asyncio
@pytest.mark.parametrize("question_failed", [False, True])
async def test_onboarding_panel_only_enables_current_nonverbal_choices(question_failed):
    h = ChildHarness()
    stand = HardwareIntegrationStand(Settings(profile="mock"), background_scheduler=False)
    original_app = stand.application
    try:
        await h.name(hint=False)
        stand.application = h.app
        record = h.app.sessions.get(h.sid)
        assert stand._enabled_buttons(record) == [5, 7]
        await h.complete(failed=question_failed)
        assert stand._enabled_buttons(record) == [5, 7]
        await h.button(7)
        assert stand._enabled_buttons(record) == list(range(1, 8))
        await h.release(7)
        await h.button(3)
        assert h.state.age_years == 3
        assert stand._enabled_buttons(record) == []
    finally:
        stand.application = original_app
        await h.close()
        await stand.aclose()
