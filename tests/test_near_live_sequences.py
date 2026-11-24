import asyncio
import pytest
from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.lighting import FakeLightingOutput
from app.foundation.stand import UARTLEDController


def fixture_app():
    app, *_ = build_application(settings=Settings(profile='mock'), background_scheduler=False)
    output = FakeLightingOutput()
    led = UARTLEDController(None, output=output, background_scheduler=False, monotonic_now=lambda: 0.)
    engine = led.exhibition_effects
    app.led = led
    seq = 0
    async def send(kind, payload=None):
        nonlocal seq
        seq += 1
        return await app.receive_async(Event(kind, 'sequence', seq, 0., 'test', payload or {}, str(seq), 0.))
    return app, engine, output, send


@pytest.mark.asyncio
@pytest.mark.parametrize("identified", [False, True])
async def test_hangup_dark_before_slow_audio_cleanup_and_after_cancellation(identified):
    app, engine, output, send = fixture_app()
    entered = asyncio.Event()
    release = asyncio.Event()
    original = app._cancel_audio_owner
    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return {'status': 'completed'}
    try:
        await send('HandsetLifted')
        assert any(output.snapshot().values())
        if identified:
            app.optional_identity = True
            app.canonical_runtime.sessions['sequence'].profile_ref = 'test-profile'
        old_epoch = app._session_epochs.get('sequence', 0)
        app._cancel_audio_owner = delayed
        app.bounded_s2s = object()
        task = asyncio.create_task(send('HandsetPlaced'))
        await asyncio.wait_for(entered.wait(), 2)
        # Physical darkness must not wait for model/native audio cancellation.
        assert set(output.snapshot().values()) == {0}
        assert engine.effect == 'idle'
        assert 'sequence' not in app._active_audio
        assert 'sequence' not in app._welcome_timers
        assert not app.sessions.get('sequence').handset_up
        assert app._session_epochs['sequence'] > old_epoch
    finally:
        if 'task' in locals():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        app._cancel_audio_owner = original
        app.bounded_s2s = None
        await app._cleanup_session_resources('sequence')
    engine.advance(100)
    assert set(output.snapshot().values()) == {0}
    await send('HandsetLifted')
    assert app.canonical_runtime.sessions['sequence'].handset == 'up'
    await send('HandsetPlaced')
    assert set(output.snapshot().values()) == {0}


@pytest.mark.asyncio
@pytest.mark.parametrize('button', range(1, 8))
async def test_tutorial_choice_held_edge_cannot_repeat_action(button):
    app, engine, output, send = fixture_app()
    try:
        await send('HandsetLifted')
        app.canonical_runtime.sessions['sequence'].mode = 'exhibition_menu_repeat_listen'
        first = await send('ButtonPressed', {'button_id': button})
        from app.foundation.canonical_contract import BUTTON_MODES
        target = BUTTON_MODES[button]
        assert first['canonical_state']['mode'] == target
        assert len([e for e in first['outputs'] if e.get('type') == 'exhibition_menu_selected']) == 1
        held = await send('ButtonPressed', {'button_id': button})
        assert held['reason'] == 'exhibition_menu_choice_held'
        assert app.canonical_runtime.sessions['sequence'].mode == target
        await send('ButtonReleased', {'button_id': button})
        fresh = await send('ButtonPressed', {'button_id': button})
        assert fresh['reason'] != 'exhibition_menu_choice_held'
        await send('HandsetPlaced')
        engine.advance(100)
        assert set(output.snapshot().values()) == {0}
    finally:
        await app._cleanup_session_resources('sequence')


@pytest.mark.asyncio
@pytest.mark.parametrize('buttons', [(a,b) for a in range(1,8) for b in range(1,8)])
async def test_near_simultaneous_button_pairs_then_hangup_never_relight(buttons):
    app, engine, output, send = fixture_app()
    try:
        for _ in range(3):
            await send('HandsetLifted')
            app.canonical_runtime.sessions['sequence'].mode = 'exhibition_menu_repeat_listen'
            results = await asyncio.gather(*(send('ButtonPressed', {'button_id': b}) for b in buttons),
                                            send('HandsetPlaced'))
            assert all(isinstance(r, dict) for r in results)
            for b in buttons:
                await send('ButtonReleased', {'button_id': b})
            await send('HandsetPlaced')
            await send('MenuTutorialStep', {'step': 7})
            await send('ExhibitionMenuWaveComplete')
            await send('WelcomeTimerFired')
            engine.advance(100)
            assert set(output.snapshot().values()) == {0}
            state = app.canonical_runtime.sessions.get('sequence')
            assert state is None or (state.handset == 'down' and state.mode == 'idle')
            assert 'sequence' not in app._active_audio
            assert 'sequence' not in app._welcome_timers
    finally:
        await app._cleanup_session_resources('sequence')
