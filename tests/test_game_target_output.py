"""Synthetic legacy target mechanics use the real application lighting bridge."""
import pytest
from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.game_engine import start

# Color targets use only fill channels; digits/pictograms must stay dark.
COLOR_FILLS = {1: 14, 2: 13, 3: 10, 4: 9}

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
        result = await send('ButtonPressed', {'button_id': 1})
        assert result['reason'] == 'game_started'
        await send('ButtonReleased', {'button_id': 1})
        state = app.canonical_runtime.sessions['target-output']
        assert state.active_game_id == 'free_lights'
        # The public free-light game has no static target. Exercise the
        # retained catch-light bridge explicitly, without restoring its slot.
        state.active_game_id = 'catch_light'
        state.game_engine_state = start('catch_light', sequence=0, now=1., selection_button=0,
            held_buttons=tuple(state.accepted_buttons_down), immersive=False)
        state.game_audio_stage = 'playing'
        await send('ButtonReleased', {'button_id': 1})
        if progress:
            result = await send('ButtonPressed', {'button_id': 1})
            assert result['reason'] == 'game_progress'
        assert state.mode == 'game_active' and state.game_engine_state.game_id == 'catch_light'
        assert not state.game_engine_state.immersive
        assert state.game_engine_state.round == int(progress)
        before = dict(app.led.output.snapshot())
        await send('ButtonHeld', {'button_id': state.game_target})
        assert app.led.output.snapshot() == before
        expected = {COLOR_FILLS[state.game_target]}
        assert app.led.output.snapshot() == {str(i): int(i in expected) for i in range(18)}
    finally:
        await app._cleanup_session_resources('target-output')
