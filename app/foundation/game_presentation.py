"""Owned public game narration and playback-anchored light cues."""
from __future__ import annotations

import asyncio
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
_COLOR_LABELS = {1:"Красный", 2:"Оранжевый", 3:"Жёлтый", 4:"Зелёный", 5:"Голубой", 6:"Синий", 7:"Фиолетовый"}
_DIGIT_LABELS = {1:"Один", 2:"Два", 3:"Три", 4:"Четыре", 5:"Пять", 6:"Шесть", 7:"Семь"}


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
        if (state is None or state.mode != "game_active" or state.handset != "up"
                or state.game_audio_stage != "playing" or engine is None):
            return None
        return (id(state), self._session_epochs.get(session_id, 0), state.active_game_id,
                engine.run_id, state.game_audio_stage, state.active_audio_id, state.playback_lease)

    def _game_prompt_segments(self, effect):
        prompt = str(effect.get("prompt") or "").strip()
        segments = []
        if effect.get("feedback_phrase_id"):
            segments.append((str(effect.get("feedback_text") or "Получилось!"), (), effect["feedback_phrase_id"]))
        if prompt:
            segments.append((prompt, (), effect.get("phrase_asset_id")))
        narrate_options = (effect.get("game_id") == "odd_one_out" or
            (effect.get("game_id") == "quiz" and effect.get("display", "digits") == "digits"))
        if narrate_options and effect.get("options"):
            labels = _COLOR_LABELS if effect.get("display") == "colors" else _DIGIT_LABELS
            for button, text in tuple(effect.get("options") or ())[:7]:
                if type(button) is int and button in labels:
                    segments.append((f"{labels[button]}: {text}.", (button,), None))
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
            chunks = []
            for segment_index, (text, buttons, phrase_id) in enumerate(self._game_prompt_segments(effect)):
                if not current():
                    return None
                stage = "normalization"
                text = engine.normalizer.normalize(text)
                if not text or len(text) > engine.config.max_response_chars:
                    failure_reason = "invalid_text"
                    return None
                stage = "validation"
                validation = await engine.response_validator.validate_trusted(text)
                if not current():
                    return None
                if not validation.allowed:
                    failure_reason = "validation_blocked"
                    return None
                stage = "asset_load"
                try:
                    audio = await asyncio.to_thread(self._load_game_asset, phrase_id, token) if phrase_id else None
                except Exception:
                    audio = None
                recorded = audio is not None
                if audio is None:
                    stage = "synthesis"
                    audio = await tts.synthesize(text, token, cancelled)
                if not current():
                    return None
                if not valid_pcm(audio):
                    failure_reason = "invalid_recorded_pcm" if recorded else "invalid_tts_pcm"
                    return None
                chunks.append((audio, buttons, recorded))
            stage, failure_reason = "baseline_assembly", "baseline_unavailable"
            baseline["audio"] = await self._assemble_game_audio(
                [(audio, buttons) for audio, buttons, _ in chunks], token, current)
            if not current() or not callable(convert):
                return baseline.get("audio")
            readiness = getattr(voice, "readiness", None)
            if callable(readiness) and not readiness().get("ready", False):
                return baseline.get("audio")
            enhanced = []
            enhancement_deadline = asyncio.get_running_loop().time() + min(
                getattr(self.audio, "owned_voice_timeout_s", 1.5), engine.config.max_turn_seconds)
            try:
                for audio, buttons, recorded in chunks:
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
                return await self._assemble_game_audio(enhanced, token, current)
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
                and state is not None and state.mode == "game_active" and state.handset == "up"
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

    @staticmethod
    def _word_game_instructions(age_band):
        if age_band == "1-3":
            rule = "Называй знакомое животное или еду и предлагай назвать ещё одно. Не требуй букв, чтения или счёта."
        elif age_band in {"6-7", "5-7"}:
            rule = "Играйте в цепочку слов по последней букве. Ь, Ъ и Ы пропускайте. Подсказывай коротко и доброжелательно."
        else:
            rule = "Играйте по очереди в знакомые слова категории животные. После удачного ответа предложи следующее слово."
        return ("\nТЕКУЩАЯ ИГРА В СЛОВА: " + rule
                + " Один короткий ход, затем жди ответа. Не превращай игру в обычную беседу."
                + " Отказ играть принимай сразу. Все правила детской безопасности и формат тегов сохраняются.")
