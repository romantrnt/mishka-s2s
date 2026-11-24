"""Actual story controls recover a failed courtesy recording; synthetic PCM only."""
import pytest

from app.foundation.domain import Event
from app.foundation.s2s import AudioChunk
from test_scene_menu_runtime import Harness


async def playing(kind, *, confirmed=True):
    h = Harness()
    h.clock = [100.]
    h.app.monotonic_now = h.app.now = h.app.canonical_runtime.monotonic_now = lambda: h.clock[0]
    async def synth(text, token, cancelled):
        return AudioChunk((.1,) * 16000, 16000, token)
    h.render.synthesize = synth
    await h.button(2 if kind == 'recorded' else 3)
    await h.release(2 if kind == 'recorded' else 3)
    await h.button(1)
    await h.release(1)
    assert h.state.mode == ('fixed_story_playing' if kind == 'recorded' else 'personal_story_playing')
    story = h.state.active_story_id
    async def pause():
        return {'story_id': story, 'offset_ms': 250} if confirmed else None
    h.app.audio.pause = pause
    return h


async def pause(h, *, resume_announcement=False):
    await h.button(6)
    await h.release(6)
    assert h.state.mode == 'story_pause_announcement'
    if resume_announcement:
        await h.complete()
        await h.button(6)
        await h.release(6)
        assert h.state.mode == 'story_resume_announcement'
    return dict(h.app._active_audio[h.sid])


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['recorded', 'authored'])
@pytest.mark.parametrize('phase', ['pause', 'resume'])
async def test_failed_announcement_retains_exact_checkpoint_and_button_six_resumes_without_repeating_phrase(kind, phase):
    h = await playing(kind)
    try:
        old = await pause(h, resume_announcement=phase == 'resume')
        story = h.state.active_story_id
        checkpoint = dict(h.state.dialogue_memory.story_checkpoint)
        assert checkpoint == {'story_id': story, 'offset_ms': 250} and h.state.story_checkpoint_confirmed
        h.clock[0] = 110.
        await h.complete(failed=True)
        assert h.state.mode == 'story_paused'
        assert h.state.dialogue_memory.story_checkpoint == checkpoint
        assert h.state.expected_inputs == ['button_6', 'button_7', 'handset_down']
        assert h.state.audio_status == 'failed' and not h.app._active_audio
        assert h.app._engagement_owners[h.sid]['deadline'] == 230.
        await h.release(6)
        assert h.state.audio_failure_code == 'story_announcement_unavailable'
        await h.button(6)
        resumed_press_sequence = h.seq
        assert h.state.mode == ('fixed_story_playing' if kind == 'recorded' else 'personal_story_playing')
        assert h.app._active_audio[h.sid]['content_id'] == story
        assert h.app._active_audio[h.sid]['playback_id'] != old['playback_id']
        assert h.state.dialogue_memory.story_checkpoint == checkpoint
        assert h.state.audio_failure_code is None
        assert h.state.expected_inputs == ['button_6', 'button_7', 'handset_down']
        if kind == 'authored':
            assert h.app._personal_stories[h.sid].offset_ms == 250
        else:
            assert any(effect.get('type') == 'story_audio_resume' and effect.get('offset_ms') == 250
                       for effect in h.app.last_canonical_effects)
        resumed = dict(h.app._active_audio[h.sid])
        checkpoint = dict(h.state.dialogue_memory.story_checkpoint)
        await h.send('AudioFailed', {**old, 'response_id': old['content_id']})
        assert h.app._active_audio[h.sid] == resumed
        assert h.state.dialogue_memory.story_checkpoint == checkpoint
        assert h.state.mode == ('fixed_story_playing' if kind == 'recorded' else 'personal_story_playing')
        # Retried transport delivery of the same button cannot pause it again.
        seq = resumed_press_sequence
        event = Event('ButtonPressed', h.sid, seq, 1., 'test', {'button_id': 6}, str(seq))
        await h.app.receive_async(event)
        assert h.app._active_audio[h.sid] == resumed
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['recorded', 'authored'])
@pytest.mark.parametrize('position', ['unknown', 'other_story'])
async def test_unknown_checkpoint_never_resumes_zero_and_still_has_button_seven_exit(kind, position):
    h = await playing(kind, confirmed=position == 'other_story')
    try:
        await pause(h)
        if position == 'other_story':
            h.state.dialogue_memory.story_checkpoint['story_id'] = 'SYNTHETIC_OTHER_STORY'
        await h.complete(failed=True)
        assert h.state.mode == 'story_paused'
        assert h.state.expected_inputs == ['button_7', 'handset_down']
        played = list(h.played)
        before = dict(h.state.dialogue_memory.story_checkpoint)
        await h.button(6)
        await h.release(6)
        await h.button(6)
        await h.release(6)
        await h.send('SpeechReceived', text='Продолжи сказку')
        assert h.state.mode == 'story_paused' and not h.app._active_audio
        assert h.state.dialogue_memory.story_checkpoint == before and h.played == played
        await h.button(7)
        assert h.state.mode == ('fixed_story_choice_wave' if kind == 'recorded' else 'root_menu')
        assert not h.app._personal_stories
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['recorded', 'authored'])
async def test_failed_announcement_starts_exact_paused_wait_and_late_failure_cannot_extend_it(kind):
    h = await playing(kind)
    try:
        old = await pause(h)
        h.clock[0] = 110.
        await h.complete(failed=True)
        assert h.state.mode == 'story_paused' and h.app._engagement_owners[h.sid]['deadline'] == 230.
        h.clock[0] = 229.999
        assert await h.app.expire_engagement_sessions() == []
        await h.send('AudioFailed', {**old, 'response_id': old['content_id']})
        assert h.app._engagement_owners[h.sid]['deadline'] == 230.
        h.clock[0] = 230.
        assert (await h.app.expire_engagement_sessions())[0]['reason'] == 'interaction_idle'
        assert h.state.mode == 'root_menu' and h.state.interaction_idle
        assert not h.app._active_audio and not h.app._personal_stories
        assert h.state.dialogue_memory.story_checkpoint is None
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['recorded', 'authored'])
@pytest.mark.parametrize('boundary', ['HandsetPlaced', 'SessionReset'])
async def test_failed_announcement_cannot_reenter_new_visit_after_terminal_boundary(kind, boundary):
    h = await playing(kind)
    try:
        old = await pause(h, resume_announcement=True)
        await h.complete(failed=True)
        assert h.state.mode == 'story_paused'
        await h.send(boundary)
        assert not h.app._personal_stories and not h.app._active_audio
        await h.send('HandsetLifted')
        fresh = h.state
        before = fresh.snapshot()
        audio = dict(h.app._active_audio)
        await h.send('AudioFailed', {**old, 'response_id': old['content_id']})
        assert h.state is fresh and h.state.snapshot() == before
        assert h.app._active_audio == audio
        assert not h.app._personal_stories
    finally:
        await h.close()


def test_graph_announcement_recovery_does_not_allow_unrelated_voice_or_audio_transitions():
    from app.scenarios.canonical_graph import CanonicalLangGraphAuthorizer
    graph = CanonicalLangGraphAuthorizer()
    for phase in ('story_pause_announcement', 'story_resume_announcement'):
        assert graph.authorize(phase, 'story_paused', 'AudioFailed')['ok']
        assert not graph.authorize(phase, 'personal_story_playing', 'AudioFailed')['ok']
    for target in ('fixed_story_playing', 'personal_story_playing'):
        assert graph.authorize('story_paused', target, 'ButtonPressed')['ok']
        assert graph.authorize('story_paused', target, 'VoiceIntentReady', intent='continue')['ok']
        assert not graph.authorize('story_paused', target, 'VoiceIntentReady', intent='unknown')['ok']
        assert not graph.authorize('story_paused', target, 'AudioFailed')['ok']
    for event in ('StoryPositionUpdated', 'ClientProfileCleared', 'IdentityAuthorizationExpired'):
        assert graph.authorize('story_paused', 'fixed_story_playing', event)['ok']


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['recorded', 'authored'])
async def test_failed_nested_announcement_retains_caller_until_explicit_button_seven(kind):
    h = await playing(kind)
    try:
        # Return-marker fixture only; story entry, pause, failure and buttons are real app events.
        h.state.activity_return_mode = 'bear_dialogue'
        await pause(h, resume_announcement=True)
        await h.complete(failed=True)
        assert h.state.mode == 'story_paused' and h.state.activity_return_mode == 'bear_dialogue'
        checkpoint = dict(h.state.dialogue_memory.story_checkpoint)
        await h.button(6)
        await h.release(6)
        assert h.state.mode == ('fixed_story_playing' if kind == 'recorded' else 'personal_story_playing')
        assert h.state.dialogue_memory.story_checkpoint == checkpoint
        assert h.state.activity_return_mode == 'bear_dialogue'
        await h.button(7)
        assert h.state.mode == 'bear_dialogue' and h.state.activity_return_mode is None
        assert not h.app._personal_stories
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['recorded', 'authored'])
async def test_checkpoint_loss_during_resume_announcement_cannot_queue_story_or_roll_back_to_wait(kind):
    h = await playing(kind)
    try:
        await pause(h, resume_announcement=True)
        before = list(h.played)
        # Explicit fault injection after real 064 admission, never a fabricated new position.
        h.state.story_checkpoint_confirmed = False
        h.state.dialogue_memory.story_checkpoint = None
        result = await h.complete()
        assert result['reason'] == 'story_checkpoint_pending'
        assert h.state.mode == 'story_paused' and not h.app._active_audio
        assert h.state.expected_inputs == ['button_7', 'handset_down']
        assert h.state.dialogue_memory.story_checkpoint is None and h.played == before
        await h.button(6)
        assert h.state.mode == 'story_paused' and not h.app._active_audio
        await h.button(7)
        assert h.state.mode == ('fixed_story_choice_wave' if kind == 'recorded' else 'root_menu')
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['recorded', 'authored'])
async def test_normal_resume_restores_pause_exit_before_next_announcement_failure(kind):
    h = await playing(kind)
    try:
        await pause(h, resume_announcement=True)
        await h.complete()
        assert h.state.mode == ('fixed_story_playing' if kind == 'recorded' else 'personal_story_playing')
        assert h.state.expected_inputs == ['button_6', 'button_7', 'handset_down']
        await h.button(6)
        await h.release(6)
        assert h.state.mode == 'story_pause_announcement'
        await h.complete(failed=True)
        assert h.state.mode == 'story_paused'
        assert h.state.expected_inputs == ['button_6', 'button_7', 'handset_down']
    finally:
        await h.close()
