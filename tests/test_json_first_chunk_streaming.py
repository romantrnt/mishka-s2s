"""Verify reversible first-phrase streaming in _DialogueJSONBridge."""
import asyncio
import pytest

from app.foundation.s2s import DialogueResponseContract, _DialogueJSONBridge


class _FakeProvider:
    def __init__(self, tokens):
        self._tokens = list(tokens)

    async def stream(self, messages, request_id, cancelled):
        for tok in self._tokens:
            if cancelled():
                raise asyncio.CancelledError
            await asyncio.sleep(0)
            yield tok


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
