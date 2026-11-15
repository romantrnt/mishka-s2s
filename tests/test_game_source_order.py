import pytest
from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event

@pytest.mark.asyncio
async def test_admitted_sources_have_game_local_ordinal_and_duplicate_is_rejected():
    app, *_ = build_application(settings=Settings(profile='mock', runtime_mode='clinical'), background_scheduler=False)
    tick = 0
    async def send(kind, sequence, source='physical', button=None, correlation=None):
        nonlocal tick
        tick += 1
        return await app.receive_async(Event(kind,'sourcegame',sequence,float(tick),source,
            {} if button is None else {'button_id':button}, correlation or f'{source}:{sequence}',float(tick)))
    try:
        await send('HandsetLifted',90)
        await send('ButtonPressed',91,button=1)
        await send('ButtonReleased',92,button=1)
        assert (await send('ButtonPressed',93,button=1))['reason'] == 'scene_menu_skipped'
        await send('ButtonReleased',94,button=1)
        assert (await send('ButtonPressed',95,button=1))['reason'] == 'game_started'
        await send('ButtonReleased',96,button=1)
        result = await send('ButtonPressed',1,'panel',1)
        assert result['reason'] == 'game_progress'
        state = app.canonical_runtime.sessions['sourcegame']
        assert state.game_engine_state.round == 1
        duplicate = await send('ButtonPressed',1,'panel',2,correlation='different-correlation')
        assert duplicate['reason'] != 'game_progress'
        assert state.game_engine_state.round == 1
        await send('ButtonPressed',97,button=7)
        await send('ButtonReleased',2,'panel',1)
        assert 1 not in state.game_engine_state.held
        await send('ButtonPressed',98,button=6)
        assert (await send('ButtonPressed',3,'panel',2))['reason'] == 'game_progress'
    finally:
        await app._cleanup_session_resources('sourcegame')
