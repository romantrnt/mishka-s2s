import pytest
from app.foundation.api import build_application
from app.foundation.canonical_contract import CanonicalState
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.game_engine import start
from app.foundation.input_adapters import KeyboardHIDInputAdapter

@pytest.mark.asyncio
@pytest.mark.parametrize('finish_instruction_before_hold',[False,True])
@pytest.mark.parametrize('early_release',[False,True])
async def test_real_hid_edges_get_owned_release_cue_without_synthetic_held(finish_instruction_before_hold,early_release):
    clock=[100.]
    app,*_=build_application(settings=Settings(profile='mock',runtime_mode='clinical'),
                            monotonic_now=lambda:clock[0],background_scheduler=False)
    # Retained compatibility mechanic, not a slot in the current public chooser.
    engine=start('hold_light',sequence=0,now=clock[0],selection_button=0,run_id='hid-hold-compat')
    state=CanonicalState('hid-hold',mode='game_active',handset='up',
        active_game_id='hold_light',game_engine_state=engine,game_target=engine.target,
        game_audio_stage='playing',expected_inputs=['button','speech','handset_down'])
    app.canonical_runtime.sessions['hid-hold']=state
    queued=[]
    adapter=KeyboardHIDInputAdapter('/dev/null',queued.append,debounce_ms=0)
    seq=0
    async def send(kind,payload=None):
        nonlocal seq
        seq+=1
        return await app.receive_async(Event(kind,'hid-hold',seq,clock[0],'hid',payload or {},f'hid:{seq}',clock[0]))
    async def edge(value):
        adapter.feed_key(2,value,timestamp=clock[0])
        while queued:
            event=queued.pop(0)
            assert event.type in {'ButtonPressed','ButtonReleased'}
            await send(event.type,event.payload)
    async def complete():
        owner=dict(app._active_audio['hid-hold'])
        return await send('AudioCompleted',{**owner,'response_id':owner['content_id']})
    try:
        # The real reminder queues the mechanic's owned instruction recording.
        app._sync_game_timer('hid-hold')
        clock[0]=engine.hint_deadline
        assert (await app.flush_game_timers())[0]['reason']=='game_wait_hint'
        instruction=dict(app._active_audio['hid-hold'])
        assert instruction['content_id']=='PHRASE_035'
        if finish_instruction_before_hold: await complete()
        pressed_at=clock[0]
        await edge(1)
        scope=app._game_timer_scopes['hid-hold']
        assert scope[-1]==pressed_at+1.
        if early_release:
            clock[0]=pressed_at+.5
            await edge(0)
            clock[0]=pressed_at+1.
            assert await app._fire_game_timer('hid-hold',scope) is None
            assert app.canonical_runtime.sessions['hid-hold'].game_engine_state.round==0
            await edge(1)
            scope=app._game_timer_scopes['hid-hold']
            assert scope[-1]==pressed_at+2.
        clock[0]=scope[-1]-.001
        assert await app.flush_game_timers()==[]
        adapter.feed_key(2,2,timestamp=clock[0])  # actual autorepeat yields no Held
        assert queued==[]
        clock[0]=scope[-1]
        result=(await app.flush_game_timers())[0]
        assert result['reason']=='game_release_ready'
        assert state.game_engine_state.round==0
        if not finish_instruction_before_hold:
            assert app._active_audio['hid-hold']['content_id']=='PHRASE_035'
            await complete()
        assert app._active_audio['hid-hold']['content_id']=='PHRASE_036'
        release_lease=state.playback_lease
        assert release_lease!=instruction['playback_lease']
        await send('AudioCompleted',{**instruction,'response_id':instruction['content_id']})
        assert state.active_audio_id=='PHRASE_036' and state.playback_lease==release_lease
        assert state.game_engine_state.round==0
        assert await app._fire_game_timer('hid-hold',scope) is None
        await edge(0)
        assert state.game_engine_state.round==1
    finally:
        adapter.close()
        await app._cleanup_session_resources('hid-hold')
