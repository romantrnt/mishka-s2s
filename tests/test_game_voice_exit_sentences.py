"""Exit requires the whole current command, not a quote or a guessed intent."""
import pytest
from test_immersive_game_routing import Route


@pytest.mark.parametrize('slot', [1,2,3,4,5])
@pytest.mark.parametrize('text', [
    'Стоп, я вспомнил слово: яблоко',
    'Я не хочу выходить из игры',
    'Мама сказала: «Я не хочу больше играть»',
    '«Я не хочу больше играть»',
    'Я сказал: стоп',
    'Я не хочу больше играть — так сказал герой',
])
def test_noncommand_sentence_cannot_exit_even_with_wrong_stop_intent(slot, text):
    h = Route(); h.menu(); h.start(slot)
    run_id = h.state.game_engine_state.run_id
    result = h.send('VoiceIntentReady', {'intent':'stop', 'safety_severity':'none'}, text)
    assert result['reason'] != 'game_exited_by_voice'
    assert h.state.mode == 'game_active' and h.state.game_engine_state.run_id == run_id


@pytest.mark.parametrize('text', ['Я не буду больше играть', 'Мишка, я не хочу играть, пожалуйста'])
def test_full_first_person_command_exits_current_word_game(text):
    h = Route(); h.menu(); h.start(5)
    result = h.send('VoiceIntentReady', {'intent':'free_speech', 'safety_severity':'none'}, text)
    assert result['reason'] == 'game_exited_by_voice' and h.state.mode == 'game_menu'
