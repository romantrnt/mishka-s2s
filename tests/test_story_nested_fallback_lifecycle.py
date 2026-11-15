"""Independent barrier at return from nested story fallback, not during TTS."""
import asyncio
import pytest
from test_personal_story_app import start_app, enter, send

@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["HandsetPlaced", "SessionReset", "HardwareDisconnected"])
async def test_nested_story_fallback_cannot_commit_after_lifecycle_end(action):
    app = await start_app()
    entered, release = asyncio.Event(), asyncio.Event()
    original = app._receive_canonical_event

    async def wrapped(event, *args, **kwargs):
        result = await original(event, *args, **kwargs)
        if event.event_type == "DialogueFallback" and event.source == "personal_story_unavailable":
            entered.set()
            await release.wait()
        return result

    app._receive_canonical_event = wrapped

    def unavailable(*args):
        raise RuntimeError("synthetic unavailable")

    app.audio.play_pcm_owned = unavailable
    pending = None
    try:
        await enter(app)
        pending = asyncio.create_task(send(app, "ButtonPressed", {"button_id": 1}))
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(send(app, action), 2)
        release.set()
        result = await asyncio.wait_for(pending, 2)
        assert result["reason"] == "stale_session_generation"
        assert not app._personal_stories
        assert not app._active_audio.get("story-test")
    finally:
        release.set()
        if pending:
            await asyncio.gather(pending, return_exceptions=True)
        await app.shutdown_identity()
