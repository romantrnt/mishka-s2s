"""Real queue completion boundaries; synthetic PCM and no device/model access."""
import asyncio
import sys
import types

import pytest

import app.foundation.s2s as s


class TTS:
    provider_name = 'mock'

    async def synthesize(self, text, token, cancelled):
        return s.AudioChunk((.1,) * 16000, 8000, token)


class NativeTTS(TTS):
    provider_name = 'vox555'

    async def synthesize_audio_stream(self, text, token, cancelled, *, first_chunk):
        for _ in range(2):
            yield s.AudioChunk((.1,) * 8000, 8000, token)


async def queue_for(path, playback):
    cancel = s.CancellationController()
    token = cancel.start('completion-test')
    metrics = s.PipelineMetrics()
    queue = s.AudioSynthesisQueue(NativeTTS() if path.startswith('native') else TTS(),
        None, playback, cancel, s.S2SConfig(), metrics, voice_mode='off')
    await queue.start('completion-test', token)
    if path.startswith('pre'):
        pending = asyncio.Queue()
        async def produce():
            await pending.put(('audio', s.AudioChunk((.1,) * 8, 8000, token)))
            await pending.put(('eof', None))
        queue._pre_streams = {0: (asyncio.create_task(produce()), pending)}
    await queue.submit('Публичная тестовая фраза.')
    return queue, cancel, token, metrics


@pytest.mark.asyncio
@pytest.mark.parametrize('native', [False, True])
async def test_real_controller_guard_revocation_cannot_finish_queue(monkeypatch, native):
    guard = {'ready': True}
    written = []
    class Stream:
        def __init__(self, **kwargs): pass
        def start(self): pass
        def write(self, values):
            written.append(len(values))
            guard['ready'] = False
        def stop(self): pass
        def abort(self): pass
        def close(self): pass
    monkeypatch.setitem(sys.modules, 'sounddevice', types.SimpleNamespace(OutputStream=Stream))
    monkeypatch.setattr(s, '_ensure_sounddevice_ready', lambda *args, **kwargs: None)
    playback = s.SoundDevicePlaybackController()
    playback.hardware_current = lambda: guard['ready']
    queue, cancel, token, metrics = await queue_for('native_stream' if native else 'ordinary', playback)
    with pytest.raises(RuntimeError, match='local playback incomplete'):
        await asyncio.wait_for(queue.finish(), 2)
    assert written == [4096]
    assert queue.completed_chunks == 0 and queue.failed_chunks == 1
    assert queue.failure_event.is_set() and cancel.cancelled('completion-test', token)
    assert not any(e['event'] == 'playback_finished' for e in metrics.events)
    assert not queue.resource_snapshot()['native_producer_active']
    assert queue.resource_snapshot()['pcm_bytes'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['ordinary', 'native_stream', 'native_chunks', 'pre_stream', 'pre_chunks'])
@pytest.mark.parametrize('outcome', ['incomplete', 'complete', 'legacy', 'cancelled'])
async def test_explicit_result_checked_in_every_queue_path(path, outcome):
    owner = {}
    def result():
        if outcome == 'cancelled':
            owner['cancel'].cancel('completion-test')
        if outcome == 'legacy':
            return None
        return s.PCMPlaybackResult(0, 8, outcome == 'complete', True)
    class Playback:
        active_token = 0
        async def play(self, audio): return result()
        async def stop(self): pass
    class StreamPlayback(Playback):
        async def play_stream(self, chunks, *, on_chunk=None):
            index = 0
            async for audio in chunks:
                if on_chunk is not None:
                    await on_chunk(index, audio)
                index += 1
            return result()
    playback = StreamPlayback() if path.endswith('_stream') else Playback()
    queue, cancel, token, metrics = await queue_for(path, playback)
    owner['cancel'] = cancel
    if outcome == 'incomplete':
        with pytest.raises(RuntimeError, match='local playback incomplete'):
            await asyncio.wait_for(queue.finish(), 2)
        assert queue.failed_chunks == 1 and queue.completed_chunks == 0
        assert queue.failure_event.is_set() and cancel.cancelled('completion-test', token)
    elif outcome == 'cancelled':
        try:
            await asyncio.wait_for(queue.finish(), 2)
        except TimeoutError:
            pass  # Existing native cancellation contract.
        assert queue.cancelled_chunks == 1 and queue.failed_chunks == queue.completed_chunks == 0
    else:
        await asyncio.wait_for(queue.finish(), 2)
        assert queue.completed_chunks == 1 and queue.failed_chunks == 0
    assert not queue.resource_snapshot()['native_producer_active']
    assert queue.resource_snapshot()['pcm_bytes'] == 0
    if outcome in {'incomplete', 'cancelled'}:
        assert not any(e['event'] == 'playback_finished' for e in metrics.events)
