"""Actual dialogue handlers, including invitation audio, own panel controls.

Synthetic providers exercise canonical/application paths; no microphone or board claim.
"""
import pytest

from app.foundation.domain import Event
from test_contextual_game_flow import Model
from test_stand_menu_enabled_buttons import MenuStand


def assert_buttons(h, expected):
    panel = h.stand.panel_snapshot()
    assert panel['session']['enabled_buttons'] == expected
    assert [b['id'] for b in panel['simulator']['devices']['buttons'] if b['enabled']] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('root', [4, 6])
@pytest.mark.parametrize('action', [5, 7])
async def test_ordinary_dialogue_only_advertises_working_help_and_return(tmp_path, root, action):
    h = MenuStand(tmp_path)
    try:
        await h.key(root)
        mode = 'support_intake' if root == 4 else 'bear_dialogue'
        assert h.state.mode == mode
        assert_buttons(h, [5, 7])
        await h.key(1)
        assert h.state.mode == mode
        assert_buttons(h, [5, 7])
        await h.key(action)
        assert h.state.mode == ('wellbeing_check' if action == 5 else 'root_menu')
        assert_buttons(h, [1, 5, 7] if action == 5 else list(range(1, 8)))
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal', ['AudioCompleted', 'AudioFailed'])
@pytest.mark.parametrize('answer', [1, 7])
async def test_game_invitation_exposes_consent_only_after_its_owned_terminal(tmp_path, terminal, answer):
    h = MenuStand(tmp_path)
    h.app.bounded_s2s.llm = Model('<action type="offer_game" game_id="quiz"/>')
    try:
        await h.key(6)
        await h.done()
        await h.harness.speech_result('Хочу поиграть')
        assert h.state.mode == 'bear_dialogue' and h.state.pending_game_offer == 'quiz'
        assert not h.state.game_offer_ready
        assert_buttons(h, [7])
        await h.key(1)
        assert h.state.mode == 'bear_dialogue' and not h.app._game_callers
        assert_buttons(h, [7])
        owner = dict(h.app._active_audio['stand'])
        h.stand._audio_event_sequence += 1
        await h.app.receive_async(Event(terminal, 'stand', h.stand._audio_event_sequence,
            h.app.now(), 'synthetic_pcm_port', {**owner, 'response_id': owner['content_id']}))
        if terminal == 'AudioFailed':
            assert not h.state.game_offer_ready and not h.state.pending_game_offer and not h.state.active_audio_id
            assert_buttons(h, [5, 7])
            if answer == 1:
                await h.harness.speech_result('Да')
            await h.key(answer)
            assert h.state.active_game_id is None and not h.app._game_callers
            await h.simulator.action({'action': 'reset'})
            assert h.stand.panel_snapshot()['canonical_state'].get('mode') != 'bear_dialogue'
            return
        assert h.state.game_offer_ready and not h.state.active_audio_id
        assert_buttons(h, [1, 7])
        await h.key(answer)
        if answer == 1:
            assert h.state.mode == 'game_active' and h.state.active_game_id == 'quiz'
        else:
            assert h.state.mode == 'bear_dialogue' and not h.state.pending_game_offer
            assert_buttons(h, [5, 7])
            await h.done(owner)
            assert_buttons(h, [5, 7])
        await h.simulator.action({'action': 'reset'})
        assert h.stand.panel_snapshot()['canonical_state'].get('mode') != 'bear_dialogue'
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('action', [5, 7])
async def test_concern_support_preserves_button_help_and_exit(tmp_path, action):
    h = MenuStand(tmp_path)
    try:
        await h.key(4)
        await h.harness.speech_result('Мне страшно')
        assert h.state.mode == 'support_intake' and h.state.safety_severity == 'concern'
        assert_buttons(h, [5, 7])
        await h.key(action)
        assert h.state.mode == ('wellbeing_check' if action == 5 else 'root_menu')
    finally:
        await h.close()
