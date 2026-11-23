"""Native controller/PTY stand integration with synthetic PCM, never actual hardware."""
import asyncio
import sys
from importlib.machinery import ModuleSpec
from types import SimpleNamespace

import pytest

from app.foundation.canonical_contract import CanonicalState
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.personal_story import StoryOwner, STORY_ID
from app.foundation.s2s import AudioChunk, SoundDevicePlaybackController
from app.foundation.stand import HardwareIntegrationStand, HeadlessAudioOutput


@pytest.mark.asyncio
@pytest.mark.parametrize('at_end',[False,True])
async def test_native_stream_pause_resume_uses_written_frames_and_new_lease(monkeypatch,at_end):
    stand = HardwareIntegrationStand(Settings(profile='mock', runtime_mode='clinical'), background_scheduler=False)
    app, sid = stand.application, stand.active_session_id
    state = CanonicalState(sid, mode='personal_story_playing', handset='up', active_story_id=STORY_ID,
                           active_audio_id=STORY_ID, playback_lease='stream-first')
    app.canonical_runtime.sessions[sid] = state
    epoch = app._session_epochs.get(sid, 0)
    owner = StoryOwner(epoch, 'synthetic-stream', (None,None), '3-5', app.monotonic_now()+60,
                       streaming=True, complete=True)
    owner.parts = [AudioChunk((.01,)*16000,16000,1), AudioChunk((.02,)*15999+((.02,) if at_end else (0.,)),16000,1)]
    owner.audio = AudioChunk(owner.parts[0].samples+owner.parts[1].samples,16000,1)
    app._personal_stories[sid] = owner
    playback = stand.playback = SoundDevicePlaybackController()
    stand.hardware_mode = 'physical'
    writes, streams = [], []
    class Stream:
        def __init__(self, **kwargs):self.index=len(streams);streams.append(self)
        def start(self):pass
        def write(self, values):writes.append((self.index,len(values)))
        def abort(self):pass
        def close(self):pass
    monkeypatch.setitem(sys.modules,'sounddevice',SimpleNamespace(OutputStream=Stream,query_devices=lambda *_:{'max_output_channels':1},__spec__=ModuleSpec('sounddevice',None)))
    blocked, release = asyncio.Event(), asyncio.Event()
    original = playback._run_io
    calls = 0
    async def barrier(operation):
        nonlocal calls
        if operation.__name__ == 'guarded_write':
            calls += 1
            if calls == 2 and not at_end:
                blocked.set();await release.wait()
        return await original(operation)
    playback._run_io = barrier
    original_close = playback._close_stream
    async def close_barrier(stream, *, drain=False):
        if drain and at_end:
            blocked.set();await release.wait()
        await original_close(stream,drain=drain)
    playback._close_stream = close_barrier
    port = HeadlessAudioOutput()
    port.on_pcm_stream_play = stand._headless_pcm_stream_play
    port.on_pause = stand._pause_story_playback
    port.on_cancel = lambda token:stand._cancel_response_audio()
    port.on_asset_play = lambda *_:None
    app.audio = port
    seq = 0
    async def send(kind,payload=None):
        nonlocal seq
        seq += 1
        return await app.receive_async(Event(kind,sid,seq,float(seq),'test',payload or {},str(seq)))
    async def done():return await send('AudioCompleted',dict(app._active_audio[sid],response_id=app._active_audio[sid]['content_id']))
    try:
        assert app._play_personal_story(sid,{'playback_lease':state.playback_lease},1)
        await asyncio.wait_for(blocked.wait(),2)
        first_handle = app._active_audio[sid]['playback_id']
        written = writes[0][1]
        pause = asyncio.create_task(send('ButtonPressed',{'button_id':6}))
        for _ in range(100):
            if playback._generation:break
            await asyncio.sleep(.001)
        assert playback._generation
        release.set()
        assert (await asyncio.wait_for(pause,2))['reason']=='story_pause_requested'
        if at_end:
            assert sum(n for _,n in writes)==32000+48  # Actual native fade tail.
        else:
            assert writes == [(0,written)]
        assert owner.checkpoint_ms == (2000 if at_end else written*1000//16000)
        assert state.dialogue_memory.story_checkpoint['offset_ms']==owner.checkpoint_ms
        await done();await send('ButtonReleased',{'button_id':6})
        await send('ButtonPressed',{'button_id':6});await done()
        if not at_end:
            assert app._active_audio[sid]['playback_id'] != first_handle
            await asyncio.wait_for(asyncio.gather(*tuple(stand._response_audio_tasks)),3)
            assert sum(n for index,n in writes if index==1)==32000-written
        else:
            assert len(streams)==1  # Resuming the exact end never opens empty PCM.
        assert state.mode=='root_menu' and sid not in app._personal_stories
    finally:
        release.set();stand.hardware_mode='simulator';await stand.aclose()
