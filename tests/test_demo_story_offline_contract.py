"""Synthetic PCM only: authored stories need no story or semantic model service."""
import asyncio

import pytest

from app.foundation.personal_story import PersonalStoryService, STORY_ID
from app.foundation.validation import AsyncResponseValidator
from test_scene_menu_runtime import Harness


class UnavailableModels:
    supports_full_story = False

    def __init__(self):
        self.calls = []

    async def stream_story(self, *args, **kwargs):
        self.calls.append("stream_story")
        raise AssertionError("An authored story must not call a story model")
        yield  # Preserve the provider's async iterator interface.

    async def stream(self, *args, **kwargs):
        self.calls.append("stream")
        raise AssertionError("An authored story must not call a dialogue model")
        yield

    async def validate(self, *args, **kwargs):
        self.calls.append("validate")
        raise AssertionError("An authored story must not call a semantic model")

    async def validate_story(self, *args, **kwargs):
        self.calls.append("validate_story")
        raise AssertionError("An authored story must not call a semantic story model")


class TrustedValidation(AsyncResponseValidator):
    def __init__(self, unavailable):
        super().__init__(mini=unavailable)
        self.trusted_decisions = []

    async def validate_trusted(self, text, generation_id=None):
        decision = await super().validate_trusted(text, generation_id)
        self.trusted_decisions.append(decision)
        return decision


def offline_harness():
    h = Harness()
    h.app._owned_menu_voice_capable = lambda: False
    unavailable = UnavailableModels()
    validator = TrustedValidation(unavailable)
    h.app.bounded_s2s.llm = unavailable
    h.app.bounded_s2s.response_validator = validator
    assert not PersonalStoryService(h.app.bounded_s2s).capable()
    assert h.app.personal_story_available()
    return h, unavailable, validator


@pytest.mark.asyncio
async def test_authored_story_plays_and_returns_without_story_or_semantic_models():
    h, unavailable, validator = offline_harness()
    try:
        await h.button(3)
        await h.release(3)
        assert len(h.state.scene_menu_descriptor["items"]) == 5
        result = await h.button(1)
        assert result["reason"] == "personal_story_ready"
        assert h.state.mode == "personal_story_playing"
        assert h.played[-1].response_id == STORY_ID
        await h.release(1)
        await h.complete()
        assert h.state.mode == "root_menu"
        assert not h.app._personal_stories
        assert not h.app._personal_story_jobs
        assert validator.trusted_decisions
        assert all(d.allowed and d.source == "regex_trusted" for d in validator.trusted_decisions)
        assert not unavailable.calls
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_authored_story_tts_failure_releases_audio_and_keeps_exit_controls():
    h, unavailable, validator = offline_harness()
    original = h.render.synthesize
    failed_owners = []
    failed_jobs = []
    calls = 0

    async def failing_tts(text, token, cancelled):
        nonlocal calls
        calls += 1
        if calls == 2:
            failed_owners.append(h.app._personal_stories[h.sid])
            failed_jobs.append(asyncio.current_task())
            raise RuntimeError("synthetic second-sentence TTS failure")
        return await original(text, token, cancelled)

    h.render.synthesize = failing_tts
    try:
        await h.button(3)
        await h.release(3)
        result = await h.button(1)
        assert result["reason"] == "personal_story_unavailable"
        assert h.state.mode == "fixed_story_menu"
        assert 7 in h.state.scene_menu_descriptor["buttons"]
        assert not h.app._personal_stories
        assert not h.app._personal_story_jobs
        assert failed_owners[0].closed and failed_owners[0].audio is None
        assert failed_jobs[0].done()
        assert not any(handle.response_id == STORY_ID for handle in h.played)
        await h.release(1)
        await h.button(7)
        assert h.state.mode == "root_menu"
        # Give completed job callbacks a turn after the user has left the fallback.
        await asyncio.sleep(0)
        assert not h.app._personal_stories
        assert not h.app._personal_story_jobs
        assert not any(handle.response_id == STORY_ID for handle in h.played)
        assert validator.trusted_decisions
        assert all(d.allowed and d.source == "regex_trusted" for d in validator.trusted_decisions)
        assert not unavailable.calls
    finally:
        await h.close()
