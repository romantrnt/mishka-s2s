"""Actual conversation controls and audio choices reach the verified catalogue."""
import asyncio
import pytest

from app.foundation.s2s import MockSpeechRecognizer
from app.foundation.menu_content import STORY_TITLES, GAME_ALIASES
from test_contextual_game_flow import Harness
from test_game_audio_reminder_race import SPEECH, SILENCE
from test_game_audio_reminder_race import DelayedRecognizer
from app.foundation.s2s import ResponseTagParser
from app.foundation.domain import Event


@pytest.mark.parametrize('action', [
    '<action type="open_catalog" catalog="games"/>',
    '<action type="open_catalog" catalog="stories"/>',
    '<action type="play_story" story_id="fixed_neznayka_v1"/>',
    '<action type="request_information" kind="where_now"/>',
])
def test_catalogue_action_parser_stays_closed(action):
    parser = ResponseTagParser()
    assert len(parser.parse(action)) == 1
    with pytest.raises(ValueError):
        parser.parse(action.replace('/>', ' path="arbitrary.wav"/>'))
    with pytest.raises(ValueError):
        parser.parse(action.replace('games', 'anything').replace('stories', 'anything')
            .replace('fixed_neznayka_v1', 'unknown').replace('where_now', 'invent_fact'))


@pytest.mark.asyncio
@pytest.mark.parametrize('catalogue,target', [('games', 'game_menu'), ('stories', 'fixed_story_menu')])
async def test_actual_model_general_request_opens_owned_catalogue(catalogue, target):
    h = Harness(f'<action type="open_catalog" catalog="{catalogue}"/>')
    try:
        await h.send('SpeechReceived', text='Хочу поиграть' if catalogue == 'games' else 'Хочу сказку')
        assert h.state.mode == target
        assert len(h.model.calls) == 1 and h.app._game_callers
        assert h.state.scene_menu_descriptor['items']
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_actual_model_named_story_uses_existing_recording_without_full_menu():
    h = Harness('<action type="play_story" story_id="fixed_aibolit_v1"/>')
    try:
        result = await h.send('SpeechReceived', text='Хочу послушать Айболита')
        assert h.state.mode == 'fixed_story_playing' and h.state.active_story_id == 'fixed_aibolit_v1'
        effect = next(effect for effect in result['outputs'] if effect.get('type') == 'play_fixed_story')
        assert effect['asset'] == 'assets/stories/original/Сказка Айболит.wav'
        assert len(h.model.calls) == 1 and h.app._game_callers
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_actual_vad_stt_named_game_interrupts_current_catalogue_narration():
    h = Harness(mode='root_menu')
    try:
        await h.key(1)
        await h.done()  # Actual recorded invitation -> owned catalogue PCM.
        assert h.state.scene_menu_phase == 'narrating' and h.state.active_audio_id == 'MENU_GAME'
        h.app.s2s_audio.recognizer = MockSpeechRecognizer('Хочу игру в слова', .99)
        h.seq += 1
        result = await h.app.receive_audio_async(h.state.session_id, SPEECH + SILENCE, h.seq)
        assert h.state.mode == 'game_active' and h.state.active_game_id == 'word_game'
        assert sum(effect.get('type') == 'game_started' for effect in result['outputs']) == 1
        assert any(effect.get('type') == 'audio_stop' for effect in result['outputs'])
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_explicit_stop_while_recorded_story_paused_leaves_playback():
    h = Harness(mode='root_menu')
    try:
        await h.key(2)
        await h.key(1)
        await h.key(6)
        await h.done()
        assert h.state.mode == 'story_paused'
        await h.send('SpeechReceived', text='Останови сказку')
        assert h.state.mode == 'root_menu'
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('story_id', list(STORY_TITLES))
@pytest.mark.parametrize('ending', ['natural','button','paused_stop'])
async def test_every_named_recording_preserves_caller_across_its_owned_lifecycle(story_id, ending):
    title = STORY_TITLES[story_id][0]
    h = Harness('<say>Я слушаю тебя.</say>', f'<action type="play_story" story_id="{story_id}"/>')
    try:
        await h.send('SpeechReceived', text='Хочу рассказать про своего кота')
        history = list(h.app.conversation_contexts[h.state.session_id].history)
        result = await h.send('SpeechReceived', text='Хочу послушать '+title)
        assert h.state.mode == 'fixed_story_playing' and h.state.active_story_id == story_id
        expected = h.app.canonical_runtime.stories.get_verified(story_id)
        effect = next(e for e in result['outputs'] if e.get('type') == 'play_fixed_story')
        assert effect['asset'] == expected['audio_file'] and effect['duration_ms'] == expected['duration_ms']
        old = dict(h.app._active_audio[h.state.session_id])
        if ending == 'natural':
            h.clock += 300
            await h.done()
        elif ending == 'button':
            await h.key(7)
        else:
            await h.key(6)
            await h.done()
            assert h.state.mode == 'story_paused'
            await h.send('SpeechReceived', text='Останови сказку')
        assert h.state.mode == 'bear_dialogue' and h.state.active_audio_id == 'DIALOGUE_RETURN'
        restored = h.app.conversation_contexts[h.state.session_id]
        assert restored.history[:1] == history and not h.app._game_callers
        await h.send('AudioCompleted', {**old, 'response_id':story_id})
        assert h.state.mode == 'bear_dialogue' and h.state.active_audio_id == 'DIALOGUE_RETURN'
        await h.done()
        await h.send('SpeechReceived', text='А теперь про облака')
        assert h.state.mode == 'bear_dialogue' and len(h.model.calls) == 3
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('game', list(GAME_ALIASES))
async def test_generic_game_catalogue_named_voice_choice_and_exit_restore_same_caller(game):
    h = Harness('<action type="open_catalog" catalog="games"/>')
    try:
        await h.send('SpeechReceived', text='Хочу поиграть')
        assert h.state.mode == 'game_menu'
        await h.send('SpeechReceived', text='Хочу '+GAME_ALIASES[game][0])
        assert h.state.active_game_id == game and h.state.game_return_mode == 'bear_dialogue'
        await h.key(7, duration=5.1 if game == 'quiz' else .01)
        assert h.state.mode == 'bear_dialogue' and not h.app._game_callers
        assert h.state.active_audio_id == 'DIALOGUE_RETURN'
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('catalogue,text', [('games','Не хочу играть'), ('stories','Назад')])
async def test_catalogue_voice_cancel_returns_its_conversation_caller(catalogue, text):
    h = Harness('<say>Люблю слушать про котят.</say>', f'<action type="open_catalog" catalog="{catalogue}"/>')
    try:
        await h.send('SpeechReceived', text='У меня котёнок')
        history = list(h.app.conversation_contexts[h.state.session_id].history)
        await h.send('SpeechReceived', text='Хочу поиграть' if catalogue == 'games' else 'Хочу сказку')
        await h.send('SpeechReceived', text=text)
        assert h.state.mode == 'bear_dialogue' and h.state.active_audio_id == 'DIALOGUE_RETURN'
        assert h.app.conversation_contexts[h.state.session_id].history[:1] == history
        assert not h.app._game_callers
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_pending_catalogue_slot_semantics_cannot_remap_to_a_new_page():
    h = Harness(mode='root_menu', band='1-3')
    entered, release = asyncio.Event(), asyncio.Event()
    task = None
    original = h.app.safety_classifier.classify
    async def delayed(text, **kwargs):
        entered.set()
        await release.wait()
        return await original(text, **kwargs)
    try:
        await h.key(2)
        h.app.safety_classifier.classify = delayed
        task = asyncio.create_task(h.send('SpeechReceived', text='Один'))
        await asyncio.wait_for(entered.wait(), 2)
        await h.app.receive_async(Event('ButtonPressed', h.state.session_id, 1, 1., 'hid', {'button_id':6}, 'page-hid', h.clock))
        assert h.state.scene_menu_descriptor['items'][0]['id'] == 'fixed_squirrel_v1'
        release.set()
        result = await task
        assert result['reason'] == 'scene_menu_choice_stale'
        assert h.state.mode == 'fixed_story_menu' and h.state.active_story_id is None
    finally:
        release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('button,scene,named', [(1,'game_menu','Хочу игру в слова'), (2,'fixed_story_menu','Хочу Телефон')])
@pytest.mark.parametrize('slot', [False, True])
async def test_actual_audio_selection_cannot_revive_after_back_and_same_catalogue_reentry(button, scene, named, slot):
    h = Harness(mode='root_menu')
    entered, release = asyncio.Event(), asyncio.Event()
    task = None
    original = h.app.safety_classifier.classify
    async def delayed(text, **kwargs):
        result = await original(text, **kwargs)
        entered.set()
        await release.wait()
        return result
    try:
        await h.key(button)
        h.app.s2s_audio.recognizer = MockSpeechRecognizer('Один' if slot else named, .99)
        h.app.safety_classifier.classify = delayed
        h.seq += 1
        task = asyncio.create_task(h.app.receive_audio_async(h.state.session_id, SPEECH + SILENCE, h.seq))
        await asyncio.wait_for(entered.wait(), 2)
        await h.key(7)
        await h.key(button)
        assert h.state.mode == scene
        release.set()
        result = await task
        assert result['reason'] == 'stale_session_generation'
        assert h.state.mode == scene and h.state.active_story_id is None and h.state.active_game_id is None
    finally:
        release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('utterance', ['Хочу Незнайку', 'Не хочу Айболита', 'Айболит или Телефон', 'Вроде Айболит'])
async def test_uncertain_negated_unknown_titles_never_launch_a_model_guessed_recording(utterance):
    h = Harness('<action type="play_story" story_id="fixed_aibolit_v1"/>')
    try:
        await h.send('SpeechReceived', text=utterance)
        assert h.state.mode != 'fixed_story_playing' and h.state.active_story_id is None
        assert not any(c.get('response_id') == 'fixed_aibolit_v1' for c in h.app.audio.commands)
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('text,target', [('Хочу Телефон','fixed_neznayka_v1'), ('Кнопка один',None)])
async def test_actual_audio_can_name_another_page_but_pending_slot_cannot_be_remapped(text, target):
    h = Harness(mode='root_menu', band='1-3')
    recognizer = DelayedRecognizer(text)
    task = None
    try:
        await h.key(2)
        assert h.state.scene_menu_descriptor['items'][0]['id'] == 'fixed_aibolit_v1'
        h.app.s2s_audio.recognizer = recognizer
        h.seq += 1
        task = asyncio.create_task(h.app.receive_audio_async(h.state.session_id, SPEECH + SILENCE, h.seq))
        await asyncio.wait_for(recognizer.entered.wait(), 2)
        if target is None:
            await h.key(6)
            assert h.state.scene_menu_descriptor['items'][0]['id'] == 'fixed_squirrel_v1'
        recognizer.release.set()
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        if target:
            assert h.state.mode == 'fixed_story_playing' and h.state.active_story_id == target
        else:
            assert h.state.mode == 'fixed_story_menu' and h.state.active_story_id is None
            assert isinstance(result, asyncio.CancelledError) or result['reason'] in {'scene_menu_choice_stale','stale_session_generation'}
    finally:
        recognizer.release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_button_wins_pending_stt_once_and_late_voice_cannot_start_second_story():
    h = Harness(mode='root_menu')
    recognizer = DelayedRecognizer('Хочу Телефон', resist_cancel=True)
    task = None
    try:
        await h.key(2)
        h.app.s2s_audio.recognizer = recognizer
        h.seq += 1
        task = asyncio.create_task(h.app.receive_audio_async(h.state.session_id, SPEECH + SILENCE, h.seq))
        await asyncio.wait_for(recognizer.entered.wait(), 2)
        await h.key(1)
        recognizer.release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert h.state.mode == 'fixed_story_playing' and h.state.active_story_id == 'fixed_aibolit_v1'
        story_commands = [c for c in h.app.audio.commands if c.get('response_id') in STORY_TITLES]
        assert len(story_commands) == 1
    finally:
        recognizer.release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_vad_capture_and_semantic_owner_survive_only_catalogue_narration_boundaries():
    from app.foundation.config import Settings
    from app.foundation.stand import HardwareIntegrationStand
    h = Harness(mode='root_menu')
    stand = HardwareIntegrationStand(settings=Settings(profile='mock'), background_scheduler=False)
    original_app = stand.application
    try:
        stand.application = h.app
        stand.active_session_id = h.state.session_id
        await h.key(2)
        assert stand._headless_audio_accepting()  # Actual source of simulator microphone.eligible.
        sid, capture = stand._headless_frame_owner()
        assert capture()
        h.app.s2s_audio.recognizer = MockSpeechRecognizer('Хочу Айболита', .99)
        h.seq += 1
        assert (await h.app.receive_audio_async(sid, SPEECH, h.seq, _ingress_guard=capture))['reason'] == 'collecting'
        await h.done()  # MENU_FIXED_STORY -> repeat question, different revision/lease.
        assert capture() and stand._headless_audio_accepting()
        h.seq += 1
        result = await h.app.receive_audio_async(sid, SILENCE, h.seq, _ingress_guard=capture)
        assert result['reason'] == 'fixed_story_started'
        assert not capture() and h.state.active_story_id == 'fixed_aibolit_v1'
    finally:
        stand.application = original_app
        await stand.aclose()
        await h.close()
