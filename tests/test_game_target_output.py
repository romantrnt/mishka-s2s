"""Exercise physical target output through the actual application effect path."""
import pytest
from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.canonical_services import BUTTON_ELEMENTS

@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['clinical', 'exhibition'])
@pytest.mark.parametrize('progress', [False, True])
async def test_game_telemetry_preserves_single_owned_target(mode, progress):
    app, *_ = build_application(settings=Settings(profile='mock', runtime_mode=mode), background_scheduler=False)
    sequence = 0
    async def send(kind, payload=None):
        nonlocal sequence
        sequence += 1
        return await app.receive_async(Event(kind, 'target-output', sequence, 1., 'test', payload or {}, f'target:{sequence}', 1.))
    try:
        await send('HandsetLifted')
        if mode == 'exhibition':
            # Isolate game output, not onboarding. Clinical path is fully unseeded.
            app.canonical_runtime.sessions['target-output'].mode = 'menu_ready'
        await send('ButtonPressed', {'button_id': 1})
        await send('ButtonReleased', {'button_id': 1})
        skipped = await send('ButtonPressed', {'button_id': 1})
        assert skipped['reason'] == 'scene_menu_skipped'
        await send('ButtonReleased', {'button_id': 1})
        result = await send('ButtonPressed', {'button_id': 1})
        assert result['reason'] == 'game_started'
        if progress:
            await send('ButtonReleased', {'button_id': 1})
            result = await send('ButtonPressed', {'button_id': 1})
            assert result['reason'] == 'game_progress'
        state = app.canonical_runtime.sessions['target-output']
        assert state.mode == 'game_active'
        expected = set(BUTTON_ELEMENTS[state.game_target])
        assert app.led.output.snapshot() == {str(i): int(i in expected) for i in range(18)}
    finally:
        await app._cleanup_session_resources('target-output')
