"""A fresh physical edge chooses once, even before the tutorial lights it."""
import pytest

from app.foundation.canonical_contract import CanonicalEvent, CanonicalRuntime, CanonicalState
from app.scenarios.canonical_graph import CanonicalLangGraphAuthorizer


PHASES = (
    'exhibition_menu_tutorial_intro', 'exhibition_menu_tutorial_prompt',
    'exhibition_menu_tutorial_buttons', 'exhibition_menu_wave',
    'exhibition_menu_repeat_prompt', 'exhibition_menu_repeat_listen',
    'exhibition_menu_replay_buttons',
)
TARGETS = {1: 'game_menu', 2: 'fixed_story_menu', 3: 'personal_story_theme',
           4: 'support_intake', 5: 'wellbeing_check', 6: 'bear_dialogue', 7: 'schedule_weather'}


def setup(phase):
    runtime = CanonicalRuntime(exhibition_mode=True)
    runtime.sessions['early-choice'] = CanonicalState('early-choice', mode=phase,
        handset='up', menu_lit_buttons=[], active_audio_id='PHRASE_008', playback_lease='old-tutorial')
    def send(kind, payload=None):
        state = runtime.sessions['early-choice']
        seq = state.last_sequence_by_source.get('test', 0) + 1
        return runtime.handle(CanonicalEvent(kind, state.session_id, seq, state.state_version,
            'test', payload or {}, str(seq), float(seq)))
    return runtime, send


@pytest.mark.parametrize('phase', PHASES)
@pytest.mark.parametrize('button', range(1, 8))
def test_unlit_tutorial_button_selects_its_root_action_once(phase, button):
    runtime, send = setup(phase)
    result = send('ButtonPressed', {'button_id': button})
    state = runtime.sessions['early-choice']
    assert state.mode == TARGETS[button]
    selected = [e for e in result['effects'] if e['type'] == 'exhibition_menu_selected']
    assert len(selected) == 1 and selected[0]['button_number'] == button
    assert any(e['type'] == 'audio_stop' for e in result['effects'])
    assert any(e['type'] == 'exhibition_effects_cancelled' for e in result['effects'])
    assert any(e['type'] == 'exhibition_menu_tutorial_skip' for e in result['effects'])
    assert CanonicalLangGraphAuthorizer().authorize(phase, state.mode, 'ButtonPressed')['ok']
    version = state.state_version
    for edge in ('ButtonPressed', 'ButtonHeld', 'ButtonLongHeld'):
        held = send(edge, {'button_id': button})
        assert state.mode == TARGETS[button]
        assert not held['effects'] and state.state_version == version
    send('ButtonReleased', {'button_id': button})
    assert button not in state.accepted_buttons_down
    stale = send('AudioCompleted', {'response_id': 'PHRASE_008', 'playback_lease': 'old-tutorial'})
    assert not stale['effects'] and state.mode == TARGETS[button]
    assert not send('MenuTutorialStep', {'step': 1})['effects']
    assert send('HandsetPlaced')['state']['mode'] == 'idle'
    assert not send('MenuTutorialStep', {'step': 2})['effects']


def test_unchosen_tutorial_still_advances_its_owned_lighting_tour():
    runtime, send = setup('exhibition_menu_tutorial_buttons')
    first = send('MenuTutorialStep', {'button': 1, 'phase': 'digit'})
    assert first['reason'] == 'menu_tutorial_digit_lit'
    assert runtime.sessions['early-choice'].mode == 'exhibition_menu_tutorial_buttons'
    assert runtime.sessions['early-choice'].menu_lit_buttons == []
    full = send('MenuTutorialStep', {'button': 1, 'phase': 'full'})
    assert full['reason'] == 'menu_tutorial_button_lit'
    assert runtime.sessions['early-choice'].menu_lit_buttons == [1]


def test_early_choice_release_allows_one_fresh_submenu_selection():
    runtime, send = setup('exhibition_menu_wave')
    send('ButtonPressed', {'button_id': 1})
    assert runtime.sessions['early-choice'].mode == 'game_menu'
    send('ButtonPressed', {'button_id': 1})
    send('ButtonReleased', {'button_id': 1})
    selected = send('ButtonPressed', {'button_id': 1})
    assert selected['state']['mode'] == 'game_active'
    assert not any(e['type'] == 'exhibition_menu_selected' for e in selected['effects'])


def test_unavailable_authored_library_keeps_its_honest_chooser_and_exit():
    runtime, send = setup('exhibition_menu_tutorial_buttons')
    runtime.personal_story_capable = False
    result = send('ButtonPressed', {'button_id': 3})
    page = result['state']['scene_menu_descriptor']
    assert result['state']['mode'] == 'personal_story_theme'
    assert not page['items'] and page['buttons'] == [7]
    assert 'нет доступных' in page['spoken_text']
    assert not any(e['type'] == 'generate_personal_story' for e in result['effects'])


def test_held_origin_cannot_reenter_after_another_key_returns_to_root():
    runtime, send = setup('exhibition_menu_wave')
    send('ButtonPressed', {'button_id': 1})
    send('ButtonPressed', {'button_id': 7})
    assert runtime.sessions['early-choice'].mode == 'root_menu'
    held = send('ButtonPressed', {'button_id': 1})
    assert not held['effects'] and runtime.sessions['early-choice'].mode == 'root_menu'
    send('ButtonReleased', {'button_id': 1})
    assert send('ButtonPressed', {'button_id': 1})['state']['mode'] == 'game_menu'


@pytest.mark.parametrize('button', range(1, 8))
def test_key_already_held_before_the_tutorial_needs_a_new_physical_edge(button):
    runtime, send = setup('exhibition_menu_tutorial_buttons')
    state = runtime.sessions['early-choice']
    state.accepted_buttons_down = [button]
    repeated = send('ButtonPressed', {'button_id': button})
    assert not repeated['effects'] and state.mode == 'exhibition_menu_tutorial_buttons'
    send('ButtonReleased', {'button_id': button})
    assert send('ButtonPressed', {'button_id': button})['state']['mode'] == TARGETS[button]


@pytest.mark.asyncio
@pytest.mark.parametrize('button', range(1, 8))
async def test_shared_panel_phone_ingress_accepts_enabled_but_unlit_root_choice(tmp_path, button):
    from test_stand_menu_enabled_buttons import MenuStand
    h = MenuStand(tmp_path)
    h.app.canonical_runtime.exhibition_mode = True
    h.state.mode = 'exhibition_menu_wave'
    h.state.menu_lit_buttons = []
    h.app.led.clear_all()
    try:
        before = h.stand.panel_snapshot()
        assert before['session']['enabled_buttons'] == list(range(1, 8))
        result = await h.edge(button)
        assert h.state.mode == TARGETS[button]
        selections = [e for e in result['outputs'] if e.get('type') == 'exhibition_menu_selected']
        assert len(selections) == 1 and selections[0]['button_number'] == button
        repeated = await h.edge(button)
        assert h.state.mode == TARGETS[button]
        assert not repeated.get('outputs')
        await h.edge(button, 'release')
    finally:
        await h.close()
