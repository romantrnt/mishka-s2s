import asyncio

import pytest
from app.foundation.config import Settings
from app.foundation.input_adapters import KeyboardHIDInputAdapter, NormalizedInputEvent
from app.foundation.stand import HardwareIntegrationStand, StandHub


@pytest.mark.asyncio
async def test_slow_observer_keeps_only_latest_snapshot_and_preserves_events():
    hub = StandHub()
    entered, release = asyncio.Event(), asyncio.Event()
    received = []
    class Observer:
        async def send_json(self, payload):
            received.append(payload)
            if len(received) == 1:
                entered.set()
                await release.wait()
    await hub.connect(Observer())
    first = asyncio.create_task(hub.publish({"type": "stand_state", "state": 0}))
    await entered.wait()
    try:
        for i in range(1, 1001):
            await hub.publish({"type": "stand_state", "state": i})
        drain = hub._state_publish_task
        assert drain is not None
        assert hub._pending_state[1]["state"] == 1000
        event = {"type": "stand_state", "state": 1001, "audio_completion": True}
        completion = asyncio.create_task(hub.publish(event))
        await asyncio.sleep(0)
        assert not completion.done()
        await hub.publish({"type": "stand_state", "state": 1002})
        release.set()
        await asyncio.gather(first, completion, drain)
        assert received == [{"type": "stand_state", "state": 0},
                            event, {"type": "stand_state", "state": 1002}]
    finally:
        release.set()
        await first
        await hub.aclose()
    assert hub._state_publish_task is None


@pytest.mark.asyncio
async def test_physical_flood_is_bounded_faults_explicitly_and_admits_hangup():
    stand = HardwareIntegrationStand(Settings(profile="mock"), background_scheduler=False)
    stand._physical_loop = asyncio.get_running_loop()
    stand._hid_generation = 1
    blocked = asyncio.Event()
    down = asyncio.Event()
    async def inject(payload):
        if payload["type"] == "SpacePressed":
            down.set()
            return {}
        await blocked.wait()
    stand.inject_physical = inject
    event = NormalizedInputEvent("ButtonPressed", {"button_id": 1})
    try:
        # No loop yield: the cap applies before coroutine creation/scheduling.
        for _ in range(1000):
            stand._on_physical_input(event, 1)
        assert stand._physical_pending_count <= stand.MAX_PENDING_PHYSICAL_INPUTS
        assert stand._capture_hid_fault == 1
        assert any(item.get("error_code") == "hid_input_overflow" for item in stand.errors)
        stand._on_physical_input(NormalizedInputEvent("SpacePressed", {}), 1)
        await asyncio.wait_for(down.wait(), 1)
        if stand._hid_loss_future is not None:
            await asyncio.wrap_future(stand._hid_loss_future)
        for _ in range(10):
            await asyncio.sleep(0)
        assert stand._physical_pending_count == 0
    finally:
        blocked.set()
        await stand.aclose()


@pytest.mark.asyncio
async def test_real_hid_edges_with_headless_playback_do_not_accumulate_behind_observer():
    stand = HardwareIntegrationStand(Settings(profile="mock"), background_scheduler=False)
    stand._physical_loop = asyncio.get_running_loop()
    stand._hid_generation = 1
    stand._admits_physical_users = lambda: True
    hid = KeyboardHIDInputAdapter("synthetic", lambda event: stand._on_physical_input(event, 1))
    hid._known_levels = set(hid.SUPPORTED_KEY_CODES)
    hid._pressed = {hid.KEY_SPACE}
    class Observer:
        async def send_json(self, payload):
            await asyncio.sleep(.1)
    observer = Observer()
    # Headless playback stops the native speaker, never sends browser PCM/stop.
    # Substitute only that device boundary; retain real HID and canonical dispatch.
    async def native_stop():
        pass
    stand.playback.stop = native_stop
    try:
        hid.feed_key(hid.KEY_SPACE, 0)
        for _ in range(100):
            await asyncio.sleep(.005)
            if stand._physical_pending_count == 0:
                break
        initial = stand.application.canonical_runtime.sessions.get(stand.active_session_id)
        assert initial is not None and initial.handset == "up"
        await stand.hub.connect(observer)
        peak = 0
        for _ in range(20):
            for key in range(2, 8):
                hid.feed_key(key, 1)
            await asyncio.sleep(.05)
            for key in range(2, 8):
                hid.feed_key(key, 0)
            await asyncio.sleep(.05)
            peak = max(peak, stand._physical_pending_count)
        assert peak < stand.MAX_PENDING_PHYSICAL_INPUTS
        assert getattr(stand, "_capture_hid_fault", None) is None
        hid.feed_key(hid.KEY_SPACE, 1)
        for _ in range(100):
            await asyncio.sleep(.005)
            state = stand.application.canonical_runtime.sessions.get(stand.active_session_id)
            if (state is None or state.handset == "down") and stand._physical_pending_count == 0:
                break
        assert state is None or state.handset == "down"
        assert stand._physical_pending_count == 0
    finally:
        stand.hub.disconnect(observer)
        await stand.aclose()


@pytest.mark.asyncio
async def test_ordered_observer_overload_disconnects_before_unbounded_growth():
    hub = StandHub()
    entered, release = asyncio.Event(), asyncio.Event()
    class Observer:
        async def send_json(self, payload):
            entered.set()
            await release.wait()
    observer = Observer()
    closed = []
    async def close(**kwargs):
        closed.append(kwargs["code"])
    observer.close = close
    await hub.connect(observer)
    first = asyncio.create_task(hub.publish({"type": "audio_stop", "generation": 0}))
    await entered.wait()
    queued = [asyncio.create_task(hub.publish({"type": "audio_stop", "generation": i}))
              for i in range(1, 100)]
    try:
        for _ in range(3):
            await asyncio.sleep(0)
        assert len(hub._ordered_publications) <= hub.MAX_PENDING_PUBLICATIONS
        assert observer not in hub.clients
        assert closed == [1013]
        release.set()
        results = await asyncio.gather(first, *queued)
        assert any(result and result[0]["reason"] == "observer backlog exceeded" for result in results)
        assert not hub._ordered_publications
    finally:
        release.set()
        await asyncio.gather(first, *queued, return_exceptions=True)
        await hub.aclose()


@pytest.mark.asyncio
async def test_shutdown_settles_reserved_hangup_before_resources_close():
    stand = HardwareIntegrationStand(Settings(profile="mock"), background_scheduler=False)
    stand._physical_loop = asyncio.get_running_loop()
    stand._hid_generation = 1
    entered = asyncio.Event()
    exited = asyncio.Event()
    async def inject(payload):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            assert not stand._closed
            exited.set()
    stand.inject_physical = inject
    stand._on_physical_input(NormalizedInputEvent("SpacePressed", {}), 1)
    await entered.wait()
    await stand.aclose()
    assert exited.is_set()
    assert stand._physical_pending_count == 0
    assert not stand._physical_pending
