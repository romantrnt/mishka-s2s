import asyncio
from types import SimpleNamespace

import pytest

from app.foundation.api import create_app
from app.foundation.lifecycle import RuntimeState


def run(coro):
    return asyncio.run(coro)


class BlockingManager:
    def __init__(self):
        self.closed = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def warmup(self):
        self.started.set()
        await self.release.wait()
        return {"ok": True, "e2e": {"ok": True}}

    async def close(self):
        self.closed += 1


@pytest.mark.unit
def test_api_lifespan_cancels_pending_warmup_before_manager_close():
    async def scenario():
        manager = BlockingManager()
        app = create_app(application=SimpleNamespace(runtime_manager=manager))
        async with app.router.lifespan_context(app):
            await manager.started.wait()
            assert app.state.lifecycle.snapshot()["state"] == RuntimeState.WARMING_UP.value
        assert manager.closed == 1
        assert app.state.lifecycle.snapshot()["warmup_running"] is False

    run(scenario())


@pytest.mark.unit
def test_api_lifespan_closes_manager_when_startup_dependency_fails():
    async def scenario():
        manager = BlockingManager()

        async def start_idle_monitor():
            raise RuntimeError("idle monitor unavailable")

        application = SimpleNamespace(runtime_manager=manager, start_idle_monitor=start_idle_monitor)
        app = create_app(application=application)
        with pytest.raises(RuntimeError, match="idle monitor unavailable"):
            async with app.router.lifespan_context(app):
                pass
        assert manager.closed == 1
        assert app.state.lifecycle.snapshot()["warmup_running"] is False

    run(scenario())


@pytest.mark.unit
def test_api_without_runtime_manager_fails_closed():
    async def scenario():
        app = create_app(application=object())
        async with app.router.lifespan_context(app):
            snapshot = app.state.lifecycle.snapshot()
            assert snapshot["state"] == RuntimeState.DEGRADED.value
            assert snapshot["ready"] is False
            assert snapshot["reason"] == "runtime:manager_unavailable"

    run(scenario())
