"""Pure audio operations for the Silero -> RVC -> PCM voice path.

The module intentionally contains no VAD, trimming, dynamics processing,
equalization, limiter, compressor, or amplitude clipping.  It only performs
text ITN, the two required sample-rate conversions, the first-chunk pre-roll,
and the post-RVC peak normalization.
"""
from __future__ import annotations

import html
import math
import re
from collections.abc import Iterable
from .spoken_numbers import normalize_spoken_numbers

SILERO_SAMPLE_RATE = 48_000
RVC_SAMPLE_RATE = 40_000
DEFAULT_PRE_ROLL_MS = 150
SAFE_PEAK = 0.92

_DASH_PAUSE_RE = re.compile(r"\s+[—–-]\s+")
_ELLIPSIS_RE = re.compile(r"(?:\.\.\.|…)\s*")
_OPENER_PAUSE_RE = re.compile(
    r"^(Знаешь|Слушай|Смотри|Ой|Ого|Хм|Ага|Погоди|Значит|Кстати),\s+",
    re.IGNORECASE,
)

# These are deliberately small and explicit.  They cover terms that can
# appear in the product/runtime dialogue without turning the text normalizer
# into a second language model.  New domain words belong here rather than in
# an ad-hoc prompt or in the audio worker.
_PRONUNCIATION_REPLACEMENTS = (
    (re.compile(r"\bRVC\b", re.IGNORECASE), "эр-ви-си"),
    (re.compile(r"\bSTT\b", re.IGNORECASE), "эс-ти-ти"),
    (re.compile(r"\bTTS\b", re.IGNORECASE), "ти-ти-эс"),
    (re.compile(r"\bS2S\b", re.IGNORECASE), "эс-два-эс"),
    (re.compile(r"\bLLM\b", re.IGNORECASE), "эл-эл-эм"),
    (re.compile(r"\bAPI\b", re.IGNORECASE), "эй-пи-ай"),
    (re.compile(r"\bLangGraph\b", re.IGNORECASE), "лэнг-граф"),
    (re.compile(r"\bQwen\b", re.IGNORECASE), "куэн"),
)

# Silero's automatic accentor is good, but these high-frequency product
# words benefit from the explicit spelling used by the Russian voice model.
_YO_REPLACEMENTS = {
    "еще": "ещё", "ее": "её", "нем": "нём",
    "идет": "идёт", "пойдет": "пойдёт", "придет": "придёт", "найдет": "найдёт",
    "пройдет": "пройдёт", "уйдет": "уйдёт", "ждет": "ждёт", "поет": "поёт",
    "живет": "живёт", "зовет": "зовёт", "берет": "берёт", "дает": "даёт",
    "ведет": "ведёт", "везет": "везёт", "плывет": "плывёт", "цветет": "цветёт",
    "растет": "растёт", "теплый": "тёплый", "теплая": "тёплая", "теплое": "тёплое",
    "веселый": "весёлый", "веселая": "весёлая", "легкий": "лёгкий", "зеленый": "зелёный",
    "желтый": "жёлтый", "черный": "чёрный", "темный": "тёмный", "звездочка": "звёздочка",
    "ежик": "ёжик", "елка": "ёлка", "мед": "мёд", "лед": "лёд",
    "самолет": "самолёт", "вертолет": "вертолёт", "котенок": "котёнок",
    "утенок": "утёнок", "лисенок": "лисёнок", "медвежонок": "медвежонок",
    "зайчонок": "зайчонок", "ребенок": "ребёнок", "береза": "берёза",
    "слезы": "слёзы", "вдвоем": "вдвоём", "втроем": "втроём",
    "твое": "твоё", "мое": "моё", "свое": "своё", "твоем": "твоём",
    "моем": "моём", "своем": "своём", "причем": "причём", "счет": "счёт",
    "полет": "полёт", "прием": "приём", "подъем": "подъём",
}


def normalize_text(text: str) -> str:
    """Normalize Russian text for stock Silero inference.

    Digits, temperature, and percentages are expanded with num2words,
    high-frequency 'ё' forms are restored, and emphatic runs are collapsed
    without flattening natural exclamation or question intonation.
    """
    value = re.sub(r"\s+", " ", str(text)).strip()
    if not value:
        raise ValueError("Silero cannot synthesize empty text")
    value = re.sub(r"!+", "!", value)
    value = re.sub(r"\?+", "?", value)

    value = normalize_spoken_numbers(value)
    for pattern, replacement in _PRONUNCIATION_REPLACEMENTS:
        value = pattern.sub(replacement, value)

    def restore_yo(match: re.Match[str]) -> str:
        word = match.group(0)
        target = _YO_REPLACEMENTS[word.lower()]
        return target.capitalize() if word[0].isupper() else target

    yo_pattern = re.compile(r"\b(" + "|".join(map(re.escape, _YO_REPLACEMENTS)) + r")\b", re.IGNORECASE)
    value = yo_pattern.sub(restore_yo, value)
    return re.sub(r"\s+", " ", value).strip()


def prepare_vox_text(text: str) -> str:
    """Normalize Russian text for VoxCPM2 and ensure clean terminal punctuation."""
    value = normalize_text(text)
    if value and value[-1] not in ".!?…:;":
        value += "."
    return value


def _format_sentence_ssml(sentence: str) -> str:
    escaped = html.escape(sentence, quote=False)
    escaped = _OPENER_PAUSE_RE.sub(lambda m: f'{m.group(1)}, <break time="90ms"/>', escaped, count=1)
    escaped = _DASH_PAUSE_RE.sub(' <break time="160ms"/> ', escaped)
    escaped = _ELLIPSIS_RE.sub('… <break time="170ms"/> ', escaped).strip()
    return f"<s>{escaped}</s>"


def plan_prosody(text: str) -> str:
    """Add natural SSML sentence boundaries and conversational micro-pauses.

    Preserves Silero's native punctuation-driven pitch and tempo while giving
    sentences, dashes, ellipses, and conversational openers human breathing room.
    """
    normalized = normalize_text(text)
    sentences = [part.strip() for part in re.findall(r".+?(?:[.!?…]+|$)", normalized, re.S) if part.strip()]
    if not sentences:
        raise ValueError("Silero cannot build prosody for empty text")
    parts: list[str] = []
    for idx, sentence in enumerate(sentences):
        if idx > 0:
            prev = sentences[idx - 1].rstrip()
            pause_ms = "220ms" if prev.endswith(("?", "!", "…")) else "180ms"
            parts.append(f'<break time="{pause_ms}"/>')
        parts.append(_format_sentence_ssml(sentence))
    return "<speak>" + "".join(parts) + "</speak>"


def prepend_pre_roll(samples: Iterable[float], sample_rate: int, milliseconds: int = DEFAULT_PRE_ROLL_MS) -> tuple[float, ...]:
    """Add exactly one leading silent buffer, expressed in source samples."""
    if milliseconds < 0:
        raise ValueError("pre-roll must be non-negative")
    source = tuple(float(value) for value in samples)
    count = round(sample_rate * milliseconds / 1000)
    return (0.0,) * count + source


def resample_mono(samples: Iterable[float], source_rate: int, target_rate: int) -> tuple[float, ...]:
    """High-quality mono resampling through torchaudio's band-limited kernel, with pure-Python fallback."""
    if source_rate <= 0 or target_rate <= 0:
        raise ValueError("sample rates must be positive")
    source = tuple(float(value) for value in samples)
    if source_rate == target_rate or not source:
        return source

    try:
        import torch
        import torchaudio.functional as F
    except ModuleNotFoundError:
        target_len = max(1, round(len(source) * target_rate / source_rate))
        if len(source) == 1:
            return (source[0],) * target_len
        ratio = source_rate / target_rate
        last = len(source) - 1
        out: list[float] = []
        for idx in range(target_len):
            pos = idx * ratio
            left = int(pos)
            if left >= last:
                out.append(source[last])
            else:
                frac = pos - left
                out.append(source[left] * (1.0 - frac) + source[left + 1] * frac)
        return tuple(out)

    tensor = torch.tensor(source, dtype=torch.float32)
    converted = F.resample(tensor, source_rate, target_rate)
    return tuple(float(value) for value in converted.cpu().tolist())


def peak_normalize(samples: Iterable[float], safe_peak: float = SAFE_PEAK) -> tuple[float, ...]:
    """Normalize audio to a safe peak."""
    values = tuple(float(value) for value in samples)
    if not values:
        return values
    max_val = max(abs(value) for value in values)
    if max_val > 0:
        values = tuple((value / max_val) * safe_peak for value in values)
    return values


def validate_audio(samples: Iterable[float], *, name: str = "audio") -> tuple[float, ...]:
    """Validate a model buffer without changing its dynamics."""
    values = tuple(float(value) for value in samples)
    if not values:
        raise ValueError(f"{name} is empty")
    if not all(math.isfinite(value) for value in values):
        raise ValueError(f"{name} contains non-finite samples")
    if max(abs(value) for value in values) > 1.0:
        raise ValueError(f"{name} exceeds [-1, 1]")
    return values
