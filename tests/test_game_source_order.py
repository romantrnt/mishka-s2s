import pytest
from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.game_engine import current_challenge

@pytest.mark.asyncio
async def test_admitted_sources_have_game_local_ordinal_and_duplicate_is_rejected():
    app, *_ = build_application(settings=Settings(profile='mock', runtime_mode='clinical'), background_scheduler=False)
    tick = 0
    audio_sequence = 0
    async def send(kind, sequence, source='physical', button=None, correlation=None):
        nonlocal tick
        tick += 1
        return await app.receive_async(Event(kind,'sourcegame',sequence,float(tick),source,
            {} if button is None else {'button_id':button}, correlation or f'{source}:{sequence}',float(tick)))
    async def finish_prompt():
        nonlocal tick,audio_sequence
        for _ in range(2):
            lease=app._active_audio.get('sourcegame')
            if lease is None:break
            tick+=1;audio_sequence+=1
            await app.receive_async(Event('AudioCompleted','sourcegame',audio_sequence,float(tick),'audio',
                {**lease,'response_id':lease['content_id']},f'audio:{audio_sequence}',float(tick)))
        assert app.canonical_runtime.sessions['sourcegame'].game_audio_stage=='playing'
    try:
        await send('HandsetLifted',90)
        await send('ButtonPressed',91,button=1)
        await send('ButtonReleased',92,button=1)
        assert (await send('ButtonPressed',93,button=4))['reason'] == 'game_started'
        await send('ButtonReleased',94,button=4)
        await finish_prompt()
        state = app.canonical_runtime.sessions['sourcegame']
        assert state.active_game_id=='odd_one_out'
        answer=current_challenge(state.game_engine_state).answer
        ordinal=state.game_engine_state.last_sequence
        result = await send('ButtonPressed',1,'panel',answer)
        assert result['reason'] == 'game_progress'
        assert state.game_engine_state.round == 1
        assert state.game_engine_state.last_sequence>ordinal
        ordinal=state.game_engine_state.last_sequence
        duplicate = await send('ButtonPressed',1,'panel',2,correlation='different-correlation')
        assert duplicate['reason'] != 'game_progress'
        assert state.game_engine_state.round == 1
        assert state.game_engine_state.last_sequence==ordinal
        await send('ButtonPressed',97,button=7)
        assert state.mode=='root_menu' and state.game_engine_state is None
        await send('ButtonReleased',2,'panel',answer)
        assert answer not in state.accepted_buttons_down
        assert state.mode=='root_menu' and state.game_engine_state is None
        await send('ButtonReleased',98,button=7)
        await send('ButtonPressed',99,button=1)
        await send('ButtonReleased',100,button=1)
        assert (await send('ButtonPressed',101,button=4))['reason']=='game_started'
        await send('ButtonReleased',102,button=4)
        await finish_prompt()
        answer=current_challenge(state.game_engine_state).answer
        assert (await send('ButtonPressed',3,'panel',answer))['reason'] == 'game_progress'
        assert state.game_engine_state.round==1
    finally:
        await app._cleanup_session_resources('sourcegame')
