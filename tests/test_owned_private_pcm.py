import asyncio
from dataclasses import replace
from importlib.machinery import ModuleSpec
import pytest
from app.foundation.adapters import RecordingAudio
from app.foundation.config import Settings
from app.foundation.domain import PlaybackHandle
from app.foundation.s2s import AudioChunk
from app.foundation.stand import HardwareIntegrationStand, HeadlessAudioOutput, UARTAudioOutput

HANDLE = PlaybackHandle('pcm-test', 0, 'play-1', 'lease-1', 'PERSONAL_WELCOME', 12)
AUDIO = AudioChunk((.12, .25), 16000, 12, 'pcm-test')

class Playback:
    def __init__(self, outcome='success'):
        self.outcome=outcome; self.played=[]; self.started=asyncio.Event(); self.release=asyncio.Event()
        self.last_playback_completed=False; self.active_token=0; self.stops=0
    async def play_owned_pcm(self, audio, owner_current):
        if owner_current():
            await self.play(audio)
    async def play(self, audio):
        self.played.append(audio); self.started.set()
        if self.outcome=='wait': await self.release.wait()
        if self.outcome=='error': raise RuntimeError('PRIVATE_NAME PCM SECRET')
        self.last_playback_completed=self.outcome!='incomplete'
    async def stop(self): self.stops+=1

async def setup(outcome='success'):
    stand=HardwareIntegrationStand(settings=Settings(profile='mock'), background_scheduler=False)
    from app.foundation.canonical_contract import CanonicalState
    stand.active_session_id=HANDLE.session_id
    stand.clock=[10.0]
    stand.application.monotonic_now=lambda:stand.clock[0]
    stand.application.optional_identity=True
    stand.application._identity_requests[HANDLE.session_id]={'request_id':'identity-1'}
    stand.application.canonical_runtime.sessions[HANDLE.session_id]=CanonicalState(
        HANDLE.session_id, handset='up', profile_ref='synthetic-profile',identity_request_id='identity-1',
        identity_expires_monotonic=20.0,identity_capabilities=['personalize'])
    stand.playback=Playback(outcome)
    stand.audio_output=HeadlessAudioOutput()
    stand.audio_output.on_pcm_play=stand._headless_pcm_play
    stand.audio_output.on_cancel=lambda token: stand._cancel_response_audio()
    stand.application.audio=stand.audio_output
    stand.events=[]; stand.diags=[]
    async def received(event): stand.events.append(event)
    stand.application.receive_async=received
    stand.application.record_audio_diagnostic=lambda code, **kwargs: stand.diags.append((code, kwargs))
    install(stand,HANDLE)
    return stand

def install(stand,handle):
    state=stand.application.canonical_runtime.sessions[handle.session_id]
    state.active_audio_id=handle.response_id
    state.playback_lease=handle.playback_lease
    stand.application._active_audio[handle.session_id]={'content_id':handle.response_id,'token':handle.token,
        'generation':handle.generation,'playback_id':handle.playback_id,'playback_lease':handle.playback_lease}

def start(stand,handle=HANDLE,audio=AUDIO):
    stand.audio_output.play_pcm_owned(handle,audio)
    return tuple(stand._response_audio_tasks)

@pytest.mark.asyncio
@pytest.mark.parametrize('outcome,kind',[('success','AudioCompleted'),('error','AudioFailed'),('incomplete','AudioFailed')])
async def test_pcm_actual_stand_dispatch_private_completion(outcome,kind):
    stand=await setup(outcome)
    try:
        await asyncio.gather(*start(stand))
        assert len(stand.events)==1 and stand.events[0].event_type==kind
        assert stand.events[0].payload.items() >= {'response_id':HANDLE.response_id,'token':12,
            'generation':0,'playback_id':'play-1','playback_lease':'lease-1'}.items()
        assert stand.active_session_id=='pcm-test'
        assert 'PRIVATE_NAME' not in repr((stand.commands,stand.errors,stand.diags,stand.events))
        assert AUDIO not in stand.commands
    finally: await stand.aclose()

@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',['lease','generation','hangup','newsession'])
async def test_stale_pcm_never_starts_or_completes(mutation):
    stand=await setup('wait')
    try:
        tasks=start(stand)
        if mutation=='lease': stand.application._active_audio[HANDLE.session_id]['playback_lease']='new'
        if mutation=='generation': stand.application._session_epochs[HANDLE.session_id]=1
        if mutation=='hangup': stand.application._active_audio.pop(HANDLE.session_id)
        if mutation=='newsession': stand.active_session_id='next-child'
        stand.playback.release.set()
        await asyncio.gather(*tasks)
        assert not stand.playback.played and not stand.events
    finally: await stand.aclose()

@pytest.mark.asyncio
async def test_cancelled_pcm_cannot_stop_or_complete_new_owner():
    stand=await setup('wait')
    try:
        old=start(stand); await asyncio.wait_for(stand.playback.started.wait(),2)
        new=replace(HANDLE,playback_id='play-2',playback_lease='lease-2',token=13)
        install(stand,new)
        tasks=start(stand,new,replace(AUDIO,token=13))
        stand.playback.release.set()
        await asyncio.gather(*old,*tasks,return_exceptions=True)
        assert [e.payload['playback_id'] for e in stand.events]==['play-2']
        assert stand.playback.stops==0 and stand.playback.active_token==13
    finally: await stand.aclose()

@pytest.mark.asyncio
async def test_hangup_during_pcm_suppresses_completion():
    stand=await setup('wait')
    try:
        tasks=start(stand); await asyncio.wait_for(stand.playback.started.wait(),2)
        stand.application._active_audio.pop(HANDLE.session_id)
        stand.audio_output.cancel(13)
        await asyncio.gather(*tasks,return_exceptions=True)
        assert not stand.events
    finally: await stand.aclose()

def test_mock_pcm_only_safe_metadata_and_uart_fail_closed():
    assert UARTAudioOutput.private_pcm_supported is False
    assert HeadlessAudioOutput.private_pcm_supported is True
    assert RecordingAudio.private_pcm_supported is True
    audio=RecordingAudio(); audio.token=12; audio.play_pcm_owned(HANDLE,AUDIO)
    assert len(audio.commands)==1
    assert set(audio.commands[0])=={'response_id','token','session_id','generation','playback_id','playback_lease','content_kind'}
    with pytest.raises(RuntimeError,match='^private_pcm_unsupported$'):
        UARTAudioOutput(None).play_pcm_owned(HANDLE,AUDIO)

@pytest.mark.asyncio
async def test_registration_after_sync_port_call_is_supported():
    stand=await setup()
    try:
        stand.application._active_audio.clear()
        tasks=start(stand)
        install(stand,HANDLE)
        await asyncio.gather(*tasks)
        assert len(stand.events)==1 and stand.events[0].event_type=='AudioCompleted'
    finally: await stand.aclose()

@pytest.mark.asyncio
async def test_private_listen_capture_waits_for_active_prompt_to_finish():
    from types import SimpleNamespace
    stand=await setup()
    try:
        record=SimpleNamespace(handset_up=True,current_node='personal_welcome_listen')
        original=stand.application.sessions.get
        stand.application.sessions.get=lambda sid:record
        assert not stand._headless_audio_accepting()
        stand.application._active_audio.clear()
        assert stand._headless_audio_accepting()
        stand.application.sessions.get=original
    finally: await stand.aclose()

@pytest.mark.asyncio
async def test_stale_queued_pcm_does_not_cancel_current_audio():
    stand=await setup('wait')
    try:
        old=start(stand); await asyncio.wait_for(stand.playback.started.wait(),2)
        stale=replace(HANDLE,playback_id='stale')
        pending=start(stand,stale)
        await asyncio.sleep(0)
        assert not old[0].cancelling()
        stand.playback.release.set()
        await asyncio.gather(*pending)
        assert [e.payload['playback_id'] for e in stand.events]==['play-1']
    finally: await stand.aclose()

@pytest.mark.asyncio
@pytest.mark.parametrize('mutation',['lease','newsession'])
async def test_late_pcm_completion_cannot_close_new_owner(mutation):
    stand=await setup('wait')
    try:
        tasks=start(stand); await asyncio.wait_for(stand.playback.started.wait(),2)
        if mutation=='lease': stand.application._active_audio[HANDLE.session_id]['playback_lease']='new'
        else: stand.active_session_id='next-child'
        stand.playback.release.set()
        await asyncio.gather(*tasks)
        assert not stand.events
    finally: await stand.aclose()

@pytest.mark.asyncio
@pytest.mark.parametrize('change',['expiry','profile','request','capability'])
async def test_private_identity_rechecked_after_previous_audio_drain(change):
    stand=await setup()
    draining=asyncio.Event(); release=asyncio.Event()
    async def previous_audio():
        try: await asyncio.Event().wait()
        except asyncio.CancelledError:
            draining.set()
            await release.wait()
    previous=asyncio.create_task(previous_audio())
    await asyncio.sleep(0)
    stand._response_audio_tasks.add(previous)
    try:
        tasks=start(stand)
        await asyncio.wait_for(draining.wait(),2)
        state=stand.application.canonical_runtime.sessions[HANDLE.session_id]
        if change=='expiry': stand.clock[0]=21.0
        if change=='profile': state.profile_ref='other-synthetic-profile'
        if change=='request':
            state.identity_request_id='identity-2'
            stand.application._identity_requests[HANDLE.session_id]={'request_id':'identity-2'}
        if change=='capability': state.identity_capabilities=[]
        release.set()
        await asyncio.gather(*tasks)
        assert not stand.playback.played and not stand.events
        assert stand.application._session_epochs.get(HANDLE.session_id,0)==0
    finally:
        release.set()
        await stand.aclose()

@pytest.mark.asyncio
@pytest.mark.parametrize('phase',['initial_prompt','retry_prompt','different_node','completed_retry'])
async def test_delayed_frame_cannot_cross_private_listening_phase(phase):
    from types import SimpleNamespace
    stand=await setup()
    original=stand.application.sessions.get
    try:
        record=SimpleNamespace(handset_up=True,current_node='personal_welcome_listen')
        stand.application.sessions.get=lambda sid:record
        state=stand.application.canonical_runtime.sessions[HANDLE.session_id]
        stand._headless_audio_queue=asyncio.Queue()
        if phase=='initial_prompt': record.current_node='voice_welcome'
        if phase in {'different_node','completed_retry'}:
            stand.application._active_audio.clear()
        owner=stand._headless_frame_owner()
        if phase=='completed_retry':
            install(stand,HANDLE)
            state.state_version+=1
        record.current_node='bear_dialogue' if phase=='different_node' else 'personal_welcome_listen'
        state.state_version+=1
        stand.application._active_audio.clear()
        stand._enqueue_headless_audio_frame((.1,.2),owner)
        assert stand._headless_audio_queue.empty()
        # A newly captured frame in the final listening phase is admitted.
        stand._enqueue_headless_audio_frame((.1,.2),stand._headless_frame_owner())
        assert stand._headless_audio_queue.qsize()==1
    finally:
        stand.application.sessions.get=original
        await stand.aclose()

@pytest.mark.asyncio
async def test_private_listener_failed_audio_keeps_capture_stopped():
    from types import SimpleNamespace
    stand=await setup()
    original=stand.application.sessions.get
    try:
        record=SimpleNamespace(handset_up=True,current_node='personal_welcome_listen')
        stand.application.sessions.get=lambda sid:record
        stand.application._active_audio.clear()
        stand._headless_audio_queue=asyncio.Queue()
        owner=stand._headless_frame_owner()
        assert stand._headless_audio_accepting()
        state=stand.application.canonical_runtime.sessions[HANDLE.session_id]
        state.audio_status='failed'
        assert not stand._headless_audio_accepting()
        stand._enqueue_headless_audio_frame((.1,.2),owner)
        stand._enqueue_headless_audio_frame((.1,.2),stand._headless_frame_owner())
        assert stand._headless_audio_queue.empty()
        record.current_node='bear_dialogue'
        assert stand._headless_audio_accepting()
    finally:
        stand.application.sessions.get=original
        await stand.aclose()

@pytest.mark.asyncio
@pytest.mark.parametrize('boundary',['play_lock','stream_start','write_dispatch'])
async def test_physical_pcm_rechecks_identity_at_native_boundaries(monkeypatch,boundary):
    import sys, threading
    from types import SimpleNamespace
    from app.foundation.s2s import SoundDevicePlaybackController
    stand=await setup()
    entered=threading.Event(); release=threading.Event(); created=[]; writes=[]
    class Stream:
        def __init__(self,**kwargs): created.append(self)
        def start(self):
            if boundary=='stream_start':
                entered.set(); release.wait(3)
        def write(self,chunk): writes.append(len(chunk))
        def abort(self): pass
        def close(self): pass
    monkeypatch.setitem(sys.modules,'sounddevice',SimpleNamespace(OutputStream=Stream,
        __spec__=ModuleSpec("sounddevice", loader=None),
        query_devices=lambda *_: {"max_output_channels": 1}))
    stand.playback=SoundDevicePlaybackController()
    lock=stand.playback._play_lock
    if boundary=='play_lock': await lock.acquire()
    original_io=stand.playback._run_io
    calls=0
    async def delayed_io(operation):
        nonlocal calls
        calls+=1
        if boundary=='write_dispatch' and calls==2:
            stand.clock[0]=21.0
        return await original_io(operation)
    stand.playback._run_io=delayed_io
    try:
        tasks=start(stand)
        if boundary=='play_lock':
            await asyncio.sleep(.02)
            stand.clock[0]=21.0
            lock.release()
        elif boundary=='stream_start':
            assert await asyncio.to_thread(entered.wait,2)
            stand.clock[0]=21.0
            release.set()
        await asyncio.wait_for(asyncio.gather(*tasks),3)
        assert not writes and not stand.events
        assert stand.playback.last_playback_completed is False
        if boundary=='play_lock': assert not created
    finally:
        release.set()
        if lock.locked(): lock.release()
        await stand.aclose()

@pytest.mark.asyncio
async def test_physical_pcm_success_uses_guarded_native_path(monkeypatch):
    import sys
    from types import SimpleNamespace
    from app.foundation.s2s import SoundDevicePlaybackController
    stand=await setup(); writes=[]
    class Stream:
        def __init__(self,**kwargs): pass
        def start(self): pass
        def write(self,chunk): writes.append(len(chunk))
        def abort(self): pass
        def close(self): pass
    monkeypatch.setitem(sys.modules,'sounddevice',SimpleNamespace(OutputStream=Stream,
        __spec__=ModuleSpec("sounddevice", loader=None),
        query_devices=lambda *_: {"max_output_channels": 1}))
    stand.playback=SoundDevicePlaybackController()
    try:
        await asyncio.gather(*start(stand))
        assert writes==[2]
        assert [e.event_type for e in stand.events]==['AudioCompleted']
    finally: await stand.aclose()

@pytest.mark.asyncio
async def test_physical_pcm_cancel_while_waiting_for_lock_never_opens_stream(monkeypatch):
    import sys
    from types import SimpleNamespace
    from app.foundation.s2s import SoundDevicePlaybackController
    stand=await setup(); created=[]
    monkeypatch.setitem(sys.modules,'sounddevice',SimpleNamespace(OutputStream=lambda **kw:created.append(kw),
        __spec__=ModuleSpec("sounddevice", loader=None),
        query_devices=lambda *_: {"max_output_channels": 1}))
    stand.playback=SoundDevicePlaybackController()
    await stand.playback._play_lock.acquire()
    try:
        tasks=start(stand)
        await asyncio.sleep(.01)
        stand._cancel_response_audio()
        stand.playback._play_lock.release()
        await asyncio.gather(*tasks,return_exceptions=True)
        assert not created and not stand.events
    finally:
        if stand.playback._play_lock.locked(): stand.playback._play_lock.release()
        await stand.aclose()

@pytest.mark.asyncio
@pytest.mark.parametrize('private',[False,True])
async def test_queued_stop_preserves_asset_semantics_but_revokes_private_pcm(monkeypatch,private):
    import sys
    from types import SimpleNamespace
    from app.foundation.s2s import SoundDevicePlaybackController
    writes=[]
    class Stream:
        def __init__(self,**kwargs): pass
        def start(self): pass
        def write(self,chunk): writes.append(len(chunk))
        def abort(self): pass
        def close(self): pass
    monkeypatch.setitem(sys.modules,'sounddevice',SimpleNamespace(OutputStream=Stream,
        __spec__=ModuleSpec("sounddevice", loader=None),
        query_devices=lambda *_: {"max_output_channels": 1}))
    playback=SoundDevicePlaybackController()
    playback.active_token=AUDIO.token
    await playback._play_lock.acquire()
    operation=(playback.play_owned_pcm(AUDIO,lambda:True) if private else playback.play(AUDIO))
    task=asyncio.create_task(operation)
    try:
        await asyncio.sleep(0)
        assert not task.done()
        await playback.stop()
        playback._play_lock.release()
        await asyncio.wait_for(task,2)
        assert writes==([] if private else [2])
        assert playback.last_playback_completed is (not private)
    finally:
        if playback._play_lock.locked(): playback._play_lock.release()
        if not task.done(): task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        await playback.stop()
