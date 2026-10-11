from __future__ import annotations

import asyncio
import time
from enum import Enum
from typing import Any, Awaitable, Callable


class RuntimeState(str, Enum):
    BOOTING = "BOOTING"
    WARMING_UP = "WARMING_UP"
    READY = "READY"
    DEGRADED = "DEGRADED"


Warmup = Callable[[], Awaitable[dict[str, Any]]]


class StartupLifecycle:
    """Single lifecycle owner shared by health, readiness and WebSocket gates."""

    def __init__(self) -> None:
        self._state = RuntimeState.BOOTING
        self._reason = "process_started"
        self._changed_at = time.monotonic()
        self._warmup: asyncio.Task[dict[str, Any]] | None = None
        self._generation = 0
        self._last_degraded: dict[str, Any] | None = None
        self._recovery_generation = 0
        self._recovery_correlation_id: str | None = None
        self._recovery_state = "idle"
        self._last_observation: dict[str, Any] | None = None
        self._lock = asyncio.Lock()

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self._state.value,
            "live": True,
            "ready": self._state is RuntimeState.READY,
            "readiness": self._state is RuntimeState.READY,
            "user_available": self.admits_users(),

            "reason": self._reason,
            "changed_monotonic": self._changed_at,
            "warmup_running": bool(self._warmup and not self._warmup.done()),
            "generation": self._generation,
            "last_degraded": self._last_degraded,
            "last_observation": self._last_observation,
            "recovery": {"state": self._recovery_state, "generation": self._recovery_generation, "correlation_id": self._recovery_correlation_id},
        }

    def is_live(self) -> bool:
        return True

    def admits_users(self) -> bool:
        return self._state is RuntimeState.READY

    def observe_noncritical(self, reason: str, details: dict[str, Any] | None = None) -> None:
        self._last_observation = {"timestamp_unix": time.time(), "reason": reason, "details": details or {}, "generation": self._generation}

    def _record_degraded(self, reason: str, details: dict[str, Any] | None = None) -> None:
        """Record every DEGRADED transition without clearing prior diagnostics."""
        self._state = RuntimeState.DEGRADED
        self._reason = reason
        self._last_degraded = {
            "timestamp_unix": time.time(),
            "at_monotonic": time.monotonic(),
            "reason": reason,
            "details": details or {},
            "generation": self._generation,
        }

    async def begin_recovery(self, reason: str, correlation_id: str) -> int:
        async with self._lock:
            self._generation += 1
            self._recovery_generation = self._generation
            self._recovery_correlation_id = correlation_id
            self._recovery_state = "recovering"
            self._state = RuntimeState.WARMING_UP
            self._reason = f"recovery:{reason}"
            self._changed_at = time.monotonic()
            return self._recovery_generation

    def finish_recovery(self, generation: int, result: dict[str, Any]) -> bool:
        if generation != self._generation or generation != self._recovery_generation:
            return False
        self._recovery_state = "ready" if result.get("ok") else "degraded"
        if result.get("ok") and result.get("e2e", {}).get("ok") is True and not result.get("mock"):
            self._state = RuntimeState.READY
            self._reason = "recovery_complete"
        else:
            reason = str(result.get("reason") or "recovery_failed")
            self._record_degraded(reason, {"source": "recovery", "correlation_id": self._recovery_correlation_id, "recovery": result.get("diagnostic", {})})
        self._changed_at = time.monotonic()
        return True

    async def start(self, warmup: Warmup) -> None:
        async with self._lock:
            if self._warmup and not self._warmup.done():
                return
            self._state = RuntimeState.WARMING_UP
            self._reason = "warmup_in_progress"
            self._changed_at = time.monotonic()
            self._generation += 1
            self._warmup = asyncio.create_task(self._run(warmup, self._generation), name="mishka-startup-warmup")

    async def run_now(self, warmup: Warmup) -> dict[str, Any]:
        await self.start(warmup)
        task = self._warmup
        if task is None:
            raise RuntimeError("startup warmup task was not created")
        return await asyncio.shield(task)

    async def _run(self, warmup: Warmup, generation: int) -> dict[str, Any]:
        try:
            result = await warmup()
        except asyncio.CancelledError:
            self._record_degraded("warmup_cancelled", {"source": "warmup", "cancelled": True})
            self._changed_at = time.monotonic()
            raise
        except Exception as exc:
            result = {"ok": False, "reason": f"warmup_exception:{type(exc).__name__}"}
        if generation != self._generation:
            return result
        if result.get("ok") and result.get("e2e", {}).get("ok") is True and not result.get("mock"):
            self._state = RuntimeState.READY
            self._reason = "warmup_complete"
        else:
            reason = str(result.get("reason") or "full_e2e_warmup_required")
            self._record_degraded(reason, {
                "source": "warmup",
                "e2e_ok": bool(result.get("e2e", {}).get("ok")) if isinstance(result.get("e2e"), dict) else False,
                "result_ok": bool(result.get("ok")),
            })
        self._changed_at = time.monotonic()
        return result

    async def stop(self) -> None:
        """Cancel lifecycle-owned warmup work before its dependencies close."""
        task = self._warmup
        self._generation += 1
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def dependency_failed(self, dependency: str, reason: str) -> None:
        self._generation += 1
        if self._warmup and not self._warmup.done():
            self._warmup.cancel()
        self._record_degraded(f"{dependency}:{reason}", {"source": "dependency", "dependency": dependency})
        self._changed_at = time.monotonic()

    def degrade(self, reason: str, details: dict[str, Any] | None = None) -> None:
        self._generation += 1
        self._record_degraded(reason, details or {"source": "watchdog"})
        self._changed_at = time.monotonic()
