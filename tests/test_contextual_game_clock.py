"""A nested active game pauses the caller's technical clock, not engagement idle."""
from types import SimpleNamespace
import time

import pytest

from app.foundation import s2s
from test_contextual_game_flow import Harness


@pytest.mark.asyncio
async def test_long_active_game_preserves_remaining_caller_time_without_reset(monkeypatch):
    h = Harness(band="6-7")
    # Replace only this module binding; asyncio's real clock remains untouched.
    clock = SimpleNamespace(**vars(time))
    clock.monotonic = lambda: h.clock
    monkeypatch.setattr(s2s, "time", clock)
    try:
        context = h.app._bounded_dialogue_context(h.state.session_id)
        context.started_at = h.clock - 40
        await h.send("SpeechReceived", text="Какого цвета солнце?")
        await h.launch("free_lights")
        for button in (2, 3):
            h.clock += 75
            await h.key(button)
        assert h.state.mode == "game_active"
        await h.key(7, duration=6)
        await h.done()
        restored = h.app.conversation_contexts[h.state.session_id]
        assert 40 <= h.clock - restored.started_at < 41
        turns = restored.turns
        await h.send("SpeechReceived", text="А какого цвета трава?")
        assert h.state.mode == "bear_dialogue"
        assert h.app.conversation_contexts[h.state.session_id].turns == turns + 1
        # The pre-game 40 seconds are retained; no fresh 120-second budget appears.
        h.clock += 81
        await h.send("SpeechReceived", text="Давай ещё поговорим")
        assert h.state.mode == "root_menu"
    finally:
        await h.close()
