"""Optional onboarding stays usable with buttons and no spoken answer."""
import asyncio

import pytest

from app.foundation.canonical_contract import CanonicalEvent, CanonicalRuntime
from app.foundation.domain import Event
from app.foundation.input_safety import InputSafetyDecision
from app.foundation.lighting import BUTTON_FILL_ELEMENTS, BUTTON_WAVE_ELEMENTS
from app.foundation.validation import ValidationDecision
from test_game_audio_reminder_race import SPEECH, SILENCE
from test_menu_stt_phase import FinishedMenuSpeech
from test_scene_menu_runtime import Harness as MenuHarness


class Flow:
    def __init__(self):
        self.runtime = CanonicalRuntime(exhibition_mode=True, monotonic_now=lambda: 10.)
        self.seq = 0

    @property
    def state(self):
        return self.runtime.sessions['child-buttons']

    def send(self, kind, payload=None):
        self.seq += 1
        state = self.runtime.sessions.get('child-buttons')
        return self.runtime.handle(CanonicalEvent(kind, 'child-buttons', self.seq,
            state.state_version if state else 0, 'hid', payload or {}, str(self.seq),
            100., 10. if self.seq == 1 else 15.))

    def complete(self, failed=False):
        return self.send('AudioFailed' if failed else 'AudioCompleted', {
            'response_id': self.state.active_audio_id,
            'playback_lease': self.state.playback_lease})

    def name(self, phase='question'):
        self.send('HandsetLifted')
        self.send('WelcomeTimerFired')
        self.complete()  # greeting
        self.complete()  # introduction
        assert self.state.mode == 'exhibition_name_question'
        if phase != 'question':
            self.complete()
            assert self.state.mode == 'exhibition_name_capture'
        if phase == 'retry':
            self.seq += 1
            self.runtime.handle(CanonicalEvent('VoiceIntentReady', 'child-buttons',
                self.seq, self.state.state_version, 'speech',
                {'intent': 'input_retry', 'safety_severity': 'none'}, str(self.seq), 100., 15.))
            assert self.state.active_audio_id == 'PHRASE_074'
        return self


@pytest.mark.parametrize('phase', ['question', 'hint', 'retry'])
def test_fresh_skip_during_name_audio_preserves_anonymous_identity(phase):
    h = Flow().name(phase)
    assert {'button_5', 'button_7'} <= set(h.state.expected_inputs)
    old = {'response_id': h.state.active_audio_id, 'playback_lease': h.state.playback_lease}
    result = h.send('ButtonPressed', {'button_id': 7})
    assert result['reason'] == 'exhibition_name_skipped_to_age'
    assert h.state.mode == 'exhibition_age_prompt'
    assert h.state.active_audio_id == 'PHRASE_108'
    assert h.state.display_name is None and not h.state.onboarding_name_collected
    assert h.state.age_years is None and not h.state.dialogue_memory.bounded_turns
    assert [x['type'] for x in result['effects']][:2] == ['audio_stop', 'capture_close']
    assert not h.send('AudioCompleted', old)['effects']
    assert h.state.active_audio_id == 'PHRASE_108'


def test_name_hint_is_owned_short_announced_and_targets_available_controls():
    h = Flow().name('hint')
    assert h.state.active_audio_id == 'NAME_BUTTON_HINT' and h.state.playback_lease
    old_lease = h.state.playback_lease
    assert {'button_5', 'button_7'} <= set(h.state.expected_inputs)
    rejected = h.send('ButtonPressed', {'button_id': 2})
    assert rejected['reason'] == 'exhibition_name_button_hint'
    assert h.state.mode == 'exhibition_name_capture'
    lit = {element for effect in rejected['effects'] if effect['type'] == 'lighting'
        for element in effect['elements'] if effect['state'] == 1}
    assert lit == set(BUTTON_WAVE_ELEMENTS[5] + BUTTON_WAVE_ELEMENTS[7])
    assert h.state.active_audio_id == 'NAME_BUTTON_HINT'
    assert h.state.playback_lease != old_lease
    assert [effect['text'] for effect in rejected['effects'] if effect['type'] == 'dialogue_prompt'] == [
        'Имя можно не говорить. Чтобы пропустить, нажми семь.']
    assert not h.send('ButtonPressed', {'button_id': 2})['effects']


@pytest.mark.parametrize('failed', [False, True])
def test_hint_terminal_requires_its_current_lease_and_keeps_buttons(failed):
    h = Flow().name('hint')
    version = h.state.state_version
    assert not h.send('AudioCompleted', {'response_id': 'NAME_BUTTON_HINT',
        'playback_lease': 'old-lease'})['effects']
    assert h.state.state_version == version
    result = h.complete(failed)
    assert result['reason'] == ('exhibition_name_hint_failed' if failed else 'exhibition_name_hint_finished')
    assert h.state.active_audio_id is None
    assert {'speech', 'button_5', 'button_7'} <= set(h.state.expected_inputs)
    assert h.send('ButtonPressed', {'button_id': 7})['reason'] == 'exhibition_name_skipped_to_age'


@pytest.mark.parametrize('age', [1, 3, 5, 6, 7])
@pytest.mark.parametrize('during', ['question', 'hint', 'selection'])
def test_age_choice_during_safe_speech_and_seven_needs_a_fresh_press(age, during):
    h = Flow().name('hint')
    assert h.send('ButtonPressed', {'button_id': 7})['reason'] == 'exhibition_name_skipped_to_age'
    if during in {'hint', 'selection'}:
        h.complete()
    if during == 'selection':
        h.complete()
    assert {f'button_{b}' for b in range(1, 8)} <= set(h.state.expected_inputs)
    old_audio = {'response_id': h.state.active_audio_id, 'playback_lease': h.state.playback_lease}
    held = h.send('ButtonPressed', {'button_id': 7})
    assert held['reason'] == 'exhibition_age_button_release_required'
    assert h.state.age_years is None and not held['effects']
    h.send('ButtonReleased', {'button_id': 7})
    result = h.send('ButtonPressed', {'button_id': age})
    assert result['reason'] == 'exhibition_age_selected'
    assert h.state.age_years == age
    assert h.state.age_band == ('1-3' if age <= 3 else '4-5' if age <= 5 else '6-7')
    assert h.state.active_audio_id == 'PHRASE_107'
    assert [effect['type'] for effect in result['effects']][:2] == ['audio_stop', 'capture_close']
    if old_audio['response_id']:
        assert not h.send('AudioCompleted', old_audio)['effects']
    assert not h.send('ButtonPressed', {'button_id': age})['effects']
    assert h.state.age_years == age and h.state.active_audio_id == 'PHRASE_107'


@pytest.mark.parametrize('phase', ['question', 'hint', 'retry'])
def test_name_help_still_asks_the_real_pain_question(phase):
    h = Flow().name(phase)
    result = h.send('ButtonPressed', {'button_id': 5})
    assert result['reason'] == 'help_priority'
    assert h.state.mode == 'wellbeing_check' and h.state.support_question_kind == 'pain_or_discomfort'
    assert h.state.active_audio_id == 'PHRASE_084'
    assert {'button_1', 'button_7'} <= set(h.state.expected_inputs)
    assert h.state.display_name is None and h.state.age_years is None


@pytest.mark.parametrize('phase', ['question', 'hint'])
def test_failed_age_audio_leaves_a_visual_age_choice(phase):
    h = Flow().name('hint')
    h.send('ButtonPressed', {'button_id': 7})
    h.send('ButtonReleased', {'button_id': 7})
    if phase == 'hint':
        h.complete()
        h.send('VoiceIntentReady', {'intent':'free_speech','safety_severity':'none'})
        assert h.state.active_audio_id == 'PHRASE_075'
    result = h.complete(failed=True)
    assert result['reason'] == 'exhibition_age_audio_failed'
    assert h.state.mode == 'exhibition_age_selection' and h.state.age_years is None
    assert h.state.active_audio_id is None
    assert not any(effect['type'] in {'speak', 'dialogue_prompt'} for effect in result['effects'])
    assert any(effect['type'] == 'lighting_wave_intent' for effect in result['effects'])
    assert h.send('ButtonPressed', {'button_id': 5})['reason'] == 'exhibition_age_selected'
    assert h.state.age_years == 5


class ChildHarness(MenuHarness):
    def __init__(self):
        super().__init__()
        self.clock = [10.]
        self.app.canonical_runtime.sessions.clear()
        self.app.canonical_runtime.exhibition_mode = True
        self.app.monotonic_now = lambda: self.clock[0]
        self.app.canonical_runtime.monotonic_now = self.app.monotonic_now

    async def send(self, kind, payload=None, text=None):
        self.seq += 1
        return await self.app.receive_async(Event(kind, self.sid, self.seq, 100.,
            'hid', payload or {}, str(self.seq), self.clock[0]), text)

    async def name(self, hint=True):
        await self.send('HandsetLifted')
        self.clock[0] += 5
        await self.send('WelcomeTimerFired')
        await self.complete()
        await self.complete()
        assert self.state.mode == 'exhibition_name_question'
        if hint:
            await self.complete()
        return self


@pytest.mark.asyncio
async def test_actual_app_owned_hint_tts_input_skip_and_age_interrupt():
    h = ChildHarness()
    try:
        await h.name()
        assert h.played[-1].response_id == 'NAME_BUTTON_HINT'
        assert h.render.texts == ['Имя можно не говорить. Чтобы пропустить, нажми семь.']
        assert {int(key) for key, value in h.app.led.output.snapshot().items() if value} == set(
            BUTTON_WAVE_ELEMENTS[5] + BUTTON_WAVE_ELEMENTS[7])
        old = dict(h.app._active_audio[h.sid])
        assert (await h.button(7))['reason'] == 'exhibition_name_skipped_to_age'
        assert h.state.mode == 'exhibition_age_prompt'
        assert h.app._active_audio[h.sid]['content_id'] == 'PHRASE_108'
        await h.send('AudioCompleted', {'response_id': old['content_id'], **{k: old[k]
            for k in ('token', 'generation', 'playback_id', 'playback_lease')}})
        assert h.state.mode == 'exhibition_age_prompt' and h.state.age_years is None
        await h.release(7)
        assert (await h.button(3))['reason'] == 'exhibition_age_selected'
        assert h.state.age_years == 3 and h.app._active_audio[h.sid]['content_id'] == 'PHRASE_107'
        assert {int(key) for key, value in h.app.led.output.snapshot().items() if value} == {BUTTON_FILL_ELEMENTS[3]}
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('phrase', ['PHRASE_099', 'PHRASE_108', 'PHRASE_075'])
async def test_actual_app_failed_question_keeps_nonverbal_choice(phrase):
    h = ChildHarness()
    try:
        await h.name(hint=False)
        if phrase != 'PHRASE_099':
            await h.button(7)
            await h.release(7)
        if phrase == 'PHRASE_075':
            await h.complete()
            await h.send('SpeechReceived', text='не уверен')
        assert h.state.active_audio_id == phrase
        result = await h.complete(failed=True)
        assert h.state.active_audio_id is None and h.sid not in h.app._active_audio
        assert h.state.age_years is None and h.state.display_name is None
        assert {'button_5', 'button_7'} <= set(h.state.expected_inputs)
        assert not any(effect['type'] in {'speak', 'dialogue_prompt'} for effect in result['outputs'])
        if phrase == 'PHRASE_099':
            assert result['reason'] == 'exhibition_name_question_failed'
            assert h.state.mode == 'exhibition_name_capture'
            assert (await h.button(7))['reason'] == 'exhibition_name_skipped_to_age'
        else:
            assert result['reason'] == 'exhibition_age_audio_failed'
            assert h.state.mode == 'exhibition_age_selection'
            assert (await h.button(5))['reason'] == 'exhibition_age_selected'
            assert h.state.age_years == 5
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_hint_keeps_mandatory_validation_and_never_plays_blocked_text():
    h = ChildHarness()
    checked = []
    class Blocked:
        async def validate(self, text):
            checked.append(text)
            return ValidationDecision('block', False, 'synthetic_block', 1., 'synthetic')
    try:
        await h.name(hint=False)
        h.app.bounded_s2s.response_validator = Blocked()
        await h.complete()
        assert checked == ['Имя можно не говорить. Чтобы пропустить, нажми семь.']
        assert not h.render.texts and not h.played
        assert h.state.mode == 'exhibition_name_capture' and h.state.active_audio_id is None
        assert {'button_5', 'button_7'} <= set(h.state.expected_inputs)
        assert (await h.button(7))['reason'] == 'exhibition_name_skipped_to_age'
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_old_hint_completion_cannot_finish_the_next_visits_hint():
    h = ChildHarness()
    try:
        await h.name()
        old = dict(h.app._active_audio[h.sid])
        await h.send('HandsetPlaced')
        await h.name()
        current = dict(h.app._active_audio[h.sid])
        version = h.state.state_version
        await h.send('AudioCompleted', {'response_id': old['content_id'], **{key: old[key]
            for key in ('token', 'generation', 'playback_id', 'playback_lease')}})
        assert h.state.state_version == version
        assert h.app._active_audio[h.sid] == current
        assert h.state.mode == 'exhibition_name_capture' and h.state.display_name is None
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_actual_app_delayed_name_classification_cannot_answer_age():
    h = ChildHarness()
    entered, release = asyncio.Event(), asyncio.Event()
    pending = None
    class Safety:
        async def classify(self, text):
            entered.set()
            await release.wait()
            return InputSafetyDecision('none', 'synthetic', True)
    try:
        await h.name()
        await h.complete()
        h.app.safety_classifier = Safety()
        pending = asyncio.create_task(h.send('SpeechReceived', text='семь'))
        await asyncio.wait_for(entered.wait(), 2)
        await h.button(7)
        await h.release(7)
        await h.complete()
        version = h.state.state_version
        release.set()
        assert (await pending)['reason'] == 'stale_session_generation'
        assert h.state.mode == 'exhibition_age_selection' and h.state.age_years is None
        assert h.state.state_version == version and h.state.display_name is None
    finally:
        release.set()
        if pending:
            await asyncio.gather(pending, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_actual_app_hangup_revokes_hint_and_new_visit_starts_without_identity():
    h = ChildHarness()
    pending = None
    try:
        await h.name(hint=False)
        h.render.block = asyncio.Event()
        pending = asyncio.create_task(h.complete())
        await asyncio.wait_for(h.render.entered.wait(), 2)
        await h.send('HandsetPlaced')
        h.render.block.set()
        await pending
        assert not h.played
        assert h.sid not in h.app._active_audio
        await h.name(hint=False)
        assert h.state.display_name is None and h.state.age_years is None
        assert (await h.button(7))['reason'] == 'exhibition_name_skipped_to_age'
    finally:
        if h.render.block:
            h.render.block.set()
        if pending:
            await asyncio.gather(pending, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('text,severity,expected', [
    ('семь', 'none', 'exhibition_age_selection'),
    ('Мне угрожают', 'urgent', 'safety_stop'),
    ('Позови взрослого', 'none', 'safety_stop'),
    ('Мне грустно', 'concern', 'support_intake'),
])
async def test_finished_name_stt_cannot_answer_age_after_skip(text, severity, expected):
    h = FinishedMenuSpeech(text, severity)
    try:
        h.state.mode = 'exhibition_name_capture'
        await h.start_completion()
        assert (await h.button(7))['reason'] == 'exhibition_name_skipped_to_age'
        await h.send('ButtonReleased', {'button_id': 7})
        await h.complete()
        version = h.state.state_version
        await h.finish_completion()
        assert h.classifications == [text], 'same-visit speech still receives mandatory safety classification'
        assert h.state.mode == expected and h.state.age_years is None
        assert h.state.display_name is None and not h.model.calls
        if expected == 'exhibition_age_selection':
            assert h.state.state_version == version and not h.updates
            assert (await h.button(7))['reason'] == 'exhibition_age_selected'
            assert h.state.age_years == 7
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_first_name_speech_buffer_cannot_reappear_as_an_age_answer():
    h = FinishedMenuSpeech('семь')
    try:
        h.state.mode = 'exhibition_name_capture'
        await h.send('ButtonReleased', {'button_id': 1})
        h.seq += 1
        result = await h.app.receive_audio_async(h.sid, SPEECH, h.seq, wait_for_turn=False)
        assert result['reason'] == 'collecting'
        pipeline = h.app.s2s_audio.ingress[h.sid]
        origin = pipeline._canonical_private_owner
        assert origin is not None and origin() and origin.lifecycle_current()
        await h.button(7)
        await h.send('ButtonReleased', {'button_id': 7})
        await h.complete()
        assert not origin() and origin.lifecycle_current()
        assert h.app.s2s_audio.ingress.get(h.sid) is not pipeline
        version = h.state.state_version
        h.seq += 1
        result = await h.app.receive_audio_async(h.sid, SILENCE, h.seq, wait_for_turn=False)
        assert result['reason'] == 'collecting' and not result['pending']
        assert not h.classifications
        assert h.state.mode == 'exhibition_age_selection' and h.state.age_years is None
        assert h.state.state_version == version
        assert (await h.button(7))['reason'] == 'exhibition_age_selected'
        assert h.state.age_years == 7
    finally:
        await h.close()
