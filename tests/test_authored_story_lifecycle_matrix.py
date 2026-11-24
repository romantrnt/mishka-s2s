"""All advertised authored choices reach owned PCM; application-clock idle boundary."""
import re

import pytest

from app.foundation.s2s import AudioChunk
from test_scene_menu_runtime import Harness


@pytest.mark.asyncio
@pytest.mark.parametrize('band', ['1-3', '3-5', '5-7'])
@pytest.mark.parametrize('slot', [1, 2, 3, 4, 5])
async def test_each_advertised_authored_slot_plays_its_exact_age_text_and_can_finish(band, slot):
    h = Harness(band)
    h.state.personal_story_seed = 'authored-lifecycle-matrix'
    try:
        await h.button(3)
        await h.release(3)
        items = h.state.scene_menu_descriptor['items']
        assert [item['slot'] for item in items] == [1, 2, 3, 4, 5]
        item = items[slot - 1]
        selected = next(entry for entry in h.state.personal_story_selection if entry['id'] == item['id'])
        assert item['title'] == selected['title']
        before = len(h.render.texts)
        result = await h.button(slot)
        await h.release(slot)
        assert result['reason'] == 'personal_story_ready'
        assert h.state.mode == 'personal_story_playing'
        handle = h.played[-1]
        assert handle.response_id == 'PERSONAL_STORY'
        assert handle.playback_lease == h.state.playback_lease
        spoken = ''.join(h.render.texts[before:])
        assert re.sub(r'\s+', '', spoken) == re.sub(r'\s+', '', selected['text'].replace('{name}', 'друг'))
        assert h.app._personal_stories[h.sid].audio.samples
        old = dict(h.app._active_audio[h.sid])
        await h.complete()
        assert h.state.mode == 'root_menu'
        assert not h.app._personal_stories and h.state.active_story_id is None
        before_state = h.state.snapshot()
        stale = await h.send('AudioCompleted', {**old, 'response_id': old['content_id']})
        assert stale['reason'] == 'stale_exhibition_audio_completion'
        assert h.state.snapshot() == before_state
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_authored_active_playback_is_not_idle_after_120_seconds_without_user_input():
    h = Harness()
    clock = [100.]
    h.app.monotonic_now = h.app.now = h.app.canonical_runtime.monotonic_now = lambda: clock[0]
    async def synth(text, token, cancelled):
        return AudioChunk((.1,) * 16000, 16000, token)
    h.render.synthesize = synth
    try:
        await h.button(3)
        await h.release(3)
        await h.button(1)
        await h.release(1)
        assert h.state.mode == 'personal_story_playing'
        lease = dict(h.app._active_audio[h.sid])
        owner = h.app._personal_stories[h.sid]
        clock[0] = 221.
        assert await h.app.expire_engagement_sessions() == []
        assert h.state.mode == 'personal_story_playing' and not h.state.interaction_idle
        assert h.app._active_audio[h.sid] == lease
        assert h.app._personal_stories[h.sid] is owner
        await h.button(7)
        await h.release(7)
        assert h.state.mode == 'root_menu' and not h.app._personal_stories
    finally:
        await h.close()
