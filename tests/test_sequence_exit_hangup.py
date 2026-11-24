import pytest
from test_owned_sequence_game import sequence_game


@pytest.mark.asyncio
@pytest.mark.parametrize('prompt',['initial','hint'])
@pytest.mark.parametrize('recognized',[False,True])
async def test_pending_sequence_exit_is_revoked_by_clinical_hangup(sequence_game,prompt,recognized):
    app,clock,send,choose,frame,tick,terminal=sequence_game
    await choose(up=True)
    await terminal()  # Optional start phrase precedes the validated game prompt.
    if prompt=='hint':
        await terminal()
        state=app.canonical_runtime.sessions['sequence']
        for _ in range(2*len(state.game_engine_state.pattern)):await tick()
        await terminal()  # PHRASE_042 before the response deadline.
        await tick()
    old=dict(app._active_audio['sequence'])
    assert old['content_id']==('GAME_PROMPT' if prompt=='initial' else 'PHRASE_041')
    assert (await send('ButtonPressed',7))['reason']=='game_exit_pending'
    state=app.canonical_runtime.sessions['sequence']
    assert state.game_engine_state.phase=='instruction'
    old_scope=app._game_timer_scopes['sequence']
    if recognized:
        await tick()
        assert state.game_engine_state.seven_exit_phase=='progress'
        frame([1])
        old_scope=app._game_timer_scopes['sequence']
    assert (await send('HandsetPlaced'))['reason']=='handset_down'
    assert state.mode=='game_active' and state.handset=='down'
    assert state.game_engine_state.seven_exit_phase=='idle'
    assert state.game_engine_state.phase=='demonstrate'
    assert state.substate!='game_exit_confirmation'
    frame([state.game_engine_state.pattern[0]])
    before=dict(app.led.output.snapshot())
    assert 'sequence' not in app._active_audio
    for kind in ('AudioCompleted','AudioFailed'):
        await terminal(kind,old)
        assert app.led.output.snapshot()==before
    assert await app._fire_game_timer('sequence',old_scope) is None
    assert app.led.output.snapshot()==before
    await send('ButtonReleased',7)
    assert state.mode=='game_active' and state.game_engine_state.seven_exit_phase=='idle'
    # A fresh physical hold owns progress and exits only on release at five seconds.
    assert (await send('ButtonPressed',7))['reason']=='game_exit_pending'
    for count in range(1,8):
        await tick()
        frame(range(1,count+1))
    assert state.mode=='game_active' and state.game_engine_state.seven_exit_phase=='ready'
    assert (await send('ButtonReleased',7))['reason']=='game_exited_by_long_button'
    assert state.mode=='root_menu' and state.game_engine_state is None
    assert 'sequence' not in app._game_timer_scopes and 'sequence' not in app._active_audio
