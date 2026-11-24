"""A timeout waiting for audio cannot reserve an unsubmitted word."""
import asyncio
from dataclasses import replace

import pytest

from app.foundation.config import Settings
from app.foundation.s2s import AudioChunk, AudioSynthesisQueue
from test_word_game_rules import start


@pytest.mark.asyncio
async def test_first_audio_gate_timeout_does_not_reserve_a_word_before_pcm(monkeypatch):
    h, render = await start()
    sid = h.state.session_id
    e = h.app.bounded_s2s
    e.config = replace(e.config, first_audio_gate_s=.02)
    release, expired = asyncio.Event(), asyncio.Event()
    render.block = release
    original = AudioSynthesisQueue.wait_first_audio
    async def observed(queue, timeout):
        ready = await original(queue, timeout)
        if not ready:
            expired.set()
        return ready
    monkeypatch.setattr(AudioSynthesisQueue, 'wait_first_audio', observed)
    pending = asyncio.create_task(h.send('SpeechReceived', text='кот'))
    try:
        await asyncio.wait_for(expired.wait(), 1)
        context = h.app.conversation_contexts[sid]
        queue = e.active_queues[sid]
        calls = []
        reserve = queue.first_pcm_hook
        def observed_reserve():
            calls.append(True)
            reserve()
        queue.first_pcm_hook = observed_reserve
        assert not e.playback.played
        assert context._word_state.pending is None
        await h.key(7, duration=6.)
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        assert h.state.mode == 'bear_dialogue'
        assert not hasattr(h.app.conversation_contexts[sid], '_word_state')
        queue._notify_first_pcm(AudioChunk((.1,.2),16000,queue.token))
        assert not calls  # An obsolete provider's late notification has no authority.
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('native,stream,pre', [(False,False,False), (True,False,False),
    (True,True,False), (True,False,True), (True,True,True)])
async def test_pcm_arriving_after_gate_timeout_reserves_once_at_submission(monkeypatch,native,stream,pre):
    h, _ = await start()
    sid = h.state.session_id
    e = h.app.bounded_s2s
    e.settings = Settings(profile='mock',voice_conversion='off')
    e.config = replace(e.config,first_audio_gate_s=.02)
    expired, release_tts, played, finish = (asyncio.Event() for _ in range(4))
    class DelayedTTS:
        supports_native_stream = native
        _shares_local_gpu = not pre
        provider_name = 'synthetic'
        async def synthesize(self,text,token,cancelled):
            await release_tts.wait()
            return AudioChunk((.1,.2),16000,token)
        async def synthesize_audio_stream(self,text,token,cancelled,*,first_chunk=True):
            await release_tts.wait()
            for _ in range(3):
                yield AudioChunk((.1,.2),16000,token)
    class Playback:
        active_token = 0
        received = 0
        async def play(self,audio):
            self.received += 1
            played.set()
            await finish.wait()
        async def stop(self): pass
    class StreamingPlayback(Playback):
        async def play_stream(self,chunks,*,on_chunk=None):
            async for audio in chunks:
                if on_chunk:
                    await on_chunk(self.received,audio)
                await self.play(audio)
    e.tts = DelayedTTS()
    e.playback = StreamingPlayback() if stream else Playback()
    original = AudioSynthesisQueue.wait_first_audio
    async def observed(queue,timeout):
        ready = await original(queue,timeout)
        if not ready:
            expired.set()
        return ready
    monkeypatch.setattr(AudioSynthesisQueue,'wait_first_audio',observed)
    pending = asyncio.create_task(h.send('SpeechReceived',text='кот'))
    try:
        await asyncio.wait_for(expired.wait(),1)
        context = h.app.conversation_contexts[sid]
        queue = e.active_queues[sid]
        assert context._word_state.pending is None and not e.playback.received
        reserve = queue.first_pcm_hook
        calls = []
        def observed_reserve():
            calls.append(True)
            reserve()
        queue.first_pcm_hook = observed_reserve
        release_tts.set()
        await asyncio.wait_for(played.wait(),1)
        assert context._word_state.pending is not None and calls == [True]
        assert context.turns == 0 and h.state.game_engine_state.round == 0
        finish.set()
        result = await asyncio.wait_for(pending,2)
        assert result['reason'] == 'word_game_turn_finished' and calls == [True]
        assert e.playback.received == (3 if native else 1)
        assert h.app.conversation_contexts[sid]._word_state.pending is None
        assert h.state.game_engine_state.round == 1
    finally:
        release_tts.set()
        finish.set()
        await asyncio.gather(pending,return_exceptions=True)
        await h.close()
