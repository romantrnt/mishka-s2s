"""Bounded ephemeral rendering for typed facts, never an unowned play_text call."""
from __future__ import annotations
import asyncio
from dataclasses import replace
from .domain import PlaybackHandle
from .private_welcome import valid_pcm

TYPED_AUDIO_IDS = frozenset({'TYPED_MENU','TYPED_DATE','TYPED_WEATHER','TYPED_WHAT_NEXT','TYPED_WHERE_NOW'})
PRIVATE_TYPED_AUDIO_IDS = frozenset({'TYPED_WHAT_NEXT','TYPED_WHERE_NOW'})
from .scene_menus import MENU_PCM_MODES
MENU_AUDIO_IDS = frozenset(MENU_PCM_MODES)
OWNED_PCM_IDS = TYPED_AUDIO_IDS | MENU_AUDIO_IDS | {'PERSONAL_WELCOME', 'PERSONAL_STORY'}


class OwnedSpeechMixin:
    async def _menu_validation_text(self, session_id, text):
        """Only the exact verified public page may use the catalogue exception."""
        state = self.canonical_runtime.sessions.get(session_id)
        if state is None or state.mode not in MENU_PCM_MODES.values():
            return None
        page = self.canonical_runtime._menu_page(state)
        normalizer = self.bounded_s2s.normalizer
        if text != normalizer.normalize(page.spoken_text) or state.scene_menu_descriptor != page.as_dict():
            return None
        validation_text = page.spoken_text
        validator = self.bounded_s2s.response_validator
        for item in page.items:
            decision = await validator.validate_trusted(item.title)
            if decision.allowed:
                continue
            verified = (self.canonical_runtime.stories.get_verified(item.id)
                        if page.scene == 'fixed_story_menu' else None)
            if not (decision.reason == 'personal_data' and item.id == 'fixed_neznayka_v1'
                    and item.title == 'Телефон' and verified
                    and verified.get('display_title') == 'Телефон'
                    and verified.get('audio_sha256') == '239c1c3d0308d2a20693c8818dfd5c77a04a1d030f0e69554b2c3cd4971637db'):
                return None
            # The descriptor is constructed locally, so this is one exact title
            # span, not arbitrary model text or a blanket catalogue bypass.
            words = {1:'один',2:'два',3:'три',4:'четыре'}
            span = f'Кнопка {words[item.slot]}: Телефон.'
            if validation_text.count(span) != 1:
                return None
            validation_text = validation_text.replace(span, f'Кнопка {words[item.slot]}: Сказка.', 1)
        return normalizer.normalize(validation_text)

    def _owned_menu_voice_capable(self):
        engine = self.bounded_s2s
        if (engine is None or not getattr(self.audio, 'private_pcm_supported', False)
                or not callable(getattr(self.audio, 'play_pcm_owned', None))):
            return False
        from .s2s import CachedTTSProvider, ResilientVoiceProvider
        tts, voice = getattr(engine, "tts", None), getattr(engine, "voice", None)
        while isinstance(tts, CachedTTSProvider):
            tts = tts.delegate
        if isinstance(voice, ResilientVoiceProvider):
            voice = voice.primary
        return (callable(getattr(tts, 'synthesize', None))
                and callable(getattr(voice, 'convert_private', None))
                and callable(getattr(getattr(engine, 'response_validator', None), 'validate_trusted', None))
                and callable(getattr(getattr(engine, 'normalizer', None), 'normalize', None))
                and getattr(getattr(engine, 'config', None), 'max_response_chars', 0) > 0)

    def _cancel_owned_text(self, session_id):
        for task in tuple(self._owned_text_jobs):
            if getattr(task, '_mishka_session_id', None) == session_id:
                task.cancel()

    async def _render_typed_audio(self, session_id, text, effect, token, current):
        engine = self.bounded_s2s
        if (engine is None or not getattr(self.audio, 'private_pcm_supported', False)
                or not callable(getattr(self.audio, 'play_pcm_owned', None))):
            return None
        # Native providers can resist cancellation. Keep a hard admission cap,
        # retaining their owner until actual completion instead of spawning more.
        if len(self._owned_text_jobs) >= 2:
            return None
        text = engine.normalizer.normalize(str(text or ''))
        if not text or len(text) > engine.config.max_response_chars:
            return None
        from .s2s import CachedTTSProvider, ResilientVoiceProvider
        tts, voice = engine.tts, engine.voice
        while isinstance(tts, CachedTTSProvider):
            tts = tts.delegate
        if isinstance(voice, ResilientVoiceProvider):
            voice = voice.primary
        convert = getattr(voice, 'convert_private', None)
        if not callable(convert):
            return None
        cancelled = lambda: not current()
        async def render():
            if cancelled():return None
            validation_text = text
            if effect.get('content_id') in MENU_AUDIO_IDS:
                validation_text = await self._menu_validation_text(session_id, text)
                if validation_text is None:
                    return None
                validation = await engine.response_validator.validate_trusted(validation_text)
            else:
                validation = await engine.response_validator.validate(text)
            if cancelled() or not validation.allowed:return None
            audio = await tts.synthesize(text, token, cancelled)
            if cancelled() or not valid_pcm(audio):return None
            audio = await convert(audio, cancelled)
            if cancelled() or not valid_pcm(audio):return None
            return audio
        task = asyncio.create_task(render(),name='mishka-typed-audio')
        task._mishka_session_id = session_id
        task._mishka_owner_current = current
        self._owned_text_jobs.add(task)
        def observe(done):
            self._owned_text_jobs.discard(done)
            if not done.cancelled():done.exception()
        task.add_done_callback(observe)
        try:
            done,_ = await asyncio.wait({task},timeout=min(8.,engine.config.max_turn_seconds))
            if not done:
                task.cancel()
                return None
            return task.result() if current() and not task.cancelled() else None
        except asyncio.CancelledError:
            task.cancel()
            raise
        except Exception:
            return None

    def _play_typed_audio(self, session_id, audio, effect, token):
        response_id = effect['content_id']
        if response_id not in TYPED_AUDIO_IDS | MENU_AUDIO_IDS:
            raise ValueError('typed_audio_invalid_owner')
        import uuid
        handle = PlaybackHandle(session_id=session_id,
            generation=self._session_epochs.get(session_id,0),
            playback_id=f'{session_id}:{uuid.uuid4().hex}',
            playback_lease=effect['playback_lease'],response_id=response_id,token=token)
        self._register_audio_lease(session_id,response_id,token,handle.playback_id,
            handle.playback_lease,duration_ms=int(1000*len(audio.samples)/audio.sample_rate))
        effect.update(playback_id=handle.playback_id,generation=handle.generation,session_id=session_id)
        try:
            self.audio.play_pcm_owned(handle,replace(audio,token=token,
                session_id=session_id,generation_id=handle.generation))
        except Exception:
            self._invalidate_audio_lease(session_id,'failed')
            raise
