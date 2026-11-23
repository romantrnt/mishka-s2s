"""Owned playback of authored local stories through the configured voice."""
from dataclasses import replace
import hashlib
import re
import time

from .name_audio import render_phrase_with_name, synthesize_private_text, resample_samples
from .private_welcome import valid_pcm
from .public_menu_audio import PublicMenuAudioCache


class StoryFragmentCache(PublicMenuAudioCache):
    """Bounded primary-voice PCM for exact public catalogue fragments only."""
    def fragment_key(self, tts, text):
        from .s2s import CachedTTSProvider
        while isinstance(tts, CachedTTSProvider):
            tts = tts.delegate
        source = (tts, self._provider_fingerprint(tts))
        if self._source is None or self._source[0] is not tts or self._source[1] != source[1]:
            self._entries.clear()
            self.bytes = 0
            self._generation += 1
            self._source = source
            self._provider_evidence = {id(tts):self._provider_evidence[id(tts)]}
        return self._generation, hashlib.sha256(text.encode()).digest()


def capable(engine):
    tts = getattr(engine, "tts", None)
    from .s2s import ResilientVoiceProvider
    voice = getattr(engine, "voice", None)
    if isinstance(voice, ResilientVoiceProvider):
        voice = voice.primary
    return bool(tts and callable(getattr(tts, "synthesize", None))
                and callable(getattr(voice, "convert_private", None))
                and callable(getattr(getattr(engine, "response_validator", None), "validate_trusted", None)))


async def render(engine, entry, profile, token, current, registry=None, *, on_chunk=None, observe=None):
    started = time.monotonic()
    def mark(stage, **fields):
        if callable(observe):
            observe(stage, elapsed_ms=round((time.monotonic() - started) * 1000), **fields)
    if not capable(engine) or not current():
        return None
    template = entry["text"]
    # The local catalogue owns this text. A name never enters a model request.
    decision = await engine.response_validator.validate_trusted(template.replace("{name}", "друг"))
    if not decision.allowed or not current():
        return None
    mark('validated')
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
    # Cache admission derives from the checked-in catalogue, never a visit name.
    from .demo_story_catalog import load_catalog
    public = any(template in (row['text'], row['young_text']) for row in load_catalog())
    fragments = set()
    if public:
        for sentence in re.findall(r"[^.!?]+[.!?]+|[^.!?]+$", template):
            fragments.add(sentence.strip().replace('{name}', 'друг'))
            if '{name}' in sentence:
                fragments.update(part.strip() for part in sentence.split('{name}'))
    cache = getattr(engine, '_story_fragment_cache', None)
    if not isinstance(cache, StoryFragmentCache):
        cache = engine._story_fragment_cache = StoryFragmentCache()

    async def synth(text):
        key = cache.fragment_key(engine.tts, text) if text in fragments else None
        cached = cache.get(key, token, None) if key is not None else None
        if cached is not None and current():
            mark('tts_cache_hit')
            return cached
        mark('tts_begin')
        audio = await synthesize_private_text(engine.tts, text, token, lambda: not current(), observe=mark)
        if current() and valid_pcm(audio) and key is not None:
            # A cancelled or replaced provider cannot populate a later generation.
            if cache.fragment_key(engine.tts, text) == key:
                cache.put(key, audio)
        mark('tts_pcm')
        return audio

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
        if callable(on_chunk):
            on_chunk(replace(audio, samples=tuple(samples), sample_rate=rate))
        mark('part_ready', samples=len(samples), rate=rate)
    mark('complete', samples=count, rate=rate or 0)
    return replace(audio, samples=tuple(chunks), sample_rate=rate) if chunks and current() else None
