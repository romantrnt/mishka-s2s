"""A valid private lease cannot keep speaking after leaving its day scene."""
from dataclasses import replace

import pytest

from test_owned_private_pcm import setup, install, HANDLE


@pytest.mark.asyncio
@pytest.mark.parametrize("response_id", ["TYPED_WHAT_NEXT", "TYPED_WHERE_NOW"])
async def test_private_schedule_owner_is_revoked_by_scene_change(response_id):
    stand = await setup()
    try:
        state = stand.application.canonical_runtime.sessions[HANDLE.session_id]
        state.mode = "schedule_weather"
        state.identity_capabilities = ["schedule"]
        handle = replace(HANDLE, response_id=response_id)
        install(stand, handle)
        identity = (state.profile_ref, state.identity_request_id)
        assert stand._pcm_owner_current(handle, identity)
        # The identity and exact lease remain valid; scene ownership is distinct.
        state.mode = "root_menu"
        assert not stand._pcm_owner_current(handle, identity)
    finally:
        await stand.aclose()
