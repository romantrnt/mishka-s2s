"""Real application exits the current game immediately and revokes its owners."""
import pytest
from app.foundation.api import build_application
from app.foundation.canonical_services import LED_PRESETS
from app.foundation.config import Settings
from app.foundation.domain import Event


class GameHarness:
    def __init__(self,sid='game-contract'):
        self.sid=sid
        self.app,*_=build_application(settings=Settings(profile='mock',runtime_mode='clinical'),background_scheduler=False)
        self.seq=0
    @property
    def state(self):return self.app.canonical_runtime.sessions[self.sid]
    async def send(self,kind,payload=None):
        self.seq+=1
        return await self.app.receive_async(Event(kind,self.sid,self.seq,10.,'test',payload or {},f'{self.sid}:{self.seq}',10.))
    async def press(self,b):
        result=await self.send('ButtonPressed',{'button_id':b})
        await self.send('ButtonReleased',{'button_id':b})
        return result
    async def start(self,slot=1,voice=True):
        if voice:await self.send('HandsetLifted')
        await self.press(1)
        if voice:assert (await self.press(1))['reason']=='scene_menu_skipped'
        return await self.press(slot)
    async def terminal(self,kind='AudioCompleted',owner=None):
        owner=owner or dict(self.app._active_audio[self.sid])
        return await self.send(kind,{**owner,'response_id':owner['content_id'],'error_code':'audio_playback_failed'})
    async def close(self):await self.app._cleanup_session_resources(self.sid)


@pytest.mark.asyncio
@pytest.mark.parametrize('handset_up',[False,True])
@pytest.mark.parametrize('slot',[1,3,4,5])
async def test_game_exit_is_immediate_and_returns_a_live_root_menu(handset_up,slot):
    h=GameHarness()
    try:
        await h.start(slot,voice=handset_up)
        result=await h.press(7)
        assert result['reason']=='game_exited_by_button'
        assert h.state.mode=='root_menu' and h.state.game_engine_state is None
        assert h.sid not in h.app._active_audio
        assert {'audio_stop','capture_close','game_exit'} <= {e['type'] for e in result['outputs']}
        expected={str(i):0 for i in range(18)}
        expected.update({str(i):value for i,value in LED_PRESETS['ROOT_MENU']})
        assert h.app.led.output.snapshot()==expected
        assert (await h.press(5))['reason']=='help_priority'
    finally:await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal',['AudioCompleted','AudioFailed'])
async def test_old_game_audio_cannot_reopen_game_after_exit(terminal):
    h=GameHarness()
    try:
        await h.start()
        old=dict(h.app._active_audio[h.sid])
        await h.press(7)
        before=h.app.led.output.snapshot()
        await h.terminal(terminal,old)
        assert h.state.mode=='root_menu' and h.state.game_engine_state is None
        assert h.app.led.output.snapshot()==before and h.sid not in h.app._active_audio
    finally:await h.close()


@pytest.mark.asyncio
async def test_game_exit_needs_no_working_speaker_or_confirmation_recording():
    h=GameHarness()
    try:
        await h.start()
        def fail(*args,**kwargs):raise OSError('speaker unavailable')
        h.app.audio.play_owned=fail
        assert (await h.press(7))['reason']=='game_exited_by_button'
        assert h.state.mode=='root_menu'
        await h.send('HandsetPlaced')
        await h.send('HandsetLifted')
        assert h.state.mode!='game_active'
    finally:await h.close()
