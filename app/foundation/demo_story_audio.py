"""Owned playback of authored local stories through the configured voice."""
from dataclasses import replace
import re

from .name_audio import render_phrase_with_name, synthesize_private_text, resample_samples
from .private_welcome import valid_pcm


def capable(engine):
    tts = getattr(engine, "tts", None)
    from .s2s import ResilientVoiceProvider
    voice = getattr(engine, "voice", None)
    if isinstance(voice, ResilientVoiceProvider):
        voice = voice.primary
    return bool(tts and callable(getattr(tts, "synthesize", None))
                and callable(getattr(voice, "convert_private", None))
                and callable(getattr(getattr(engine, "response_validator", None), "validate_trusted", None)))


async def render(engine, entry, profile, token, current, registry=None):
    if not capable(engine) or not current():
        return None
    template = entry["text"]
    # The local catalogue owns this text. A name never enters a model request.
    decision = await engine.response_validator.validate_trusted(template.replace("{name}", "друг"))
    if not decision.allowed or not current():
        return None
    voice = engine.voice
    from .s2s import ResilientVoiceProvider
    if isinstance(voice, ResilientVoiceProvider):
        voice = voice.primary
    convert = getattr(voice, "convert_private", None)
    if not callable(convert):
        return None
    from .demo_story_catalog import render_story
    safe_rendered = render_story({"text": "{name}"}, profile)
    name = safe_rendered if safe_rendered != "друг" else None
    chunks, rate, count = [], None, 0

    async def synth(text):
        return await synthesize_private_text(engine.tts, text, token, lambda: not current())

    for sentence in re.findall(r"[^.!?]+[.!?]+|[^.!?]+$", template):
        if not current():
            return None
        sentence = sentence.strip()
        if not sentence:
            continue
        if "{name}" in sentence:
            audio, _ = await render_phrase_with_name(
                sentence, name, synthesize_fn=synth, registry=registry,
                neutral_fallback=sentence.replace("{name}", "друг"))
        else:
            audio = await synth(sentence)
        if not current() or not valid_pcm(audio):
            return None
        if audio.token != token:
            return None
        audio = await convert(audio, lambda: not current())
        if not current() or not valid_pcm(audio) or audio.token != token or audio.sample_rate > 192000:
            return None
        rate = rate or audio.sample_rate
        samples = resample_samples(audio.samples, audio.sample_rate, rate)
        count += len(samples)
        if count > rate * 180:
            return None
        chunks.extend(samples)
    return replace(audio, samples=tuple(chunks), sample_rate=rate) if chunks and current() else None
