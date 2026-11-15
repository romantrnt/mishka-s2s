import pytest
from test_owned_sequence_game import sequence_game

@pytest.mark.asyncio
@pytest.mark.parametrize('prompt',['initial','hint'])
@pytest.mark.parametrize('choice',[6,7])
async def test_pending_sequence_exit_keeps_frame_across_hangup(sequence_game,prompt,choice):
    app,clock,send,choose,frame,tick=sequence_game
    await choose(up=True)
    if prompt=='hint':
        owner=dict(app._active_audio['sequence'])
        await send('AudioCompleted',**owner,response_id='PHRASE_040')
        for _ in range(4): await tick()
        owner=dict(app._active_audio['sequence'])
        await send('AudioCompleted',**owner,response_id='PHRASE_042')
        await tick()
    old=dict(app._active_audio['sequence'])
    assert old['content_id']==('PHRASE_040' if prompt=='initial' else 'PHRASE_041')
    await send('ButtonPressed',7)
    before=dict(app.led.output.snapshot())
    await send('HandsetPlaced')
    state=app.canonical_runtime.sessions['sequence']
    assert state.substate=='game_exit_confirmation'
    assert state.game_engine_state.phase=='instruction'
    assert app.led.output.snapshot()==before
    assert 'sequence' not in app._game_timer_scopes
    await send('AudioCompleted',**old,response_id=old['content_id'])
    assert state.substate=='game_exit_confirmation'
    assert app.led.output.snapshot()==before
    await send('ButtonReleased',7)
    result=await send('ButtonPressed',choice)
    assert result['reason']==('game_exit_cancelled' if choice==6 else 'game_exit_confirmed')
    if choice==6:
        assert state.game_engine_state.phase=='demonstrate'
        frame([1])
        assert 'sequence' in app._game_timer_scopes
        await tick();frame([])
    else:
        assert state.mode=='root_menu'
        assert state.game_engine_state is None
        assert 'sequence' not in app._game_timer_scopes
    assert 'sequence' not in app._active_audio
