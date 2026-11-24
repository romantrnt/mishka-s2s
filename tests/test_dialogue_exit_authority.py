"""Current user authority, never a model tag, owns conversation exit."""
import asyncio
import json
import pytest
from app.foundation.s2s import dialogue_exit_requested
from test_contextual_game_flow import Harness
from test_conversation_catalogue_gateway import quick_public_audio
from test_dialogue_json_contract import wire
from test_dialogue_json_lifecycle import install


REJECTED = 'Теперь выбери занятие.'
REPAIRED = 'Расскажи о своём коте.'
USER = 'У меня есть кот.'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['bear_dialogue', 'support_intake'])
@pytest.mark.parametrize('structured', [False, True])
async def test_invented_return_repairs_same_utterance_before_rejected_speech_pcm(monkeypatch, mode, structured):
    h = Harness(f'<say>{REJECTED}</say><action type="return_to_scenario" target="root_menu"/>',
                f'<say>{REPAIRED}</say>', mode=mode)
    requests = None
    try:
        render = quick_public_audio(h)
        if structured:
            answers = iter([{'type':'say_return','text':REJECTED,'target':'root_menu'},
                            {'type':'say','text':REPAIRED}])
            requests = wire(monkeypatch, lambda _: json.dumps(next(answers), ensure_ascii=False))
            install(h)
            render = h.app.bounded_s2s.tts
        result = await h.send('SpeechReceived', text=USER)
        assert h.state.mode == mode
        assert render.texts == [REPAIRED] and h.app.bounded_s2s.playback.played
        assert not any(x.get('fallback_reason') or x.get('phrase_id') == 'PHRASE_095' for x in result['outputs'])
        context = h.app.conversation_contexts[h.state.session_id]
        assert context.turns == 1 and context.history == [(USER, REPAIRED)]
        calls = requests if structured else h.model.calls
        assert len(calls) == 2
        if structured:
            assert all(call['messages'][-1]['content'] == USER for call in calls)
            assert 'SPEECH_FORM only' in calls[-1]['messages'][0]['content']
            assert 'say_return' not in json.dumps(calls[0]['format'])
        else:
            assert all(call[-1]['content'] == USER for call in calls)
            assert 'SPEECH_FORM only' in calls[-1][0]['content']
        assert not h.app.bounded_s2s.active_queues
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_split_legacy_closed_goodbye_has_no_tts_or_pcm_before_late_action_check():
    h = Harness()
    closed, release = asyncio.Event(), asyncio.Event()
    class SplitModel:
        calls = []
        async def stream(self, messages, request_id, cancelled):
            self.calls.append(messages)
            if len(self.calls) == 1:
                yield f'<say>{REJECTED}</say>'
                closed.set()
                await release.wait()
                yield '<action type="return_to_scenario" target="root_menu"/>'
            else:
                yield f'<say>{REPAIRED}</say>'
    model = SplitModel()
    h.app.bounded_s2s.llm = model
    render = quick_public_audio(h)
    pending = asyncio.create_task(h.send('SpeechReceived', text=USER))
    try:
        await asyncio.wait_for(closed.wait(), 1)
        await asyncio.sleep(.03)  # Consumer has the closed say; return is still blocked.
        assert not render.texts and not h.app.bounded_s2s.playback.played
        release.set()
        await pending
        assert render.texts == [REPAIRED] and h.state.mode == 'bear_dialogue'
        assert len(model.calls) == 2
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['Пока.', 'Стоп.', 'Вернись в меню.', 'Закончим разговор.',
                                 'Мишка, верни меня в главное меню, пожалуйста.', 'Хочу вернуться в меню.'])
async def test_explicit_current_exit_works_without_model_authority(text):
    h = Harness('<say>Модель не должна решать выход.</say>')
    try:
        result = await h.send('SpeechReceived', text=text)
        assert h.state.mode == 'root_menu' and not h.model.calls
        assert any(x.get('fallback_reason') == 'dialogue_exit' for x in result['outputs'])
    finally:
        await h.close()


@pytest.mark.parametrize('text', ['Не выходи в меню.', 'Не хочу заканчивать разговор.',
    'Мама сказала: вернись в меню.', 'Я прочитал «пока».', '"Вернись в меню"',
    'Спасибо.', 'Да.', 'Пока я думаю, расскажи о котах.'])
def test_quoted_reported_negated_or_ambiguous_exit_has_no_authority(text):
    assert not dialogue_exit_requested(text)


@pytest.mark.asyncio
async def test_old_exit_in_history_does_not_authorize_current_yes():
    h = Harness('<say>Я слушаю.</say>')
    try:
        quick_public_audio(h)
        await h.send('SpeechReceived', text=USER)
        h.app.conversation_contexts[h.state.session_id].history = [('Вернись в меню.', 'Хорошо.')]
        h.model.answers = [f'<say>{REJECTED}</say><action type="return_to_scenario" target="root_menu"/>',
                           '<say>О чём поговорим?</say>']
        await h.send('SpeechReceived', text='Да.')
        assert h.state.mode == 'bear_dialogue'
        assert h.app.conversation_contexts[h.state.session_id].history[-1] == ('Да.', 'О чём поговорим?')
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_urgent_safety_precedes_exit_command():
    h = Harness()
    try:
        await h.send('SpeechReceived', text='Стоп. Я не хочу жить.')
        assert h.state.mode == 'safety_stop' and not h.model.calls
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_deferred_legacy_say_blocks_preserve_sentence_boundaries():
    h = Harness('<say>Привет, друг.</say><say>Давай поговорим.</say>')
    try:
        render = quick_public_audio(h)
        await h.send('SpeechReceived', text=USER)
        assert h.state.mode == 'bear_dialogue'
        assert render.texts == ['Привет, друг.', 'Давай поговорим.']
        assert h.app.conversation_contexts[h.state.session_id].history == [(USER, 'Привет, друг. Давай поговорим.')]
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_application_does_not_trust_bare_engine_return_flag():
    from app.foundation.s2s import TurnResult
    h = Harness()
    calls = []
    async def invented_return(text, context, **kwargs):
        calls.append(text)
        return TurnResult((), (), True)
    try:
        h.app.bounded_s2s.run_text_turn = invented_return
        result = await h.send('SpeechReceived', text=USER)
        assert calls == [USER] and h.state.mode == 'bear_dialogue'
        assert not any(x.get('fallback_reason') == 'dialogue_exit' or x.get('phrase_id') == 'PHRASE_095'
                       for x in result['outputs'])
    finally:
        await h.close()
