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

SILERO_SAMPLE_RATE = 48_000
RVC_SAMPLE_RATE = 40_000
DEFAULT_PRE_ROLL_MS = 150
SAFE_PEAK = 0.92

_NUMBER_RE = re.compile(r"[0-9]+(?:[.,][0-9]+)?")

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
    "еще": "ещё",
    "все": "всё",
    "идет": "идёт",
    "пойдет": "пойдёт",
    "зовет": "зовёт",
    "живет": "живёт",
}


def normalize_text(text: str) -> str:
    """Normalize Russian text for stock Silero inference.

    Digits are expanded with num2words and emphatic exclamation runs are
    softened to a sentence boundary.  No SSML or speed controls are added.
    """
    from num2words import num2words

    value = re.sub(r"\s+", " ", str(text)).strip()
    if not value:
        raise ValueError("Silero cannot synthesize empty text")
    value = re.sub(r"!+", ".", value)

    def number_to_words(match: re.Match[str]) -> str:
        raw = match.group(0).replace(",", ".")
        if "." not in raw:
            return num2words(int(raw), lang="ru")
        integer, fraction = raw.split(".", 1)
        digits = " ".join(num2words(int(digit), lang="ru") for digit in fraction)
        return f"{num2words(int(integer), lang='ru')} целых {digits}"

    value = _NUMBER_RE.sub(number_to_words, value)
    for pattern, replacement in _PRONUNCIATION_REPLACEMENTS:
        value = pattern.sub(replacement, value)
    for source, replacement in _YO_REPLACEMENTS.items():
        value = re.sub(rf"\b{re.escape(source)}\b", replacement, value, flags=re.IGNORECASE)
    return value


def plan_prosody(text: str) -> str:
    """Add restrained SSML at semantic sentence boundaries.

    SSML is intentionally applied to phrases, never individual words.  The
    Silero model accepts this subset and keeps punctuation-driven intonation
    while giving questions and longer explanations a stable macro-curve.
    """
    normalized = normalize_text(text)
    sentences = [part.strip() for part in re.findall(r".+?(?:[.!?…]+|$)", normalized, re.S) if part.strip()]
    if not sentences:
        raise ValueError("Silero cannot build prosody for empty text")
    rendered = []
    for sentence in sentences:
        escaped = html.escape(sentence, quote=False)
        if sentence.rstrip().endswith("?"):
            escaped = f'<prosody pitch="medium">{escaped}</prosody>'
        elif len(sentence) >= 80:
            escaped = f'<prosody rate="slow">{escaped}</prosody>'
        rendered.append(f"<s>{escaped}</s>")
    return "<speak>" + '<break time="110ms"/>'.join(rendered) + "</speak>"


def prepend_pre_roll(samples: Iterable[float], sample_rate: int, milliseconds: int = DEFAULT_PRE_ROLL_MS) -> tuple[float, ...]:
    """Add exactly one leading silent buffer, expressed in source samples."""
    if milliseconds < 0:
        raise ValueError("pre-roll must be non-negative")
    source = tuple(float(value) for value in samples)
    count = round(sample_rate * milliseconds / 1000)
    return (0.0,) * count + source


def resample_mono(samples: Iterable[float], source_rate: int, target_rate: int) -> tuple[float, ...]:
    """High-quality mono resampling through torchaudio's band-limited kernel."""
    if source_rate <= 0 or target_rate <= 0:
        raise ValueError("sample rates must be positive")
    source = tuple(float(value) for value in samples)
    if source_rate == target_rate or not source:
        return source

    import torch
    import torchaudio.functional as F

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
