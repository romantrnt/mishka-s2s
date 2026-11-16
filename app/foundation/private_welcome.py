"""Session-private greeting preparation; never serialize a preparation owner."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import re
import math
import unicodedata

from app.safety.output_filter import PROFANITY_PATTERN


def valid_name(value):
    """Reject rather than repair suspicious structured names; no name dictionary."""
    if not isinstance(value, str) or not 1 <= len(value) <= 80:
        return False
    if value != value.strip() or len(value.split(' ')) > 3:
        return False
    prior_letter = False
    for char in value:
        letter = unicodedata.category(char).startswith('L')
        if not letter and (char not in " -'’" or not prior_letter):
            return False
        prior_letter = letter
    if not prior_letter:
        return False
    # Grammar is not proof of identity. Backstop obvious instruction/profanity
    # values before applying the existing trusted-output safety validator.
    blocked = re.compile(r"\b(?:скажи|игнорируй|инструкции|выполни|ignore|instructions|system|assistant|fuck\w*|shit|бляд\w*|хуй\w*|пизд\w*|ебать|сука)\b", re.I)
    return not blocked.search(value) and not PROFANITY_PATTERN.search(value)


@dataclass(repr=False)
class WelcomePreparation:
    epoch: int
    request_id: str
    profile_ref: str
    deadline: float
    state_owner: object = field(repr=False)
    task: object = field(default=None, repr=False)
    audio: object = field(default=None, repr=False)
    status: str = 'preparing'
    closed: bool = False
    chosen: bool = False

    def discard(self):
        self.closed = True
        self.audio = None
        if self.task is not None and not self.task.done():
            self.task.cancel()


def valid_pcm(audio):
    from .s2s import AudioChunk
    return (isinstance(audio, AudioChunk) and type(audio.sample_rate) is int
        and audio.sample_rate > 0 and isinstance(audio.samples, tuple) and bool(audio.samples)
        and all(type(sample) in (int, float) and abs(sample) <= 1 and math.isfinite(sample) for sample in audio.samples))


class PrivateWelcomeMixin:
    def _discard_private_welcome(self, session_id):
        owner = self._private_welcomes.pop(session_id, None)
        if owner is not None:
            owner.discard()

    def _private_welcome_scope(self, session_id, owner):
        state = self.canonical_runtime.sessions.get(session_id)
        return bool(not owner.closed and not self._identity_shutdown
            and self._private_welcomes.get(session_id) is owner
            and state is owner.state_owner and state.handset == 'up'
            and state.mode == 'voice_welcome'
            and self._session_epochs.get(session_id, 0) == owner.epoch
            and state.profile_status == 'identified'
            and state.profile_ref == owner.profile_ref
            and (self._canonical_profiles.get(session_id) or {}).get('profile_ref') == owner.profile_ref
            and state.identity_request_id == owner.request_id
            and state.welcome_deadline_monotonic == owner.deadline
            and self._identity_scope_live(session_id, 'personalize'))

    def _prepare_private_welcome(self, session_id):
        self._discard_private_welcome(session_id)
        state = self.canonical_runtime.sessions.get(session_id)
        if not (self.max_json and state and state.profile_status == 'identified'
                and state.profile_ref and state.identity_request_id
                and self._identity_scope_live(session_id, 'personalize')):
            return
        owner = WelcomePreparation(self._session_epochs.get(session_id, 0),
            state.identity_request_id, state.profile_ref,
            state.welcome_deadline_monotonic, state)
        self._private_welcomes[session_id] = owner
        name = (self._canonical_profiles.get(session_id) or {}).get('display_name')
        if not valid_name(name):
            owner.status = 'personal_welcome_name_unavailable'
            return
        engine = self.bounded_s2s
        if (engine is None or not callable(getattr(self.audio, 'play_pcm_owned', None))
                or not getattr(self.audio, 'private_pcm_supported', True)):
            owner.status = 'personal_welcome_prepare_failed'
            return
        from .s2s import CachedTTSProvider, ResilientVoiceProvider
        tts = engine.tts
        while isinstance(tts, CachedTTSProvider):
            tts = tts.delegate

        def cancelled():
            return (not self._private_welcome_scope(session_id, owner)
                    or owner.chosen or self.monotonic_now() >= owner.deadline)

        async def prepare():
            audio = phrase = None
            try:
                if cancelled():
                    return
                phrase = f'Привет, {name}. Я Мишка.'
                decision = await engine.response_validator.validate_trusted(phrase)
                if cancelled():
                    return
                if not decision.allowed:
                    owner.status = 'personal_welcome_name_unavailable'
                    return
                audio = await tts.synthesize(phrase, owner.epoch, cancelled)
                phrase = None
                if cancelled():
                    return
                if not valid_pcm(audio):
                    owner.status = 'personal_welcome_prepare_failed'
                    return
                # Trusted TTS is already usable. RVC may improve it before the
                # deadline, but must never gate an authorized personal welcome.
                owner.audio, owner.status = audio, 'personal_welcome_tts_fallback'
                voice = engine.voice
                if isinstance(voice, ResilientVoiceProvider):
                    voice = voice.primary
                convert = getattr(voice, 'convert_private', None)
                if not callable(convert):
                    return
                audio = await convert(audio, cancelled)
                if cancelled():
                    return
                if not valid_pcm(audio):
                    return
                owner.audio, owner.status = audio, 'personal_welcome_ready'
            except asyncio.CancelledError:
                pass
            except Exception:
                # Exception messages may contain the private phrase.
                if owner.status == 'preparing':
                    owner.status = 'personal_welcome_prepare_failed'
            finally:
                audio = phrase = None
        owner.task = asyncio.create_task(prepare(), name='mishka-private-welcome')
        self._private_welcome_tasks.add(owner.task)
        owner.task.add_done_callback(self._private_welcome_tasks.discard)

    def _choose_private_welcome(self, session_id, *, commit=True):
        owner = self._private_welcomes.get(session_id)
        if owner is None or owner.chosen:
            return False
        ready = self._private_welcome_scope(session_id, owner) and owner.audio is not None
        if not commit:
            return ready
        owner.chosen = True
        code = owner.status if ready else (
            owner.status if owner.status != 'preparing' else 'personal_welcome_not_ready')
        self.record_audio_diagnostic('personal_welcome_choice', code=code)
        if not ready:
            self._discard_private_welcome(session_id)
        return ready
