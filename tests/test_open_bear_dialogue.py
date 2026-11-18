"""Real application -> canonical safety -> bounded engine dialogue regressions."""
import asyncio
import json
import time

import pytest

from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.validation import ValidationDecision


def event(kind, sequence, payload=None):
    return Event(kind, "open-dialogue", sequence, 1.0, "test", payload or {}, str(sequence), 0.0)


class CountingLLM:
    def __init__(self, answer="Привет, я слушаю."):
        self.calls = 0
        self.answer = answer

    async def stream(self, messages, request_id, cancelled):
        self.calls += 1
        yield f"<say>{self.answer}</say>"


async def setup_dialogue():
    app, *_ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"), background_scheduler=False)
    await app.receive_async(event("HandsetLifted", 1))
    # Clinical menu button6 enters the real canonical bear_dialogue branch.
    await app.receive_async(event("ButtonPressed", 2, {"button_id": 6}))
    assert app.canonical_runtime.sessions["open-dialogue"].mode == "bear_dialogue"
    llm = CountingLLM()
    app.bounded_s2s.llm = llm
    return app, llm


@pytest.mark.asyncio
async def test_ordinary_replies_reach_llm_and_stay_bounded():
    app, llm = await setup_dialogue()
    rows = []
    try:
        for sequence, text in enumerate(("Да", "Нет", "Я люблю динозавров", "У меня есть кошка", "Сегодня был в школе"), 3):
            before = llm.calls
            started = time.perf_counter()
            result = await app.receive_async(event("SpeechReceived", sequence), text)
            rows.append({"text": text, "latency_ms": round((time.perf_counter()-started)*1000, 3),
                         "llm_calls": llm.calls-before, "reason": result["reason"]})
            assert llm.calls == before + 1, rows
            assert result["reason"] == "dialogue_turn"
            assert app.canonical_runtime.sessions["open-dialogue"].mode == "bear_dialogue"
            effect = next(item for item in result["outputs"] if item.get("type") == "bounded_dialogue_turn")
            assert effect["fallback_reason"] is None
        context = app.conversation_contexts["open-dialogue"]
        assert context.turns == 5 and len(context.history) <= app.bounded_s2s.config.max_history_entries
        assert context.max_turns == 5 and context.max_duration_seconds == 120
        result = await app.receive_async(event("SpeechReceived", 8), "Ещё поговорим")
        assert llm.calls == 5
        assert any(item.get("fallback_reason") == "bounded_limit" for item in result["outputs"])
    finally:
        print("DIALOGUE_PROBE " + json.dumps(rows, ensure_ascii=False))
        await app._cleanup_session_resources("open-dialogue")


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["не хочу жить", "Мне страшно", "Назначь лекарство", "secret"])
async def test_open_dialogue_keeps_input_safety_and_forbidden_topics(text):
    app, llm = await setup_dialogue()
    try:
        result = await app.receive_async(event("SpeechReceived", 3), text)
        assert llm.calls == 0
        if text == "не хочу жить":
            assert app.canonical_runtime.sessions["open-dialogue"].safety_severity == "urgent"
        if text == "Мне страшно":
            assert app.canonical_runtime.sessions["open-dialogue"].safety_severity == "concern"
    finally:
        await app._cleanup_session_resources("open-dialogue")


@pytest.mark.asyncio
async def test_open_dialogue_still_requires_output_semantic_validation():
    app, llm = await setup_dialogue()
    calls = []
    class Deny:
        async def validate(self, text, context=()):
            calls.append(text)
            return ValidationDecision("block", False, "synthetic_unsafe", 1.0, "test")
    app.bounded_s2s.response_validator.mini = Deny()
    try:
        result = await app.receive_async(event("SpeechReceived", 3), "У меня есть кошка")
        assert llm.calls == 1
        assert calls
        assert not app.bounded_s2s.playback.played
        assert any(item.get("fallback_reason") for item in result["outputs"])
    finally:
        await app._cleanup_session_resources("open-dialogue")


@pytest.mark.asyncio
async def test_other_canonical_scenarios_keep_the_topic_allowlist():
    app, llm = await setup_dialogue()
    try:
        state = app.canonical_runtime.sessions["open-dialogue"]
        assert app._canonical_audio_context("open-dialogue").allowed_topics == ("__all__",)
        for mode in ("fixed_story_playing", "personal_story_theme", "game_active", "exhibition_name_capture"):
            state.mode = mode
            context = app._canonical_audio_context("open-dialogue")
            assert "__all__" not in context.allowed_topics
            assert context.forbidden_topics == ("diagnosis", "medicine", "secret", "personal_data")
            result = await app.bounded_s2s.run_text_turn("У меня есть кошка", context)
            assert result.fallback_reason == "unknown_topic"
        assert llm.calls == 0
    finally:
        await app._cleanup_session_resources("open-dialogue")
