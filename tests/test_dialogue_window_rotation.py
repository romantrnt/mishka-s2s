"""Generation windows renew transactionally; session/idle and games stay bounded."""
import asyncio
import time
import pytest
from test_contextual_game_flow import Harness
from test_conversation_catalogue_gateway import quick_public_audio


@pytest.mark.asyncio
@pytest.mark.parametrize('band,limit', [('1-3',4),('4-5',5),('6-7',6)])
@pytest.mark.parametrize('mode', ['bear_dialogue','support_intake'])
async def test_ordinary_speech_continues_across_age_generation_window(band,limit,mode):
    h = Harness(band=band,mode=mode)
    try:
        quick_public_audio(h)
        for _ in range(limit):
            await h.send('SpeechReceived',text='Поговорим о котах.')
        old = h.app.conversation_contexts[h.state.session_id]
        assert old.turns == limit
        owner, history = old._owner, list(old.history)
        epoch = h.app._session_epochs.get(h.state.session_id,0)
        record = h.app.sessions.get(h.state.session_id)
        session_started = record.started_at if record else None
        for _ in range(2):
            result = await h.send('SpeechReceived',text='Коты умеют мурлыкать?')
            assert h.state.mode == mode
            assert not any(x.get('fallback_reason') or x.get('phrase_id') == 'PHRASE_095' for x in result['outputs'])
        current = h.app.conversation_contexts[h.state.session_id]
        assert len(h.model.calls) == limit + 2 and current.turns == 2
        assert current.max_turns == limit and current.max_duration_seconds == 120
        assert current._owner == owner and h.app._session_epochs.get(h.state.session_id,0) == epoch
        assert h.app.sessions.get(h.state.session_id).started_at == session_started
        assert history and len(current.history) <= h.app.bounded_s2s.config.max_history_entries
        assert sum(len(a)+len(b) for a,b in current.history) <= h.app.bounded_s2s.config.max_history_chars
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_expired_generation_window_retains_bounded_history_and_owner():
    h = Harness()
    try:
        quick_public_audio(h)
        await h.send('SpeechReceived',text='У меня есть кот.')
        old = h.app.conversation_contexts[h.state.session_id]
        history,owner = list(old.history),old._owner
        old.started_at = time.monotonic()-121
        await h.send('SpeechReceived',text='А коты умеют мурлыкать?')
        current = h.app.conversation_contexts[h.state.session_id]
        assert h.state.mode == 'bear_dialogue' and len(h.model.calls) == 2
        assert current.turns == 1 and current._owner == owner and current.history[:-1] == history
        assert old.turns == 1 and old.history == history  # Detached rollover commit.
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_mid_turn_window_timeout_does_not_exit_or_automatically_replay():
    h = Harness()
    class Slow:
        calls = 0
        async def stream(self,*args):
            self.calls += 1
            await asyncio.sleep(.2)
            yield '<say>Я слушаю.</say>'
    try:
        quick_public_audio(h)
        await h.send('SpeechReceived',text='У меня есть кот.')
        context = h.app.conversation_contexts[h.state.session_id]
        context.max_duration_seconds = .03
        context.started_at = time.monotonic()
        model = Slow(); h.app.bounded_s2s.llm = model
        result = await h.send('SpeechReceived',text='Расскажи о котах.')
        assert model.calls == 1 and h.state.mode == 'bear_dialogue'
        assert any(x.get('fallback_reason') == 'turn_timeout' for x in result['outputs'])
        assert not any(x.get('phrase_id') == 'PHRASE_095' for x in result['outputs'])
        assert h.app.conversation_contexts[h.state.session_id].turns == 0
        assert not h.app.bounded_s2s.active_queues
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_window_timeout_after_first_native_pcm_does_not_replay_or_exit(monkeypatch):
    from test_native_audio_stream_queue import RecordingNativeTTS, Playback, Voice
    h = Harness()
    class PartialJSON:
        supports_dialogue_json = True
        calls = 0
        closed = False
        async def stream_dialogue(self, messages, request_id, cancelled, *, response_schema):
            self.calls += 1
            try:
                yield '{"type":"say","text":"Я слушаю тебя. '
                await asyncio.Event().wait()
            finally:
                self.closed = True
    pending = None
    try:
        quick_public_audio(h)
        await h.send('SpeechReceived', text='У меня есть кот.')
        context = h.app.conversation_contexts[h.state.session_id]
        context.max_duration_seconds = .5
        context.started_at = time.monotonic()
        model, tts, playback = PartialJSON(), RecordingNativeTTS(), Playback()
        engine = h.app.bounded_s2s
        engine.llm, engine.tts, engine.voice, engine.playback = model, tts, Voice(), playback
        monkeypatch.setenv('MISHKA_STREAM_FIRST_JSON_CHUNK', '1')
        pending = asyncio.create_task(h.send('SpeechReceived', text='Расскажи о котах.'))
        await asyncio.wait_for(playback.first.wait(), .4)
        assert not pending.done() and tts.texts == ['Я слушаю тебя.']
        result = await asyncio.wait_for(pending, 2)
        assert model.calls == 1 and model.closed and h.state.mode == 'bear_dialogue'
        assert tts.texts == ['Я слушаю тебя.'] and len(playback.records) == 1
        assert any(x.get('fallback_reason') == 'turn_timeout' for x in result['outputs'])
        assert any(x.get('phrase_id') == 'PHRASE_104' for x in result['outputs'])
        assert not any(x.get('phrase_id') == 'PHRASE_095' for x in result['outputs'])
        assert not engine.active_queues and not engine.active_tasks
    finally:
        if pending and not pending.done(): pending.cancel()
        if pending: await asyncio.gather(pending, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
async def test_hangup_during_rollover_does_not_commit_detached_generation():
    h = Harness()
    entered,release = asyncio.Event(),asyncio.Event()
    class Waiting:
        async def stream(self,*args):
            entered.set(); await release.wait(); yield '<say>Я слушаю.</say>'
    pending = None
    try:
        render = quick_public_audio(h)
        await h.send('SpeechReceived',text='У меня есть кот.')
        old = h.app.conversation_contexts[h.state.session_id]; old.turns = old.max_turns
        old_history = list(old.history); render.texts.clear()
        h.app.bounded_s2s.llm = Waiting()
        pending = asyncio.create_task(h.send('SpeechReceived',text='Расскажи о котах.'))
        await asyncio.wait_for(entered.wait(),1)
        assert h.app.conversation_contexts[h.state.session_id] is old and old.turns == old.max_turns
        await h.send('HandsetPlaced')
        release.set()
        await asyncio.gather(pending,return_exceptions=True)
        assert old.turns == old.max_turns and old.history == old_history and not render.texts
        assert not h.app.bounded_s2s.active_queues
    finally:
        release.set()
        if pending:await asyncio.gather(pending,return_exceptions=True)
        await h.close()
