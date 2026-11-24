"""New game prompts reuse the existing exact owned-PCM playback boundary."""
from dataclasses import replace

import pytest

from test_owned_private_pcm import setup, install, HANDLE


@pytest.mark.asyncio
@pytest.mark.parametrize("response_id, mode", [
    ("DIALOGUE_INTRO", "bear_dialogue"),
    ("DIALOGUE_OFFER", "bear_dialogue"),
    ("DIALOGUE_RETURN", "bear_dialogue"),
    ("DIALOGUE_FINISH", "root_menu"),
])
async def test_dialogue_pcm_keeps_exact_lease_and_scene_owner(response_id, mode):
    stand = await setup()
    try:
        state = stand.application.canonical_runtime.sessions[HANDLE.session_id]
        state.mode = mode
        state.pending_game_offer = "quiz" if response_id == "DIALOGUE_OFFER" else None
        state.game_offer_ready = False
        handle = replace(HANDLE, response_id=response_id)
        install(stand, handle)
        assert stand._pcm_owner_current(handle, None)
        assert not stand._pcm_owner_current(replace(handle, token=handle.token + 1), None)
        assert not stand._pcm_owner_current(replace(handle, playback_lease="obsolete"), None)
        if response_id == "DIALOGUE_OFFER":
            state.game_offer_ready = True
            assert not stand._pcm_owner_current(handle, None)
            state.game_offer_ready = False
        state.mode = "game_active"
        assert not stand._pcm_owner_current(handle, None)
    finally:
        await stand.aclose()
