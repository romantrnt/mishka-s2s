import asyncio
import io
import wave
import pytest
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.stand import HardwareIntegrationStand, HeadlessAudioOutput

@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", [None, "paused_lease", "epoch", "hid_fault", "handset_down"])
async def test_real_button6_preserves_physical_story_checkpoint_and_resume(revocation):
    stand = HardwareIntegrationStand(Settings(profile='mock', runtime_mode='clinical'), background_scheduler=False)
    app = stand.application
    sid = stand.active_session_id
    await app.receive_async(Event('HandsetLifted', sid, 1, 1., 'test', {}, 'lift', 0.))
    state = app.canonical_runtime.sessions[sid]
    story = 'placeholder:fixed_story:sample'
    state.mode='fixed_story_playing'; state.substate='story_playing'; state.handset='up'
    state.active_story_id=story; state.active_audio_id=story; state.playback_lease='story-lease'
    state.expected_inputs=['button_6','button_7','handset_down']
    stand.stories[story]={}
    app.canonical_runtime.stories.get_verified=lambda story_id: {"audio_file":"synthetic.wav", "duration_ms":1000, "status":"approved"}
    stand.hardware_mode='physical'
    started=asyncio.Event(); stopped=asyncio.Event(); guards=[]
    class Playback:
        active_token=0; start_position_samples=0; position_samples=0; last_playback_completed=False
        async def play_owned_pcm(self,audio,current):
            guards.append(current)
            assert current()
            started.set()
            await stopped.wait()
            self.position_samples=4000
            self.last_playback_completed=False
        async def stop(self):
            if revocation == 'paused_lease': app._paused_story_playback[sid]['playback_id']='other'
            elif revocation == 'epoch': app._session_epochs[sid]=1
            elif revocation == 'hid_fault': stand._capture_hid_fault=0
            elif revocation == 'handset_down': app.canonical_runtime.sessions[sid].handset='down'
            stopped.set()
    stand.playback=Playback()
    output=HeadlessAudioOutput()
    output.on_asset_play=stand._headless_asset_play
    output.on_pause=stand._pause_story_playback
    output.on_cancel=lambda token: None
    app.audio=output
    wav=io.BytesIO()
    with wave.open(wav,'wb') as out:
        out.setnchannels(1);out.setsampwidth(2);out.setframerate(16000)
        out.writeframes(b'\x01\x00'*16000)
    async def read(*args):return wav.getvalue()
    stand._offload_audio=read
    effect={'playback_lease':'story-lease'}
    app._play_owned_audio(sid,story,'synthetic.wav',1,effect)
    try:
        await asyncio.wait_for(started.wait(),2)
        # Let announcements be recorded but not start another synthetic stream.
        output.on_asset_play=lambda *args: None
        result=await app.receive_async(Event('ButtonPressed',sid,2,2.,'button',{'button_id':6},'pause',1.))
        if revocation:
            checkpoint=app.canonical_runtime.sessions[sid].dialogue_memory.story_checkpoint
            assert checkpoint.get('offset_ms',0) == 0
            assert not guards[0]()
            return
        assert result['reason']=='story_pause_requested'
        assert not guards[0]()  # checkpoint authority never authorizes old PCM.
        checkpoint=app.canonical_runtime.sessions[sid].dialogue_memory.story_checkpoint
        assert checkpoint=={'story_id':story,'offset_ms':250}
        assert sid not in app._paused_story_playback
        # Actual resume dispatch must receive the stored non-zero offset.
        resumed=[]
        output.on_asset_play=lambda *args: resumed.append(args)
        async def complete(seq, phrase):
            lease=app._active_audio[sid]
            return await app.receive_async(Event('AudioCompleted',sid,seq,float(seq),'physical_audio',{
                'response_id':phrase,'token':lease['token'],'generation':lease['generation'],
                'playback_id':lease['playback_id'],'playback_lease':lease['playback_lease']},f'complete-{seq}',float(seq)))
        await complete(3,'PHRASE_062')
        assert app.canonical_runtime.sessions[sid].mode == 'story_paused'
        await app.receive_async(Event('ButtonPressed',sid,4,4.,'button',{'button_id':6},'resume-button',4.))
        assert app.canonical_runtime.sessions[sid].mode == 'story_resume_announcement'
        result = await complete(5,'PHRASE_064')
        stories=[args for args in resumed if args[0]==story]
        assert stories and stories[0][3]==250
    finally:
        stopped.set()
        stand.hardware_mode='simulator'
        await stand.aclose()
