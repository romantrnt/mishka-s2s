import pytest
import pytest_asyncio
from app.foundation.config import Settings
from app.foundation.stand import HardwareIntegrationStand


@pytest_asyncio.fixture
async def stand():
    instance = HardwareIntegrationStand(Settings(profile='mock'), background_scheduler=False)
    instance._admits_physical_users = lambda: True
    original_cancel = instance.application.audio.cancel
    original_clear = instance.application.led.output.clear_all
    try:
        yield instance
    finally:
        instance.application.audio.cancel = original_cancel
        instance.application.led.output.clear_all = original_clear
        instance.adapter.connected = True
        await instance.aclose()


@pytest.mark.asyncio
async def test_disconnected_uart_audio_cannot_prevent_hangup_dark_and_cleanup(stand):
    await stand.inject_physical({'type': 'SpaceReleased'})
    stand.adapter.connected = False
    result = await stand.inject_physical({'type': 'SpacePressed'})
    assert stand.application.led.exhibition_effects.effect == 'idle'
    assert set(stand.application.led.output.snapshot().values()) == {0}
    assert any(x.get('type') == 'cleanup_failed' and x.get('resource') == 'audio' for x in result['outputs'])


@pytest.mark.asyncio
@pytest.mark.parametrize("audio_failure,lighting_failure", [(True, False), (False, True), (True, True)])
async def test_independent_device_failures_release_session_and_report_exact_resource(stand, audio_failure, lighting_failure):
    await stand.inject_physical({'type': 'SpaceReleased'})
    app = stand.application
    attempts = []
    original_cancel = app.audio.cancel
    def cancel(token):
        attempts.append('audio')
        if audio_failure:
            raise RuntimeError('private exception detail must not be recorded')
        return original_cancel(token)
    app.audio.cancel = cancel
    output = app.led.output
    original_clear = output.clear_all
    def clear(*args, **kwargs):
        attempts.append('lighting')
        if lighting_failure:
            raise OSError('private exception detail must not be recorded')
        return original_clear(*args, **kwargs)
    output.clear_all = clear
    result = await stand.inject_physical({'type': 'SpacePressed'})
    assert attempts == ['audio', 'lighting']
    expected = ({'audio'} if audio_failure else set()) | ({'lighting'} if lighting_failure else set())
    errors = [x for x in result['outputs'] if x.get('type') == 'cleanup_failed']
    assert {x['resource'] for x in errors} == expected
    panel = stand.panel_snapshot()
    public_errors = [x for x in panel['effects'] if x.get('type') == 'cleanup_failed']
    diagnostics = [x for x in stand.diagnostics_snapshot()['diagnostics'] if x.get('type') == 'cleanup_failed']
    assert {x['resource'] for x in public_errors} == expected
    assert {x['resource'] for x in diagnostics} == expected
    assert {x['exception_type'] for x in public_errors} == ({'RuntimeError'} if audio_failure else set()) | ({'OSError'} if lighting_failure else set())
    assert 'private exception' not in str(panel) + str(diagnostics)
    assert 'private exception' not in str(errors)
    assert not app._active_audio and not app._welcome_timers and not app._session_reset_waiters
    assert not app.canonical_runtime.sessions
    assert app.led.exhibition_effects.effect == 'idle'
    dark = next(x for x in result['outputs'] if x.get('type') == 'standby_dark_started')
    assert dark['resolution'] == ('lighting_cleanup_failed' if lighting_failure else 'canonical_lighting_dark')
    if not lighting_failure:
        assert set(output.snapshot().values()) == {0}
    app.audio.cancel = original_cancel
    output.clear_all = original_clear
    await stand.inject_physical({'type': 'SpaceReleased'})
    await stand.inject_physical({'type': 'SpacePressed'})
    assert set(output.snapshot().values()) == {0}


@pytest.mark.asyncio
@pytest.mark.parametrize('malformed', ['private child text', ['private'], {'private': 'text'}, None, 42])
async def test_cleanup_public_projection_accepts_only_fixed_enums(stand, malformed):
    item = {'type': 'cleanup_failed', 'status': malformed, 'operation': malformed,
            'resource': malformed, 'exception_type': malformed, 'detail': 'private child text',
            'kind': 'private child text', 'source': 'private child text'}
    stand.application.canonical_diagnostics.append(item)
    stand.application.last_canonical_effects = [item]
    expected = {'type': 'cleanup_failed', 'status': 'failed', 'operation': 'unknown',
                'resource': 'unknown', 'exception_type': 'unknown'}
    assert stand.panel_snapshot()['effects'] == [expected]
    assert stand.diagnostics_snapshot()['diagnostics'][0] == expected
    assert stand._panel_result_item({'outputs': [item]})['outputs'] == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize('button', range(1, 8))
async def test_reconstructed_root_choice_held_does_not_repeat(stand, button):
    from app.foundation.domain import Event
    await stand.inject_physical({'type': 'SpaceReleased'})
    app = stand.application
    session_id = stand.active_session_id
    app.canonical_runtime.sessions[session_id].mode = 'exhibition_menu_repeat_listen'
    async def send(kind, sequence):
        return await app.receive_async(Event(kind, session_id, sequence, 0., 'skip-test',
                                            {'button_id': button}, f'skip:{sequence}', 0.))
    from app.foundation.canonical_contract import BUTTON_MODES
    await send('ButtonPressed', 1)
    assert app.canonical_runtime.sessions[session_id].mode == BUTTON_MODES[button]
    assert (await send('ButtonPressed', 2))['reason'] == 'exhibition_menu_choice_held'
    await send('ButtonReleased', 3)
    assert (await send('ButtonPressed', 4))['reason'] != 'exhibition_menu_choice_held'
    await stand.inject_physical({'type': 'SpacePressed'})
    assert set(app.led.output.snapshot().values()) == {0}
