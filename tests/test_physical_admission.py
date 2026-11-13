import pytest
from app.foundation.config import Settings
from app.foundation.lifecycle import RuntimeState
from app.foundation.stand import create_stand_app

@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [RuntimeState.BOOTING,RuntimeState.WARMING_UP,RuntimeState.DEGRADED,RuntimeState.STOPPING])
async def test_physical_new_interaction_requires_actual_ready(phase):
    api=create_stand_app(Settings())
    stand=api.state.stand
    api.state.lifecycle._state=phase
    try:
        for payload in ({"type":"SpaceReleased"},{"type":"button","button_id":1}):
            result=await stand.inject_physical(payload)
            assert result["reason"]=="runtime_unavailable"
        assert stand.application.sessions.get(stand.active_session_id) is None
    finally:
        await stand.aclose()

@pytest.mark.asyncio
async def test_hangup_allowed_after_degradation():
    api=create_stand_app(Settings())
    stand=api.state.stand
    try:
        api.state.lifecycle._state=RuntimeState.READY
        await stand.inject_physical({"type":"SpaceReleased"})
        api.state.lifecycle._state=RuntimeState.DEGRADED
        result=await stand.inject_physical({"type":"SpacePressed"})
        assert result.get("reason")!="runtime_unavailable"
        session=stand.application.sessions.get(stand.active_session_id)
        assert session is None or not session.handset_up
    finally:
        await stand.aclose()

def test_shutdown_gate_closes_before_async_cleanup():
    from app.foundation.lifecycle import StartupLifecycle
    lifecycle=StartupLifecycle()
    lifecycle._state=RuntimeState.READY
    lifecycle.begin_shutdown()
    assert not lifecycle.admits_users()
    assert not lifecycle.snapshot()["ready"]

@pytest.mark.asyncio
async def test_shutdown_cannot_be_reopened_by_recovery_or_new_warmup():
    from app.foundation.lifecycle import StartupLifecycle
    lifecycle=StartupLifecycle()
    generation=await lifecycle.begin_recovery("test","test")
    lifecycle.begin_shutdown()
    result={"ok":True,"e2e":{"ok":True}}
    assert not lifecycle.finish_recovery(generation,result)
    blocked=await lifecycle.begin_recovery("late","late")
    assert not lifecycle.finish_recovery(blocked,result)
    async def warm(): return result
    with pytest.raises(RuntimeError,match="lifecycle_stopping"):
        await lifecycle.start(warm)
    assert lifecycle.snapshot()["state"]=="STOPPING"
    assert not lifecycle.admits_users()

@pytest.mark.asyncio
async def test_cancel_resistant_warmup_cannot_reopen_shutdown():
    import asyncio
    from app.foundation.lifecycle import StartupLifecycle
    lifecycle=StartupLifecycle()
    entered=asyncio.Event()
    async def warm():
        entered.set()
        try: await asyncio.sleep(30)
        except asyncio.CancelledError: return {"ok":True,"e2e":{"ok":True}}
    await lifecycle.start(warm)
    await entered.wait()
    await lifecycle.stop()
    assert lifecycle.snapshot()["state"]=="STOPPING"
    assert not lifecycle.admits_users()
