"""Actual story state owns button affordances in both stand and simulator panels."""
import pytest

from test_stand_menu_enabled_buttons import MenuStand


def assert_buttons(h, expected):
    panel = h.stand.panel_snapshot()
    assert panel['session']['enabled_buttons'] == expected
    assert [b['id'] for b in panel['simulator']['devices']['buttons'] if b['enabled']] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['recorded', 'authored'])
@pytest.mark.parametrize('confirmed', [False, True])
async def test_story_buttons_follow_playing_announcement_and_failed_pause_checkpoint(tmp_path, kind, confirmed):
    h = MenuStand(tmp_path, '3-5')
    # Synthetic buffered PCM port; no browser websocket is attached in this test.
    h.app.audio._private_stream_play = None
    from app.foundation.s2s import AudioChunk
    async def synth(text, token, cancelled):
        return AudioChunk((.1,) * 16000, 16000, token)
    h.app.bounded_s2s.tts.synthesize = synth
    try:
        await h.key(2 if kind == 'recorded' else 3)
        await h.key(1)
        assert h.state.mode == ('fixed_story_playing' if kind == 'recorded' else 'personal_story_playing')
        assert_buttons(h, [5, 6, 7])
        story = h.state.active_story_id
        async def pause():
            return {'story_id': story, 'offset_ms': 100} if confirmed else None
        h.app.audio.pause = pause
        await h.key(6)
        assert h.state.mode == 'story_pause_announcement'
        assert_buttons(h, [5, 7])
        await h.done()
        assert h.state.mode == 'story_paused'
        assert_buttons(h, [5, 6, 7] if confirmed else [5, 7])
        if confirmed:
            await h.key(6)
            assert h.state.mode == 'story_resume_announcement'
            assert_buttons(h, [5, 7])
            await h.done()
            assert h.state.mode == ('fixed_story_playing' if kind == 'recorded' else 'personal_story_playing')
            assert_buttons(h, [5, 6, 7])
            await h.key(6)
            await h.done()
            await h.key(6)
            assert h.state.mode == 'story_resume_announcement'
            old = dict(h.app._active_audio['stand'])
            failed = {**old, 'response_id': old['content_id']}
            from app.foundation.domain import Event
            h.stand._audio_event_sequence += 1
            seq = h.stand._audio_event_sequence
            await h.app.receive_async(Event('AudioFailed', 'stand', seq, h.app.now(), 'synthetic_pcm_port', failed))
            assert h.state.mode == 'story_paused'
            assert_buttons(h, [5, 6, 7])
        await h.key(7)
        assert h.state.mode not in {'story_paused', 'story_pause_announcement', 'story_resume_announcement'}
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['recorded', 'authored'])
@pytest.mark.parametrize('phase', ['announcement', 'unknown_checkpoint'])
async def test_priority_help_remains_advertised_and_works_during_story_wait(tmp_path, kind, phase):
    h = MenuStand(tmp_path, '3-5')
    h.app.audio._private_stream_play = None
    from app.foundation.s2s import AudioChunk
    async def synth(text, token, cancelled):
        return AudioChunk((.1,) * 16000, 16000, token)
    h.app.bounded_s2s.tts.synthesize = synth
    try:
        await h.key(2 if kind == 'recorded' else 3)
        await h.key(1)
        async def pause():
            return None
        h.app.audio.pause = pause
        await h.key(6)
        if phase == 'unknown_checkpoint':
            await h.done()
            assert h.state.mode == 'story_paused'
        else:
            assert h.state.mode == 'story_pause_announcement'
        assert_buttons(h, [5, 7])
        await h.key(5)
        assert h.state.mode == 'wellbeing_check'
        assert_buttons(h, [1, 5, 7])
        assert not h.app._personal_stories
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_story_speech_analysis_only_advertises_its_actual_pause_and_exit(tmp_path):
    h = MenuStand(tmp_path, '3-5')
    try:
        await h.key(2)
        await h.key(1)
        assert h.state.mode == 'fixed_story_playing'
        await h.harness.speech_result('Бла-бла, самолёты')
        assert h.state.mode == 'story_speech_analysis'
        assert_buttons(h, [6, 7])
        await h.key(6)
        assert h.state.mode == 'story_pause_announcement'
        assert_buttons(h, [5, 7])
    finally:
        await h.close()
