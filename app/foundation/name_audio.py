"""Registry and safe audio splicing for recorded announcer name WAVs."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import inspect
import json
import math
from pathlib import Path
import struct
from typing import Any, Callable
import wave

from .private_welcome import valid_name, valid_pcm
from .s2s import AudioChunk

ALLOWED_NAME_CASES = frozenset({
    "nominative",
    "genitive",
    "dative",
    "accusative",
    "instrumental",
    "prepositional",
    "vocative",
})


def _norm_name_key(value: str) -> str:
    return " ".join(value.strip().casefold().split()).replace("ё", "е")


@dataclass(frozen=True)
class NameEntry:
    key: str
    case: str
    file: str
    aliases: tuple[str, ...] = ()
    sha256: str | None = None


@dataclass(frozen=True)
class NameRegistryReport:
    total_entries: int
    valid_entries: int
    missing_files: tuple[str, ...] = ()
    corrupt_files: tuple[str, ...] = ()
    ambiguous_keys: tuple[str, ...] = ()
    invalid_cases: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not (self.missing_files or self.corrupt_files or self.ambiguous_keys or self.invalid_cases)


def _read_wav_chunk(path: Path, expected_sha256: str | None = None) -> AudioChunk | None:
    try:
        if not path.is_file() or path.stat().st_size > 4_000_000:
            return None
        raw_bytes = path.read_bytes()
    except OSError:
        return None
    if expected_sha256:
        actual_sha = hashlib.sha256(raw_bytes).hexdigest()
        if actual_sha.casefold() != expected_sha256.casefold():
            return None
    try:
        with wave.open(str(path), "rb") as wf:
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            nframes = wf.getnframes()
            if channels not in (1, 2) or sampwidth != 2 or not 8000 <= framerate <= 96000 or not 0 < nframes <= framerate * 6:
                return None
            frames = wf.readframes(nframes)
    except (wave.Error, EOFError, OSError):
        return None
    expected_len = nframes * channels * sampwidth
    if len(frames) < expected_len:
        return None
    total_samples = nframes * channels
    ints = struct.unpack(f"<{total_samples}h", frames[:expected_len])
    if channels == 2:
        samples = tuple(
            max(-1.0, min(1.0, ((ints[i] + ints[i + 1]) / 2.0) / 32768.0))
            for i in range(0, total_samples, 2)
        )
    else:
        samples = tuple(max(-1.0, min(1.0, val / 32768.0)) for val in ints)
    chunk = AudioChunk(samples=samples, sample_rate=framerate, token=0)
    return chunk if valid_pcm(chunk) else None


class NameAudioRegistry:
    """Loads and validates recorded name WAVs from assets/names/manifest.json."""

    def __init__(self, manifest_path: str | Path = "assets/names/manifest.json") -> None:
        self.manifest_path = Path(manifest_path)
        self.base_dir = self.manifest_path.parent
        self.entries: list[NameEntry] = []
        self._lookup: dict[tuple[str, str], NameEntry] = {}
        self._ambiguous: set[tuple[str, str]] = set()
        self._invalid_cases: list[str] = []
        self._load()

    def _load(self) -> None:
        if not self.manifest_path.is_file():
            return
        try:
            if self.manifest_path.stat().st_size > 512_000:
                return
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
            return
        default_case = str(data.get("default_case") or "nominative").strip().casefold()
        for raw in data.get("entries") or ():
            if not isinstance(raw, dict):
                continue
            key = str(raw.get("key") or "").strip()
            case = str(raw.get("case") or default_case).strip().casefold()
            file_rel = str(raw.get("file") or "").strip()
            aliases = tuple(a.strip() for a in (raw.get("aliases") if isinstance(raw.get("aliases"), (list, tuple)) else ()) if isinstance(a, str) and a.strip())
            sha256 = str(raw.get("sha256")).strip() if raw.get("sha256") else None
            if not key or not file_rel:
                continue
            # A catalogue entry may only name a file inside the name pack.
            try:
                (self.base_dir / file_rel).resolve().relative_to(self.base_dir.resolve())
            except (ValueError, OSError):
                continue
            entry = NameEntry(key=key, case=case, file=file_rel, aliases=aliases, sha256=sha256)
            self.entries.append(entry)
            if case not in ALLOWED_NAME_CASES:
                self._invalid_cases.append(f"{key}:{case}")
                continue
            for variant in (key, *aliases):
                lookup_key = (_norm_name_key(variant), case)
                existing = self._lookup.get(lookup_key)
                if existing is not None and existing.file != entry.file:
                    self._ambiguous.add(lookup_key)
                else:
                    self._lookup[lookup_key] = entry
        for bad in self._ambiguous:
            self._lookup.pop(bad, None)

    def validate(self) -> NameRegistryReport:
        missing: list[str] = []
        corrupt: list[str] = []
        valid_count = 0
        for entry in self.entries:
            if entry.case not in ALLOWED_NAME_CASES:
                continue
            wav_path = self.base_dir / entry.file
            if not wav_path.is_file():
                missing.append(entry.file)
                continue
            chunk = _read_wav_chunk(wav_path, entry.sha256)
            if chunk is None:
                corrupt.append(entry.file)
                continue
            valid_count += 1
        ambiguous_labels = tuple(sorted(f"{k}:{c}" for k, c in self._ambiguous))
        return NameRegistryReport(
            total_entries=len(self.entries),
            valid_entries=valid_count,
            missing_files=tuple(missing),
            corrupt_files=tuple(corrupt),
            ambiguous_keys=ambiguous_labels,
            invalid_cases=tuple(self._invalid_cases),
        )

    def resolve(self, name: str | None, *, case: str = "nominative") -> AudioChunk | None:
        if not isinstance(name, str) or not name.strip():
            return None
        norm_case = case.strip().casefold()
        if norm_case not in ALLOWED_NAME_CASES:
            return None
        lookup_key = (_norm_name_key(name), norm_case)
        if lookup_key in self._ambiguous:
            return None
        entry = self._lookup.get(lookup_key)
        if entry is None:
            return None
        return _read_wav_chunk(self.base_dir / entry.file, entry.sha256)


def resample_samples(samples: tuple[float, ...], src_rate: int, dst_rate: int) -> tuple[float, ...]:
    if src_rate == dst_rate or not samples:
        return samples
    out_len = max(1, int(round(len(samples) * dst_rate / src_rate)))
    if out_len == 1:
        return (samples[0],)
    last = len(samples) - 1
    out: list[float] = []
    for i in range(out_len):
        pos = i * last / (out_len - 1)
        idx = int(pos)
        frac = pos - idx
        if idx >= last:
            out.append(samples[last])
        else:
            out.append(samples[idx] * (1.0 - frac) + samples[idx + 1] * frac)
    return tuple(out)


def _rms(samples: tuple[float, ...]) -> float:
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def _apply_fade(samples: list[float], fade_samples: int) -> None:
    if fade_samples <= 0 or len(samples) < 2:
        return
    count = min(fade_samples, len(samples) // 2)
    for i in range(count):
        gain = (i + 1) / (count + 1)
        samples[i] *= gain
        samples[-1 - i] *= gain


def splice_audio_chunks(
    segments: list[tuple[AudioChunk, bool]],
    *,
    target_rate: int | None = None,
    pause_ms: int = 35,
    fade_ms: int = 12,
) -> AudioChunk | None:
    """Splice (AudioChunk, is_recorded_name) segments smoothly without volume jumps."""
    if not segments:
        return None
    rate = target_rate or segments[0][0].sample_rate
    tts_rms_values = [_rms(resample_samples(chunk.samples, chunk.sample_rate, rate)) for chunk, is_rec in segments if not is_rec]
    ref_rms = sum(tts_rms_values) / len(tts_rms_values) if tts_rms_values else 0.0
    fade_samples = int(rate * fade_ms / 1000)
    pause_samples = (0.0,) * int(rate * pause_ms / 1000)
    out: list[float] = []
    for idx, (chunk, is_rec) in enumerate(segments):
        resampled = list(resample_samples(chunk.samples, chunk.sample_rate, rate))
        if is_rec and ref_rms > 1e-4:
            rec_rms = _rms(tuple(resampled))
            if rec_rms > 1e-4:
                gain = max(0.4, min(2.2, ref_rms / rec_rms))
                resampled = [max(-0.98, min(0.98, s * gain)) for s in resampled]
        _apply_fade(resampled, fade_samples)
        if idx > 0 and pause_samples:
            out.extend(pause_samples)
        out.extend(resampled)
    result = AudioChunk(samples=tuple(out), sample_rate=rate, token=segments[0][0].token)
    return result if valid_pcm(result) else None


async def _call_tts(tts: Any, text: str, synthesize_fn: Callable[[str], Any] | None = None) -> AudioChunk | None:
    res = synthesize_fn(text) if synthesize_fn is not None else tts.synthesize(text)
    if inspect.isawaitable(res):
        res = await res
    return res if valid_pcm(res) else None


async def render_phrase_with_name(
    template: str,
    name: str | None,
    *,
    tts: Any = None,
    synthesize_fn: Callable[[str], Any] | None = None,
    registry: NameAudioRegistry | None = None,
    case: str = "nominative",
    neutral_fallback: str | None = None,
    valid_name_fn: Callable[[Any], bool] = valid_name,
) -> tuple[AudioChunk | None, str]:
    """Render a template with an explicit {name} slot using recorded WAV or whole-phrase TTS."""
    if "{name}" not in template:
        if neutral_fallback:
            return await _call_tts(tts, neutral_fallback, synthesize_fn), "no_slot_neutral"
        return await _call_tts(tts, template, synthesize_fn), "no_slot_tts"
    if template.count("{name}") != 1:
        return None, "invalid_name_slots"
    has_name = bool(name and valid_name_fn(name))
    if has_name and registry is not None:
        recorded = registry.resolve(name, case=case)
        if recorded is not None:
            prefix, suffix = template.split("{name}", 1)
            segments: list[tuple[AudioChunk, bool]] = []
            target_rate: int | None = None
            if any(char.isalnum() for char in prefix):
                pre_chunk = await _call_tts(tts, prefix.strip(), synthesize_fn)
                if pre_chunk is None:
                    return None, "tts_prefix_failed"
                target_rate = pre_chunk.sample_rate
                segments.append((pre_chunk, False))
            from dataclasses import replace
            recorded = replace(recorded, token=segments[0][0].token if segments else recorded.token)
            segments.append((recorded, True))
            if any(char.isalnum() for char in suffix):
                post_chunk = await _call_tts(tts, suffix.strip(), synthesize_fn)
                if post_chunk is None:
                    return None, "tts_suffix_failed"
                if target_rate is None:
                    target_rate = post_chunk.sample_rate
                if len(segments) == 1:
                    segments[0] = (replace(recorded, token=post_chunk.token), True)
                segments.append((post_chunk, False))
            spliced = splice_audio_chunks(segments, target_rate=target_rate or recorded.sample_rate)
            if spliced is not None:
                return spliced, "recorded_name_spliced"
    if has_name and case == "nominative":
        full_text = template.replace("{name}", name.strip())
        chunk = await _call_tts(tts, full_text, synthesize_fn)
        return chunk, ("tts_full_phrase" if chunk is not None else "tts_failed")
    if neutral_fallback:
        chunk = await _call_tts(tts, neutral_fallback, synthesize_fn)
        return chunk, ("neutral_fallback" if chunk is not None else "tts_failed")
    return None, "name_unavailable"


async def synthesize_private_text(tts, text, token, cancelled, *, observe=None):
    """Use the configured primary voice without caching visit-private text."""
    from .s2s import CachedTTSProvider
    while isinstance(tts, CachedTTSProvider):
        tts = tts.delegate
    if cancelled():
        return None
    wait_idle = getattr(tts, "wait_for_idle", None)
    if callable(wait_idle):
        if callable(observe):observe('voice_idle_wait')
        await wait_idle(cancelled)
        if cancelled():
            return None
        if callable(observe):observe('voice_idle_ready')
    stream = getattr(tts, "synthesize_audio_stream", None)
    if not callable(stream):
        audio = await tts.synthesize(text, token, cancelled)
        return audio if (not cancelled() and valid_pcm(audio) and audio.token == token
                         and audio.sample_rate <= 192000 and len(audio.samples) <= audio.sample_rate * 180) else None
    samples = []
    rate = None
    async for chunk in stream(text, token, cancelled, first_chunk=True):
        if cancelled() or not valid_pcm(chunk) or chunk.token != token or chunk.sample_rate > 192000:
            return None
        if rate is None:
            rate = chunk.sample_rate
            if callable(observe):observe('first_pcm')
        if rate != chunk.sample_rate or len(samples) + len(chunk.samples) > rate * 180:
            return None
        samples.extend(chunk.samples)
    return AudioChunk(tuple(samples), rate, token) if samples and rate and not cancelled() else None


class VisitNameTTSProvider:
    """A per-turn adapter: no mutable cross-visit name on the shared provider."""
    def __init__(self, delegate, name, registry):
        from .s2s import CachedTTSProvider
        while isinstance(delegate, CachedTTSProvider):
            delegate = delegate.delegate
        self.delegate, self.name, self.registry = delegate, name, registry
        self.provider_name = getattr(delegate, 'provider_name', None)
        self.supports_native_stream = (callable(getattr(delegate, 'synthesize_audio_stream', None))
                                       and self.provider_name != 'qwen765')
        self._recorded = registry.resolve(name, case='nominative')

    def _template(self, text):
        import re
        if self._recorded is None or not valid_name(self.name):
            return None
        matches = list(re.finditer(r"(?<![\w'’\-])" + re.escape(self.name) + r"(?![\w'’\-])", text, re.IGNORECASE))
        # Only one exact nominative occurrence is safe to splice. Declined or
        # repeated forms remain a whole phrase in the configured primary voice.
        if len(matches) != 1:
            return None
        match = matches[0]
        return text[:match.start()] + '{name}' + text[match.end():]

    async def synthesize(self, text, token, cancelled):
        from dataclasses import replace
        template = self._template(text)
        if template is None:
            return await synthesize_private_text(self.delegate, text, token, cancelled)
        audio, _ = await render_phrase_with_name(
            template, self.name, registry=self.registry,
            synthesize_fn=lambda part: synthesize_private_text(self.delegate, part, token, cancelled))
        if cancelled() or not valid_pcm(audio):
            return None
        return replace(audio, token=token)

    async def synthesize_stream_chunk(self, text, token, cancelled, *, first_chunk=True):
        if self._template(text) is None:
            method = getattr(self.delegate, 'synthesize_stream_chunk', None)
            if callable(method):
                return await method(text, token, cancelled, first_chunk=first_chunk)
        return await self.synthesize(text, token, cancelled)

    async def synthesize_audio_stream(self, text, token, cancelled, *, first_chunk=True):
        if self._template(text) is None:
            native = getattr(self.delegate, 'synthesize_audio_stream', None)
            if callable(native):
                async for chunk in native(text, token, cancelled, first_chunk=first_chunk):
                    if cancelled():
                        return
                    yield chunk
                return
        audio = await self.synthesize(text, token, cancelled)
        if audio is not None and not cancelled():
            yield audio

    def request_metadata(self):
        method = getattr(self.delegate, 'request_metadata', None)
        return method() if callable(method) else {}
