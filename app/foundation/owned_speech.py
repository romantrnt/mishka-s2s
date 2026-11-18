"""Bounded ephemeral rendering for typed facts, never an unowned play_text call."""
from __future__ import annotations
import asyncio
from dataclasses import replace
from .domain import PlaybackHandle
from .private_welcome import valid_pcm

TYPED_AUDIO_IDS = frozenset({'TYPED_MENU','TYPED_DATE','TYPED_WEATHER','TYPED_WHAT_NEXT','TYPED_WHERE_NOW', 'TYPED_CARD_DOCTORS',
    'DIALOGUE_OFFER', 'DIALOGUE_RETURN', 'DIALOGUE_FINISH'})
PRIVATE_TYPED_AUDIO_IDS = frozenset({'TYPED_WHAT_NEXT','TYPED_WHERE_NOW', 'TYPED_CARD_DOCTORS'})
from .scene_menus import MENU_PCM_MODES
MENU_AUDIO_IDS = frozenset(MENU_PCM_MODES)
OWNED_PCM_IDS = TYPED_AUDIO_IDS | MENU_AUDIO_IDS | {'PERSONAL_WELCOME', 'PERSONAL_STORY', 'GAME_PROMPT', 'GAME_HINT'}


class OwnedSpeechMixin:
    async def _menu_validation_text(self, session_id, text):
        """Only the exact verified public page may use the catalogue exception."""
        from .public_menu_audio import current_menu_page, menu_validation_text
        page = current_menu_page(self, session_id, text)
        return await menu_validation_text(self, page, text) if page is not None else None

    async def warm_public_menu_audio(self, *, progress=None):
        from .public_menu_audio import warm_public_menu_audio
        return await warm_public_menu_audio(self, progress=progress)

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
            render_tts, render_convert = tts, convert
            validation_text = text
            page = key = cache = None
            if effect.get('content_id') in MENU_AUDIO_IDS:
                from .public_menu_audio import PublicMenuAudioCache, current_menu_page
                page = current_menu_page(self, session_id, text)
                if page is None or MENU_PCM_MODES[effect['content_id']] != page.scene:
                    return None
                validation_text = await self._menu_validation_text(session_id, text)
                if validation_text is None:
                    return None
                validation = await engine.response_validator.validate_trusted(validation_text)
                if current_menu_page(self, session_id, text) != page:
                    return None
                # Capture the same live provider pair whose identity keys the
                # cache, after all awaited descriptor validation has finished.
                from .public_menu_audio import providers
                render_tts, render_voice = providers(self)
                render_convert = getattr(render_voice, 'convert_private', None)
                if not callable(render_convert):
                    return None
                candidate = getattr(self, '_public_menu_audio_cache', None)
                if isinstance(candidate, PublicMenuAudioCache):
                    cache = candidate
                    key = cache.key(self, page, text)
            elif effect.get('content_id') == 'TYPED_CARD_DOCTORS':
                # This exact application template contains private source names.
                # Validate its current atomic DTO and rendering receipt locally;
                # never send those names to the semantic/model-output gate.
                from .identity import _card_doctor_facts
                from .canonical_services import TypedInfoRenderer
                if cancelled() or not self._identity_scope_live(session_id, 'personalize'):
                    return None
                try:
                    facts = _card_doctor_facts(self._canonical_profiles.get(session_id) or {})
                    expected = TypedInfoRenderer(None, facts, None)._card_doctors_text()
                except (ValueError, TypeError, KeyError, RuntimeError):
                    return None
                if engine.normalizer.normalize(expected) != text:
                    return None
                validation = await engine.response_validator.validate_trusted(text)
            else:
                validation = await engine.response_validator.validate(text)
            if cancelled() or not validation.allowed:return None
            if cache is not None:
                cached = cache.get(key, token, session_id)
                if cached is not None:
                    return cached
            audio = await render_tts.synthesize(text, token, cancelled)
            if cancelled() or not valid_pcm(audio):return None
            audio = await render_convert(audio, cancelled)
            if cancelled() or not valid_pcm(audio):return None
            if cache is not None:
                # A cancellation-resistant provider cannot populate a menu
                # after a timeout, remapping or revoked semantic owner.
                if (asyncio.current_task().cancelling()
                        or current_menu_page(self, session_id, text) != page
                        or cache.key(self, page, text) != key):
                    return None
                cache.put(key, audio)
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
            done,_ = await asyncio.wait({task},timeout=min(
                getattr(self.audio, 'owned_render_timeout_s', 8.), engine.config.max_turn_seconds))
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
