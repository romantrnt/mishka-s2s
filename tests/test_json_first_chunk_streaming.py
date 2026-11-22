"""Verify reversible first-phrase streaming in _DialogueJSONBridge."""
import asyncio
import pytest

from app.foundation.s2s import DialogueResponseContract, _DialogueJSONBridge


class _FakeProvider:
    supports_dialogue_json = True

    def __init__(self, tokens):
        self._tokens = list(tokens)

    async def stream(self, messages, request_id, cancelled, **_kwargs):
        for tok in self._tokens:
            if cancelled():
                raise asyncio.CancelledError
            await asyncio.sleep(0)
            yield tok

    def stream_dialogue(self, messages, request_id, cancelled, response_schema=None):
        return self.stream(messages, request_id, cancelled)


@pytest.mark.asyncio
async def test_first_say_sentence_streams_before_terminal_and_preserves_remainder(monkeypatch):
    monkeypatch.setenv("MISHKA_STREAM_FIRST_JSON_CHUNK", "1")
    tokens = [
        '{"type":"say",',
        '"text":"Привет! ',
        'Я рад тебя слышать."}',
    ]
    boundaries = []
    bridge = _DialogueJSONBridge(
        _FakeProvider(tokens),
        DialogueResponseContract(catalogue=False, young=False, max_words=32),
        2048,
        lambda t: None,
        boundaries.append,
    )
    emitted = []
    async for chunk in bridge.stream([], "req-1", lambda: False):
        emitted.append((chunk, list(boundaries)))
    assert emitted[0][0] == "<say>Привет!</say>"
    assert "llm_clean_terminal" not in emitted[0][1]
    assert emitted[1][0] == "<say>Я рад тебя слышать.</say>"
    assert "json_validated" in emitted[1][1]


def test_extract_first_say_phrase_skips_decimal_comma_and_single_letter_abbrev():
    # Must not split at "1, 5"
    assert _DialogueJSONBridge._extract_first_say_phrase('{"type":"say","text":"Смотри, у меня есть число 1, 5 ') is None
    # Must not split at single-letter "т."
    assert _DialogueJSONBridge._extract_first_say_phrase('{"type":"say","text":"И т. ') is None
    # Must not early-emit non-"say" types
    assert _DialogueJSONBridge._extract_first_say_phrase('{"type":"say_led","text":"Привет! ') is None
    assert _DialogueJSONBridge._extract_first_say_phrase('{"type":"offer_game","game_id":"quiz"}') is None
    # Natural clause >= 32 chars splits cleanly at comma
    clause = _DialogueJSONBridge._extract_first_say_phrase('{"type":"say","text":"Конечно, я очень рад тебя видеть, ')
    assert clause == "Конечно, я очень рад тебя видеть,"


def test_prepare_vox_text_preserves_ambiguous_words_and_adds_terminal_punctuation():
    from app.foundation.voice_pipeline import normalize_text, prepare_vox_text

    assert normalize_text("Все кнопки работают лучше, чем вчера") == "Все кнопки работают лучше, чем вчера"
    assert "ещё" in normalize_text("Давай еще поиграем")
    assert normalize_text("Приём в 14:30, кабинет 12") == "Приём в четырнадцать тридцать, кабинет двенадцать"
    assert prepare_vox_text("Верно") == "Верно."
    assert prepare_vox_text("Привет!") == "Привет!"


@pytest.mark.asyncio
async def test_stream_first_json_chunk_runs_parallel_pre_stream_with_validation_gate(monkeypatch):
    from app.foundation.config import Settings
    from app.foundation.s2s import (
        AudioChunk,
        BoundedConversationEngine,
        ConversationContext,
        MockPlaybackController,
        RVCProvider,
        S2SConfig,
    )
    from app.foundation.validation import ValidationDecision

    monkeypatch.setenv("MISHKA_STREAM_FIRST_JSON_CHUNK", "1")
    timeline = []
    val_gate = asyncio.Event()

    class _StreamingNativeTTS:
        provider_name = "vox555"

        async def synthesize(self, text, token, cancelled):
            return AudioChunk((0.1,) * 240, 24000, token)

        async def synthesize_audio_stream(self, text, token, cancelled, *, first_chunk=False):
            timeline.append(("tts_start", text, first_chunk))
            yield AudioChunk((0.1,) * 240, 24000, token)
            timeline.append(("tts_yielded", text))

    class _StreamPlayback(MockPlaybackController):
        async def play_stream(self, chunk_aiter, *, on_chunk=None, owner_current=None):
            played = 0
            idx = 0
            async for chunk in chunk_aiter:
                timeline.append(("play_chunk", idx))
                if callable(on_chunk):
                    res = on_chunk(idx, chunk)
                    if asyncio.iscoroutine(res):
                        await res
                played += len(chunk.samples)
                idx += 1
            from app.foundation.s2s import PCMPlaybackResult
            return PCMPlaybackResult(0, played, True, True)

    tokens = ['{"type":"say","text":"Привет, мой дорогой друг! ', 'Как у тебя дела сегодня?"}']
    engine = BoundedConversationEngine(
        _FakeProvider(tokens),
        _StreamingNativeTTS(),
        RVCProvider(),
        _StreamPlayback(),
        config=S2SConfig(first_chunk_chars=16, max_chunk_chars=120),
        profile="mock",
        settings=Settings(profile="mock", voice_conversion="off"),
    )

    async def gated_validate(text, *, context=(), generation_id=0, spoken_requirement=None):
        timeline.append(("validate_start", text))
        await val_gate.wait()
        timeline.append(("validate_done", True))
        return ValidationDecision("allow", True, "ok", 1.0, "test")

    monkeypatch.setattr(engine.response_validator, "validate", gated_validate)
    ctx = ConversationContext("s1", "canonical_product", "bear_dialogue", ("__all__",), (), 2, 120, 2)

    async def unblock_validation():
        for _ in range(50):
            if any(ev[0] == "tts_yielded" for ev in timeline):
                break
            await asyncio.sleep(0.01)
        assert not any(ev[0] == "play_chunk" for ev in timeline)
        val_gate.set()

    unblocker = asyncio.create_task(unblock_validation())
    result = await engine.run_text_turn("Привет", ctx)
    await unblocker
    assert result.fallback_reason is None
    assert [ev[0] for ev in timeline if ev[0] in {"tts_start", "validate_done", "play_chunk"}][:3] == [
        "tts_start", "validate_done", "play_chunk"
    ]
    assert timeline[next(i for i, ev in enumerate(timeline) if ev[0] == "tts_start")][1] == "Привет, мой дорогой друг!"


@pytest.mark.asyncio
async def test_stream_first_json_chunk_validation_block_prevents_playback(monkeypatch):
    from app.foundation.config import Settings
    from app.foundation.s2s import (
        AudioChunk,
        BoundedConversationEngine,
        ConversationContext,
        MockPlaybackController,
        RVCProvider,
        S2SConfig,
    )
    from app.foundation.validation import ValidationDecision

    monkeypatch.setenv("MISHKA_STREAM_FIRST_JSON_CHUNK", "1")
    played = []

    class _StreamingNativeTTS:
        provider_name = "vox555"

        async def synthesize(self, text, token, cancelled):
            return AudioChunk((0.1,) * 240, 24000, token)

        async def synthesize_audio_stream(self, text, token, cancelled, *, first_chunk=False):
            yield AudioChunk((0.1,) * 240, 24000, token)

    class _TrackPlayback(MockPlaybackController):
        async def play_stream(self, chunk_aiter, *, on_chunk=None, owner_current=None):
            async for chunk in chunk_aiter:
                played.append(chunk)
            from app.foundation.s2s import PCMPlaybackResult
            return PCMPlaybackResult(0, 0, True, True)

    engine = BoundedConversationEngine(
        _FakeProvider(['{"type":"say","text":"Привет, мой дорогой друг! ', 'Как дела?"}']),
        _StreamingNativeTTS(),
        RVCProvider(),
        _TrackPlayback(),
        config=S2SConfig(first_chunk_chars=16, max_chunk_chars=120),
        profile="mock",
        settings=Settings(profile="mock", voice_conversion="off"),
    )

    async def block_validate(text, *, context=(), generation_id=0, spoken_requirement=None):
        return ValidationDecision("block", False, "unsafe_test", 1.0, "test")

    monkeypatch.setattr(engine.response_validator, "validate", block_validate)
    ctx = ConversationContext("s1", "canonical_product", "bear_dialogue", ("__all__",), (), 2, 120, 2)
    result = await engine.run_text_turn("Привет", ctx)
    assert result.fallback_reason == "provider_or_validation_failure"
    assert played == []
