"""Owned public game narration and playback-anchored light cues."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import replace
import math
from pathlib import Path
import struct
import uuid
import wave

from .domain import PlaybackHandle
from .lighting import BUTTON_DIGIT_ELEMENTS, BUTTON_FILL_ELEMENTS, LIGHTING_ELEMENT_IDS, PICTOGRAM_ELEMENTS, LightingCommand
from .private_welcome import valid_pcm

GAME_PCM_IDS = frozenset({"GAME_PROMPT", "GAME_HINT"})
_GAME_CONTENT_VERSION = "game_v2"
_GAME_CACHE_MAX_ENTRIES = 256
_GAME_CACHE_MAX_BYTES = 48 * 1024 * 1024
_COLOR_LABELS = {1:"Красный", 2:"Оранжевый", 3:"Жёлтый", 4:"Зелёный", 5:"Голубой", 6:"Синий", 7:"Фиолетовый"}
_DIGIT_LABELS = {1:"Один", 2:"Два", 3:"Три", 4:"Четыре", 5:"Пять", 6:"Шесть", 7:"Семь"}
_ORDINAL_LABELS = {1:"Первый", 2:"Второй", 3:"Третий", 4:"Четвёртый", 5:"Пятый", 6:"Шестой", 7:"Седьмой"}
_NUMERIC_WORDS = {"один": 1, "одно": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7}
STATIC_GAME_PHRASES = (
    "Нажимай нужный цвет или цифру. Чтобы выйти, подержи семь.",
    "Нажми свой ответ.",
    "Что из этого лишнее?",
    "Пока не угадал. Оставил три варианта — попробуй ещё раз!",
    "Посмотри на огонёк — попробуй нажать эту кнопку!",
    "Нажимай кнопки и смотри, как расходится свет.",
    "Посмотри на огоньки. Потом повтори их порядок.",
    "Сыграем небольшую партию. Назови животное или еду. Я назову другое.",
    "Сыграем небольшую партию. Называем слова на последнюю букву предыдущего. Мягкий и твёрдый знак, ы пропускаем. Назови первое слово.",
    "Сыграем небольшую партию. Будем по очереди называть животных. Назови любое животное.",
    "Чтобы выйти, держи семь. Когда скажу «Теперь отпускай», отпусти.",
    "Чтобы выйти, нажми семь.",
)


def game_light_commands(buttons, display="digits"):
    """A scene lights exactly the role the child has been asked to use."""
    elements = set()
    for button in buttons:
        if type(button) is not int or button not in BUTTON_FILL_ELEMENTS:
            continue
        if display == "pictograms":
            if button in PICTOGRAM_ELEMENTS:
                elements.add(PICTOGRAM_ELEMENTS[button])
        elif display == "colors":
            elements.add(BUTTON_FILL_ELEMENTS[button])
        else:
            elements.update(BUTTON_DIGIT_ELEMENTS[button])
    return tuple((element, 1) for element in sorted(elements))


class GamePresentationMixin:
    def _game_voice_scope(self, session_id):
        """Ordinary key edges and pattern ticks keep one game's speech owner."""
        state = self.canonical_runtime.sessions.get(session_id)
        engine = getattr(state, "game_engine_state", None)
        if (state is None or state.mode != "game_active" or not state.interaction_open
                or state.game_audio_stage != "playing" or engine is None or engine.seven_exit_phase != 'idle'):
            return None
        return (id(state), self._session_epochs.get(session_id, 0), state.active_game_id,
                engine.run_id, state.game_audio_stage, state.active_audio_id, state.playback_lease)

    def _game_prompt_segments(self, effect):
        prompt = str(effect.get("prompt") or "").strip()
        rules_intro = str(effect.get("rules_intro") or "").strip()
        segments = []
        if effect.get("feedback_phrase_id"):
            segments.append((str(effect.get("feedback_text") or "Получилось!"), (), effect["feedback_phrase_id"]))
        if rules_intro:
            segments.append((rules_intro, (), None))
        if prompt:
            segments.append((prompt, (), effect.get("phrase_asset_id")))
        narrate_options = effect.get("game_id") == "odd_one_out"
        if narrate_options and effect.get("options") and not effect.get("is_hint"):
            labels = _COLOR_LABELS if effect.get("display") == "colors" else _DIGIT_LABELS
            digits_display = effect.get("display", "digits") == "digits"
            for button, text in tuple(effect.get("options") or ())[:7]:
                if type(button) is int and button in labels:
                    clean = str(text or "").strip()
                    num_val = _NUMERIC_WORDS.get(clean.lower()) if digits_display else None
                    if num_val is not None:
                        phrase = f"{clean}." if num_val == button else f"{_ORDINAL_LABELS[button]} вариант: {clean}."
                    else:
                        phrase = f"{labels[button]}: {clean}."
                    segments.append((phrase, (button,), None))
            segments.append(("Что из этого лишнее?" if effect.get("game_id") == "odd_one_out"
                             else "Нажми свой ответ.", (), None))
        if effect.get("exit_hint"):
            segments.append((effect["exit_hint"], (), None))
        return segments

    def _load_game_asset(self, phrase_id, token):
        if not phrase_id:
            return None
        asset = self.voice_manifest.resolve(str(phrase_id))
        if not asset:
            return None
        path = Path(str(asset.get("asset_path") or asset.get("audio_file") or asset.get("asset") or ""))
        if not path.is_absolute():
            root = Path(__file__).resolve().parents[2]
            path = next((candidate for candidate in (root / path, root / "content/audio" / path,
                        root / "content" / path) if candidate.is_file()), root / path)
        from .s2s import AudioChunk
        with wave.open(str(path), "rb") as source:
            if source.getnchannels() != 1 or source.getsampwidth() != 2:
                return None
            rate = source.getframerate()
            if source.getnframes() > rate * 45:
                return None
            raw = source.readframes(source.getnframes())
        return AudioChunk(tuple(value / 32768. for value in struct.unpack("<%dh" % (len(raw) // 2), raw)), rate, token)

    async def _assemble_game_audio(self, chunks, token, current):
        from .s2s import AudioChunk
        from .voice_pipeline import peak_normalize, resample_mono
        samples, cues = [], []
        rate = 24000
        for audio, buttons in chunks:
            if not current() or not valid_pcm(audio):
                return None
            converted = (audio.samples if audio.sample_rate == rate else
                         await asyncio.to_thread(resample_mono, audio.samples, audio.sample_rate, rate))
            if not current():
                return None
            cues.append((len(samples) / rate, buttons))
            samples.extend(converted)
            samples.extend((0.,) * int(rate * .14))
            if len(samples) > rate * 60:
                return None
        values = tuple(samples)
        # Sinc resampling can overshoot an otherwise valid source waveform.
        if values and all(math.isfinite(value) for value in values) and max(map(abs, values)) > 1.:
            values = peak_normalize(values, safe_peak=1.)
        audio = AudioChunk(values, rate, token)
        return (audio, tuple(cues)) if valid_pcm(audio) else None

    async def _render_game_prompt(self, session_id, effect, token, current):
        started = asyncio.get_running_loop().time()
        stage, segment_index, failure_reason = "admission", None, "empty_prompt"
        def failed(reason, exception=None):
            self._record_audio_completion_diagnostic(
                "game_prompt_render_failed", session_id=session_id,
                operation="game_prompt_render", generation=self._session_epochs.get(session_id, 0),
                reason=reason, stage=stage, chunk_index=segment_index,
                elapsed_ms=round((asyncio.get_running_loop().time() - started) * 1000),
                exception_type=type(exception).__name__ if exception is not None else None)
            self.record_audio_diagnostic("game_prompt_render_failed", code=reason, stage=stage,
                generation=self._session_epochs.get(session_id, 0))
            return None

        engine = self.bounded_s2s
        if engine is None:
            return failed("engine_unavailable")
        if not getattr(self.audio, "private_pcm_supported", False):
            return failed("private_pcm_unavailable")
        if not callable(getattr(self.audio, "play_pcm_owned", None)):
            return failed("pcm_dispatch_unavailable")
        if len(self._owned_text_jobs) >= 2:
            return failed("render_capacity")
        from .s2s import CachedTTSProvider, ResilientVoiceProvider
        tts, voice = engine.tts, engine.voice
        while isinstance(tts, CachedTTSProvider):
            tts = tts.delegate
        if isinstance(voice, ResilientVoiceProvider):
            voice = voice.primary
        convert = getattr(voice, "convert_private", None)
        baseline = {}
        cancelled = lambda: not current()

        async def render():
            nonlocal stage, segment_index, failure_reason
            raw_segments = self._game_prompt_segments(effect)
            if not raw_segments:
                return None
            normalized_segments = []
            for segment_index, (text, buttons, phrase_id) in enumerate(raw_segments):
                if not current():
                    return None
                stage = "normalization"
                norm = engine.normalizer.normalize(text)
                if not norm or len(norm) > engine.config.max_response_chars:
                    failure_reason = "invalid_text"
                    return None
                normalized_segments.append((norm, buttons, phrase_id))
            stage = "validation"
            combined_text = "\n".join(text for text, _, _ in normalized_segments)
            if len(combined_text) <= engine.config.max_response_chars:
                validation = await engine.response_validator.validate_trusted(combined_text)
                if not current():
                    return None
                if not validation.allowed:
                    failure_reason = "validation_blocked"
                    return None
            else:
                for segment_index, (text, _, _) in enumerate(normalized_segments):
                    validation = await engine.response_validator.validate_trusted(text)
                    if not current():
                        return None
                    if not validation.allowed:
                        failure_reason = "validation_blocked"
                        return None

            # Load recorded assets individually; batch consecutive TTS-only segments
            # to avoid N sequential TTS round-trips while preserving proportional cues.
            prepared = []
            pending_tts = []

            async def flush_tts():
                nonlocal stage, failure_reason
                if not pending_tts:
                    return True
                joined = " ".join(item[0] for item in pending_tts)
                tts_text = joined
                if getattr(tts, "provider_name", None) == "vox555":
                    from .voice_pipeline import prepare_vox_text
                    tts_text = prepare_vox_text(joined)
                stage = "synthesis"
                from .experimental_voice import ExperimentalVoiceUnavailable
                busy_deadline = asyncio.get_running_loop().time() + 12.0
                while True:
                    try:
                        audio = await tts.synthesize(tts_text, token, cancelled)
                        break
                    except ExperimentalVoiceUnavailable as exc:
                        if str(exc) == "experimental_voice_busy" and current() and asyncio.get_running_loop().time() < busy_deadline:
                            await asyncio.sleep(0.08)
                            continue
                        raise
                if not current():
                    return False
                if not valid_pcm(audio):
                    failure_reason = "invalid_tts_pcm"
                    return False
                if len(pending_tts) == 1:
                    prepared.append((audio, pending_tts[0][1], False, ()))
                else:
                    total_chars = max(1, sum(len(item[0]) for item in pending_tts))
                    dur_s = len(audio.samples) / audio.sample_rate
                    sub_cues = []
                    pos = 0
                    for seg_text, seg_buttons in pending_tts:
                        sub_cues.append(((pos / total_chars) * dur_s, seg_buttons))
                        pos += len(seg_text)
                    prepared.append((audio, (), False, tuple(sub_cues)))
                pending_tts.clear()
                return True

            for segment_index, (text, buttons, phrase_id) in enumerate(normalized_segments):
                if not current():
                    return None
                stage = "asset_load"
                try:
                    audio = await asyncio.to_thread(self._load_game_asset, phrase_id, token) if phrase_id else None
                except Exception:
                    audio = None
                if audio is not None:
                    if not await flush_tts():
                        return None
                    if not valid_pcm(audio):
                        failure_reason = "invalid_recorded_pcm"
                        return None
                    prepared.append((audio, buttons, True, ()))
                else:
                    pending_tts.append((text, buttons))
            if not await flush_tts():
                return None

            stage, failure_reason = "baseline_assembly", "baseline_unavailable"
            base_assembled = await self._assemble_game_audio(
                [(audio, buttons) for audio, buttons, _, _ in prepared], token, current)
            if base_assembled is None:
                return None
            # Expand proportional sub_cues for batched TTS blocks.
            base_audio, base_cues = base_assembled
            expanded_cues = []
            for (cue_offset, cue_buttons), (_, _, _, sub_cues) in zip(base_cues, prepared):
                if sub_cues:
                    for sub_off, sub_btns in sub_cues:
                        expanded_cues.append((cue_offset + sub_off, sub_btns))
                else:
                    expanded_cues.append((cue_offset, cue_buttons))
            baseline["audio"] = (base_audio, tuple(expanded_cues))
            if not current() or not callable(convert):
                return baseline.get("audio")
            readiness = getattr(voice, "readiness", None)
            if callable(readiness) and not readiness().get("ready", False):
                return baseline.get("audio")
            enhanced = []
            enhancement_deadline = asyncio.get_running_loop().time() + min(
                getattr(self.audio, "owned_voice_timeout_s", 1.5), engine.config.max_turn_seconds)
            try:
                for audio, buttons, recorded, _ in prepared:
                    if recorded:
                        enhanced.append((audio, buttons))
                        continue
                    remaining = enhancement_deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        return baseline.get("audio")
                    stage = "voice_conversion"
                    async with asyncio.timeout(remaining):
                        converted = await convert(audio, cancelled)
                    if not current():
                        return None
                    if not valid_pcm(converted):
                        return baseline.get("audio")
                    enhanced.append((converted, buttons))
                stage, failure_reason = "enhanced_assembly", "enhanced_assembly_unavailable"
                enh_assembled = await self._assemble_game_audio(enhanced, token, current)
                if enh_assembled is None:
                    return None
                enh_audio, enh_cues = enh_assembled
                enh_expanded = []
                for (cue_offset, cue_buttons), (_, _, _, sub_cues) in zip(enh_cues, prepared):
                    if sub_cues:
                        for sub_off, sub_btns in sub_cues:
                            enh_expanded.append((cue_offset + sub_off, sub_btns))
                    else:
                        enh_expanded.append((cue_offset, cue_buttons))
                return (enh_audio, tuple(enh_expanded))
            except asyncio.CancelledError:
                raise
            except Exception:
                return baseline.get("audio")

        task = asyncio.create_task(render(), name="mishka-game-narration")
        task._mishka_session_id = session_id
        task._mishka_owner_current = current
        self._owned_text_jobs.add(task)
        def observe(done):
            self._owned_text_jobs.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(observe)
        try:
            done, _ = await asyncio.wait({task}, timeout=min(
                getattr(self.audio, "owned_render_timeout_s", 8.), engine.config.max_turn_seconds))
            if not done:
                task.cancel()
                if not current():
                    return None
                return baseline.get("audio") or failed("render_timeout")
            if not current():
                return None
            if task.cancelled():
                return failed("render_cancelled")
            return task.result() or failed(failure_reason)
        except asyncio.CancelledError:
            task.cancel()
            raise
        except Exception as exc:
            if not current():
                return None
            reason = ("native_queue_full" if stage == "synthesis" and isinstance(exc, RuntimeError)
                      and exc.args == ("Silero TTS worker queue is full",) else "render_exception")
            return baseline.get("audio") or failed(reason, exc)

    def _play_game_prompt(self, session_id, rendered, effect, token):
        audio, cues = rendered
        handle = PlaybackHandle(session_id=session_id, generation=self._session_epochs.get(session_id, 0),
            playback_id=f"{session_id}:{uuid.uuid4().hex}", playback_lease=effect["playback_lease"],
            response_id=effect["content_id"], token=token)
        self._register_audio_lease(session_id, handle.response_id, token, handle.playback_id,
            handle.playback_lease, duration_ms=int(audio.duration_ms))
        self._game_prompt_cues[session_id] = (handle.playback_id, handle.playback_lease,
            handle.generation, cues, str(effect.get("display") or "digits"))
        effect.update(playback_id=handle.playback_id, generation=handle.generation, session_id=session_id)
        try:
            self.audio.play_pcm_owned(handle, replace(audio, token=token, session_id=session_id,
                generation_id=handle.generation))
        except Exception as exc:
            self._invalidate_audio_lease(session_id, "failed")
            self._record_audio_completion_diagnostic(
                "game_prompt_dispatch_failed", session_id=session_id,
                operation="game_prompt_dispatch", generation=handle.generation,
                reason="pcm_dispatch_exception", exception_type=type(exc).__name__)
            self.record_audio_diagnostic("game_prompt_dispatch_failed", code="pcm_dispatch_exception",
                stage="dispatch", generation=handle.generation)
            raise

    def _cancel_game_cues(self, session_id):
        owner = self._game_prompt_cues.pop(session_id, None)
        if owner:
            self._game_cue_started.discard((session_id, owner[0]))
        task = self._game_cue_tasks.pop(session_id, None)
        if task is not None and not task.done():
            try:
                current = asyncio.current_task()
            except RuntimeError:
                current = None
            if task is not current:
                task.cancel()

    def _game_audio_started(self, session_id, playback_id, playback_lease, generation):
        owner = self._game_prompt_cues.get(session_id)
        if not owner or owner[:3] != (playback_id, playback_lease, generation):
            return
        if (session_id, playback_id) in self._game_cue_started:
            return
        state = self.canonical_runtime.sessions.get(session_id)
        def current():
            lease = self._active_audio.get(session_id)
            return bool(self._game_prompt_cues.get(session_id) is owner
                and self.canonical_runtime.sessions.get(session_id) is state
                and state is not None and state.mode == "game_active" and state.interaction_open
                and state.game_engine_state is not None and state.game_engine_state.seven_exit_phase == 'idle'
                and state.playback_lease == playback_lease
                and self._session_epochs.get(session_id, 0) == generation
                and lease and lease.get("playback_id") == playback_id
                and lease.get("playback_lease") == playback_lease)
        if not current():
            return
        self._game_cue_started.add((session_id, playback_id))
        started = self.monotonic_now()
        async def run():
            try:
                for offset, buttons in owner[3]:
                    while current() and self.monotonic_now() < started + offset:
                        await asyncio.sleep(max(.001, started + offset - self.monotonic_now()))
                    if not current():
                        return
                    cancel = getattr(self.led, "cancel_effects", None)
                    if callable(cancel):
                        cancel(clear=False)
                    active = dict(game_light_commands(buttons, owner[4]))
                    self.led.set_elements(tuple(LightingCommand(element, int(element in active), 140)
                        for element in LIGHTING_ELEMENT_IDS))
            except asyncio.CancelledError:
                return
            except Exception:
                self.record_audio_diagnostic("game_light_cue_failed", session_id=session_id)
            finally:
                if self._game_cue_tasks.get(session_id) is asyncio.current_task():
                    self._game_cue_tasks.pop(session_id, None)
        self._game_cue_tasks[session_id] = asyncio.create_task(run(), name="mishka-game-light-cues")

    def _static_game_cache(self):
        cache = getattr(self, "_static_game_audio", None)
        if cache is None:
            cache = OrderedDict()
            self._static_game_audio = cache
            self._static_game_audio_bytes = 0
        return cache

    def _game_cache_key(self, text: str) -> tuple[str, str, str, str]:
        from .s2s import CachedTTSProvider
        engine = self.bounded_s2s
        tts = getattr(engine, "tts", None)
        while isinstance(tts, CachedTTSProvider):
            tts = tts.delegate
        provider_fp = str(getattr(tts, "provider_name", None) or type(tts).__name__)
        voice_mode = str(getattr(engine, "voice_mode", "off") or "off").lower()
        return (text, _GAME_CONTENT_VERSION, provider_fp, voice_mode)

    def _get_cached_game_chunk(self, text: str):
        cache = self._static_game_cache()
        key = self._game_cache_key(text)
        chunk = cache.get(key)
        if chunk is not None and valid_pcm(chunk):
            cache.move_to_end(key)
            return chunk
        if key in cache:
            self._evict_game_cache_key(key)
        return None

    def _evict_game_cache_key(self, key) -> None:
        cache = self._static_game_cache()
        old = cache.pop(key, None)
        if old is not None:
            cur = getattr(self, "_static_game_audio_bytes", 0)
            self._static_game_audio_bytes = max(0, cur - len(old.samples) * 4)

    def _put_cached_game_chunk(self, text: str, chunk) -> None:
        if not valid_pcm(chunk):
            return
        entry_bytes = len(chunk.samples) * 4
        if entry_bytes > _GAME_CACHE_MAX_BYTES // 4:
            return
        cache = self._static_game_cache()
        key = self._game_cache_key(text)
        if key in cache:
            self._evict_game_cache_key(key)
        while cache and (
            len(cache) >= _GAME_CACHE_MAX_ENTRIES
            or getattr(self, "_static_game_audio_bytes", 0) + entry_bytes > _GAME_CACHE_MAX_BYTES
        ):
            evict_key, _ = cache.popitem(last=False)
            cur = getattr(self, "_static_game_audio_bytes", 0)
            # Recompute accurately on eviction
            self._static_game_audio_bytes = sum(len(v.samples) * 4 for v in cache.values())
        cache[key] = replace(chunk, token=0, session_id="", generation_id=0)
        self._static_game_audio_bytes = getattr(self, "_static_game_audio_bytes", 0) + entry_bytes

    def _game_tts_synthesis_lock(self) -> asyncio.Lock:
        lock = getattr(self, "_game_tts_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._game_tts_lock = lock
        return lock

    def _cancel_game_prewarm(self, session_id: str | None = None) -> None:
        tasks = getattr(self, "_game_prewarm_tasks", None)
        if not tasks:
            return
        if session_id is None:
            for task in list(tasks.values()):
                task.cancel()
            tasks.clear()
            return
        task = tasks.pop(session_id, None)
        meta = getattr(self, "_game_prewarm_meta", None)
        if meta is not None:
            meta.pop(session_id, None)
        if task is not None:
            task.cancel()

    async def _cancel_and_wait_game_prewarm(self, session_id: str) -> None:
        tasks = getattr(self, "_game_prewarm_tasks", None)
        meta = getattr(self, "_game_prewarm_meta", None)
        if meta is not None:
            meta.pop(session_id, None)
        if not tasks:
            return
        task = tasks.pop(session_id, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except BaseException:
                pass

    async def _await_or_cancel_game_prewarm(self, session_id: str, run_id: Any, texts: tuple[str, ...], current) -> None:
        tasks = getattr(self, "_game_prewarm_tasks", None)
        meta = getattr(self, "_game_prewarm_meta", None)
        if not tasks or session_id not in tasks:
            return
        task = tasks.get(session_id)
        if task is not None and not task.done() and meta and meta.get(session_id) == (run_id, texts):
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except BaseException:
                pass
            if not current() or not task.done():
                await self._cancel_and_wait_game_prewarm(session_id)
            else:
                tasks.pop(session_id, None)
                meta.pop(session_id, None)
            return
        await self._cancel_and_wait_game_prewarm(session_id)

    def _schedule_game_prompt_prewarm(self, session_id: str, state) -> None:
        from .game_engine import current_challenge, hold_exit_policy
        from .canonical_contract import IMMERSIVE_GAME_SPECS, game_answer_buttons
        self._cancel_game_prewarm(session_id)
        engine_state = getattr(state, "game_engine_state", None)
        if engine_state is None or not getattr(state, "interaction_open", False):
            return
        challenge = current_challenge(engine_state)
        spec = next((item for item in IMMERSIVE_GAME_SPECS if item.game_id == engine_state.game_id), None)
        if spec is None:
            return
        word_prompt = (
            "Сыграем небольшую партию. Назови животное или еду. Я назову другое."
            if state.age_band == "1-3" else
            "Сыграем небольшую партию. Называем слова на последнюю букву предыдущего. Мягкий и твёрдый знак, ы пропускаем. Назови первое слово."
            if state.age_band in {"5-7", "6-7"} else
            "Сыграем небольшую партию. Будем по очереди называть животных. Назови любое животное."
        )
        prompt = (challenge.prompt if challenge else {
            "free_lights": "Нажимай кнопки и смотри, как расходится свет.",
            "repeat_pattern": "Посмотри на огоньки. Потом повтори их порядок.",
            "word_game": word_prompt,
        }.get(engine_state.game_id, "Выбери ответ."))
        allowed = set(game_answer_buttons(engine_state))
        rules_intro = (
            "Я назову варианты. Выбери ответ кнопкой. Чтобы выйти, подержи семь."
            if engine_state.game_id == "quiz" and not getattr(state, "quiz_rules_spoken", False)
            else None
        )
        exit_hint = (
            None if engine_state.game_id == "quiz" else
            ("Чтобы выйти, держи семь. Когда скажу «Теперь отпускай», отпусти." if hold_exit_policy(engine_state)
             else "Чтобы выйти, нажми семь.")
            if engine_state.round == 0 else None
        )
        effect = {
            "game_id": engine_state.game_id,
            "prompt": prompt,
            "rules_intro": rules_intro,
            "options": [item for item in challenge.options if item[0] in allowed] if challenge else [],
            "is_hint": False,
            "phrase_id": (challenge.phrase_id if challenge else spec.instruction_phrase_id
                          if engine_state.game_id in {"free_lights", "repeat_pattern"} else None),
            "answer_buttons": list(allowed),
            "exit_hint": exit_hint,
        }
        segments = self._game_prompt_segments(effect)
        run_id = getattr(engine_state, "run_id", None)
        epoch = self._session_epochs.get(session_id, 0)

        async def _prewarm() -> None:
            s2s = self.bounded_s2s
            if s2s is None or getattr(s2s, "tts", None) is None:
                return
            raw_tts = s2s.tts.delegate if hasattr(s2s.tts, "delegate") else s2s.tts
            vc_mode = str(getattr(s2s, "voice_mode", getattr(getattr(s2s, "settings", None), "voice_conversion", "off")) or "off").lower()
            # Streaming TTS yields the first 0.4s block in ~250ms; running a blocking full-segment
            # synthesize() prewarm contends for the single worker lock after short intro WAVs.
            if hasattr(raw_tts, "synthesize_audio_stream") and vc_mode in {"off", "disabled"}:
                return
            lock = self._game_tts_synthesis_lock()
            for raw_text, _buttons, phrase_id in segments:
                if phrase_id:
                    continue
                st = self.canonical_runtime.sessions.get(session_id)
                cur_eng = getattr(st, "game_engine_state", None) if st is not None else None
                if (st is None or st.mode != "game_active" or not st.interaction_open
                        or self._session_epochs.get(session_id, 0) != epoch
                        or cur_eng is None or cur_eng.run_id != run_id):
                    return
                norm = s2s.normalizer.normalize(raw_text)
                if not norm or self._get_cached_game_chunk(norm) is not None:
                    continue
                try:
                    async with lock:
                        st2 = self.canonical_runtime.sessions.get(session_id)
                        cur_eng2 = getattr(st2, "game_engine_state", None) if st2 is not None else None
                        if (st2 is None or st2.mode != "game_active" or not st2.interaction_open
                                or self._session_epochs.get(session_id, 0) != epoch
                                or cur_eng2 is None or cur_eng2.run_id != run_id):
                            return
                        if self._get_cached_game_chunk(norm) is not None:
                            continue
                        prewarm_cancelled = lambda: (
                            (self.canonical_runtime.sessions.get(session_id) is None)
                            or self._session_epochs.get(session_id, 0) != epoch
                            or getattr(getattr(self.canonical_runtime.sessions.get(session_id), "game_engine_state", None), "run_id", None) != run_id
                        )
                        raw_tts = s2s.tts.delegate if hasattr(s2s.tts, "delegate") else s2s.tts
                        tts_norm = norm
                        if getattr(raw_tts, "provider_name", None) == "vox555":
                            from .voice_pipeline import prepare_vox_text
                            tts_norm = prepare_vox_text(norm)
                        audio = await s2s.tts.synthesize(tts_norm, 0, prewarm_cancelled)
                        chunk = await self._to_24k_chunk(audio, 0, lambda: True, pad_s=0.05)
                        if chunk is not None:
                            self._put_cached_game_chunk(norm, chunk)
                        # Only prewarm the leading uncached segment when streaming TTS is available
                        # so subsequent prompt segments start streaming immediately after intro playback.
                        if hasattr(raw_tts, "synthesize_audio_stream"):
                            return
                except asyncio.CancelledError:
                    raise
                except Exception:
                    return

        tasks = getattr(self, "_game_prewarm_tasks", None)
        if tasks is None:
            tasks = {}
            self._game_prewarm_tasks = tasks
        meta = getattr(self, "_game_prewarm_meta", None)
        if meta is None:
            meta = {}
            self._game_prewarm_meta = meta
        meta[session_id] = (run_id, tuple(raw_text for raw_text, _, pid in segments if not pid))
        task = asyncio.create_task(_prewarm(), name=f"mishka-game-prewarm-{session_id}")
        tasks[session_id] = task
        task.add_done_callback(lambda t: tasks.pop(session_id, None) if tasks.get(session_id) is t else None)

    async def _to_24k_chunk(self, audio, token, current, *, pad_s: float = 0.05):
        from .s2s import AudioChunk
        from .voice_pipeline import peak_normalize, resample_mono
        if not current() or not valid_pcm(audio):
            return None
        rate = 24000
        converted = (audio.samples if audio.sample_rate == rate else
                     await asyncio.to_thread(resample_mono, audio.samples, audio.sample_rate, rate))
        if not current():
            return None
        values = tuple(converted) + ((0.,) * int(rate * pad_s) if pad_s > 0 else ())
        if values and all(math.isfinite(v) for v in values) and max(map(abs, values)) > 1.:
            values = peak_normalize(values, safe_peak=1.)
        out = AudioChunk(values, rate, token)
        return out if valid_pcm(out) else None

    async def warm_static_game_audio(self):
        engine = self.bounded_s2s
        tts = engine.tts
        lock = self._game_tts_synthesis_lock()
        for phrase in STATIC_GAME_PHRASES:
            norm = engine.normalizer.normalize(phrase)
            if not norm or self._get_cached_game_chunk(norm) is not None:
                continue
            try:
                async with lock:
                    audio = await tts.synthesize(norm, 0, lambda: False)
                    chunk = await self._to_24k_chunk(audio, 0, lambda: True, pad_s=0.05)
                    if chunk is not None:
                        self._put_cached_game_chunk(norm, chunk)
            except Exception:
                continue

    async def _validate_game_segments(self, effect, token, current):
        engine = self.bounded_s2s
        raw_segments = self._game_prompt_segments(effect)
        if not raw_segments:
            return None, "empty_prompt"
        normalized_segments = []
        for text, buttons, phrase_id in raw_segments:
            if not current():
                return None, "cancelled"
            norm = engine.normalizer.normalize(text)
            if not norm or len(norm) > engine.config.max_response_chars:
                return None, "invalid_text"
            normalized_segments.append((norm, buttons, phrase_id))
        combined_text = "\n".join(text for text, _, _ in normalized_segments)
        if len(combined_text) <= engine.config.max_response_chars:
            validation = await engine.response_validator.validate_trusted(combined_text)
            if not current():
                return None, "cancelled"
            if not validation.allowed:
                return None, "validation_blocked"
        else:
            for text, _, _ in normalized_segments:
                validation = await engine.response_validator.validate_trusted(text)
                if not current():
                    return None, "cancelled"
                if not validation.allowed:
                    return None, "validation_blocked"
        return normalized_segments, None

    async def _stream_game_prompt(self, session_id, effect, token, current):
        from .s2s import AudioChunk, CachedTTSProvider
        st_pre = self.canonical_runtime.sessions.get(session_id)
        run_id_pre = getattr(getattr(st_pre, "game_engine_state", None), "run_id", None) if st_pre is not None else None
        raw_texts = tuple(text for text, _, pid in self._game_prompt_segments(effect) if not pid)
        await self._await_or_cancel_game_prewarm(session_id, run_id_pre, raw_texts, current)
        if not current():
            return False
        normalized_segments, failure_reason = await self._validate_game_segments(effect, token, current)
        if not normalized_segments:
            self._record_game_prompt_failure(session_id, effect, token, failure_reason or "empty_prompt", "validation", None, None)
            return False
        engine = self.bounded_s2s
        tts = engine.tts
        while isinstance(tts, CachedTTSProvider):
            tts = tts.delegate
        voice_mode = str(getattr(engine, "voice_mode", "off") or "off").lower()
        can_stream_tts = hasattr(tts, "synthesize_audio_stream") and voice_mode in {"off", "disabled"}
        handle = PlaybackHandle(
            session_id=session_id,
            generation=self._session_epochs.get(session_id, 0),
            playback_id=f"{session_id}:{uuid.uuid4().hex}",
            playback_lease=effect["playback_lease"],
            response_id=effect["content_id"],
            token=token,
        )
        self._register_audio_lease(session_id, handle.response_id, token, handle.playback_id, handle.playback_lease, duration_ms=0)
        effect.update(playback_id=handle.playback_id, generation=handle.generation, session_id=session_id)
        display = str(effect.get("display") or "digits")
        default_buttons = tuple(effect.get("answer_buttons") or ())

        def on_cue(buttons):
            st = self.canonical_runtime.sessions.get(session_id)
            eng = getattr(st, "game_engine_state", None) if st is not None else None
            if not current() or eng is None or eng.seven_exit_phase != "idle":
                return
            st.game_prompt_audible = True
            target = buttons if buttons else default_buttons
            cancel = getattr(self.led, "cancel_effects", None)
            if callable(cancel):
                cancel(clear=False)
            active = dict(game_light_commands(target, display))
            self.led.set_elements(tuple(LightingCommand(element, int(element in active), 140)
                for element in LIGHTING_ELEMENT_IDS))

        async def segment_stream():
            cancelled = lambda: not current()
            for text, buttons, phrase_id in normalized_segments:
                if not current():
                    return
                audio = None
                if phrase_id:
                    try:
                        audio = await asyncio.to_thread(self._load_game_asset, phrase_id, token)
                    except Exception:
                        audio = None
                if audio is not None:
                    chunk = await self._to_24k_chunk(audio, token, current, pad_s=0.05)
                    if chunk is None:
                        if not current():
                            return
                        raise RuntimeError("incomplete_game_prompt_asset")
                    yield replace(chunk, token=token, session_id=session_id, generation_id=handle.generation), buttons
                    continue
                cached = self._get_cached_game_chunk(text)
                if cached is not None:
                    yield replace(cached, token=token, session_id=session_id, generation_id=handle.generation), buttons
                    continue
                if can_stream_tts:
                    from .experimental_voice import ExperimentalVoiceUnavailable
                    tts_text = text
                    if getattr(tts, "provider_name", None) == "vox555":
                        from .voice_pipeline import prepare_vox_text
                        tts_text = prepare_vox_text(text)
                    deadline = asyncio.get_running_loop().time() + 12.0
                    while True:
                        first_sub = True
                        collected_samples = []
                        try:
                            async for sub in tts.synthesize_audio_stream(tts_text, token, cancelled, first_chunk=True):
                                if not current():
                                    return
                                chunk = await self._to_24k_chunk(sub, token, current, pad_s=0.0)
                                if chunk is None:
                                    if not current():
                                        return
                                    raise RuntimeError("incomplete_game_prompt_stream")
                                if first_sub and chunk.samples:
                                    vals = list(chunk.samples)
                                    # Trim leading dead silence / resampler edge blip and apply a 10ms fade-in.
                                    onset = 0
                                    while onset < len(vals) and abs(vals[onset]) < 0.004:
                                        onset += 1
                                    lead = max(0, onset - int(chunk.sample_rate * 0.015))
                                    if lead > 0 and lead < len(vals):
                                        vals = vals[lead:]
                                    fade = min(len(vals), int(chunk.sample_rate * 0.010))
                                    for fi in range(fade):
                                        vals[fi] *= fi / max(1, fade)
                                    chunk = replace(chunk, samples=tuple(vals))
                                collected_samples.extend(chunk.samples)
                                yield replace(chunk, token=token, session_id=session_id, generation_id=handle.generation), (buttons if first_sub else ())
                                first_sub = False
                            break
                        except ExperimentalVoiceUnavailable as exc:
                            if str(exc) == "experimental_voice_busy" and first_sub and current() and asyncio.get_running_loop().time() < deadline:
                                await asyncio.sleep(0.08)
                                continue
                            raise
                    if first_sub:
                        if not current():
                            return
                        raise RuntimeError("empty_game_prompt_segment")
                    if current():
                        tail = (0.,) * int(24000 * 0.05)
                        yield AudioChunk(tail, 24000, token, session_id, handle.generation), ()
                        collected_samples.extend(tail)
                        self._put_cached_game_chunk(text, AudioChunk(tuple(collected_samples), 24000, 0))
                    continue
                audio = await engine.tts.synthesize(text, token, cancelled)
                chunk = await self._to_24k_chunk(audio, token, current, pad_s=0.05)
                if chunk is None:
                    if not current():
                        return
                    raise RuntimeError("incomplete_game_prompt_tts")
                self._put_cached_game_chunk(text, chunk)
                yield replace(chunk, token=token, session_id=session_id, generation_id=handle.generation), buttons

        try:
            self.audio.play_pcm_stream_owned(handle, segment_stream(), on_cue=on_cue)
        except Exception:
            self._invalidate_audio_lease(session_id, "failed")
            raise
        return True

    def _record_game_prompt_failure(self, session_id, effect, token, reason, stage, segment_index, exc):
        self._record_audio_completion_diagnostic(
            "game_prompt_render_failed", session_id=session_id, operation="game_prompt_render",
            generation=self._session_epochs.get(session_id, 0), reason=reason, stage=stage,
            segment_index=segment_index, exception_type=type(exc).__name__ if exc else None)
        self.record_audio_diagnostic("game_prompt_render_failed", code=reason, stage=stage,
            segment_index=segment_index, exception_type=type(exc).__name__ if exc else None)

    @staticmethod
    def _word_game_instructions(age_band):
        if age_band == "1-3":
            rule = "Назови другое знакомое животное или еду и передай очередь одной короткой фразой. Весь ответ до 8 слов. Не требуй букв, чтения или счёта."
        elif age_band in {"6-7", "5-7"}:
            rule = (
                "Играйте в цепочку слов строго по последней букве: определи последнюю букву слова ребёнка "
                "(если это Ь, Ъ или Ы — бери предыдущую букву), назови ОДНО понятное ребёнку русское слово, "
                "которое начинается ровно на эту букву, и попроси назвать слово на последнюю букву твоего слова. "
                "Если ребёнок назвал слово не на ту букву, мягко напомни нужную букву."
            )
        else:
            rule = "Играйте по очереди в знакомых животных: похвали ответ ребёнка, назови ОДНО знакомое животное (не предмет и не еду) и передай ход."
        return ("\nТЕКУЩАЯ ИГРА В СЛОВА: " + rule
                + " Один короткий ход, затем жди ответа. Не превращай игру в обычную беседу."
                + " Если ниже указан последний ход, завершение важнее приглашения назвать следующее слово."
                + " Отказ играть принимай сразу. Все правила детской безопасности и текущий формат ответа сохраняются.")
