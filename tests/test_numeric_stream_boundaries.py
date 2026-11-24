"""Exercise the real stream splitters before a numeric speech copy reaches TTS."""
import json

import pytest

from app.foundation.s2s import SentenceChunker, _DialogueJSONBridge
from app.foundation.spoken_numbers import normalize_spoken_numbers


@pytest.mark.parametrize("text,spoken", [
    ("Температура −2.5°C.", "Температура минус две целых пять десятых градуса по Цельсию."),
    ("Сегодня 24.11.2026.", "Сегодня двадцать четвёртое ноября две тысячи двадцать шестого года."),
    ("Встреча в 09:05.", "Встреча в девять ноль пять."),
    ("Сейчас +1e-05°C.", "Сейчас плюс ноль целых одна стотысячная градуса по Цельсию."),
])
def test_actual_chunker_preserves_numbers_at_every_stream_cut(text, spoken):
    for cut in range(len(text) + 1):
        chunker = SentenceChunker(min_sentences=1, max_sentences=1)
        chunks = chunker.feed(text[:cut]) + chunker.feed(text[cut:]) + chunker.flush()
        assert chunks == [text], (cut, chunks)
        assert normalize_spoken_numbers(chunks[0]) == spoken


@pytest.mark.parametrize("ascii_wire", [False, True])
@pytest.mark.parametrize("text", ["Температура −2.5°C.", "Сегодня 24.11.2026."])
def test_actual_json_prefix_never_releases_a_partial_number(text, ascii_wire):
    wire = json.dumps({"type": "say", "text": text}, ensure_ascii=ascii_wire)
    for cut in range(len(wire) + 1):
        phrase = _DialogueJSONBridge._extract_first_say_phrase(wire[:cut])
        assert phrase in (None, text), (cut, phrase)


def test_normal_first_sentence_still_streams_before_json_eof():
    prefix = '{"type":"say","text":"Я тебя слушаю. Расскажи'
    assert _DialogueJSONBridge._extract_first_say_phrase(prefix) == "Я тебя слушаю."


def test_number_at_sentence_end_waits_for_the_next_word_or_flush():
    chunker = SentenceChunker(min_sentences=1, max_sentences=1)
    assert chunker.feed("Хорошо. Нажми 2.") == ["Хорошо."]
    assert chunker.feed(" Потом продолжим.") == ["Нажми 2.", "Потом продолжим."]
    assert chunker.flush() == []
