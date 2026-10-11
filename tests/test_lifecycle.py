import asyncio

from app.foundation.lifecycle import RuntimeState, StartupLifecycle


def run(coro):
    return asyncio.run(coro)


def test_lifecycle_does_not_admit_users_before_ready():
    async def scenario():
        gate = StartupLifecycle()
        started = asyncio.Event()
        release = asyncio.Event()

        async def warmup():
            started.set()
            await release.wait()
            return {"ok": True, "e2e": {"ok": True}}

        await gate.start(warmup)
        await started.wait()
        assert gate.snapshot()["state"] == RuntimeState.WARMING_UP.value
        assert not gate.admits_users()
        release.set()
        await gate.run_now(warmup)
        assert gate.snapshot()["state"] == RuntimeState.READY.value
        assert gate.admits_users()

    run(scenario())


def test_lifecycle_fails_closed_on_missing_e2e_or_mock_warmup():
    async def scenario():
        cases = (
            ({"ok": True}, "full_e2e_warmup_required"),
            ({"ok": True, "mock": True, "e2e": {"ok": True}}, "full_e2e_warmup_required"),
            ({"ok": False, "reason": "ollama:model_missing"}, "ollama:model_missing"),
        )
        for result, reason in cases:
            gate = StartupLifecycle()

            async def warmup(result=result):
                return result

            actual = await gate.run_now(warmup)
            assert actual == result
            assert gate.snapshot()["state"] == RuntimeState.DEGRADED.value
            assert not gate.admits_users()
            assert gate.snapshot()["reason"] == reason

    run(scenario())


def test_lifecycle_repeated_warmup_and_dependency_failure_are_deterministic():
    async def scenario():
        gate = StartupLifecycle()

        async def good():
            return {"ok": True, "e2e": {"ok": True}}

        await gate.run_now(good)
        assert gate.admits_users()
        gate.dependency_failed("ollama", "disconnected")
        assert gate.snapshot()["state"] == RuntimeState.DEGRADED.value
        assert gate.snapshot()["reason"] == "ollama:disconnected"
        await gate.run_now(good)
        assert gate.snapshot()["state"] == RuntimeState.READY.value

    run(scenario())



def test_resource_failure_cannot_leave_lifecycle_ready():
    async def scenario():
        gate = StartupLifecycle()

        async def warmup():
            return {
                "ok": False,
                "e2e": {"ok": True},
                "resources": {
                    "ready": False,
                    "reason": "vram_reserve_insufficient",
                },
                "reason": "resource_capacity_insufficient",
            }

        result = await gate.run_now(warmup)
        assert result["e2e"]["ok"] is True
        assert gate.snapshot()["state"] == RuntimeState.DEGRADED.value
        assert gate.snapshot()["ready"] is False
        assert gate.snapshot()["user_available"] is False
        assert gate.snapshot()["reason"] == "resource_capacity_insufficient"

    run(scenario())
