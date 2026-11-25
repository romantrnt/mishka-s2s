"""Synthetic admitted text/PCM receipts; no field transcript or real decoding claim."""
import pytest
from test_child_button_access import ChildHarness
from test_onboarding_voice_slots import speech


@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['typed', 'recognized_pcm'])
@pytest.mark.parametrize('text', ['Зо', 'Меня зо', 'Зовут', 'МенязовутРома'])
async def test_actual_name_entry_keeps_cue_provenance_before_commit(route, text):
    h = ChildHarness()
    try:
        await h.name(); await h.complete()
        await speech(h, route, text, .99)
        if text == 'МенязовутРома':
            assert h.state.display_name == 'Рома' and h.state.active_audio_id == 'PHRASE_108'
            assert h.state.dialogue_memory.bounded_turns == [{'kind': 'name', 'value': 'Рома'}]
        else:
            assert h.state.display_name is None and not h.state.onboarding_name_collected
            assert not h.state.dialogue_memory.bounded_turns
            assert h.app._canonical_audio_context(h.sid).profile.display_name is None
            if text == 'Зо':
                assert h.state.pending_name == 'Зо' and h.state.active_audio_id == 'NAME_CONFIRMATION'
            else:
                assert h.state.pending_name is None and h.state.active_audio_id == 'NAME_BUTTON_HINT'
                assert h.render.texts[-1] == 'Назови только имя. Можно пропустить — семь.'
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['typed', 'recognized_pcm'])
@pytest.mark.parametrize('text', ['Менязовут', 'Менязо', 'МенязоРома'])
async def test_compact_missing_or_broken_cue_never_becomes_a_name(route, text):
    h = ChildHarness()
    try:
        await h.name(); await h.complete()
        await speech(h, route, text, .99)
        assert h.state.display_name is None and h.state.pending_name is None
        assert not h.state.dialogue_memory.bounded_turns
        assert h.state.active_audio_id == 'NAME_BUTTON_HINT'
        assert h.render.texts[-1] == 'Назови только имя. Можно пропустить — семь.'
    finally:
        await h.close()


@pytest.mark.parametrize('text,name', [
    ('Меня зовут Рома', 'Рома'), ('менязовутРома', 'Рома'), ('меня зовутРома', 'Рома'),
    ('Менязовут Ли', 'Ли'), ('Меня зовут, Рома', 'Рома'), ('Привет, меня зовут Ару', 'Ару'),
    ('Меня зовут Зо', 'Зо'), ('Зоя', 'Зоя'), ('Зоря', 'Зоря'), ('Зовей', 'Зовей'),
    ('Ли', 'Ли'), ('Ия', 'Ия'), ('Ян', 'Ян'), ('Эн', 'Эн'), ('Ару', 'Ару'), ('Ома', 'Ома'),
])
def test_clear_actual_name_spans_keep_fast_path_without_catalogue_or_score(text, name):
    from app.foundation.canonical_contract import normalize_exhibition_name, exhibition_name_uncertain
    assert normalize_exhibition_name(text) == name
    assert not exhibition_name_uncertain(text)


@pytest.mark.parametrize('text', ['Зо', 'Зов', 'Зову', 'Я Зо', 'Это я Зо'])
def test_unframed_cue_prefix_is_only_an_observed_ambiguity_not_a_name_rejection(text):
    from app.foundation.canonical_contract import normalize_exhibition_name, exhibition_name_uncertain
    assert normalize_exhibition_name(text) in {'Зо', 'Зов', 'Зову'}
    assert exhibition_name_uncertain(text)


@pytest.mark.parametrize('text', ['Меня', 'Меня з', 'Меня зо', 'Меня зов', 'Меня зову',
    'Меня зовут', 'Менязовут', 'Менязо', 'МенязоРома', 'Зовут', 'Моё имя', 'Зови меня', 'Я', 'Это я'])
def test_no_name_span_is_invented_from_a_service_cue(text):
    from app.foundation.canonical_contract import normalize_exhibition_name, exhibition_name_cue_only
    assert normalize_exhibition_name(text) is None
    assert exhibition_name_cue_only(text)


@pytest.mark.asyncio
@pytest.mark.parametrize('failed', [False, True])
@pytest.mark.parametrize('ending', ['name', 'skip', 'refusal', 'repeat'])
async def test_cue_clarification_has_one_budget_and_fresh_voice_or_button_exit(failed, ending):
    h = ChildHarness()
    try:
        await h.name(); await h.complete()
        owner = h.state
        await speech(h, 'recognized_pcm', 'Меня зо', .99)
        old = dict(h.app._active_audio[h.sid])
        assert h.state.name_confirmation_attempts == 1
        await h.complete(failed=failed)
        if ending == 'skip':
            await h.button(7); await h.release(7)
        else:
            await speech(h, 'recognized_pcm', {'name': 'Меня зовут Ли', 'refusal': 'Не хочу', 'repeat': 'Зовут'}[ending], .99)
        expected_mode = 'exhibition_menu_tutorial_prompt' if ending == 'refusal' else 'exhibition_age_prompt'
        assert h.state is owner and h.state.mode == expected_mode
        assert h.state.display_name == ('Ли' if ending == 'name' else None)
        assert h.state.pending_name is None
        assert sum(text.startswith('Назови только имя.') for text in h.render.texts) == 1
        assert h.state.dialogue_memory.bounded_turns == ([{'kind': 'name', 'value': 'Ли'}] if ending == 'name' else [])
        version = h.state.state_version
        await h.send('AudioCompleted', {**old, 'response_id': 'NAME_BUTTON_HINT'})
        assert h.state.state_version == version
        assert h.state.active_audio_id == ('PHRASE_007' if ending == 'refusal' else 'PHRASE_108')
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_cue_hint_current_pcm_gate_and_queued_old_scope_end_are_owned():
    from app.foundation.config import Settings
    from app.foundation.stand import HardwareIntegrationStand
    h = ChildHarness()
    stand = HardwareIntegrationStand(Settings(profile='mock'), background_scheduler=False)
    original = stand.application
    try:
        await h.name(); await h.complete()
        stand.application = h.app; stand.active_session_id = h.sid
        old_scope = stand._headless_frame_owner()
        assert old_scope[1]()
        await speech(h, 'recognized_pcm', 'Меня зо', .99)
        lease = dict(h.app._active_audio[h.sid])
        assert not stand._interaction_audio_accepting() and not old_scope[1]()
        assert stand._pcm_owner_current(h.played[-1], None)
        await h.send('AudioCompleted', {**lease, 'response_id': 'NAME_BUTTON_HINT', 'playback_lease': 'foreign'})
        assert not stand._interaction_audio_accepting()
        await h.complete()
        assert stand._interaction_audio_accepting() and not old_scope[1]()
        assert stand._headless_frame_owner()[1]()
        await h.app.session_action(h.sid, 'end', ownership_guard=lambda: True)
        assert h.state.pending_name is None and h.state.display_name is None
        assert not stand._pcm_owner_current(h.played[-1], None)
        await h.send('AudioCompleted', {**lease, 'response_id': 'NAME_BUTTON_HINT'})
        assert h.state.display_name is None and not h.state.dialogue_memory.bounded_turns
    finally:
        stand.application = original
        await h.close(); await stand.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('text,confidence,name', [
    ('Меня зовут Рома.', .7319382031299391, 'Рома'),
    ('Ома.', .5699511807787089, 'Ома'),
    ('Ару', .46, 'Ару'),
])
async def test_admitted_literal_receipts_do_not_guess_a_different_identity_or_add_score_policy(text, confidence, name):
    # First two are saved robotic synthetic ASR receipts, replayed here by a
    # fixture. This is neither a new decode nor proof of the visitor's name.
    from app.foundation.s2s import STTResult
    from test_game_audio_reminder_race import SPEECH, SILENCE
    h = ChildHarness()
    class Receipt:
        async def transcribe(self, audio, cancelled):
            return STTResult(text, confidence, 0., False, {})
    try:
        await h.name(); await h.complete()
        h.app.s2s_audio.recognizer = Receipt()
        h.seq = max(h.seq, h.app.sessions.get(h.sid).last_sequence) + 1
        await h.app.receive_audio_async(h.sid, SPEECH + SILENCE, h.seq)
        assert h.state.display_name == name and h.state.pending_name is None
        assert h.state.active_audio_id == 'PHRASE_108'
        assert h.state.dialogue_memory.bounded_turns == [{'kind': 'name', 'value': name}]
    finally:
        await h.close()
