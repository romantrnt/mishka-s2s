"""Fixed-story activity and stale playback callbacks across idle and re-entry."""
from copy import deepcopy

import pytest

from test_scene_menu_runtime import Harness


@pytest.mark.asyncio
async def test_fixed_story_pause_idle_and_stale_completion_after_reentry():
    h = Harness()
    clock = [100.0]
    h.app.monotonic_now = lambda: clock[0]
    h.app.now = lambda: clock[0]
    h.app.canonical_runtime.monotonic_now = lambda: clock[0]
    sid = h.sid

    def ownership_snapshot():
        return deepcopy((h.state.snapshot(), h.app._engagement_owners.get(sid),
                         h.app._active_audio.get(sid)))

    try:
        await h.button(2); await h.release(2)
        await h.button(1); await h.release(1)  # Skip menu narration.
        await h.button(1); await h.release(1)  # Select its first verified story.
        assert h.state.mode == 'fixed_story_playing'
        assert h.state.active_story_id == 'fixed_aibolit_v1'
        old_story = dict(h.app._active_audio[sid])
        old_completion = {**old_story, 'response_id': old_story['content_id']}

        clock[0] = 500.0
        assert await h.app.expire_engagement_sessions() == []
        assert h.state.mode == 'fixed_story_playing' and not h.state.interaction_idle

        result = await h.button(6); await h.release(6)
        assert result['reason'] == 'story_pause_requested'
        assert h.state.active_audio_id == 'PHRASE_062'
        clock[0] = 510.0  # Waiting starts after the announcement, not the button.
        await h.complete()
        assert h.state.mode == 'story_paused'
        assert h.app._engagement_owners[sid]['deadline'] == 630.0

        clock[0] = 629.999
        assert await h.app.expire_engagement_sessions() == []
        clock[0] = 630.0
        assert (await h.app.expire_engagement_sessions())[0]['reason'] == 'interaction_idle'
        assert h.state.interaction_idle and h.state.mode == 'root_menu'
        assert h.state.handset == 'up' and sid not in h.app._active_audio
        assert not any(h.app.led.output.snapshot().values())

        await h.button(2); await h.release(2)
        assert h.state.mode == 'fixed_story_menu' and not h.state.interaction_idle
        before = ownership_snapshot()
        result = await h.send('AudioCompleted', old_completion)
        assert result['reason'] == 'stale_exhibition_audio_completion'
        assert ownership_snapshot() == before

        await h.send('HandsetPlaced')
        assert sid not in h.app.canonical_runtime.sessions
        assert h.app.sessions.get(sid) is None
        result = await h.send('AudioCompleted', old_completion)
        assert result['reason'] == 'stale_exhibition_audio_completion'
        assert sid not in h.app.canonical_runtime.sessions
        assert h.app.sessions.get(sid) is None

        await h.send('HandsetLifted')
        assert h.state.handset == 'up'
        before = ownership_snapshot()
        result = await h.send('AudioCompleted', old_completion)
        assert result['reason'] == 'stale_exhibition_audio_completion'
        assert ownership_snapshot() == before
    finally:
        await h.close()
