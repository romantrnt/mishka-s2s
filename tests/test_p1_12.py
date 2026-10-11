import asyncio
import json
from collections import deque

import pytest

from app.foundation.application import ScenarioApplication
from app.foundation.stand import HardwareIntegrationStand


def _application():
    app = object.__new__(ScenarioApplication)
    app._audio_completion_diagnostics = deque(maxlen=100)
    app._audio_completion_observed = __import__("weakref").WeakSet()
    app._audio_completions = {}
    app._audio_completion_started_at = {}
    app._audio_hook_counters = {
        "invocations": 0, "successes": 0, "failures": 0,
        "cancelled": 0, "mandatory_failures": 0, "duplicate_callbacks": 0,
    }
    app.audio_update_hook = None
    app.audio_update_hook_mandatory = False
    app.runtime_status = {}
    app.metrics = None
    app._last_audio_cleanup = None
    return app


def test_p1_12_optional_hook_failure_is_structured_and_does_not_fail_audio():
    async def flow():
        app = _application()

        async def broken(_session_id, _payload):
            raise ValueError("child text must not be persisted")

        app.audio_update_hook = broken
        await app._invoke_audio_update_hook(
            "s-12", {"correlation_id": "corr-12", "chunk_index": 3, "token": 9},
            generation=4, correlation_id="corr-12",
        )
        event = app._audio_completion_diagnostics[-1]
        assert event["event_type"] == "audio_update_hook_error"
        assert event["session_id"] == "s-12"
        assert event["correlation_id"] == "corr-12"
        assert event["chunk_index"] == 3 and event["token"] == 9
        assert event["hook_name"].endswith(".broken")
        assert event["exception_class"] == "ValueError"
        assert event["mandatory"] is False
        assert "child text" in event["reason"]
        assert app._audio_hook_counters["failures"] == 1

    asyncio.run(flow())


def test_p1_12_mandatory_hook_failure_propagates_with_cleanup_contract():
    async def flow():
        app = _application()
        app.audio_update_hook_mandatory = True

        def broken(_session_id, _payload):
            raise RuntimeError("state transition unavailable")

        app.audio_update_hook = broken
        with pytest.raises(RuntimeError, match="mandatory audio completion hook failed"):
            await app._invoke_audio_update_hook("s-12", {"sequence": 7}, correlation_id="corr")
        assert app._audio_hook_counters["mandatory_failures"] == 1
        assert app._audio_completion_diagnostics[-1]["mandatory"] is True

    asyncio.run(flow())


def test_p1_12_cancelled_hook_is_distinct_and_observed():
    async def flow():
        app = _application()
        started = asyncio.Event()
        release = asyncio.Event()

        async def cancelled(_session_id, _payload):
            started.set()
            await release.wait()

        app.audio_update_hook = cancelled
        task = asyncio.create_task(app._invoke_audio_update_hook("s", {}))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert app._audio_completion_diagnostics[-1]["event_type"] == "audio_update_hook_cancelled"
        assert app._audio_hook_counters["cancelled"] == 1

    asyncio.run(flow())


def test_p1_12_detached_task_error_and_cancellation_are_consumed():
    async def flow():
        app = _application()
        stand = object.__new__(HardwareIntegrationStand)
        stand.application = app
        owner = set()
        unhandled = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))

        async def broken():
            raise LookupError("bounded detached failure")

        failed = asyncio.create_task(broken())
        owner.add(failed)
        await asyncio.sleep(0)
        stand._observe_detached_audio_task(failed, owner, operation="response_audio", session_id="s")
        cancelled = asyncio.create_task(asyncio.sleep(10))
        owner.add(cancelled)
        cancelled.cancel()
        await asyncio.sleep(0)
        stand._observe_detached_audio_task(cancelled, owner, operation="story_audio", session_id="s")
        await asyncio.sleep(0)
        assert not owner
        assert not unhandled
        assert {item["event_type"] for item in app._audio_completion_diagnostics} >= {
            "detached_audio_task_error", "detached_audio_task_cancelled"
        }

    asyncio.run(flow())


def test_p1_12_hub_delivery_failure_is_not_silent():
    async def flow():
        app = _application()

        class FailingHub:
            async def publish(self, _payload):
                return [{"exception_class": "ConnectionError", "reason": "client closed"}]

        stand = object.__new__(HardwareIntegrationStand)
        stand.application = app
        stand.hub = FailingHub()
        stand.results = deque()
        stand.snapshot = lambda: {"bounded": True}
        await stand._publish_audio_completion("s", {"correlation_id": "corr", "chunk_index": 2, "token": 5})
        event = app._audio_completion_diagnostics[-1]
        assert event["event_type"] == "audio_update_hook_error"
        assert event["hook_name"] == "StandHub.publish"
        assert event["exception_class"] == "ConnectionError"
        assert event["mandatory"] is False

    asyncio.run(flow())


def test_p1_12_completion_callback_is_idempotent_and_bounded_json_safe():
    async def flow():
        app = _application()

        async def done():
            return None

        task = asyncio.create_task(done())
        app._audio_completions["s"] = {task}
        await task
        callback = app._audio_completion_callback("s")
        callback(task)
        callback(task)
        assert app._audio_completions == {}
        assert app._audio_hook_counters["duplicate_callbacks"] == 1
        for index in range(250):
            app._record_audio_completion_diagnostic(
                "audio_update_hook_error", session_id="s", correlation_id="corr",
                hook_name="test", mandatory=False, exception_type="ValueError",
                reason=f"failure-{index}", chunk_index=index, token=index,
            )
        snapshot = {"diagnostics": list(app._audio_completion_diagnostics), "counters": app._audio_hook_counters}
        assert len(snapshot["diagnostics"]) == 100
        assert all(value >= 0 for value in snapshot["counters"].values())
        json.dumps(snapshot)

    asyncio.run(flow())
