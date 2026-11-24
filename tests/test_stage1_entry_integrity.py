"""Synthetic Max cards and actual panel actions across child-context boundaries.

No physical hook or identity grants are assigned directly by these checks.
"""
import pytest

from test_panel_session_actions import PanelStand


@pytest.mark.asyncio
@pytest.mark.parametrize('identified_before', [False, True])
async def test_old_panel_end_cannot_terminate_fresh_max_context(tmp_path, identified_before):
    h = PanelStand(tmp_path, identity=True)
    try:
        if identified_before:
            await h.ready()
        await h.control('start')
        old_scope = h.scope()
        old_state = h.state
        old_generation = h.app._session_epochs['stand']
        assert h.state.handset == 'down' and h.state.interaction_open

        assert (await h.ready())['reason'] == 'profile_ready'
        fresh = h.state
        request = fresh.identity_request_id
        assert fresh is not old_state and request
        assert h.app._session_epochs['stand'] > old_generation
        assert h.app._identity_scope_live('stand', 'personalize')

        # Delayed request retains the actual wire scope captured before ready.
        await h.control('end', old_scope)
        assert h.state is fresh
        assert h.state.identity_request_id == request
        assert h.state.profile_status == 'identified' and h.state.interaction_open
        assert h.app._identity_scope_live('stand', 'personalize')
        assert h.client.patient() is not None
    finally:
        await h.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('physical_up', [False, True])
async def test_fresh_panel_end_survives_ordinary_state_versions(tmp_path, physical_up):
    h = PanelStand(tmp_path, identity=True)
    try:
        if physical_up:
            await h.simulator.action({'action': 'handset', 'state': 'up'})
        await h.ready()
        await h.control('start')
        scope = h.scope()
        version = h.state.state_version
        generation = h.app._session_epochs['stand']
        if physical_up:
            await h.harness.advance(5)  # Real current welcome timer.
            assert h.state.mode == 'voice_welcome'
        else:
            await h.key(7)  # Ordinary current day-menu action.
            assert h.state.mode == 'schedule_weather'
        assert h.state.state_version > version
        assert h.app._session_epochs['stand'] == generation
        assert (await h.control('end', scope))['reason'] == 'soft_end'
        assert h.state.handset == ('up' if physical_up else 'down')
        assert not h.state.interaction_open and not h.app._canonical_profiles
        assert not h.app._active_audio and h.client.patient() is None
    finally:
        await h.close()


@pytest.mark.asyncio
async def test_end_cancels_accepted_start_before_new_snapshot_is_published(tmp_path):
    import asyncio
    h = PanelStand(tmp_path, identity=True)
    entered, release = asyncio.Event(), asyncio.Event()
    original = h.stand._flush_device_commands
    starting = ending = None

    async def flush(*args, **kwargs):
        if asyncio.current_task() is starting:
            entered.set()
            await release.wait()
        return await original(*args, **kwargs)

    h.stand._flush_device_commands = flush
    try:
        scope = h.scope()  # The browser has not received Start's new snapshot.
        starting = asyncio.create_task(h.control('start', scope))
        await asyncio.wait_for(entered.wait(), 2)
        assert not starting.done() and h.state.interaction_open
        assert h.state.mode == 'menu_ready' and h.state.handset == 'down'
        ending = asyncio.create_task(h.control('end', scope))
        async def stopped():
            while h.state.interaction_open:
                await asyncio.sleep(.001)
        await asyncio.wait_for(stopped(), 2)
        # Publication itself is serialized; cleanup must already be effective.
        assert not starting.done()
        release.set()
        assert (await asyncio.wait_for(ending, 2))['reason'] == 'soft_end'
        await asyncio.wait_for(starting, 2)
        assert not h.state.interaction_open and not h.app._active_audio
        assert h.state.mode == 'session_closed'
    finally:
        release.set()
        await asyncio.gather(*(task for task in (starting, ending) if task), return_exceptions=True)
        h.stand._flush_device_commands = original
        await h.close()


@pytest.mark.asyncio
async def test_pending_old_end_rechecks_context_after_application_await(tmp_path):
    import asyncio
    h = PanelStand(tmp_path, identity=True)
    entered, release = asyncio.Event(), asyncio.Event()
    original = h.app.expire_identity_leases
    ending = None

    async def expiry():
        if asyncio.current_task() is ending:
            entered.set()
            await release.wait()
        return await original()

    try:
        await h.control('start')
        scope = h.scope()
        h.app.expire_identity_leases = expiry
        ending = asyncio.create_task(h.control('end', scope))
        await asyncio.wait_for(entered.wait(), 2)
        assert (await h.ready())['reason'] == 'profile_ready'
        fresh = h.state
        request = fresh.identity_request_id
        assert request and h.app._identity_scope_live('stand', 'personalize')
        release.set()
        assert (await asyncio.wait_for(ending, 2))['reason'] == 'stale_session_generation'
        assert h.state is fresh and h.state.identity_request_id == request
        assert h.state.interaction_open and h.app._identity_scope_live('stand', 'personalize')
    finally:
        release.set()
        if ending:
            await asyncio.gather(ending, return_exceptions=True)
        h.app.expire_identity_leases = original
        await h.close()
