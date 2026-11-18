from __future__ import annotations

import asyncio
import time
from enum import Enum
from typing import Any, Awaitable, Callable


class RuntimeState(str, Enum):
    BOOTING = "BOOTING"
    WARMING_UP = "WARMING_UP"
    READY = "READY"
    STOPPING = "STOPPING"
    DEGRADED = "DEGRADED"


Warmup = Callable[[], Awaitable[dict[str, Any]]]


class RuntimeBusyError(RuntimeError):
    pass


class StartupLifecycle:
    """Single lifecycle owner shared by health, readiness and WebSocket gates."""

    def __init__(self, *, admission_guard: Callable[[], bool] | None = None,
                 maintenance_acquire: Callable[[], Callable[[], None]] | None = None) -> None:
        self._admission_guard = admission_guard
        self._stopping = False
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
        self._maintenance_acquire = maintenance_acquire
        self._maintenance_token: object | None = None
        self._maintenance_release: Callable[[], None] | None = None
        self._maintenance_task: asyncio.Task | None = None
        self._recovery_token: object | None = None
        self._warmup_generation = 0

    def _claim_maintenance(self):
        if self._maintenance_token is not None:
            raise RuntimeBusyError("runtime_busy")
        release = self._maintenance_acquire() if self._maintenance_acquire is not None else None
        token = object()
        self._maintenance_token, self._maintenance_release = token, release
        return token

    def _own_maintenance(self, token, task, *, warmup_generation=None):
        self._maintenance_task = task
        # Also release a task cancelled before its coroutine's first step.
        def finished(done):
            if (done.cancelled() and warmup_generation == self._generation
                    and not self._stopping and self._state is RuntimeState.WARMING_UP):
                self._record_degraded("warmup_cancelled", {"source": "warmup", "cancelled": True})
                self._changed_at = time.monotonic()
            self._release_maintenance(token)
        task.add_done_callback(finished)

    def _release_maintenance(self, token):
        if token is None or self._maintenance_token is not token:
            return
        release = self._maintenance_release
        self._maintenance_token = self._maintenance_release = self._maintenance_task = None
        if release is not None:
            release()

    def _cancel_maintenance(self):
        for task in {self._warmup, self._maintenance_task} - {None}:
            if not task.done() and not task.cancelling():
                task.cancel()

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self._state.value,
            "live": True,
            "ready": self.admits_users(),
            "readiness": self.admits_users(),
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
        if self._stopping or self._state is not RuntimeState.READY:
            return False
        try:
            return self._admission_guard is None or bool(self._admission_guard())
        except Exception:
            # Missing/unavailable live diagnostics must never admit a user.
            return False

    def observe_noncritical(self, reason: str, details: dict[str, Any] | None = None) -> None:
        self._last_observation = {"timestamp_unix": time.time(), "reason": reason, "details": details or {}, "generation": self._generation}

    def _record_degraded(self, reason: str, details: dict[str, Any] | None = None) -> None:
        """Record every DEGRADED transition without clearing prior diagnostics."""
        if self._stopping:
            return
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
            if self._stopping:
                return -1
            token = self._claim_maintenance()
            self._generation += 1
            self._recovery_generation = self._generation
            self._recovery_correlation_id = correlation_id
            self._recovery_state = "recovering"
            self._state = RuntimeState.WARMING_UP
            self._reason = f"recovery:{reason}"
            self._changed_at = time.monotonic()
            self._recovery_token = token
            self._own_maintenance(token, asyncio.current_task())
            return self._recovery_generation

    def finish_recovery(self, generation: int, result: dict[str, Any]) -> bool:
        try:
            return self._finish_recovery(generation, result)
        finally:
            if generation == self._recovery_generation:
                self._release_maintenance(self._recovery_token)

    def _finish_recovery(self, generation: int, result: dict[str, Any]) -> bool:
        if self._stopping or generation != self._generation or generation != self._recovery_generation:
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
            if self._stopping:
                raise RuntimeError("lifecycle_stopping")
            if self._warmup and not self._warmup.done():
                if self._warmup_generation == self._generation:
                    return
                raise RuntimeBusyError("runtime_busy")
            token = self._claim_maintenance()
            try:
                self._state = RuntimeState.WARMING_UP
                self._reason = "warmup_in_progress"
                self._changed_at = time.monotonic()
                self._generation += 1
                self._warmup_generation = self._generation
                self._warmup = asyncio.create_task(self._run(warmup, self._generation, token), name="mishka-startup-warmup")
                self._own_maintenance(token, self._warmup, warmup_generation=self._generation)
            except BaseException:
                self._release_maintenance(token)
                raise

    async def run_now(self, warmup: Warmup) -> dict[str, Any]:
        await self.start(warmup)
        task = self._warmup
        if task is None:
            raise RuntimeError("startup warmup task was not created")
        return await asyncio.shield(task)

    async def _run(self, warmup: Warmup, generation: int, token) -> dict[str, Any]:
        try:
            return await self._run_warmup(warmup, generation)
        finally:
            self._release_maintenance(token)

    async def _run_warmup(self, warmup: Warmup, generation: int) -> dict[str, Any]:
        try:
            result = await warmup()
        except asyncio.CancelledError:
            # A superseded warmup no longer owns lifecycle state or diagnostics.
            if not self._stopping and generation == self._generation:
                self._record_degraded("warmup_cancelled", {"source": "warmup", "cancelled": True})
                self._changed_at = time.monotonic()
            raise
        except Exception as exc:
            result = {"ok": False, "reason": f"warmup_exception:{type(exc).__name__}"}
        if self._stopping or generation != self._generation:
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

    def begin_shutdown(self) -> None:
        """Close ingress synchronously before any cleanup operation can yield."""
        self._stopping = True
        self._state = RuntimeState.STOPPING
        self._reason = "shutdown_in_progress"
        self._changed_at = time.monotonic()
        self._generation += 1
        self._cancel_maintenance()

    async def stop(self) -> None:
        """Cancel lifecycle-owned warmup work before its dependencies close."""
        tasks = {self._warmup, self._maintenance_task} - {None, asyncio.current_task()}
        self.begin_shutdown()
        self._generation += 1
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

    def dependency_failed(self, dependency: str, reason: str) -> None:
        self._generation += 1
        self._cancel_maintenance()
        self._record_degraded(f"{dependency}:{reason}", {"source": "dependency", "dependency": dependency})
        self._changed_at = time.monotonic()

    def degrade(self, reason: str, details: dict[str, Any] | None = None) -> None:
        self._generation += 1
        self._record_degraded(reason, details or {"source": "watchdog"})
        self._changed_at = time.monotonic()
