"""Clinical on-hook games/menus retain their usable lighting when voice joins."""
import pytest

from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event


@pytest.mark.asyncio
@pytest.mark.parametrize("buttons,expected_mode", [
    ([1], "game_menu"), ([2], "fixed_story_menu"),
    ([4], "personal_story_theme"), ([5], "wellbeing_check"),
    ([7], "schedule_weather"), ([1, 1], "game_active"),
])
async def test_lift_does_not_replace_visual_activity_controls(buttons, expected_mode):
    app, *_ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"), background_scheduler=False)
    sid = "visual-join"
    sequence = 0

    async def send(kind, payload=None):
        nonlocal sequence
        sequence += 1
        return await app.receive_async(Event(kind, sid, sequence, 1., "test", payload or {},
                                             f"join:{sequence}", 1.))

    try:
        for button in buttons:
            await send("ButtonPressed", {"button_id": button})
            await send("ButtonReleased", {"button_id": button})
        state = app.canonical_runtime.sessions[sid]
        assert state.mode == expected_mode
        assert state.handset == "down"
        before = dict(app.led.output.snapshot())
        assert any(before.values()), "visual controls must be visible before voice joins"
        result = await send("HandsetLifted")
        assert result["canonical_state"]["mode"] == expected_mode
        assert result["canonical_state"]["handset"] == "up"
        assert dict(app.led.output.snapshot()) == before
        assert any(effect.get("type") == "capture_open" for effect in result["outputs"])
    finally:
        await app._cleanup_session_resources(sid)
