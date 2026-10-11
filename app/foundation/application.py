from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import time
import uuid
from collections import deque
from typing import Any, Callable
from weakref import WeakSet

from .domain import AudioOutput, DisplayController, Event, EventPublisher, IntentClassifier, LEDController, Lifecycle, ProfileProvider, ScenarioRepository, SessionRecord, SessionRepository
from .led_contract import BUTTON_LED_COLORS, fixed_button_colors
from .schedule_weather import ScheduleWeatherProviderError, validate_report
from .adapters import ProfileProviderError
from ..scenarios.langgraph_runtime import LangGraphRuntime
from .config import settings_from_compat_env

logger = logging.getLogger("mishka.application")


class ScenarioApplication:
    """Use-case layer. It depends only on domain ports, never FastAPI/files/UART/LLM."""
    AUDIO_RECOVERABLE_FAILURES = frozenset({
        "vad_failure",
        "stt_failure",
        "stt_low_confidence",
        "audio_too_long",
    })

    def __init__(self, sessions: SessionRepository, scenarios: ScenarioRepository, profiles: ProfileProvider, classifier: IntentClassifier, audio: AudioOutput, led: LEDController, publisher: EventPublisher, now=time.time, installation_id="default", threshold=0.6, bounded_s2s=None, history=None, max_session_seconds=1200, idle_timeout_seconds: float | None = None, display: DisplayController | None = None, schedule_weather: Any = None, allow_quick_replies: bool | None = None, settings: Any = None):
        self.sessions, self.scenarios, self.profiles, self.classifier, self.audio, self.led, self.publisher = sessions, scenarios, profiles, classifier, audio, led, publisher
        self.now, self.installation_id, self.threshold = now, installation_id, threshold
        self.settings = settings if settings is not None else settings_from_compat_env()
        settings = self.settings
        profile = str(getattr(settings, "profile", None) or "mock").strip().lower()
        self.display = display
        self.schedule_weather = schedule_weather
        self.allow_quick_replies = (profile == "mock") if allow_quick_replies is None else bool(allow_quick_replies)
        self.bounded_s2s = bounded_s2s
        self.history = history
        self.max_session_seconds = max_session_seconds
        self.idle_timeout_seconds = None if idle_timeout_seconds is None else float(idle_timeout_seconds)
        if self.idle_timeout_seconds is not None and self.idle_timeout_seconds <= 0:
            raise ValueError("idle_timeout_seconds must be positive")
        self._idle_monitor_task: asyncio.Task[Any] | None = None
        self._idle_expiration_count = 0
        self._last_idle_cleanup: dict[str, Any] | None = None
        self.conversation_contexts: dict[str, Any] = {}
        self.s2s_audio: Any = None
        # Stand integrations install an async callback here so that a
        # non-blocking browser audio turn can publish its completed result
        # without making the WebSocket receive loop wait for STT/LLM/TTS.
        self.audio_update_hook: Any = None
        # Browser PCM turns complete after the transport request has returned.
        # Keep those tasks owned by the application so reset/handset-down can
        # cancel and await them instead of leaving stale completions alive.
        self._audio_completions: dict[str, set[asyncio.Task[Any]]] = {}
        self._audio_completion_started_at: dict[str, float] = {}
        self._audio_completion_diagnostics: deque[dict[str, Any]] = deque(maxlen=100)
        self._audio_completion_observed: WeakSet[asyncio.Task[Any]] = WeakSet()
        self._audio_hook_counters: dict[str, int] = {
            "invocations": 0, "successes": 0, "failures": 0,
            "cancelled": 0, "mandatory_failures": 0, "duplicate_callbacks": 0,
        }
        # The stand's browser notification is observability-only.  A future
        # mandatory state/playback hook can opt in explicitly without
        # changing the detached completion ownership contract.
        self.audio_update_hook_mandatory = False
        self._last_audio_cleanup: dict[str, Any] | None = None
        self.audio_cleanup_timeout_seconds = max(0.01, float(getattr(settings, "audio_cleanup_timeout_s", 1.0)))
        self._session_epochs: dict[str, int] = {}
        self._last_audio_ack_at: dict[str, float] = {}
        self.runtime_status: dict[str, Any] = {}
        self.runtime_manager: Any = None
        self.metrics: Any = None
        self.watchdog_interval_seconds = float(getattr(settings, "watchdog_interval_s", 5.0))
        self.watchdog_stale_after_seconds = float(getattr(settings, "watchdog_stale_after_s", 30.0))
        self.watchdog_cooldown_seconds = float(getattr(settings, "watchdog_cooldown_s", 30.0))
        self._watchdog_task: asyncio.Task[Any] | None = None
        self._watchdog_lifecycle: Any = None
        self._watchdog_runtime_manager: Any = None
        self._watchdog_cooldown_until: dict[tuple[str, str | None], float] = {}
        self.history_repository: Any = history
        scenario_documents: dict[str, dict[str, Any]] = {}
        for scenario_id in ("fixture_core", "dev_core"):
            try:
                document = scenarios.get(scenario_id)
            except Exception as exc:
                if profile != "mock":
                    raise RuntimeError(f"production scenario document unavailable: {scenario_id}") from exc
                document = None
            if isinstance(document, dict):
                scenario_documents[scenario_id] = document
        self.langgraph = LangGraphRuntime(
            scenario_documents,
            enabled=str(getattr(settings, "scenario_engine", "legacy")).lower() not in {"legacy", "off", "0"},
            strict=(
                profile != "mock"
                and bool(getattr(settings, "langgraph_strict", True))
            ),
        )

    def _iter_sessions(self) -> tuple[SessionRecord, ...]:
        iterator = getattr(self.sessions, "iter_sessions", None)
        if callable(iterator):
            return tuple(iterator())
        items = getattr(self.sessions, "items", {})
        return tuple(items.values()) if isinstance(items, dict) else ()

    def _session_has_active_work(self, session_id: str) -> bool:
        for owner in (self.bounded_s2s, self.s2s_audio):
            if owner is None:
                continue
            active = getattr(owner, "is_active", None)
            if callable(active) and active(session_id):
                return True
            for registry_name in ("pending_tasks", "active_tasks"):
                task = getattr(owner, registry_name, {}).get(session_id)
                if task is not None and not task.done():
                    return True
        return bool(self._audio_completions.get(session_id))

    async def _cleanup_session_resources(self, session_id: str) -> dict[str, Any]:
        """Idempotently release all application-owned resources for a session."""
        completion_cleanup = await self._cancel_audio_completions(session_id, operation="session_cleanup")
        if self.s2s_audio is not None:
            await self.s2s_audio.cancel_session(session_id)
        elif self.bounded_s2s is not None:
            await self.bounded_s2s.cancel_session(session_id)
        self.conversation_contexts.pop(session_id, None)
        self._last_audio_ack_at.pop(session_id, None)
        self._session_epochs.pop(session_id, None)
        snapshot = self.resource_snapshot()
        snapshot["session_id"] = session_id
        snapshot["audio_completion_cleanup"] = completion_cleanup
        return snapshot

    async def expire_idle_sessions(self, now: float | None = None) -> list[dict[str, Any]]:
        """End abandoned sessions and drain their RAM/audio ownership."""
        if self.idle_timeout_seconds is None:
            return []
        current_time = self.now() if now is None else float(now)
        expired: list[dict[str, Any]] = []
        for session in self._iter_sessions():
            if session.state in {Lifecycle.IDLE, Lifecycle.ENDED} or not session.started_at:
                continue
            last_activity = session.last_activity_at or session.started_at
            if current_time - last_activity < self.idle_timeout_seconds:
                continue
            if self._session_has_active_work(session.session_id):
                continue
            await self._cleanup_session_resources(session.session_id)
            outputs = self._end(session, "idle_timeout", session.last_sequence)
            self.sessions.save(session)
            self._idle_expiration_count += 1
            cleanup = self.resource_snapshot()
            cleanup["session_id"] = session.session_id
            report = {"session_id": session.session_id, "reason": "idle_timeout",
                      "idle_seconds": round(max(0.0, current_time - last_activity), 3),
                      "cleanup": cleanup, "outputs": outputs}
            self._last_idle_cleanup = report
            expired.append(report)
        return expired

    async def start_idle_monitor(self) -> None:
        if self.idle_timeout_seconds is None or self._idle_monitor_task is not None:
            return
        interval = min(30.0, max(0.5, self.idle_timeout_seconds / 4.0))

        async def monitor() -> None:
            while True:
                try:
                    await self.expire_idle_sessions()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.runtime_status["idle_monitor_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
                await asyncio.sleep(interval)

        self._idle_monitor_task = asyncio.create_task(monitor(), name="mishka-idle-session-monitor")

    async def stop_idle_monitor(self) -> None:
        task, self._idle_monitor_task = self._idle_monitor_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _watchdog_engine(self) -> Any:
        manager = self._watchdog_runtime_manager or self.runtime_manager
        return getattr(manager, "engine", None) or self.bounded_s2s

    def _watchdog_record(self, event: dict[str, Any]) -> None:
        self.runtime_status["last_watchdog_event"] = dict(event)
        metrics = self.metrics or getattr(self._watchdog_engine(), "metrics", None)
        recorder = getattr(metrics, "record_watchdog_event", None)
        if callable(recorder):
            recorder(event)

    async def start_watchdog(self, lifecycle: Any = None, runtime_manager: Any = None) -> bool:
        """Start one internal watchdog loop; repeated calls are idempotent."""
        if self._watchdog_task is not None and not self._watchdog_task.done():
            return False
        self._watchdog_lifecycle = lifecycle
        self._watchdog_runtime_manager = runtime_manager or self.runtime_manager
        self._watchdog_task = asyncio.create_task(self._watchdog_loop(), name="mishka-stale-watchdog")
        return True

    async def stop_watchdog(self) -> None:
        task, self._watchdog_task = self._watchdog_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._watchdog_lifecycle = None
        self._watchdog_runtime_manager = None
        self._watchdog_cooldown_until.clear()

    @staticmethod
    def _watchdog_session_id(condition_key: str) -> str | None:
        return condition_key.split(":", 1)[1] if ":" in condition_key else condition_key

    async def watchdog_check_once(self) -> dict[str, Any]:
        """Run one bounded check and safely clean confirmed stale ownership."""
        now = time.monotonic()
        lifecycle = self._watchdog_lifecycle
        engine = self._watchdog_engine()
        pipeline = getattr(self._watchdog_runtime_manager or self.runtime_manager, "audio_pipeline", None) or self.s2s_audio
        source_metrics = getattr(engine, "metrics", None)
        metrics = self.metrics or source_metrics
        tasks = dict(getattr(engine, "task_started_at", {}))
        tasks.update({f"pending:{key}": value for key, value in getattr(pipeline, "pending_started_at", {}).items()})
        tasks.update({f"completion:{key}": value for key, value in self._audio_completion_started_at.items()})
        playback = dict(getattr(source_metrics, "active_playback", {}))
        queue_generations: dict[str, tuple[Any, Any]] = {}
        current_tokens = getattr(getattr(engine, "cancel", None), "tokens", {})
        for session_id, queue in getattr(engine, "active_queues", {}).items():
            queue_generations[session_id] = (getattr(queue, "token", None), current_tokens.get(session_id))
        try:
            check = metrics.watchdog_check(
                now, tasks=tasks, playback=playback,
                max_age_s=self.watchdog_stale_after_seconds,
                queue_generations=queue_generations, record=False,
            ) if metrics is not None and hasattr(metrics, "watchdog_check") else {
                "stale_tasks": [], "stale_playback": [], "stale_queue_generations": [],
            }
        except Exception as exc:
            state = lifecycle.snapshot() if lifecycle is not None else {}
            event = {"at_monotonic": now, "condition": "watchdog_check_exception",
                     "session_id": None, "generation": state.get("generation"),
                     "action": "observe", "outcome": "check_failed",
                     "error": f"{type(exc).__name__}: {str(exc)[:160]}",
                     "lifecycle_state": state.get("state")}
            self._watchdog_record(event)
            if lifecycle is not None and hasattr(lifecycle, "observe_noncritical"):
                lifecycle.observe_noncritical("watchdog_check_failed", {"error": event["error"]})
            logger.warning("watchdog check failed; continuing without lifecycle degradation: %s", event["error"])
            return {"stale_tasks": [], "stale_playback": [], "stale_queue_generations": [], "error": event["error"]}
        conditions = (
            [("stale_task", key) for key in check.get("stale_tasks", [])]
            + [("stale_playback", key) for key in check.get("stale_playback", [])]
            + [("generation_mismatch", key) for key in check.get("stale_queue_generations", [])]
        )
        if not conditions:
            state = lifecycle.snapshot() if lifecycle is not None else {}
            self._watchdog_record({"at_monotonic": now, "condition": "clean",
                                   "session_id": None, "generation": state.get("generation"),
                                   "action": "none", "outcome": "ok",
                                   "lifecycle_state": state.get("state")})
            return check
        for condition, key in conditions:
            session_id = self._watchdog_session_id(str(key))
            cooldown_key = (condition, session_id)
            if now < self._watchdog_cooldown_until.get(cooldown_key, 0.0):
                continue
            self._watchdog_cooldown_until[cooldown_key] = now + self.watchdog_cooldown_seconds
            action, outcome, error = "none", "detected", None
            try:
                if session_id:
                    await self._cleanup_session_resources(session_id)
                    action, outcome = "session_cleanup", "cleaned"
                elif lifecycle is not None:
                    lifecycle.observe_noncritical("watchdog_" + condition, {"key": key})
                    action, outcome = "observe", "noncritical"
                    logger.warning("watchdog observed noncritical condition=%s key=%s", condition, key)
            except Exception as exc:
                error = f"{type(exc).__name__}: {str(exc)[:160]}"
                action, outcome = "observe", "cleanup_failed"
                if lifecycle is not None:
                    lifecycle.observe_noncritical("watchdog_cleanup_failed", {"condition": condition, "session_id": session_id, "error": error})
                logger.warning("watchdog cleanup failed; continuing without degrading runtime: %s", error)
            state = lifecycle.snapshot() if lifecycle is not None else {}
            event = {"at_monotonic": now, "condition": condition, "session_id": session_id,
                     "generation": state.get("generation"), "action": action,
                     "outcome": outcome, "lifecycle_state": state.get("state")}
            if error:
                event["error"] = error
            self._watchdog_record(event)
        return check

    async def _watchdog_loop(self) -> None:
        while True:
            try:
                await self.watchdog_check_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                now = time.monotonic()
                state = self._watchdog_lifecycle.snapshot() if self._watchdog_lifecycle is not None else {}
                self._watchdog_record({"at_monotonic": now, "condition": "watchdog_loop_exception",
                                       "session_id": None, "generation": state.get("generation"),
                                       "action": "observe", "outcome": "loop_survived",
                                       "lifecycle_state": state.get("state"),
                                       "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
            await asyncio.sleep(self.watchdog_interval_seconds)

    def resource_snapshot(self) -> dict[str, Any]:
        base = dict(self.bounded_s2s.resource_snapshot()) if self.bounded_s2s is not None and hasattr(self.bounded_s2s, "resource_snapshot") else {}
        active_sessions = sum(1 for session in self._iter_sessions() if session.state not in {Lifecycle.IDLE, Lifecycle.ENDED})
        context_chars = sum(sum(len(user) + len(response) for user, response in getattr(context, "history", ())) for context in self.conversation_contexts.values())
        base.update({"application_active_sessions": active_sessions, "session_contexts": len(self.conversation_contexts),
                     "history_chars": context_chars, "idle_expirations": self._idle_expiration_count,
                     "last_idle_cleanup": self._last_idle_cleanup,
                     "generation_registry": len(getattr(self.s2s_audio, "generations", {})) if self.s2s_audio is not None else 0,
                     "audio_completion_registry": sum(len(tasks) for tasks in self._audio_completions.values()),
                     "audio_completion_sessions": len(self._audio_completions),
                     "audio_completion_diagnostics": list(self._audio_completion_diagnostics),
                     "audio_hook_counters": dict(self._audio_hook_counters),
                     "last_audio_cleanup": self._last_audio_cleanup})
        return base

    def _fixture_button_targets(self) -> dict[int, str]:
        """Read the physical button contract from the canonical fixture content."""
        scenario = self.scenarios.get("fixture_core") or {}
        for node in scenario.get("nodes", []):
            if isinstance(node, dict) and node.get("id") == "fixture_choose":
                transitions = node.get("transitions", {})
                return {
                    int(key.removeprefix("button_")): str(target)
                    for key, target in transitions.items()
                    if isinstance(key, str) and key.startswith("button_")
                    and key.removeprefix("button_").isdigit()
                    and 1 <= int(key.removeprefix("button_")) <= 7
                    and isinstance(target, str) and target
                }
        return {}

    def _prepare_visual_guest(self, session: SessionRecord) -> None:
        """Start an anonymous visual session from a pre-auth physical button."""
        if session.current_node or session.state in {Lifecycle.ACTIVE, Lifecycle.FALLBACK, Lifecycle.ENDED}:
            return
        scenario = self.scenarios.get("fixture_core")
        session.anonymous = True
        session.visual_only = True
        session.state = Lifecycle.ACTIVE
        session.scenario_id = "fixture_core"
        session.scenario_version = (scenario or {}).get("metadata", {}).get("version")
        session.current_node = str((scenario or {}).get("start", "fixture_choose"))
        session.started_at = self.now()
        session.last_activity_at = session.started_at
        session.last_sequence = -1
        self.publisher.publish(Event("VisualGuestStarted", session.session_id, session.last_sequence, self.now(), "application", session.correlation_id))

    async def receive_async(self, event: Event, transient_text: str | None = None) -> dict[str, Any]:
        await self.expire_idle_sessions()
        session = self.sessions.get(event.session_id)
        # Pre-auth physical buttons start a visual-only guest route. Authenticated
        # sessions must continue through the normal scenario engine.
        if event.event_type == "ButtonPressed" and str(event.payload.get("button_id")) in {"1", "2", "3", "4", "5", "7"}:
            if session is None:
                session = SessionRecord(event.session_id, self.installation_id, event.correlation_id or str(uuid.uuid4()), started_at=self.now(), last_activity_at=self.now())
            # Persist the canonical record before handing the event to receive().
            self.sessions.save(session)
            pre_auth = (
                session.rfid_tag is None
                and session.current_node is None
                and session.state == Lifecycle.WAITING_RFID
            )
            visual_choice = session.visual_only and session.current_node == "fixture_choose"
            if pre_auth or visual_choice:
                session.anonymous = True
                session.visual_only = True
                session.state = Lifecycle.ACTIVE
                session.scenario_id = "fixture_core"
                session.current_node = "fixture_choose"
                session.last_sequence = event.sequence - 1
                button_id = int(event.payload.get("button_id"))
                targets = self._fixture_button_targets()
                session.current_node = targets[button_id]
                self.publisher.publish(Event("VisualGuestStarted", session.session_id, session.last_sequence, self.now(), "application", session.correlation_id))
                self.sessions.save(session)
                return self._result(session, [{"action": "visual_guest", "button_id": button_id, "node": session.current_node, "audio": False}], "visual_guest")
        if (event.event_type == "ButtonPressed" and session is not None
                and session.state not in {Lifecycle.ACTIVE, Lifecycle.FALLBACK, Lifecycle.ENDED}
                and session.rfid_tag is None
                and not session.current_node
                and str(event.payload.get("button_id")) in {"1", "2", "3", "4", "5", "7"}):
            self._prepare_visual_guest(session)
        if (event.event_type == "ButtonPressed" and session is not None and self.schedule_weather is not None
                and session.scenario_id == "fixture_core"
                and str(event.payload.get("button_id")) == "7"
                and self.bounded_s2s is not None
                and not session.game_id
                and self._node(self.scenarios.get(session.scenario_id or "fixture_core"), session.current_node).get("type") != "story"):
            await self._cancel_audio_completions(event.session_id, operation="schedule_weather")
            if self.s2s_audio is not None:
                await self.s2s_audio.cancel_session(event.session_id)
            await self.bounded_s2s.cancel_session(event.session_id)
            result = self.receive(event, transient_text)
            profile = self.profiles.resolve(session.rfid_tag or "") or {}
            schedule_token = session.cancellation_token
            try:
                report = validate_report(await self.schedule_weather.build(profile, session.rfid_tag or ""), allow_demo=getattr(getattr(self, "settings", None), "profile", "production") == "mock")
            except ScheduleWeatherProviderError as exc:
                result["reason"] = "schedule_weather_provider_failure"
                result.setdefault("outputs", []).append({"action": "schedule_weather_error", "reason": str(exc)})
                self.publisher.publish(Event("SafetyFallbackTriggered", session.session_id, event.sequence, self.now(), "schedule_weather", {"reason": str(exc)}, session.correlation_id))
                self.sessions.save(session)
                return result
            current = self.sessions.get(event.session_id)
            if current is None or current.state == Lifecycle.ENDED or current.cancellation_token != schedule_token:
                result["reason"] = "schedule_weather_cancelled"
                result.setdefault("outputs", []).append({"action": "schedule_weather_error", "reason": "session_ended_or_cancelled"})
                self.sessions.save(current or session)
                return result
            # Deterministic application text uses the same bounded TTS/PCM
            # route as conversational output.
            speak_text = getattr(self.bounded_s2s, "speak_text", None)
            if callable(speak_text):
                spoken_result = await speak_text(
                    session.session_id, report["spoken_text"]
                )
                if spoken_result.fallback_reason:
                    result["reason"] = "schedule_weather_audio_failure"
                    result.setdefault("outputs", []).append({
                        "action": "schedule_weather_error",
                        "reason": spoken_result.fallback_reason,
                    })
                    self.sessions.save(session)
                    return result
            result.setdefault("outputs", []).extend([
                {"action": "schedule_weather", "schedule": report["schedule"], "weather": report["weather"], "source": report["source"], "spoken_text": report["spoken_text"]},
                {"action": "audio_text", "response_id": "schedule_weather", "text": report["spoken_text"]},
                {"action": "led_pattern", "pattern": "schedule_weather", "color": "blue", "duration_ms": 500},
            ])
            if self.history is not None and session.child_id and not session.anonymous:
                item_id = str(session.rfid_tag or session.child_id)
                if not self.history.has("schedule_weather", session.child_id, item_id):
                    self.history.append("schedule_weather", session.child_id, item_id, report)
            self.sessions.save(session)
            return result

        if event.event_type == "ButtonPressed" and session is not None and self.bounded_s2s is not None:
            # Cancel before the next scenario response is emitted. Scheduling
            # a late stop from the synchronous domain handler could otherwise
            # mute the *new* story/game prompt in the browser.
            if not (session.scenario_id == "dev_core" and session.current_node in {"dev_welcome", "dev_dialogue"}):
                await self._cancel_audio_completions(event.session_id, operation="barge_in")
                if self.s2s_audio is not None:
                    await self.s2s_audio.cancel_session(event.session_id)
                await self.bounded_s2s.cancel_session(event.session_id)
            return self._async_sync_result(event, session, transient_text)
        if event.event_type == "HandsetPlaced" and self.bounded_s2s is not None:
            await self._cancel_audio_completions(event.session_id, operation="handset_down")
            await self.bounded_s2s.cancel_session(event.session_id)
            if self.s2s_audio is not None:
                await self.s2s_audio.cancel_session(event.session_id)
            self.conversation_contexts.pop(event.session_id, None)
            return self._async_sync_result(event, session, transient_text)
        if event.event_type != "SpeechReceived" or session is None or self.bounded_s2s is None:
            return self._async_sync_result(event, session, transient_text)
        if session.started_at and self.now()-session.started_at>=self.max_session_seconds:
            outputs=self._end(session,"session_duration_limit",event.sequence); self.sessions.save(session); return self._result(session,outputs,"session_duration_limit")
        # Direct pre-auth visual path: physical play buttons work without RFID/handset.
        if (
            event.event_type == "ButtonPressed"
            and str(event.payload.get("button_id")) in {"1", "2", "3", "4", "5", "7"}
            and (
                (session.state not in {Lifecycle.ACTIVE, Lifecycle.FALLBACK, Lifecycle.ENDED} and not session.current_node)
                or (session.visual_only and session.current_node == "fixture_choose")
            )
        ):
            self._prepare_visual_guest(session)
            button_id = int(event.payload.get("button_id"))
            scenario = self.scenarios.get("fixture_core") or {}
            start_node = self._node(scenario, session.current_node) if scenario else {}
            static_targets = self._fixture_button_targets()
            target = (start_node.get("transitions") or {}).get(f"button_{button_id}") or static_targets[button_id]
            session.current_node = str(target)
            self.publisher.publish(Event("ButtonPressed", {"button_id": button_id, "visual_only": True}, session.session_id, session.correlation_id))
            return self._result(session, [{"action": "visual_guest", "button_id": button_id, "node": session.current_node, "audio": False}], "visual_guest")

        # Text may arrive before RFID/handset initialization. Do not ask the
        # scenario repository to resolve a null node; let the synchronous
        # lifecycle handler return a structured, harmless ignored-input result.
        if not session.current_node or not session.scenario_id:
            return self._async_sync_result(event, session, transient_text)
        scenario = self.scenarios.get(session.scenario_id or "fixture_core")
        if session.scenario_id == "dev_core" and session.current_node == "dev_welcome":
            session.current_node = "dev_dialogue"
            self.sessions.save(session)
        node = self._node(scenario, session.current_node)
        if node["type"] != "bounded_free_conversation":
            return self._async_sync_result(event, session, transient_text)
        if event.sequence <= session.last_sequence:
            return self._result(session, [], "idempotent_duplicate")
        if session.state == Lifecycle.ENDED:
            return self._result(session, [], "late_event_rejected")
        if session.started_at and self.now()-session.started_at>=self.max_session_seconds:
            outputs=self._end(session,"session_duration_limit",event.sequence); self.sessions.save(session); return self._result(session,outputs,"session_duration_limit")
        turn_epoch = self._session_epochs.get(event.session_id, 0) + 1
        self._session_epochs[event.session_id] = turn_epoch
        # The previous LLM task may already be complete while several seconds
        # of its PCM are still queued in the browser. Every new text turn must
        # stop browser playback as well as cancel any in-flight voice/LLM work.
        await self._cancel_audio_completions(event.session_id, operation="new_turn")
        if self.s2s_audio is not None:
            await self.s2s_audio.cancel_session(event.session_id)
        await self.bounded_s2s.cancel_session(event.session_id)
        session.last_sequence, session.last_activity_at = event.sequence, self.now(); self.publisher.publish(event)
        context = self._conversation_context(session, node)
        quick_response = self._quick_response_id(transient_text or "", context) if self.allow_quick_replies else None
        if quick_response:
            context.turns += 1
            outputs = self._apply_quick_response(session, quick_response)
            self.sessions.save(session)
            return self._result(session, outputs, "quick_reply")
        try:
            result = await self.bounded_s2s.run_text_turn(transient_text or "", context)
        except asyncio.CancelledError:
            # A newer turn/reset may cancel this HTTP handler while Ollama or
            # Silero is streaming. Return a bounded cancellation response
            # instead of leaking CancelledError as HTTP 500/No response.
            if self._session_epochs.get(event.session_id, 0) != turn_epoch:
                current = self.sessions.get(event.session_id)
                if current is None:
                    return {"session_id":event.session_id,"state":"RESET","node":None,
                            "reason":"cancelled_by_new_input","outputs":[],"correlation_id":session.correlation_id}
                return self._result(current, [], "cancelled_by_new_input")
            raise
        if result is None:
            # Cancellation can complete the worker without raising CancelledError.
            # Never let that race escape as Starlette 500 / No response.
            current = self.sessions.get(event.session_id) or session
            self.sessions.save(current)
            return self._result(current, [], "cancelled_no_response")
        outputs = self._apply_s2s_result(session, scenario, node, result)
        self.sessions.save(session)
        return {**self._result(session, outputs, "bounded_s2s"), "pending": False}

    def _ignored_audio_result(self, session: Any, sequence: int) -> dict[str, Any]:
        """Consume stale PCM without changing the scenario lifecycle.

        A browser upload is split into many frames.  The frame that ends a
        turn may advance the scenario while already-buffered tail frames are
        still in flight.  Those frames are input noise, not a lifecycle
        event: they must be acknowledged as ignored and advance the transport
        sequence, but must never call ``_fallback`` or ``_end``.
        """
        if sequence > session.last_sequence:
            session.last_sequence = sequence
            session.last_activity_at = self.now()
            self.sessions.save(session)
        return {**self._result(session, [], "audio_not_listening"),
                "audio_status": "ignored", "audio_flow": "stale_input",
                "barge_in": False, "pending": False}

    async def receive_audio_async(self, session_id: str, samples: tuple[float, ...], sequence: int, *, wait_for_turn: bool = True) -> dict[str, Any]:
        """Accept one RAM-only PCM chunk through the application boundary."""
        session = self.sessions.get(session_id)
        if session is None:
            return {"session_id": session_id, "state": Lifecycle.IDLE.value, "outputs": [], "reason": "audio_session_required", "audio_status": "ignored", "pending": False}
        if self.s2s_audio is None or self.bounded_s2s is None:
            return {**self._result(session, [], "audio_unavailable"), "audio_status": "ignored", "barge_in": False, "pending": False}
        if session.state == Lifecycle.ENDED or sequence <= session.last_sequence:
            return {**self._result(session, [], "late_or_duplicate_audio"), "pending": False}
        if not session.current_node or not session.scenario_id:
            return self._ignored_audio_result(session, sequence)
        scenario = self.scenarios.get(session.scenario_id or "fixture_core")
        node = self._node(scenario, session.current_node)
        if node["type"] != "bounded_free_conversation":
            # A WAV/PCM upload is chunked. The first chunk may complete the turn
            # and move the scenario before the tail of that same upload arrives.
            # Treat that tail as stale input instead of surfacing a transport
            # error to the tester.
            return self._ignored_audio_result(session, sequence)
        # Every accepted PCM frame is part of the event stream, including
        # frames while VAD is still collecting. Persisting the sequence here
        # makes duplicate UART/WebSocket delivery idempotent and keeps SQLite
        # session activity current during a long utterance.
        session.last_sequence, session.last_activity_at = sequence, self.now()
        context = self._conversation_context(session, node)
        audio_result = await self.s2s_audio.feed_audio(session_id, samples, context, wait_for_turn=wait_for_turn)
        barge_outputs=[]
        if audio_result.barge_in:
            self.publisher.publish(Event("BargeInDetected",session.session_id,sequence,self.now(),"uart_audio",{},session.correlation_id))
            barge_outputs=[{"action":"barge_in","status":"previous_output_cancelled"}]
        if audio_result.pending is not None:
            # VAD has completed a turn, but browser ingress must remain free
            # to receive the next utterance immediately.  Completion is
            # applied in a separate task and published through the stand hook.
            ack_enabled = bool(getattr(self.settings, "fast_audio_ack", False))
            ack_cooldown = float(getattr(self.settings, "fast_audio_ack_cooldown_s", 4.0))
            now_mono = time.monotonic()
            last_ack = self._last_audio_ack_at.get(session_id, 0.0)
            ack_allowed = ack_enabled and not audio_result.barge_in and now_mono - last_ack >= ack_cooldown
            if ack_allowed:
                self._last_audio_ack_at[session_id] = now_mono
                self.audio.play("ack_listening", None, session.cancellation_token)
                engine = getattr(getattr(self, "runtime_manager", None), "engine", None)
                if engine is not None:
                    engine.metrics.event("audio_ack_requested", response_id="ack_listening")
                barge_outputs.append({"action": "audio_ack", "response_id": "ack_listening"})
            completion = asyncio.create_task(
                self._complete_audio_turn(session_id, sequence, context, audio_result.pending,
                                          self._session_epochs.get(session_id, 0), session.correlation_id),
                name=f"audio-completion-{session_id}",
            )
            owned = self._audio_completions.setdefault(session_id, set())
            owned.add(completion)
            self._audio_completion_started_at[session_id] = time.monotonic()
            completion.add_done_callback(self._audio_completion_callback(session_id))
            self.sessions.save(session)
            return {**self._result(session, barge_outputs, "audio_processing"),
                    "audio_status": "processing", "pending": True,
                    "barge_in": audio_result.barge_in}
        if audio_result.turn is None:
            self.sessions.save(session)
            return {**self._result(session, barge_outputs, audio_result.ingress_status), "audio_status":audio_result.ingress_status,"barge_in":audio_result.barge_in}
        return self._apply_audio_turn_result(session, sequence, context, audio_result, barge_outputs, scenario, node)

    @staticmethod
    def _bounded_diagnostic_reason(value: Any) -> str | None:
        if value is None:
            return None
        text = " ".join(str(value).split())
        # Diagnostics must not become a second user-input or secret store.
        for marker in ("Bearer ", "token=", "api_key=", "password="):
            if marker.casefold() in text.casefold():
                text = text[:text.casefold().index(marker.casefold())] + "[redacted]"
                break
        return text[:160] or None

    def _record_audio_completion_diagnostic(self, event_name: str, *, session_id: str | None = None,
                                           operation: str | None = None, generation: Any = None,
                                           correlation_id: str | None = None, **details: Any) -> dict[str, Any]:
        # Keep the event schema stable and JSON-safe even when a test/double
        # supplies an exotic exception or payload object.
        diagnostic: dict[str, Any] = {
            "event": event_name, "event_type": event_name,
            "timestamp": time.time(), "session_id": session_id,
            "correlation_id": correlation_id, "generation": generation,
            "operation": operation,
        }
        for key, value in details.items():
            if value is None:
                continue
            if key in {"reason", "error", "detail"}:
                value = self._bounded_diagnostic_reason(value)
            elif key in {"exception_type", "exception_class"}:
                value = str(value)[:120]
                diagnostic["exception_class"] = value
                if key == "exception_type":
                    diagnostic["exception_type"] = value
                continue
            elif key in {"hook_name", "mandatory", "chunk_index", "token", "sequence", "operation"}:
                if key == "hook_name":
                    value = str(value)[:120]
                elif key == "mandatory":
                    value = bool(value)
                elif key in {"chunk_index", "token", "sequence"}:
                    try:
                        value = int(value)
                    except (TypeError, ValueError):
                        continue
            elif isinstance(value, (str, int, float, bool)):
                pass
            else:
                continue
            if value is not None:
                diagnostic[key] = value
        diagnostic = {key: value for key, value in diagnostic.items() if value is not None}
        self._audio_completion_diagnostics.append(diagnostic)
        self.runtime_status["last_audio_completion_diagnostic"] = diagnostic
        metrics = self.metrics
        inc = getattr(metrics, "inc", None)
        if callable(inc):
            try:
                inc("audio_completion_diagnostics")
                inc(f"audio_completion_{event_name}")
            except Exception as exc:
                # Diagnostics are best effort and must never make a done
                # callback itself raise another detached exception.
                self.runtime_status["last_audio_diagnostic_metrics_error"] = type(exc).__name__
        publish = getattr(metrics, "event", None)
        if callable(publish):
            try:
                publish(event_name, **diagnostic)
            except Exception as exc:
                self.runtime_status["last_audio_diagnostic_event_error"] = type(exc).__name__
        return diagnostic

    def _forget_audio_completion(self, session_id: str, task: asyncio.Task[Any]) -> None:
        owned = self._audio_completions.get(session_id)
        if owned is None:
            return
        owned.discard(task)
        if not owned:
            self._audio_completions.pop(session_id, None)
            self._audio_completion_started_at.pop(session_id, None)

    def _audio_completion_callback(self, session_id: str) -> Callable[[asyncio.Task[Any]], None]:
        def callback(task: asyncio.Task[Any]) -> None:
            if task in self._audio_completion_observed:
                self._audio_hook_counters["duplicate_callbacks"] += 1
                self._record_audio_completion_diagnostic(
                    "audio_completion_duplicate_callback", session_id=session_id,
                    operation="completion", mandatory=False,
                )
                return
            self._audio_completion_observed.add(task)
            if not task.cancelled():
                try:
                    error = task.exception()
                except asyncio.CancelledError:
                    error = None
                if error is not None:
                    self._record_audio_completion_diagnostic(
                        "audio_completion_task_error", session_id=session_id,
                        exception_type=type(error).__name__,
                        exception_class=type(error).__name__,
                        mandatory=False,
                    )
            self._forget_audio_completion(session_id, task)
        return callback

    async def _cancel_audio_completions(self, session_id: str, *, operation: str = "cleanup",
                                        timeout_s: float | None = None) -> dict[str, Any]:
        """Cancel and classify owned completion tasks without hiding failures."""
        tasks = tuple(self._audio_completions.get(session_id, set()))
        report: dict[str, Any] = {
            "session_id": session_id, "operation": operation, "task_count": len(tasks),
            "cancelled": 0, "completed": 0, "failed": 0, "timed_out": 0,
            "remaining": 0, "already_done": sum(task.done() for task in tasks),
            "cleanup_completed": True,
        }
        if not tasks:
            self._last_audio_cleanup = report
            return report
        for task in tasks:
            if not task.done():
                task.cancel()
        try:
            done, pending = await asyncio.wait(
                tasks, timeout=self.audio_cleanup_timeout_seconds if timeout_s is None else max(0.0, float(timeout_s))
            )
        except asyncio.CancelledError:
            report["cleanup_completed"] = False
            report["remaining"] = sum(not task.done() for task in tasks)
            self._last_audio_cleanup = report
            self._record_audio_completion_diagnostic(
                "audio_completion_cleanup_cancelled", session_id=session_id,
                operation=operation, task_count=len(tasks), remaining=report["remaining"],
                cleanup_completed=False,
            )
            raise
        for task in done:
            if task.cancelled():
                report["cancelled"] += 1
                continue
            try:
                error = task.exception()
            except asyncio.CancelledError:
                report["cancelled"] += 1
                continue
            if error is None:
                report["completed"] += 1
            else:
                report["failed"] += 1
                self._record_audio_completion_diagnostic(
                    "audio_completion_task_error", session_id=session_id,
                    operation=operation, exception_type=type(error).__name__,
                    exception_class=type(error).__name__, mandatory=False,
                )
        report["timed_out"] = len(pending)
        report["remaining"] = sum(not task.done() for task in tasks)
        report["cleanup_completed"] = report["remaining"] == 0
        owned = self._audio_completions.get(session_id)
        if owned is not None:
            owned.difference_update(done)
            if not owned:
                self._audio_completions.pop(session_id, None)
                self._audio_completion_started_at.pop(session_id, None)
        self._last_audio_cleanup = report
        self._record_audio_completion_diagnostic(
            "audio_completion_cleanup", session_id=session_id, operation=operation,
            task_count=report["task_count"], cancelled=report["cancelled"],
            completed=report["completed"], failed=report["failed"],
            timed_out=report["timed_out"], remaining=report["remaining"],
            already_done=report["already_done"], cleanup_completed=report["cleanup_completed"],
        )
        return report

    async def _complete_audio_turn(self, session_id: str, sequence: int, context: Any,
                                   pending: asyncio.Task[Any], epoch: int,
                                   correlation_id: str) -> None:
        try:
            audio_result = await pending
        except asyncio.CancelledError:
            self._record_audio_completion_diagnostic(
                "audio_completion_cancelled", session_id=session_id,
                generation=epoch, correlation_id=correlation_id,
                operation="completion", cleanup_completed=True,
            )
            return
        except Exception as exc:
            # The synchronous upload path already turns provider failures into
            # a bounded retry.  A detached browser task must have the same
            # safety behavior and must never become an unhandled task error.
            self._record_audio_completion_diagnostic(
                "audio_completion_error", session_id=session_id,
                generation=epoch, correlation_id=correlation_id,
                operation="completion", exception_type=type(exc).__name__,
                cleanup_completed=True,
            )
            self.runtime_status["last_audio_completion_error"] = {"session_id": session_id, "error": type(exc).__name__}
            return
        session = self.sessions.get(session_id)
        if (session is None or session.state == Lifecycle.ENDED or
                self._session_epochs.get(session_id, 0) != epoch or
                session.correlation_id != correlation_id):
            self._record_audio_completion_diagnostic(
                "audio_completion_stale_generation", session_id=session_id,
                generation=epoch, correlation_id=correlation_id,
                operation="completion", cleanup_completed=True,
            )
            return
        scenario = self.scenarios.get(session.scenario_id or "fixture_core")
        node = self._node(scenario, session.current_node) if session.current_node else None
        if node is None or node["type"] != "bounded_free_conversation":
            return
        payload = self._apply_audio_turn_result(session, sequence, context, audio_result, [], scenario, node)
        self.sessions.save(session)
        await self._invoke_audio_update_hook(
            session_id, payload, generation=epoch, correlation_id=correlation_id,
        )

    async def _invoke_audio_update_hook(self, session_id: str, payload: dict[str, Any], *,
                                        generation: Any = None,
                                        correlation_id: str | None = None) -> None:
        """Observe optional completion delivery without changing audio state.

        Playback/state transitions happen before this method and are
        mandatory.  ``audio_update_hook`` is an optional browser/telemetry
        notification.  Its error is therefore recorded and swallowed only at
        this explicit contract boundary; a future mandatory hook sets
        ``audio_update_hook_mandatory`` and the failure propagates to the
        owned completion task and cleanup report.
        """
        hook = self.audio_update_hook
        if hook is None:
            return
        self._audio_hook_counters["invocations"] += 1
        hook_name = getattr(hook, "__qualname__", getattr(hook, "__name__", type(hook).__name__))
        mandatory = bool(getattr(self, "audio_update_hook_mandatory", False))
        hook_name = str(hook_name)[:120]
        hook_details = {
            "hook_name": hook_name,
            "mandatory": mandatory,
            "chunk_index": payload.get("chunk_index"),
            "token": payload.get("token"),
            "sequence": payload.get("sequence"),
        }
        try:
            result = hook(session_id, payload)
            if inspect.isawaitable(result):
                await result
            self._audio_hook_counters["successes"] += 1
        except asyncio.CancelledError:
            self._audio_hook_counters["cancelled"] += 1
            self._record_audio_completion_diagnostic(
                "audio_update_hook_cancelled", session_id=session_id,
                generation=generation, correlation_id=correlation_id,
                operation="audio_update_hook", **hook_details,
            )
            raise
        except Exception as exc:
            self._audio_hook_counters["failures"] += 1
            if mandatory:
                self._audio_hook_counters["mandatory_failures"] += 1
            self._record_audio_completion_diagnostic(
                "audio_update_hook_error", session_id=session_id,
                generation=generation, correlation_id=correlation_id,
                operation="audio_update_hook", exception_type=type(exc).__name__,
                exception_class=type(exc).__name__, reason=str(exc),
                cleanup_completed=True, **hook_details,
            )
            if mandatory:
                raise RuntimeError("mandatory audio completion hook failed") from exc

    def _apply_audio_turn_result(self, session: Any, sequence: int, context: Any, audio_result: Any, barge_outputs: list[dict[str, Any]], scenario: Any, node: Any) -> dict[str, Any]:
        """Apply a completed STT/LLM result for both HTTP and WebSocket paths."""
        if audio_result.stt is not None:
            self.publisher.publish(Event("SpeechReceived", session.session_id, sequence, self.now(), "uart_audio", {"length":len(audio_result.stt.text)}, session.correlation_id))
            quick_response = self._quick_response_id(audio_result.stt.text, context) if self.allow_quick_replies else None
            if quick_response:
                context.turns += 1
                outputs = barge_outputs + self._apply_quick_response(session, quick_response)
                self.sessions.save(session)
                return {**self._result(session, outputs, "quick_reply_audio"), "audio_status":audio_result.ingress_status,
                        "stt_confidence":audio_result.stt.confidence, "barge_in":audio_result.barge_in}
        # Audio ingress failures are transport/recognition problems, not a
        # scenario decision.  Previously these were passed to
        # ``_apply_s2s_result`` like a safety or provider failure, which moved
        # an otherwise healthy conversation to ``*_fallback``.  The browser
        # then continued sending PCM to a non-listening node and every next
        # chunk was reported as ``audio_not_listening``.  Keep the node alive,
        # give the tester a bounded retry prompt, and let the next utterance
        # start a fresh VAD/STT turn.
        if audio_result.turn.fallback_reason in self.AUDIO_RECOVERABLE_FAILURES:
            retry_reason = audio_result.turn.fallback_reason
            self.audio.play("fallback_repeat", None, session.cancellation_token)
            outputs = barge_outputs + [
                {"action": "audio_retry", "reason": retry_reason},
                {"action": "audio", "response_id": "fallback_repeat"},
            ]
            self.sessions.save(session)
            return {**self._result(session, outputs, "audio_retry"),
                    "audio_status": audio_result.ingress_status,
                    "audio_error": retry_reason,
                    "stt_confidence": audio_result.stt.confidence if audio_result.stt else None,
                    "barge_in": audio_result.barge_in}
        outputs = barge_outputs+self._apply_s2s_result(session, scenario, node, audio_result.turn)
        self.sessions.save(session)
        return {**self._result(session, outputs, "bounded_s2s_audio"), "audio_status":audio_result.ingress_status,
                "stt_confidence":audio_result.stt.confidence if audio_result.stt else None,"barge_in":audio_result.barge_in}

    def _conversation_context(self, session, node):
        from .s2s import ConversationContext, ProfileContext
        context = self.conversation_contexts.get(session.session_id)
        if context is None:
            profile = self.profiles.resolve(session.rfid_tag or "") or {}
            if getattr(self.profiles, "last_error", None):
                raise RuntimeError("profile provider unavailable")
            context = ConversationContext(session.session_id, session.scenario_id or "fixture_core", node["id"], tuple(node["allowed_topics"]), tuple(node["forbidden_topics"]), int(node["max_turns"]), int(node["max_duration_seconds"]), int(node["context_window"]), ProfileContext.from_profile(profile))
            self.conversation_contexts[session.session_id] = context
        return context

    @staticmethod
    def _quick_response_id(text: str, context: Any) -> str | None:
        if context.scenario_id != "fixture_core":
            return None
        key = " ".join(str(text).casefold().strip().rstrip("?!.,").split())
        return {
            "привет": "quick_greeting",
            "как настроение": "quick_mood",
            "как настроение сегодня": "quick_mood",
            "мне грустно": "quick_sad",
            "мне немного грустно": "quick_sad",
            "что ты умеешь": "quick_capabilities",
            "расскажи сказку": "quick_story_prompt",
        }.get(key)

    def _apply_quick_response(self, session, response_id: str) -> list[dict[str, Any]]:
        self.audio.play(response_id, None, session.cancellation_token)
        engine = getattr(getattr(self, "runtime_manager", None), "engine", None)
        if engine is not None:
            engine.metrics.event("quick_reply_audio", response_id=response_id)
        return [{"action":"quick_reply", "response_id":response_id},
                {"action":"audio", "response_id":response_id}]

    def _apply_s2s_result(self, session, scenario, node, result):
        outputs = [{"action":"s2s_text", "text":chunk} for chunk in result.chunks] + [{"action":item.value, **item.params} for item in result.actions]
        if result.fallback_reason:
            # Expose only the bounded reason, never raw child input or model
            # output. This makes a tester's fallback diagnosable in the stand
            # without weakening the fail-safe scenario routing.
            outputs.insert(0, {"action":"s2s_fallback", "reason":result.fallback_reason})
            if result.fallback_reason == "safety_signal":
                self.publisher.publish(Event("SafetySignalDetected", session.session_id, session.last_sequence, self.now(), "safety", {"category":"child_signal"}, session.correlation_id))
            outputs += self._goto(session, scenario, node["on_safety_signal"] if "safety" in result.fallback_reason or "blocked" in result.fallback_reason else node["fallback_node"], result.fallback_reason)
        elif result.return_to_scenario:
            outputs += self._goto(session, scenario, node["return_node"], "s2s_return")
        return outputs

    def start_session(self, session_id: str, correlation_id: str | None = None) -> dict[str, Any]:
        current = self.sessions.get(session_id)
        if current is not None:
            return self._result(current, [], "existing_session")
        active = self.sessions.active_for_installation(self.installation_id)
        session = SessionRecord(session_id, self.installation_id, correlation_id or str(uuid.uuid4()), started_at=self.now(), last_activity_at=self.now())
        if active is not None:
            return self._result(session, [], "installation_busy")
        self.sessions.save(session)
        return self._result(session, [], "session_created")

    async def reset_session(self, session_id: str) -> dict[str, Any]:
        self._session_epochs[session_id] = self._session_epochs.get(session_id, 0) + 1
        cleanup = await self._cancel_audio_completions(session_id, operation="reset")
        if self.bounded_s2s is not None:
            await self.bounded_s2s.cancel_session(session_id)
        if self.s2s_audio is not None:
            await self.s2s_audio.cancel_session(session_id)
        self.conversation_contexts.pop(session_id, None)
        self._last_audio_ack_at.pop(session_id, None)
        self.sessions.delete(session_id)
        return {"session_id":session_id,"state":"RESET","reset":True,"cleanup":cleanup}

    def receive(self, event: Event, transient_text: str | None = None) -> dict[str, Any]:
        session = self.sessions.get(event.session_id) or SessionRecord(event.session_id, self.installation_id, event.correlation_id or str(uuid.uuid4()), started_at=self.now(), last_activity_at=self.now())
        button_id = str(event.payload.get("button_id", ""))
        # Text/voice arriving before RFID or handset setup is a benign early input.
        # Keep the session in an explicit waiting state instead of resolving a null graph node.
        if event.event_type == "SpeechReceived" and (session.current_node is None or session.state == Lifecycle.IDLE):
            session.state = Lifecycle.WAITING_RFID
            self.sessions.save(session)
            return self._result(session, [{"action": "audio_cancelled"}, {"action": "input_ignored"}], "processed")
        # Button release is a physical edge, not a new scenario command.
        # It must never re-route an already accepted press into safety fallback.
        if event.event_type == "ButtonReleased":
            self.sessions.save(session)
            return self._result(session, [], "button_released")

        # Speech normally completes in receive_async(), but the synchronous
        # API and hardware smoke tests must still receive a structured result.
        # Keep text transient, apply the same safety gate, and defer ordinary
        # input to the bounded S2S/LLM worker instead of falling through.
        if event.event_type == "SpeechReceived":
            text = str(transient_text or event.payload.get("text", ""))
            lowered = text.lower()
            unsafe = len(text) > 512 or any(
                marker in lowered
                for marker in ("ignore instructions", "system prompt", "покажи промпт")
            )
            if unsafe:
                outputs = self._fallback(session, "unsafe_input", event.sequence)
                self.sessions.save(session)
                return self._result(session, outputs, "unsafe_input")
            self.sessions.save(session)
            return self._result(session, [], "speech_deferred")
        if event.event_type == "ButtonPressed" and button_id not in {"1","2","3","4","5","6","7"} and session.state == Lifecycle.AUTHENTICATED:
            event.event_type = "HandsetLifted"
        if (
            event.event_type == "ButtonPressed"
            and str(event.payload.get("button_id")) in {"1", "2", "3", "4", "5", "7"}
            and session.rfid_tag is None
            and session.current_node is None
            and session.state == Lifecycle.WAITING_RFID
        ):
            self._prepare_visual_guest(session)
            button_id = int(event.payload.get("button_id"))
            targets = self._fixture_button_targets()
            session.current_node = targets[button_id]
            self.publisher.publish(Event("VisualGuestStarted", session.session_id, session.last_sequence, self.now(), "application", session.correlation_id))
            self.sessions.save(session)
            return self._result(session, [{"action": "visual_guest", "button_id": button_id, "node": session.current_node, "audio": False}], "visual_guest")


        if (event.event_type == "ButtonPressed" and session is not None
                and session.state not in {Lifecycle.ACTIVE, Lifecycle.FALLBACK, Lifecycle.ENDED}
                and not session.current_node
                and str(event.payload.get("button_id")) in {"1", "2", "3", "4", "5", "7"}):
            self._prepare_visual_guest(session)
        if event.sequence <= session.last_sequence:
            return self._result(session, [], "idempotent_duplicate")
        if session.state == Lifecycle.ENDED:
            return self._result(session, [], "late_event_rejected")
        session.last_sequence, session.last_activity_at = event.sequence, self.now()
        self.publisher.publish(event)
        outputs: list[dict] = []
        # Pre-auth visual guest input is deterministic hardware routing. It must
        # not ask LangGraph/LLM to authorize a branch before a session exists.
        if (event.event_type == "ButtonPressed" and session.visual_only
                and session.scenario_id == "fixture_core"
                and session.current_node == "fixture_choose"):
            scenario = self.scenarios.get("fixture_core") or {}
            node = self._node(scenario, session.current_node) if scenario else {}
            static_targets = self._fixture_button_targets()
            button_value = int(event.payload.get("button_id", 0))
            target = node.get("transitions", {}).get(f"button_{button_value}") or static_targets.get(button_value)
            if target:
                session.current_node = str(target)
                self.publisher.publish(Event("ScenarioAdvanced", session.session_id, session.last_sequence, self.now(), "application", session.correlation_id))
                visual = [{"action": "visual_guest", "button_id": int(event.payload.get("button_id", 0)), "node": session.current_node, "audio": False}]
                if session.current_node == "fixture_led_mix":
                    visual.append({"action": "led_pattern", "pattern": "led_mix", "color": "yellow", "duration_ms": 500})
                elif session.current_node == "fixture_game_repeat_pattern":
                    visual.append({"action": "led_pattern", "pattern": "game_repeat_pattern", "color": "yellow", "duration_ms": 800})
                return self._result(session, visual, "visual_guest")
        if event.event_type == "RfidPresented":
            tag = str(event.payload.get("tag_id", ""))
            try:
                profile = self.profiles.resolve(tag)
            except ProfileProviderError as exc:
                # Provider failure is not an unknown tag.  Do not persist a new
                # session or bind an existing one; keep the diagnostic explicit.
                if self.sessions.get(event.session_id) is not None:
                    session.error_state = "profile_provider_unavailable"
                    session.state = Lifecycle.WAITING_RFID
                    self.sessions.save(session)
                return self._result(
                    session,
                    [{"action": "auth", "status": "error", "reason": "profile_provider_unavailable"}],
                    "profile_provider_unavailable",
                )
            # A child may lift the handset before the RFID reader reports the tag.
            # Bind the profile into the already-running anonymous fixture session
            # instead of destroying the active scenario and leaving the device
            # in a disabled/no-node state.
            if session.state == Lifecycle.ACTIVE and session.current_node and profile and not session.child_id:
                session.rfid_tag = tag
                session.child_id = profile.get("child_id")
                session.anonymous = False
                outputs.append({"action":"auth", "status":"authenticated"})
                outputs.append({"action":"rfid_late_bound", "tag_id":tag, "child_id":session.child_id})
                self.sessions.save(session)
                return self._result(session, outputs, "rfid_late_bound")
            if session.current_node or session.state in {Lifecycle.ACTIVE, Lifecycle.FALLBACK, Lifecycle.ENDING}:
                result = self._result(session, [{"action":"auth", "status":"rejected", "reason":"session_active"}], "session_rebind_rejected")
                self.sessions.save(session)
                return result
            existing = self.sessions.active_for_installation(session.installation_id)
            if existing and existing.session_id != session.session_id:
                # Persist the rejected session as a blocked placeholder.  A
                # later handset event for the same id must not silently turn
                # an installation-busy request into anonymous mode.
                session.error_state = "installation_busy"
                self.sessions.save(session)
                return self._result(session, [], "installation_busy")
            tag = str(event.payload.get("tag_id", ""))
            # Resolve once per RFID edge.  An unknown/unauthorized tag must not
            # bind the session or open an anonymous voice channel.
            if not profile:
                session.rfid_tag = None
                session.child_id = None
                session.anonymous = False
                session.state = Lifecycle.WAITING_RFID
                outputs.append({"action": "auth", "status": "rejected", "reason": "unknown_rfid"})
                self.sessions.save(session)
                return self._result(session, outputs, "unknown_rfid")
            session.rfid_tag = tag
            session.child_id = profile.get("child_id")
            session.anonymous = False
            session.state = Lifecycle.AUTHENTICATED
            if profile.get("child_id") and not session.current_node:
                scenario = self.scenarios.get("fixture_core") or {}
                session.scenario_id = "fixture_core"
                session.scenario_version = (scenario.get("metadata") or {}).get("version")
                session.current_node = str(scenario.get("start", "fixture_choose"))
            outputs.append({"action":"auth", "status":"authenticated" if profile else "anonymous"})
            rainbow_colors = {str(button_id): color for button_id, color in BUTTON_LED_COLORS.items()}
            self.led.pattern_map("rfid_wave", rainbow_colors, 1200, session.cancellation_token)
            outputs.extend([{"action":"led_pattern", "pattern":"rfid_wave", "colors":rainbow_colors, "duration_ms":1200}] if profile else [])
            self.sessions.save(session)
            return self._result(session, outputs, "rfid_presented")
        elif event.event_type == "HandsetLifted":
            if session.error_state == "installation_busy":
                self.sessions.save(session)
                return self._result(session, [], "installation_busy")
            # A handset lift is a lifecycle edge, never a button command.
            if session.handset_up:
                self.sessions.save(session)
                return self._result(session, [], "idempotent_duplicate")
            session.handset_up = True
            session.state = Lifecycle.ACTIVE
            profile = self.profiles.resolve(session.rfid_tag or "") or {}
            session.scenario_id = "dev_core" if profile.get("dev_mode") else "fixture_core"
            scenario = self.scenarios.get(session.scenario_id) or self.scenarios.get("fixture_core") or {}
            session.scenario_version = (scenario.get("metadata") or {}).get("version", "")
            if session.scenario_id == "dev_core":
                welcome_outputs = self._goto(session, scenario, "dev_welcome", "handset_lifted")
                session.current_node = "dev_dialogue"
                outputs.extend(welcome_outputs)
            else:
                # RFID admission records the scenario entry node so visual
                # button input can be handled before the handset is lifted.
                # Strict LangGraph entry authorization expects that node to be
                # entered from the pre-auth marker (current_node=None). Clear
                # only that marker before the first handset greeting; a child
                # who already chose a visual branch must keep the live node.
                start_node = str(scenario.get("start", "fixture_welcome"))
                if session.current_node == start_node:
                    session.current_node = None
                outputs.extend(self._goto(session, scenario, start_node, "handset_lifted"))
            self.publisher.publish(Event("SessionStarted", session.session_id, event.sequence, self.now(), "application", {}, session.correlation_id))
            self.sessions.save(session)
            return self._result(session, outputs, "handset_lifted")
            if event.event_type not in {"HandsetLifted", "HandsetPlaced"} and session.state in {Lifecycle.ACTIVE, Lifecycle.FALLBACK, Lifecycle.ENDING} and session.current_node:
                result = self._result(session, [], "idempotent_duplicate")
                self.sessions.save(session)
                return result
            if session.state == Lifecycle.WAITING_RFID: session.anonymous = True
            session.state = Lifecycle.ACTIVE
            profile = self.profiles.resolve(session.rfid_tag or "") or {}
            session.scenario_id = "dev_core" if profile.get("dev_mode") else "fixture_core"
            scenario = self.scenarios.get(session.scenario_id); session.scenario_version = scenario["metadata"]["version"]
            if session.scenario_id == "dev_core":
                welcome_outputs = self._goto(session, scenario, "dev_welcome", "handset_lifted")
                session.current_node = "dev_dialogue"
                outputs += welcome_outputs
            else:
                outputs += self._goto(session, scenario, scenario["start"], "handset_lifted")
            self.publisher.publish(Event("SessionStarted", session.session_id, event.sequence, self.now(), "application", {}, session.correlation_id))
        if event.event_type == "HandsetLifted":
            if session.handset_up:
                return self._result(session, outputs, "idempotent_duplicate")
                session.handset_up = True
                self.sessions.save(session)
                return self._result(session, outputs, "handset_lifted")

        if event.event_type == "HandsetPlaced":
            session.handset_up = False
            # Async S2S cancellation is handled by receive_async() before it
            # enters this synchronous lifecycle boundary.  The sync boundary
            # cancels deterministic audio through _end() and must not create an
            # un-awaited coroutine.
            ended = self._end(session, "handset_placed", event.sequence)
            # Lifecycle events are terminal for this dispatch.
            if isinstance(ended, dict):
                return ended
            outputs.extend(ended)
            self.sessions.save(session)
            return self._result(session, outputs, "handset_placed")
        elif event.event_type == "HardwareDisconnected":
            outputs.extend(self._fallback(session, "hardware_disconnected", event.sequence))
            self.sessions.save(session)
            return self._result(session, outputs, "hardware_disconnected")
        elif event.event_type in {"ButtonHeld", "ButtonLongHeld"}:
            if int(event.payload.get("duration_ms", 0)) >= 20_000:
                outputs.extend(self._end(session, "long_hold", event.sequence))
            self.sessions.save(session)
            return self._result(session, outputs, "button_hold")
        elif event.event_type == "ButtonPressed":
            button_id = int(event.payload.get("button_id", 0))
            # DEV dialogue starts when the handset is lifted. Its physical
            # buttons are unused, so they must not cancel an answer that is
            # still being streamed to the browser.
        scenario = self.scenarios.get(session.scenario_id or "fixture_core")
        node = self._node(scenario, session.current_node)
        if session.current_node == "fixture_welcome":
            # The welcome handoff to the button-choice node is routing-only.
            # Do not play the choice node's generic game_menu prompt before
            # immediately entering the branch selected by this same button.
            session.current_node = "fixture_choose"
            node = self._node(scenario, session.current_node)
        # Physical contract: 6 opens Mishka/LLM; 7 opens schedule/weather.
        # Child button 6 is the real bounded Mishka/LLM dialogue route.
        # Keep the child scenario/profile intact; dev_core is reserved for
        # the explicit adult engineering profile.
        if session.scenario_id == "fixture_core" and button_id == 6 and not session.game_id:
            target = "fixture_free"
            if self._node(scenario, target) is not None:
                outputs += self._goto(session, scenario, target, "button_6_dialogue")
            self.sessions.save(session)
            return self._result(session, outputs, "button_6_dialogue")
            # Physical contract: child button 7 always opens schedule + weather,
        # regardless of the active child activity/dialogue node.
        if (session.scenario_id == "fixture_core" and button_id == 7
                and not session.game_id
                and node.get("type") != "story"):
            target = "fixture_schedule_weather"
            if self._node(scenario, target) is not None:
                outputs += self._goto(session, scenario, target, "button_7_schedule_weather")
            self.sessions.save(session)
            return self._result(session, outputs, "button_7_schedule_weather")

        if session.scenario_id == "dev_core" and node["type"] == "bounded_free_conversation":
            if button_id == 6:
                self.sessions.save(session)
                return self._result(session, [], "dev_dialogue_ready")
            if button_id == 7:
                outputs = self._goto(session, "fixture_core", "fixture_schedule_weather", "button:7")
                self.sessions.save(session)
                return self._result(session, outputs, "dev_schedule_weather")
            self.sessions.save(session)
            return self._result(session, [{"action":"dev_button_ignored","button_id":button_id,"reason":"dialogue_already_active"}], "processed")
        if session.game_id:
            outputs = self._game_button(session, button_id, event.sequence)
            self.sessions.save(session)
            return self._result(session, outputs, "game_button")
        target = node.get("transitions", {}).get(f"button_{button_id}")
        return self._goto(session, scenario, target, f"button:{button_id}") if target else self._fallback(session, "unknown_button", event.sequence)

    def _button(self, session, button_id, sequence):
        # Duplicate/empty mock or noisy handset frames must not tear down an active session.
        if (not button_id or str(button_id) not in {"1", "2", "3", "4", "5", "6", "7"}) and session.current_node and session.state in {Lifecycle.ACTIVE, Lifecycle.FALLBACK}:
            return [{"action": "input_ignored", "reason": "invalid_button", "button_id": str(button_id)}]
        if not session.current_node or not session.scenario_id:
            return [{"action":"input_ignored", "reason":"session_not_ready", "button_id":button_id}]
        if session.game_id: return self._game_button(session,button_id,sequence)
        scenario = self.scenarios.get(session.scenario_id or "fixture_core"); node = self._node(scenario, session.current_node)
        # Adult engineering mode is already in the dialogue node after the
        # handset is lifted. Physical buttons are intentionally inert there;
        # they must never route a harmless button press into safety fallback.
        if session.scenario_id == "dev_core" and node["type"] == "bounded_free_conversation":
            if button_id == 6:
                return [{"action":"dev_dialogue_ready","button_id":6,"reason":"dialogue_active"}]
            return [{"action":"dev_button_ignored", "button_id":button_id, "reason":"dialogue_already_active"}]
        target = node.get("transitions", {}).get(f"button_{button_id}")
        return self._goto(session, scenario, target, f"button:{button_id}") if target else self._fallback(session, "unknown_button", sequence)

    def _game_button(self,session,button_id,sequence):
        scenario=self.scenarios.get(session.scenario_id or "fixture_core"); node=self._node(scenario,session.current_node); game_id=session.game_id or ""; game=self.scenarios.game(game_id) or {}
        expected=session.game_expected[session.game_index]
        if button_id==expected:
            session.game_index+=1
            if session.game_index<len(session.game_expected):
                next_button=session.game_expected[session.game_index]
                colors=fixed_button_colors((next_button,))
                self.led.pattern_map(f"next_button_{next_button}",colors,500,session.cancellation_token)
                return [{"action":"led_pattern","pattern":f"next_button_{next_button}","colors":colors,"duration_ms":500}]
            if self.history is not None and session.child_id and not session.anonymous: self.history.append("game",session.child_id,game_id,{"success":True,"attempts":session.game_attempts})
            self.publisher.publish(Event("GameCompleted",session.session_id,sequence,self.now(),"application",{"game_id":game_id,"success":True,"attempts":session.game_attempts},session.correlation_id))
            self.audio.play(str(game.get("success_response","correct")),None,session.cancellation_token)
            session.game_id=None; session.game_expected=[]
            return [{"action":"audio","response_id":game.get("success_response","correct")}] + self._goto(session,scenario,node.get("transitions",{}).get("success") or "fixture_choose","game_success")
        session.game_attempts+=1; session.game_index=0; self.audio.play("incorrect",None,session.cancellation_token)
        pattern=str(game.get("pattern","attention")); colors=fixed_button_colors(game.get("expected",()))
        self.led.pattern_map(pattern,colors,800,session.cancellation_token)
        outputs=[{"action":"audio","response_id":"incorrect"},{"action":"led_pattern","pattern":pattern,"colors":colors,"duration_ms":800}]
        if session.game_attempts>=session.game_max_attempts:
            if self.history is not None and session.child_id and not session.anonymous: self.history.append("game",session.child_id,game_id,{"success":False,"attempts":session.game_attempts})
            self.publisher.publish(Event("GameCompleted",session.session_id,sequence,self.now(),"application",{"game_id":game_id,"success":False,"attempts":session.game_attempts},session.correlation_id))
            session.game_id=None; session.game_expected=[]
            outputs+=self._goto(session,scenario,node.get("transitions",{}).get("failure") or "fixture_choose","game_failure")
        return outputs

    def _speech(self, session, text, sequence):
        if not session.current_node or not session.scenario_id:
            return [{"action":"input_ignored", "reason":"session_not_ready"}]
        if len(text) > 512 or any(x in text.lower() for x in ("ignore instructions", "system prompt", "покажи промпт")): return self._fallback(session, "unsafe_input", sequence)
        scenario = self.scenarios.get(session.scenario_id or "fixture_core"); node = self._node(scenario, session.current_node)
        intent = self.classifier.classify(text, str(uuid.uuid4()), lambda: session.state == Lifecycle.ENDED)
        self.publisher.publish(Event("IntentClassified", session.session_id, sequence, self.now(), "classifier", {"intent":intent.value, "confidence":intent.confidence}, session.correlation_id))
        target = node.get("transitions", {}).get(intent.value) if intent.is_valid(self.threshold) else None
        return self._goto(session, scenario, target, f"intent:{intent.value}") if target else self._fallback(session, "intent_fallback", sequence)

    def _fallback(self, session, reason, sequence):
        if not session.current_node or not session.scenario_id:
            return [{"action":"input_ignored", "reason":"session_not_ready", "trigger":reason}]
        session.retries += 1; self.publisher.publish(Event("SafetyFallbackTriggered", session.session_id, sequence, self.now(), "application", {"reason":reason}, session.correlation_id))
        if session.retries >= 3: return self._end(session, "retry_limit", sequence)
        scenario = self.scenarios.get(session.scenario_id or "fixture_core")
        node = self._node(scenario, session.current_node)
        target = node.get("fallback") or "end"
        outputs = self._goto(session, scenario, target, reason)
        if outputs and outputs[0].get("action") == "scenario_rejected":
            return outputs
        # _goto activates normal destinations; safety fallback remains visible
        # as a distinct lifecycle state while its safe response is playing.
        session.state = Lifecycle.FALLBACK
        return outputs

    def _cancel(self, session, sequence):
        session.cancellation_token += 1
        self.audio.cancel(session.cancellation_token)
        # Async ingress already stopped browser PCM and any in-flight model
        # before entering this deterministic state-machine transition.
        self.publisher.publish(Event("AudioCancelled", session.session_id, sequence, self.now(), "application", {}, session.correlation_id))
        return [{"action":"audio_cancelled", "token":session.cancellation_token}]
    def _end(self, session, reason, sequence, emit=True):
        if session.state == Lifecycle.ENDED:
            return []
        session.state = Lifecycle.ENDED
        session.termination_reason = reason
        session.cancellation_token += 1
        self.audio.cancel(session.cancellation_token)
        # Session termination is also a device-state transition: clear the
        # physical LEDs through the same canonical command consumed by UI.
        self.led.pattern("off", "off", 0, session.cancellation_token)
        self.publisher.publish(Event("SessionEnded", session.session_id, sequence, self.now(), "application", {"reason": reason}, session.correlation_id))
        return [{"action": "led_pattern", "pattern": "off", "color": "off", "duration_ms": 0}, {"action": "session_ended", "reason": reason}]
    def _goto(self, session, scenario, target, reason="transition"):
        """Advance the deterministic scenario graph and start node-owned output."""
        if not isinstance(scenario, dict):
            return self._fallback(session, "invalid_scenario", session.last_sequence)
        node = self._node(scenario, target)
        if not node:
            return self._fallback(session, "unknown_node", session.last_sequence)
        scenario_id = str(scenario.get("id", session.scenario_id or "fixture_core"))
        decision = self.langgraph.authorize(
            scenario_id,
            session.current_node,
            str(target),
            str(reason),
        )
        if not decision.get("ok"):
            diagnostic = {
                "scenario_id": scenario_id,
                "current_node": session.current_node,
                "target": str(target),
                "reason": decision.get("reason", "provider_or_validation_failure"),
                "environment": decision.get("environment"),
            }
            self.runtime_status["last_scenario_transition_failure"] = diagnostic
            self.publisher.publish(Event(
                "ScenarioTransitionRejected",
                session.session_id,
                session.last_sequence,
                self.now(),
                "scenario_engine",
                diagnostic,
                session.correlation_id,
            ))
            return [{
                "action": "scenario_rejected",
                "reason": diagnostic["reason"],
                "scenario_id": scenario_id,
                "node": session.current_node,
                "target": str(target),
                "environment": diagnostic["environment"],
            }]
        session.scenario_id = scenario_id
        session.scenario_version = str(scenario.get("metadata", {}).get("version", ""))
        session.current_node = str(target)
        session.state = Lifecycle.ACTIVE
        actions = [{"action": "scenario", "node": str(target), "reason": str(reason)}]
        node_type = str(node.get("type", ""))
        if node_type == "say" and not session.visual_only and session.handset_up:
            meta = node.get("metadata", {}) if isinstance(node.get("metadata", {}), dict) else {}
            response_id = meta.get("personalized_response") if session.child_id and not session.anonymous else None
            response_id = response_id or node.get("prompt")
            if response_id:
                self.audio.play(str(response_id), node.get("audio_asset"), session.cancellation_token)
                actions.append({"action": "audio", "response_id": str(response_id)})
        elif node_type == "story":
            story_id = str(node.get("metadata", {}).get("story_id", ""))
            story = self.scenarios.story(story_id) if story_id else None
            if story:
                if session.handset_up and not session.visual_only:
                    self.audio.play(story_id, story.get("audio_asset"), session.cancellation_token)
                actions.append({"action": "story", "story_id": story_id, "audio_asset": story.get("audio_asset")})
                self.publisher.publish(Event("StorySelected", session.session_id, session.last_sequence, self.now(), "application", {"story_id": story_id}, session.correlation_id))
                if self.history is not None and session.child_id and not session.anonymous:
                    self.history.append("story", session.child_id, story_id, {"completed": False})
        elif node_type == "game":
            game_id = str(node.get("metadata", {}).get("game_id", ""))
            game = self.scenarios.game(game_id) if game_id else None
            if game:
                session.game_id = game_id
                session.game_expected = [int(v) for v in game.get("expected", [])]
                session.game_index = 0
                session.game_attempts = 0
                if session.handset_up and not session.visual_only:
                    self.audio.play(node.get("prompt", "game_press"), node.get("audio_asset"), session.cancellation_token)
                actions.append({"action": "game", "game_id": game_id})
                if not node.get("actions") and not node.get("led_pattern"):
                    first_button = session.game_expected[0]
                    pattern = f"next_button_{first_button}"
                    colors = fixed_button_colors((first_button,))
                    self.led.pattern_map(pattern, colors, 500, session.cancellation_token)
                    actions.append({"action": "led_pattern", "pattern": pattern, "colors": colors, "duration_ms": 500})
        elif node.get("prompt") and session.handset_up and not session.visual_only and node_type not in {"wait", "bounded_free_conversation"}:
            self.audio.play(str(node["prompt"]), node.get("audio_asset"), session.cancellation_token)
            actions.append({"action": "audio", "response_id": str(node["prompt"])})
        if node.get("led_pattern"):
            self.led.pattern(str(node["led_pattern"]), node.get("led_color", "yellow"), 500, session.cancellation_token)
            actions.append({"action": "led_pattern", "pattern": str(node["led_pattern"]), "color": node.get("led_color", "yellow"), "duration_ms": 500})
        for command in node.get("actions", []):
            if not isinstance(command, dict):
                continue
            command_type = str(command.get("type", ""))
            if command_type == "led_pattern":
                pattern = str(command.get("pattern", "off"))
                color = str(command.get("color", "off"))
                duration_ms = int(command.get("duration_ms", 500))
                raw_colors = command.get("colors")
                if isinstance(raw_colors, dict):
                    colors = fixed_button_colors(
                        int(button_id) for button_id, value in raw_colors.items()
                        if str(button_id).isdigit() and str(value).lower() != "off"
                    )
                    self.led.pattern_map(pattern, colors, duration_ms, session.cancellation_token)
                    actions.append({"action": "led_pattern", "pattern": pattern, "colors": colors, "duration_ms": duration_ms})
                else:
                    self.led.pattern(pattern, color, duration_ms, session.cancellation_token)
                    actions.append({"action": "led_pattern", "pattern": pattern, "color": color, "duration_ms": duration_ms})
            elif command_type == "display":
                actions.append({
                    "action": "display",
                    "asset_id": str(command.get("asset_id", "")),
                    "text": str(command.get("text", "")),
                })
        # Story completion is a transport boundary: the browser reports the
        # end of the single story audio asset by pressing button 7.  The
        # scenario contract makes story_done an acknowledgement node whose
        # next transition returns to the choice menu; apply that transition
        # in the same dispatch so the client cannot remain in a dead-end node.
        if target in {"fixture_story_done", "fixture_game_done"}:
            next_target = node.get("transitions", {}).get("next")
            if next_target:
                session.current_node = str(next_target)
                actions.append({"action": "scenario", "node": str(next_target), "reason": "story_completed"})
        self.sessions.save(session)
        return actions

    @staticmethod
    def _node(scenario, node_id):
        for node in scenario["nodes"]:
            if node["id"] == node_id: return node
        raise ValueError("validated scenario lost node")
    def _async_sync_result(self, event, session, transient_text):
        result = self.receive(event, transient_text)
        if isinstance(result, dict):
            return result
        current = self.sessions.get(event.session_id) or session
        if current is None:
            return {"session_id": event.session_id, "state": "RESET", "node": None, "reason": "ignored_input", "outputs": result, "correlation_id": event.correlation_id}
        return self._result(current, result, "sync_result")

    @staticmethod
    def _result(session, outputs, reason): return {"session_id":session.session_id, "state":session.state.value, "node":session.current_node, "reason":reason, "outputs":outputs, "correlation_id":session.correlation_id}
