"""Bounded ephemeral rendering for typed facts, never an unowned play_text call."""
from __future__ import annotations
import asyncio
from dataclasses import replace
from .domain import PlaybackHandle
from .private_welcome import valid_pcm
from .scene_menus import MENU_PCM_MODES, CATALOGUE_CLARIFY, CATALOGUE_CLARIFY_TEXT

TYPED_AUDIO_IDS = frozenset({'TYPED_MENU','TYPED_DATE','TYPED_WEATHER','TYPED_WHAT_NEXT','TYPED_WHERE_NOW', 'TYPED_CARD_DOCTORS',
    'DIALOGUE_INTRO', 'DIALOGUE_OFFER', 'DIALOGUE_RETURN', 'DIALOGUE_FINISH', 'NAME_BUTTON_HINT', 'NAME_CONFIRMATION', 'AGE_ACCEPTED', CATALOGUE_CLARIFY})
PRIVATE_TYPED_AUDIO_IDS = frozenset({'TYPED_WHAT_NEXT','TYPED_WHERE_NOW', 'TYPED_CARD_DOCTORS'})
MENU_AUDIO_IDS = frozenset(MENU_PCM_MODES)
_PUBLIC_DIALOGUE_INTRO = 'Давай поговорим. Чтобы вернуться в меню, нажми семь.'
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

    async def _stream_menu_audio(self, session_id, text, effect, token, current):
        """None means unsupported; False forbids replay, True means queued."""
        from .experimental_voice import ExperimentalTTSProvider
        from .public_menu_audio import PublicMenuAudioCache, current_menu_page, providers
        from .s2s import AudioChunk, PassthroughVoiceProvider
        engine = self.bounded_s2s
        public_intro = (effect.get('content_id') == 'DIALOGUE_INTRO'
                        and text == _PUBLIC_DIALOGUE_INTRO)
        if (engine is None or (effect.get('content_id') not in MENU_AUDIO_IDS and not public_intro)
                or not getattr(self.audio, 'private_pcm_supported', False)
                or not getattr(self.audio, 'private_pcm_stream_supported', False)
                or not callable(getattr(self.audio, 'play_pcm_stream_owned', None))):
            return None
        tts, voice = providers(self)
        if (not isinstance(tts, ExperimentalTTSProvider) or tts.provider_name == 'qwen765'
                or not callable(getattr(tts, 'synthesize_audio_stream', None))
                or not isinstance(voice, PassthroughVoiceProvider)
                or getattr(self.settings, 'voice_conversion', None) not in {'off', 'disabled'}):
            return None
        if len(self._owned_text_jobs) >= 2:
            return False
        text = engine.normalizer.normalize(str(text or ''))
        if not text or len(text) > engine.config.max_response_chars:
            return False
        loop = asyncio.get_running_loop()
        generation_budget = min(float(getattr(self.audio, 'owned_render_timeout_s', 8.)),
                                engine.config.max_turn_seconds)
        lifetime = min(60., float(getattr(self.settings, 'playback_timeout_s', 60.)))
        queue, changed = asyncio.Queue(maxsize=2), asyncio.Event()
        status = {'aborted': False, 'complete': False, 'cached': None}
        page = key = cache = None
        normalizer, validator = engine.normalizer, engine.response_validator

        def live():
            return bool(not status['aborted'] and not producer.cancelling()
                and not self._identity_shutdown and current()
                and self.bounded_s2s is engine and engine.normalizer is normalizer
                and engine.response_validator is validator
                and all(a is b for a, b in zip(providers(self), (tts, voice)))
                and (page is None or current_menu_page(self, session_id, text) == page)
                and (cache is None or cache.key(self, page, text) == key))

        def abort():
            status['aborted'] = True
            changed.set()
            if not producer.done():
                producer.cancel()

        async def generate():
            nonlocal page, key, cache, generation_budget
            iterator = None
            # Provider generation excludes queue backpressure and real-time playback.
            async def bounded(awaitable):
                nonlocal generation_budget
                started = loop.time()
                timer = loop.call_later(max(0., generation_budget), abort)
                try:
                    return await awaitable
                finally:
                    timer.cancel()
                    generation_budget -= loop.time() - started
            overall = loop.call_later(lifetime, abort)
            try:
                if not live():
                    return
                if public_intro:
                    # This one authored literal has no session facts or catalogue data.
                    validation_text = text
                else:
                    page = current_menu_page(self, session_id, text)
                    if page is None or MENU_PCM_MODES[effect['content_id']] != page.scene:
                        return
                    validation_text = await bounded(self._menu_validation_text(session_id, text))
                if validation_text is None or not live():
                    return
                validation = await bounded(validator.validate_trusted(validation_text))
                if not validation.allowed or not live():
                    return
                candidate = getattr(self, '_public_menu_audio_cache', None)
                if page is not None and isinstance(candidate, PublicMenuAudioCache):
                    cache = candidate
                    key = cache.key(self, page, text)
                    status['cached'] = cache.get(key, token, session_id)
                    if status['cached'] is not None:
                        return
                await bounded(tts.wait_for_idle(lambda: not live()))
                if not live():
                    return
                iterator = tts.synthesize_audio_stream(text, token, lambda: not live(), first_chunk=True).__aiter__()
                samples, rate, count = [], None, 0
                while live():
                    # At most two pending chunks, in addition to the one playing.
                    while queue.full() and live():
                        changed.clear()
                        await changed.wait()
                    if not live():
                        return
                    try:
                        audio = await bounded(anext(iterator))
                    except StopAsyncIteration:
                        break
                    if not live():
                        return
                    count += 1
                    if (not valid_pcm(audio) or audio.token != token
                            or audio.session_id not in (None, session_id)
                            or not 8000 <= audio.sample_rate <= 96000
                            or (rate is not None and audio.sample_rate != rate)
                            # Native frames are smaller than semantic TTS chunks.
                            or count > 4096):
                        raise ValueError('menu_native_pcm_invalid')
                    rate = audio.sample_rate
                    if len(samples) + len(audio.samples) > rate * lifetime:
                        raise ValueError('menu_native_pcm_limit')
                    samples.extend(audio.samples)
                    queue.put_nowait(audio)
                    changed.set()
                if not samples or not live():
                    return
                # Close the native iterator before publishing a successful EOF/cache.
                await iterator.aclose()
                iterator = None
                if not live():
                    return
                if cache is not None:
                    cache.put(key, AudioChunk(tuple(samples), rate, token))
                status['complete'] = True
            except asyncio.CancelledError:
                status['aborted'] = True
                raise
            except Exception:
                status['aborted'] = True
            finally:
                if iterator is not None:
                    try:
                        await iterator.aclose()
                    except (Exception, asyncio.CancelledError):
                        status['aborted'] = True
                overall.cancel()
                changed.set()

        producer = asyncio.create_task(generate(), name='mishka-menu-native')
        producer._mishka_session_id = session_id
        producer._mishka_owner_current = current
        self._owned_text_jobs.add(producer)
        def observe(done):
            self._owned_text_jobs.discard(done)
            changed.set()
            if not done.cancelled():
                done.exception()
        producer.add_done_callback(observe)

        class Stream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    while live():
                        if not queue.empty():
                            audio = queue.get_nowait()
                            changed.set()
                            return replace(audio, token=token, session_id=session_id,
                                           generation_id=handle.generation), ()
                        if producer.done():
                            if status['complete']:
                                raise StopAsyncIteration
                            break
                        changed.clear()
                        await changed.wait()
                    raise RuntimeError('menu_native_stream_unavailable')
                except BaseException:
                    abort()
                    raise

            async def aclose(self):
                # Port rejection may close us before its first __anext__ call.
                abort()

        try:
            while queue.empty() and not producer.done() and not status['aborted']:
                changed.clear()
                await changed.wait()
            if not live():
                abort()
                return False
            if status['cached'] is not None:
                self._play_typed_audio(session_id, status['cached'], effect, token)
                return True
            if queue.empty():
                abort()
                return False
            import uuid
            handle = PlaybackHandle(session_id=session_id,
                generation=self._session_epochs.get(session_id, 0),
                playback_id=f'{session_id}:{uuid.uuid4().hex}',
                playback_lease=effect['playback_lease'], response_id=effect['content_id'], token=token)
            self._register_audio_lease(session_id, handle.response_id, token, handle.playback_id,
                                       handle.playback_lease, duration_ms=0)
            effect.update(playback_id=handle.playback_id, generation=handle.generation, session_id=session_id)
            self.audio.play_pcm_stream_owned(handle, Stream())
            return True
        except asyncio.CancelledError:
            abort()
            raise
        except Exception:
            abort()
            self._invalidate_audio_lease(session_id, 'failed')
            return False

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
            elif effect.get('content_id') == CATALOGUE_CLARIFY:
                state = self.canonical_runtime.sessions.get(session_id)
                if (cancelled() or self._catalogue_voice_scope(session_id) is None
                        or state.active_audio_id != CATALOGUE_CLARIFY or not state.playback_lease
                        or effect.get('playback_lease') != state.playback_lease
                        or text != engine.normalizer.normalize(CATALOGUE_CLARIFY_TEXT)):
                    return None
                validation = await engine.response_validator.validate_trusted(text)
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
            from .name_audio import synthesize_private_text
            audio = await synthesize_private_text(render_tts, text, token, cancelled)
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
        if response_id == CATALOGUE_CLARIFY:
            import copy
            lease = self._active_audio[session_id]
            lease['_catalogue_choice_scope'] = self._catalogue_voice_scope(session_id)
            lease['_catalogue_choice_descriptor'] = copy.deepcopy(
                self.canonical_runtime.sessions[session_id].scene_menu_descriptor)
        effect.update(playback_id=handle.playback_id,generation=handle.generation,session_id=session_id)
        try:
            self.audio.play_pcm_owned(handle,replace(audio,token=token,
                session_id=session_id,generation_id=handle.generation))
        except Exception:
            self._invalidate_audio_lease(session_id,'failed')
            raise
