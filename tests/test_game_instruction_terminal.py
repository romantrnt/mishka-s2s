import pytest
from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event

@pytest.mark.asyncio
@pytest.mark.parametrize('terminal', ['AudioCompleted','AudioFailed'])
@pytest.mark.parametrize('game',['catch_light','visual_free_play','find_pair','press_together','hold_light'])
async def test_owned_game_instruction_terminal_preserves_play_and_frame(terminal,game,monkeypatch):
    import app.foundation.canonical_contract as contract
    from app.foundation.game_engine import AVAILABLE_GAME_SPECS
    monkeypatch.setattr(contract,'AVAILABLE_GAME_SPECS',tuple(x for x in AVAILABLE_GAME_SPECS if x.game_id==game))
    selection=1
    app, *_ = build_application(settings=Settings(profile='mock',runtime_mode='clinical'),background_scheduler=False)
    seq=0
    async def send(kind,payload=None):
        nonlocal seq
        seq+=1
        return await app.receive_async(Event(kind,'game-audio',seq,float(seq),'test',payload or {},f'audio:{seq}',float(seq)))
    try:
        await send('HandsetLifted')
        await send('ButtonPressed',{'button_id':1})
        await send('ButtonReleased',{'button_id':1})
        assert (await send('ButtonPressed',{'button_id':1}))['reason']=='scene_menu_skipped'
        await send('ButtonReleased',{'button_id':1})
        await send('ButtonPressed',{'button_id':selection})
        await send('ButtonReleased',{'button_id':selection})
        state=app.canonical_runtime.sessions['game-audio']
        owner=dict(app._active_audio['game-audio'])
        before=dict(app.led.output.snapshot())
        result=await send(terminal,{**owner,'response_id':owner['content_id'],'error_code':'audio_playback_failed'})
        assert result['reason'] in {'game_instruction_audio_failed','game_instruction_audio_finished'}
        assert state.mode=='game_active'
        assert 'button' in state.expected_inputs
        assert app.led.output.snapshot()==before
        assert state.game_engine_state.round==0
        assert 'game-audio' not in app._active_audio
        result=await send('ButtonPressed',{'button_id':1})
        assert result['reason'] in {'game_progress','game_free_play','game_pair_partial','game_waiting_overlap','game_holding'}
        frame=dict(app.led.output.snapshot())
        await send(terminal,{**owner,'response_id':owner['content_id']})
        assert state.mode=='game_active'
        assert app.led.output.snapshot()==frame
    finally:
        await app._cleanup_session_resources('game-audio')

@pytest.mark.asyncio
async def test_instruction_dispatch_failure_keeps_visual_game():
    app,*_=build_application(settings=Settings(profile='mock',runtime_mode='clinical'),background_scheduler=False)
    seq=0
    async def send(kind,button=None):
        nonlocal seq
        seq+=1
        return await app.receive_async(Event(kind,'game-dispatch',seq,float(seq),'test',{} if button is None else {'button_id':button},f'd:{seq}',float(seq)))
    try:
        await send('HandsetLifted')
        await send('ButtonPressed',1)
        await send('ButtonReleased',1)
        assert (await send('ButtonPressed',1))['reason']=='scene_menu_skipped'
        await send('ButtonReleased',1)
        def fail(*args,**kwargs): raise OSError('speaker unavailable')
        app.audio.play_owned=fail
        result=await send('ButtonPressed',1)
        assert result['reason']=='game_instruction_audio_failed'
        assert result['canonical_state']['mode']=='game_active'
        await send('ButtonReleased',1)
        assert (await send('ButtonPressed',1))['reason']=='game_progress'
    finally:
        await app._cleanup_session_resources('game-dispatch')

@pytest.mark.asyncio
@pytest.mark.parametrize('terminal',['AudioCompleted','AudioFailed'])
async def test_release_instruction_terminal_does_not_score_hold(terminal,monkeypatch):
    import app.foundation.canonical_contract as contract
    from app.foundation.game_engine import AVAILABLE_GAME_SPECS
    monkeypatch.setattr(contract,'AVAILABLE_GAME_SPECS',tuple(x for x in AVAILABLE_GAME_SPECS if x.game_id=='hold_light'))
    app,*_=build_application(settings=Settings(profile='mock',runtime_mode='clinical'),background_scheduler=False)
    seq=0
    async def send(kind,payload=None):
        nonlocal seq
        seq+=1
        return await app.receive_async(Event(kind,'release-audio',seq,float(seq),'test',payload or {},f'r:{seq}',float(seq)))
    try:
        await send('HandsetLifted')
        await send('ButtonPressed',{'button_id':1})
        await send('ButtonReleased',{'button_id':1})
        assert (await send('ButtonPressed',{'button_id':1}))['reason']=='scene_menu_skipped'
        await send('ButtonReleased',{'button_id':1})
        for kind in ['ButtonPressed','ButtonReleased','ButtonPressed','ButtonHeld']:
            await send(kind,{'button_id':1})
        owner=dict(app._active_audio['release-audio'])
        assert owner['content_id']=='PHRASE_036'
        result=await send(terminal,{**owner,'response_id':'PHRASE_036'})
        assert result['reason'] in {'game_instruction_audio_failed','game_instruction_audio_finished'}
        state=app.canonical_runtime.sessions['release-audio']
        assert state.game_engine_state.round==0
        assert (await send('ButtonReleased',{'button_id':1}))['reason']=='game_progress'
    finally:
        await app._cleanup_session_resources('release-audio')

@pytest.mark.asyncio
async def test_old_instruction_cannot_clear_new_exit_prompt():
    app,*_=build_application(settings=Settings(profile='mock',runtime_mode='clinical'),background_scheduler=False)
    seq=0
    async def send(kind,payload=None):
        nonlocal seq
        seq+=1
        return await app.receive_async(Event(kind,'stale-game',seq,float(seq),'test',payload or {},f's:{seq}',float(seq)))
    try:
        await send('HandsetLifted')
        await send('ButtonPressed',{'button_id':1})
        await send('ButtonReleased',{'button_id':1})
        assert (await send('ButtonPressed',{'button_id':1}))['reason']=='scene_menu_skipped'
        await send('ButtonReleased',{'button_id':1})
        await send('ButtonPressed',{'button_id':1})
        old=dict(app._active_audio['stale-game'])
        await send('ButtonPressed',{'button_id':7})
        current=dict(app._active_audio['stale-game'])
        before=dict(app.led.output.snapshot())
        assert current['content_id']=='PHRASE_062'
        await send('AudioFailed',{**old,'response_id':old['content_id']})
        assert app._active_audio['stale-game']==current
        assert app.led.output.snapshot()==before
        assert app.canonical_runtime.sessions['stale-game'].substate=='game_exit_confirmation'
    finally:
        await app._cleanup_session_resources('stale-game')
