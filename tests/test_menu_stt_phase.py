"""Finished synthetic STT cannot answer a different physical menu phase."""
import asyncio

import pytest

from app.foundation.input_safety import InputSafetyDecision
from app.foundation.s2s import MockSpeechRecognizer, PassthroughVoiceProvider
from test_contextual_game_flow import Model
from test_game_audio_reminder_race import SPEECH, SILENCE
from test_scene_menu_runtime import Harness


class FinishedMenuSpeech(Harness):
    def __init__(self, text, severity='none'):
        super().__init__()
        self.app.canonical_runtime.exhibition_mode = True
        self.state.mode = 'exhibition_menu_repeat_listen'
        self.state.active_audio_id = self.state.playback_lease = None
        self.model = Model('<say>Это старый ответ.</say>')
        self.app.bounded_s2s.llm = self.model
        self.app.bounded_s2s.voice = PassthroughVoiceProvider()
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.original_expiry = self.app.expire_identity_leases
        self.classifications = []
        self.updates = []
        owner = self
        class Recognizer(MockSpeechRecognizer):
            finished = False
            async def transcribe(self, audio, cancelled):
                result = await super().transcribe(audio, cancelled)
                self.finished = True
                return result
        self.recognizer = Recognizer(text, .99)
        self.app.s2s_audio.recognizer = self.recognizer
        class Safety:
            async def classify(self, value):
                owner.classifications.append(value)
                return InputSafetyDecision(severity if value == text else 'none', 'test', True)
        self.app.safety_classifier = Safety()
        async def gate():
            if asyncio.current_task().get_name().startswith('canonical-audio-'):
                self.entered.set()
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    # Exercise the epoch/session fence, not merely task.cancel().
                    await self.release.wait()
            return await self.original_expiry()
        self.app.expire_identity_leases = gate
        async def updated(_sid, result):
            self.updates.append(result)
        self.app.audio_update_hook = updated
        self.completions = ()

    async def start_completion(self):
        await self.send('ButtonReleased', {'button_id': 1})
        self.seq += 1
        result = await self.app.receive_audio_async(self.sid, SPEECH + SILENCE,
            self.seq, wait_for_turn=False)
        assert result['pending'] and result['reason'] == 'audio_processing'
        await asyncio.wait_for(self.entered.wait(), 2)
        assert self.recognizer.finished
        self.completions = tuple(self.app._audio_completions[self.sid])

    async def finish_completion(self):
        self.release.set()
        await asyncio.wait_for(asyncio.gather(*self.completions, return_exceptions=True), 3)

    async def close(self):
        await self.finish_completion()
        self.app.expire_identity_leases = self.original_expiry
        await super().close()


@pytest.mark.asyncio
@pytest.mark.parametrize('text,severity,expected', [
    ('Нет', 'none', 'bear_dialogue'),
    ('Мне угрожают', 'urgent', 'safety_stop'),
    ('Позови взрослого', 'none', 'safety_stop'),
    ('Мне грустно', 'concern', 'support_intake'),
])
async def test_finished_menu_stt_after_tap_is_ordinary_stale_but_still_safety_classified(text, severity, expected):
    h = FinishedMenuSpeech(text, severity)
    try:
        await h.start_completion()
        assert (await h.button(6))['reason'] == 'bear_dialogue_started'
        intro = dict(h.app._active_audio[h.sid])
        await h.finish_completion()
        assert h.classifications == [text]
        assert not h.model.calls
        assert h.state.mode == expected
        if expected == 'bear_dialogue':
            assert h.state.active_audio_id == 'DIALOGUE_INTRO'
            assert h.app._active_audio[h.sid] == intro
            assert len(h.render.texts) == 1 and not h.updates
            await h.complete()
            h.app.s2s_audio.recognizer = MockSpeechRecognizer('Привет', .99)
            h.seq += 1
            await h.app.receive_audio_async(h.sid, SPEECH + SILENCE, h.seq)
            assert len(h.model.calls) == 1 and h.model.calls[0][-1]['content'] == 'Привет'
            assert [item[0] for item in h.app.conversation_contexts[h.sid].history] == ['Привет']
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_finished_answer_still_applies_to_unchanged_menu_question():
    h = FinishedMenuSpeech('Нет')
    try:
        await h.start_completion()
        await h.finish_completion()
        assert h.state.mode == 'menu_ready' and not h.model.calls
        assert h.classifications == ['Нет']
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_new_question_in_same_mode_revokes_finished_ordinary_answer():
    h = FinishedMenuSpeech('Нет')
    try:
        await h.start_completion()
        old_owner, old_version = h.state, h.state.state_version
        result = await h.send('SpeechReceived', text='не понял')
        assert result['reason'] == 'exhibition_menu_repeat_unclear'
        await h.complete()
        assert h.state is old_owner and h.state.mode == 'exhibition_menu_repeat_listen'
        assert h.state.state_version > old_version
        before = h.state.snapshot()
        await h.finish_completion()
        assert h.state.mode == 'exhibition_menu_repeat_listen'
        assert h.state.state_version == before['state_version']
        assert h.classifications == ['не понял', 'Нет'] and not h.model.calls
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_buffered_menu_utterance_keeps_first_phase_after_question_replacement():
    h = FinishedMenuSpeech('Нет')
    try:
        await h.send('ButtonReleased', {'button_id': 1})
        h.seq += 1
        assert (await h.app.receive_audio_async(h.sid, SPEECH, h.seq,
            wait_for_turn=False))['reason'] == 'collecting'
        pipeline = h.app.s2s_audio.ingress[h.sid]
        origin = pipeline._canonical_private_owner
        assert origin is not None and origin() and origin.lifecycle_current()
        await h.send('SpeechReceived', text='не понял')
        await h.complete()
        assert not origin() and origin.lifecycle_current()
        version = h.state.state_version
        h.seq += 1
        result = await h.app.receive_audio_async(h.sid, SILENCE, h.seq, wait_for_turn=False)
        assert result['pending']
        await asyncio.wait_for(h.entered.wait(), 2)
        h.completions = tuple(h.app._audio_completions[h.sid])
        await h.finish_completion()
        assert h.classifications == ['не понял', 'Нет']
        assert h.state.mode == 'exhibition_menu_repeat_listen' and h.state.state_version == version
        assert not h.model.calls
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('text,severity', [('Нет','none'), ('Мне угрожают','urgent'), ('Позови взрослого','none')])
async def test_finished_old_visit_speech_cannot_classify_or_mutate_new_visit(text, severity):
    h = FinishedMenuSpeech(text, severity)
    h.app.audio_cleanup_timeout_seconds = .01
    try:
        await h.start_completion()
        old_epoch = h.app._session_epochs.get(h.sid, 0)
        await h.send('HandsetPlaced')
        await h.send('HandsetLifted')
        assert h.app._session_epochs[h.sid] > old_epoch
        current = h.state
        version = current.state_version
        await h.finish_completion()
        assert h.state is current and h.state.mode == 'voice_welcome'
        assert h.state.state_version == version
        assert not h.classifications and not h.model.calls
    finally:
        await h.close()
