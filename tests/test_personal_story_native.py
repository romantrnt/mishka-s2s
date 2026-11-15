"""Real app selection and native controller, with only model/device seams mocked."""
import asyncio
import sys
from importlib.machinery import ModuleSpec
from types import SimpleNamespace
import pytest
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.s2s import SoundDevicePlaybackController
from app.foundation.stand import HardwareIntegrationStand, HeadlessAudioOutput
from test_personal_story_service import engine


async def setup(monkeypatch):
    stand = HardwareIntegrationStand(Settings(profile='mock',runtime_mode='clinical'),background_scheduler=False)
    app, sid = stand.application, stand.active_session_id
    app.bounded_s2s = engine()
    stand.hardware_mode = 'physical'
    stand.playback = playback = SoundDevicePlaybackController()
    writes, streams, handles = [], [], []
    class Stream:
        def __init__(self, **kwargs): self.index=len(streams); streams.append(self)
        def start(self): pass
        def write(self, values): writes.append((self.index,len(values)))
        def abort(self): pass
        def close(self): pass
    monkeypatch.setitem(sys.modules,'sounddevice',SimpleNamespace(OutputStream=Stream, __spec__=ModuleSpec('sounddevice', None), query_devices=lambda *args: {'max_output_channels': 1}))
    output=HeadlessAudioOutput()
    output.on_asset_play=lambda *args:None  # Owned announcement callbacks below.
    output.on_cancel=lambda token:stand._cancel_response_audio()
    output.on_pause=stand._pause_story_playback
    def pcm(handle,audio):
        handles.append((handle,len(audio.samples)))
        stand._headless_pcm_play(handle,audio)
    output.on_pcm_play=pcm
    app.audio=output
    seq=0
    async def send(kind,payload=None):
        nonlocal seq
        seq+=1
        return await app.receive_async(Event(kind,sid,seq,float(seq),'native-story-test',payload or {},f'native:{seq}',float(seq)))
    async def complete(phrase):
        lease=dict(app._active_audio[sid]);lease['response_id']=phrase
        return await send('AudioCompleted',lease)
    await send('HandsetLifted');await send('ButtonPressed',{'button_id':4})
    assert app.canonical_runtime.sessions[sid].mode=='personal_story_theme'
    return stand,send,complete,writes,handles


@pytest.mark.asyncio
async def test_actual_generated_pause_resume_uses_owned_native_position(monkeypatch):
    stand,send,complete,writes,handles=await setup(monkeypatch)
    app,sid,playback=stand.application,stand.active_session_id,stand.playback
    blocked,release=asyncio.Event(),asyncio.Event()
    original=playback._run_io; count=0
    async def barrier(operation):
        nonlocal count
        if operation.__name__=='guarded_write':
            count+=1
            if count==2:blocked.set();await release.wait()
        return await original(operation)
    playback._run_io=barrier
    try:
        assert (await send('ButtonPressed',{'button_id':1}))['reason']=='personal_story_ready'
        await asyncio.wait_for(blocked.wait(),2)
        owner=app._personal_stories[sid]; original_audio=owner.audio
        pause=asyncio.create_task(send('ButtonPressed',{'button_id':6}))
        for _ in range(100):
            if playback._generation:break
            await asyncio.sleep(.001)
        assert playback._generation
        release.set();await asyncio.wait_for(pause,2)
        state=app.canonical_runtime.sessions[sid]
        expected=int(2048*1000/original_audio.sample_rate)
        assert state.dialogue_memory.story_checkpoint=={'story_id':'PERSONAL_STORY','offset_ms':expected}
        assert writes==[(0,2048)]
        assert owner.audio is original_audio
        await complete('PHRASE_062');await send('ButtonReleased',{'button_id':6})
        await send('ButtonPressed',{'button_id':6});await complete('PHRASE_064')
        await asyncio.wait_for(asyncio.gather(*tuple(stand._response_audio_tasks)),2)
        assert len(handles)==2 and handles[0][0].playback_id!=handles[1][0].playback_id
        assert handles[1][1]==len(original_audio.samples)-2048
        assert sum(n for _,n in writes)==len(original_audio.samples)
        assert len(app.bounded_s2s.llm.calls)==1
        assert sid not in app._personal_stories
        assert state.mode=='root_menu'
    finally:
        release.set();await stand.aclose()


@pytest.mark.asyncio
async def test_pause_before_native_start_cannot_read_previous_owner_position(monkeypatch):
    stand,send,complete,writes,handles=await setup(monkeypatch)
    app,sid,playback=stand.application,stand.active_session_id,stand.playback
    playback.position_samples=999999
    await playback._play_lock.acquire()
    try:
        await send('ButtonPressed',{'button_id':1})
        await asyncio.sleep(0)
        pause=asyncio.create_task(send('ButtonPressed',{'button_id':6}))
        for _ in range(100):
            if playback._generation:break
            await asyncio.sleep(.001)
        assert playback._generation
        playback._play_lock.release()
        await asyncio.wait_for(pause,2)
        assert writes==[]
        assert app.canonical_runtime.sessions[sid].dialogue_memory.story_checkpoint=={'story_id':'PERSONAL_STORY','offset_ms':0}
    finally:
        if playback._play_lock.locked():playback._play_lock.release()
        await stand.aclose()


@pytest.mark.asyncio
async def test_hid_fault_at_native_start_blocks_generated_pcm_before_first_write(monkeypatch):
    stand,send,complete,writes,handles=await setup(monkeypatch)
    stand._hid_generation=7;stand._physical_loop=asyncio.get_running_loop()
    release=asyncio.Event(); old_cleanup=stand._handle_hid_loss
    async def cleanup(*args):
        await release.wait()
        return await old_cleanup(*args)
    stand._handle_hid_loss=cleanup
    class Stream:
        def __init__(self,**kwargs):pass
        def start(self):stand._on_hid_error({'kind':'reader_exception'},7)
        def write(self,values):writes.append(len(values))
        def abort(self):pass
        def close(self):pass
    monkeypatch.setitem(sys.modules,'sounddevice',SimpleNamespace(OutputStream=Stream, __spec__=ModuleSpec('sounddevice', None), query_devices=lambda *args: {'max_output_channels': 1}))
    try:
        await send('ButtonPressed',{'button_id':1})
        await asyncio.wait_for(asyncio.gather(*tuple(stand._response_audio_tasks)),2)
        assert writes==[]
        assert stand._capture_hid_fault is not None
    finally:
        release.set()
        if stand._hid_loss_future is not None:await asyncio.wrap_future(stand._hid_loss_future)
        await stand.aclose()


@pytest.mark.asyncio
async def test_pause_during_native_close_keeps_full_checkpoint_and_does_not_replay(monkeypatch):
    stand,send,complete,writes,handles=await setup(monkeypatch)
    app,sid,playback=stand.application,stand.active_session_id,stand.playback
    closing,release=asyncio.Event(),asyncio.Event()
    original=playback._close_stream; first=True
    async def barrier(stream):
        nonlocal first
        if first:
            first=False; closing.set(); await release.wait()
        return await original(stream)
    playback._close_stream=barrier
    try:
        await send('ButtonPressed',{'button_id':1})
        await asyncio.wait_for(closing.wait(),2)
        owner=app._personal_stories[sid]
        total=len(owner.audio.samples); duration=int(total*1000/owner.audio.sample_rate)
        assert sum(n for _,n in writes)==total
        pause=asyncio.create_task(send('ButtonPressed',{'button_id':6}))
        for _ in range(100):
            if playback._generation:break
            await asyncio.sleep(.001)
        assert playback._generation
        release.set();await asyncio.wait_for(pause,2)
        state=app.canonical_runtime.sessions[sid]
        assert state.dialogue_memory.story_checkpoint=={'story_id':'PERSONAL_STORY','offset_ms':duration}
        assert owner.checkpoint_ms==duration
        await complete('PHRASE_062');await send('ButtonReleased',{'button_id':6})
        await send('ButtonPressed',{'button_id':6}); result=await complete('PHRASE_064')
        assert state.mode=='root_menu' and sid not in app._personal_stories
        assert len(handles)==1 and sum(n for _,n in writes)==total
        assert result['reason']=='personal_story_finished'
    finally:
        release.set();await stand.aclose()
