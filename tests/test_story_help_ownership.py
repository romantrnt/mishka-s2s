"""Help owns its controls after interrupting a personal story."""
import pytest

from app.foundation.api import build_application
from app.foundation.config import Settings
from test_personal_story_app import enter, send
from test_personal_story_service import engine


async def start_app():
    app, *_ = build_application(settings=Settings(profile='mock', runtime_mode='clinical'),
                                background_scheduler=False)
    providers = engine()
    # Stub only story providers; keep the actual cancellation/controller boundary.
    for name in ('llm', 'tts', 'voice', 'response_validator', 'config'):
        setattr(app.bounded_s2s, name, getattr(providers, name))
    app._owned_menu_voice_capable = lambda: False
    app._story_seq = 0
    return app


async def complete(app):
    lease = dict(app._active_audio['story-test'])
    return await send(app, 'AudioCompleted', {**lease, 'response_id': lease['content_id']})


async def story(app, paused):
    await enter(app)
    await send(app, 'ButtonPressed', {'button_id': 1})
    old = dict(app._active_audio['story-test'])
    if paused:
        async def pause():
            return {'story_id': 'PERSONAL_STORY', 'offset_ms': 1000}
        app.audio.pause = pause
        await send(app, 'ButtonPressed', {'button_id': 6})
        await complete(app)
        assert app.canonical_runtime.sessions['story-test'].mode == 'story_paused'
    return old


@pytest.mark.asyncio
@pytest.mark.parametrize('paused', [False, True])
@pytest.mark.parametrize('answer', ['button', 'voice'])
async def test_help_yes_after_playing_or_paused_personal_story_keeps_safety_owner(paused, answer):
    app = await start_app()
    try:
        old = await story(app, paused)
        await send(app, 'ButtonPressed', {'button_id': 5})
        await complete(app)
        state = app.canonical_runtime.sessions['story-test']
        assert state.mode == 'wellbeing_check'
        if answer == 'button':
            result = await send(app, 'ButtonPressed', {'button_id': 7})
        else:
            result = await send(app, 'SpeechReceived', text='Да')
        assert result['reason'] == 'help_requested'
        assert state.mode == 'safety_stop' and state.safety_severity == 'concern'
        assert state.active_audio_id == 'PHRASE_091'
        assert state.active_story_id is None and state.dialogue_memory.story_checkpoint is None
        assert not app._personal_stories
        safety_lease = state.playback_lease
        await send(app, 'AudioCompleted', {**old, 'response_id': 'PERSONAL_STORY'})
        assert state.mode == 'safety_stop' and state.active_audio_id == 'PHRASE_091'
        assert state.playback_lease == safety_lease
    finally:
        await app.shutdown_identity()


@pytest.mark.asyncio
async def test_personal_story_help_no_then_yes_uses_current_fear_question():
    app = await start_app()
    try:
        await story(app, True)
        await send(app, 'ButtonPressed', {'button_id': 5})
        await complete(app)
        await send(app, 'ButtonPressed', {'button_id': 1})
        state = app.canonical_runtime.sessions['story-test']
        assert state.mode == 'wellbeing_check' and state.support_question_kind == 'fear'
        await complete(app)
        result = await send(app, 'ButtonPressed', {'button_id': 7})
        assert result['reason'] == 'help_requested' and state.mode == 'safety_stop'
        assert state.active_audio_id == 'PHRASE_091'
    finally:
        await app.shutdown_identity()


@pytest.mark.asyncio
@pytest.mark.parametrize('paused', [False, True])
async def test_genuine_personal_story_back_still_works_and_releases_story_checkpoint(paused):
    app = await start_app()
    try:
        await story(app, paused)
        result = await send(app, 'ButtonPressed', {'button_id': 7})
        state = app.canonical_runtime.sessions['story-test']
        assert result['reason'] == 'personal_story_back' and state.mode == 'root_menu'
        assert state.active_story_id is None and state.dialogue_memory.story_checkpoint is None
        assert not app._personal_stories
    finally:
        await app.shutdown_identity()


@pytest.mark.asyncio
async def test_paused_personal_story_idle_clears_old_owner_before_fresh_root_day_button():
    app = await start_app()
    clock = [100.]
    app.monotonic_now = lambda: clock[0]
    try:
        await story(app, True)
        sid = 'story-test'
        app._refresh_engagement_busy(sid)
        deadline = app._engagement_owners[sid]['deadline']
        clock[0] = deadline
        result = await app.expire_engagement_sessions()
        assert result and result[0]['reason'] == 'interaction_idle'
        state = app.canonical_runtime.sessions[sid]
        assert state.mode == 'root_menu' and state.active_story_id is None
        assert state.dialogue_memory.story_checkpoint is None and not app._personal_stories
        await send(app, 'ButtonPressed', {'button_id': 7})
        assert state.mode == 'schedule_weather'
    finally:
        await app.shutdown_identity()
