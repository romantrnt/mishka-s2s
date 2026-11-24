"""Actual Stand/screen controls and WS owners; synthetic identity/PCM, no devices."""
import asyncio
from copy import deepcopy

import pytest

from test_panel_session_actions import PanelStand
from test_button_input_ownership import setup, button


@pytest.mark.asyncio
@pytest.mark.parametrize('identity', [False, True])
@pytest.mark.parametrize('handset_up', [False, True])
async def test_screen_end_is_dark_after_delayed_frame_and_fresh_start_works(tmp_path, identity, handset_up):
    h = PanelStand(tmp_path, identity=identity)
    dark = {str(n):0 for n in range(18)}
    try:
        if identity:
            await h.ready()
        if handset_up:
            await h.simulator.action({'action':'handset', 'state':'up'})
        await h.control('start')
        await h.menu()
        await h.key(1); await h.key(2)
        assert h.state.mode in {'game_active', 'awaiting_handset'}
        assert h.state.handset == ('up' if handset_up else 'down')
        await h.control('end')
        assert h.state.mode == 'session_closed' and not h.state.interaction_open
        assert h.state.active_audio_id is None and not h.app._active_audio
        assert not h.app._canonical_profiles and not h.app.conversation_contexts
        assert h.stand.panel_snapshot()['physical_lighting'] == dark
        await h.harness.advance(10.)
        h.app.led.exhibition_effects.advance(h.harness.clock.now())
        panel = h.stand.panel_snapshot()
        assert panel['physical_lighting'] == dark
        assert panel['simulator']['devices']['lighting'] == dark
        assert h.state.handset == ('up' if handset_up else 'down')
        assert (await h.control('start'))['reason'] == ('panel_session_started' if handset_up else 'panel_silent_session_started')
        assert h.state.interaction_open
        await h.control('end')
        assert h.stand.panel_snapshot()['physical_lighting'] == dark
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal', ['release', 'cancelled_release', 'disconnect'])
async def test_safety_stop_releases_only_owned_mechanical_edges_without_advancing_scene(terminal):
    h = await setup()
    try:
        ws, task = await h.connect()
        await h.stand.inject_physical({'type':'ButtonPressed', 'button_id':2,
                                      '_source':'physical_hid', '_sequence':100})
        await button(h, ws, 5, 'press', 'help-five')
        await button(h, ws, 5, 'release', 'help-five')
        await button(h, ws, 7, 'press', 'help-seven')
        state = h.app.canonical_runtime.sessions[h.sid]
        assert state.mode == 'safety_stop' and state.accepted_buttons_down == [2,7]
        before = (state.state_version, state.mode, state.safety_severity, state.active_audio_id,
                  state.playback_lease, deepcopy(h.app._active_audio),
                  h.stand.lighting_output.snapshot(), h.app.led.effect_snapshot()['generation'],
                  h.app._session_revisions.get(h.sid))
        # A browser cannot release a physical key, even during the local stop.
        wrong = await button(h, ws, 2, 'release', 'forged-two', True)
        assert wrong['reason'] == 'button_gesture_not_owned'
        stale = await button(h, ws, 7, 'release', 'old-seven', True)
        assert stale['reason'] == 'button_gesture_not_owned'
        assert state.accepted_buttons_down == [2,7]
        await button(h, ws, 1, 'press', 'blocked-new')
        assert state.accepted_buttons_down == [2,7] and 1 not in state._button_gesture_owners
        if terminal == 'disconnect':
            ws.messages.put_nowait(None)
            await asyncio.wait_for(task, 3)
        else:
            await button(h, ws, 7, 'release', 'help-seven', terminal == 'cancelled_release')
        assert state.accepted_buttons_down == [2] and 7 not in state._button_gesture_owners
        await h.stand.inject_physical({'type':'ButtonReleased', 'button_id':2,
                                      '_source':'physical_hid', '_sequence':101})
        assert not state.accepted_buttons_down and not state._button_gesture_owners
        assert (state.state_version, state.mode, state.safety_severity, state.active_audio_id,
                state.playback_lease, h.app._active_audio,
                h.stand.lighting_output.snapshot(), h.app.led.effect_snapshot()['generation'],
                h.app._session_revisions.get(h.sid)) == before
    finally:
        await h.close()
