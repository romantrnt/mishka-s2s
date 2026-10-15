from __future__ import annotations

import asyncio
import copy
import contextlib
import inspect
import logging
import time
import uuid
from collections import deque
from typing import Any, Callable
from weakref import WeakSet

from .domain import AudioOutput, DisplayController, Event, EventPublisher, IntentClassifier, LEDController, Lifecycle, ProfileProvider, ScenarioRepository, SessionRecord, SessionRepository
from .lighting import BUTTON_DIGIT_ELEMENTS, BUTTON_FILL_ELEMENTS, LIGHTING_ELEMENT_IDS, LightingCommand, EXHIBITION_DIGIT_WAVE_GROUPS, EXHIBITION_MENU_WAVE_GROUPS, EXHIBITION_WAVE_STAGE_MS, READINESS_WAVE_DURATION_MS, READINESS_WAVE_GROUPS
from .schedule_weather import ScheduleWeatherProviderError, validate_report
from .adapters import ProfileProviderError
from .canonical_services import LED_PRESETS, BUTTON_ELEMENTS, RecordedPhraseResolver, SafetyClassifier, SimulatedAdultHandoff, TypedInfoRenderer, classify_intent
from .config import settings_from_compat_env
from .canonical_contract import CanonicalEvent, CanonicalRuntime, FixedStoryManifest, validate_generated_story
from .system_prompt import build_system_prompt
from app.scenarios.canonical_graph import CanonicalLangGraphAuthorizer

logger = logging.getLogger("mishka.application")

# PHRASE_008 is one approved 18.406 s WAV. These offsets are the measured
# starts of each spoken digit and its following action phrase in that file.
STORY_PLAYBACK_WAVE_GROUPS = tuple((BUTTON_FILL_ELEMENTS[button],) for button in range(1, 8))
STORY_CHOICE_WAVE_GROUPS = tuple((BUTTON_FILL_ELEMENTS[button],) for button in range(1, 6))
STORY_MENU_BASE_ELEMENTS = tuple(
    [BUTTON_DIGIT_ELEMENTS[button][1] for button in range(1, 8)]
    + [BUTTON_FILL_ELEMENTS[6], BUTTON_FILL_ELEMENTS[7]]
)

EXHIBITION_MENU_PHASE_TIMINGS_MS = (
    (1, "digit", 380), (1, "full", 1520),
    (2, "digit", 2800), (2, "full", 3690),
    (3, "digit", 5160), (3, "full", 5960),
    (4, "digit", 8370), (4, "full", 9400),
    (5, "digit", 11110), (5, "full", 12200),
    (6, "digit", 13510), (6, "full", 14430),
    (7, "digit", 15910), (7, "full", 16830),
)


AUDIO_ASSET_STATUSES = frozenset({
    "absent", "placeholder", "recorded_pending_approval", "approved",
    "queued", "playing", "finished", "cancelled", "failed",
})


def _set_audio_effect_status(effect: dict[str, Any], *, asset_status: str,
                             queued: bool, resolution: str | None = None) -> None:
    """Attach honest transport-independent audio status to an effect."""
    normalized = str(asset_status or "absent")
    if normalized not in AUDIO_ASSET_STATUSES:
        normalized = "absent"
    effect["asset_status"] = normalized
    effect["audio_status"] = "queued" if queued else ("unavailable" if normalized in {"absent", "missing"} else "failed")
    effect["audio_requested"] = True
    effect["audio_resolved"] = normalized not in {"absent", "missing"}
    effect["audio_queued"] = bool(queued)
    effect["audio_started"] = False
    effect["audio_played"] = False
    effect["audio_cancelled"] = False
    effect["audio_failed"] = not queued and normalized not in {"absent", "missing"}
    effect["audio_unavailable"] = not queued and normalized in {"absent", "missing"}
    effect["played"] = False
    if resolution:
        effect["resolution"] = resolution


class ScenarioApplication:
    """Use-case layer. It depends only on domain ports, never FastAPI/files/UART/LLM."""
    AUDIO_RECOVERABLE_FAILURES = frozenset({
        "vad_failure",
        "stt_failure",
        "stt_low_confidence",
        "audio_too_long",
    })

    def __init__(self, sessions: SessionRepository, scenarios: ScenarioRepository, profiles: ProfileProvider, classifier: IntentClassifier, audio: AudioOutput, led: LEDController, publisher: EventPublisher, now=time.time, installation_id="default", threshold=0.6, bounded_s2s=None, history=None, max_session_seconds=1200, idle_timeout_seconds: float | None = None, display: DisplayController | None = None, schedule_weather: Any = None, allow_quick_replies: bool | None = None, settings: Any = None, adult_handoff: Any = None, monotonic_now=time.monotonic, background_scheduler: bool = True):
        self.sessions, self.scenarios, self.profiles, self.classifier, self.audio, self.led, self.publisher = sessions, scenarios, profiles, classifier, audio, led, publisher
        self.now, self.monotonic_now, self.installation_id, self.threshold = now, monotonic_now, installation_id, threshold
        self.background_scheduler = bool(background_scheduler)
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
        # One active immutable playback lease per session. Adapter callbacks
        # must match it before entering the canonical state machine.
        self._active_audio: dict[str, dict[str, Any]] = {}
        self._audio_timeout_tasks: dict[str, asyncio.Task[Any]] = {}
        self._audio_timeout_sequences: dict[str, int] = {}
        self._welcome_timers: dict[str, asyncio.Task[Any]] = {}
        self._welcome_timer_sequences: dict[str, int] = {}
        self._menu_tutorial_tasks: dict[str, asyncio.Task[Any]] = {}
        self._menu_tutorial_sequences: dict[str, int] = {}
        self._lighting_wave_tasks: dict[str, asyncio.Task[Any]] = {}
        self._lighting_wave_sequences: dict[str, int] = {}
        self._last_audio_ack_at: dict[str, float] = {}
        self.runtime_status: dict[str, Any] = {}
        self.last_canonical_effects: list[dict[str, Any]] = []
        self.last_canonical_state: dict[str, Any] = {}
        self.last_canonical_event: dict[str, Any] = {}
        self.canonical_trace: deque[dict[str, Any]] = deque(maxlen=200)
        self.canonical_diagnostics: deque[dict[str, Any]] = deque(maxlen=100)
        self.audio_runtime_diagnostics: deque[dict[str, Any]] = deque(maxlen=100)
        self.state_update_hook: Any = None
        self.runtime_manager: Any = None
        self.metrics: Any = None
        self.watchdog_interval_seconds = float(getattr(settings, "watchdog_interval_s", 5.0))
        self.watchdog_stale_after_seconds = float(getattr(settings, "watchdog_stale_after_s", 30.0))
        self.watchdog_cooldown_seconds = float(getattr(settings, "watchdog_cooldown_s", 30.0))
        self._watchdog_task: asyncio.Task[Any] | None = None
        self._watchdog_lifecycle: Any = None
        self._watchdog_runtime_manager: Any = None
        self._watchdog_cooldown_until: dict[tuple[str, str | None], float] = {}
        self.canonical_runtime = None
        self.allow_development_placeholders = profile in {"mock", "local_cpu", "local_gpu"} or str(getattr(settings, "hardware_mode", "physical")).lower() != "physical"
        if str(getattr(settings, "scenario_id", "")).lower() == "canonical_product":
            root = str(getattr(settings, "content_root", "content"))
            self.canonical_runtime = CanonicalRuntime(now=self.now, monotonic_now=self.monotonic_now, stories=FixedStoryManifest(f"{root}/stories/fixed_manifest.json", "."), allow_development_placeholders=self.allow_development_placeholders, exhibition_mode=(str(getattr(settings, "runtime_mode", "exhibition")).lower() == "exhibition"))
        self.history_repository: Any = history
        # Canonical scenario documents and LangGraph are the single source of
        self.voice_manifest = RecordedPhraseResolver(f"{getattr(settings, 'content_root', 'content')}/audio/voice_manifest.json", allow_development_assets=self.allow_development_placeholders)
        self.safety_classifier = SafetyClassifier()
        self.adult_handoff = adult_handoff if adult_handoff is not None else (SimulatedAdultHandoff() if self.allow_development_placeholders else None)
        self._canonical_profiles: dict[str, dict[str, Any] | None] = {}
        self._canonical_schedule_refs: dict[str, str | None] = {}
        self.transition_authorizer = CanonicalLangGraphAuthorizer() if self.canonical_runtime is not None else None

    def record_audio_diagnostic(self, kind: str, **fields: Any) -> dict[str, Any]:
        item = {"kind": str(kind), "at_monotonic": self.monotonic_now(), **fields}
        self.audio_runtime_diagnostics.append(item)
        self.canonical_diagnostics.append(item)
        self.runtime_status["audio_last_diagnostic"] = dict(item)
        return item

    def _cancel_audio_timeout(self, session_id: str) -> None:
        task = self._audio_timeout_tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _audio_playback_watchdog(self, session_id: str, content_id: str, token: int,
                                       playback_lease: str, generation: int) -> None:
        timeout_s = max(0.01, float(getattr(self.settings, "playback_timeout_s", 60.0)))
        try:
            await asyncio.sleep(timeout_s)
            lease = self._active_audio.get(session_id)
            if not lease or lease.get("content_id") != content_id or lease.get("playback_lease") != playback_lease:
                return
            self.record_audio_diagnostic("audio_playback_timeout", session_id=session_id,
                                        content_id=content_id, generation=generation,
                                        playback_lease=playback_lease, timeout_s=timeout_s)
            sequence = self._audio_timeout_sequences.get(session_id, 0) + 1
            self._audio_timeout_sequences[session_id] = sequence
            current = self.canonical_runtime.sessions.get(session_id) if self.canonical_runtime is not None else None
            state_version = current.state_version if current is not None else 0
            await self.receive_async(Event(
                "AudioFailed", session_id, sequence, self.now(), "audio_watchdog",
                {"response_id": content_id, "token": token, "playback_lease": playback_lease,
                 "generation": generation, "error_code": "audio_playback_timeout",
                 "state_version": state_version},
                "audio-timeout:" + session_id + ":" + content_id + ":" + str(generation),
                self.monotonic_now(),
            ))
        except asyncio.CancelledError:
            return
        finally:
            if self._audio_timeout_tasks.get(session_id) is asyncio.current_task():
                self._audio_timeout_tasks.pop(session_id, None)

    def _register_audio_lease(self, session_id: str, content_id: str, token: int, playback_id: str,
                              playback_lease: str | None = None) -> None:
        self._cancel_audio_timeout(session_id)
        generation = int(self._session_epochs.get(session_id, 0))
        lease_value = str(playback_lease or "")
        self._active_audio[session_id] = {
            "content_id": str(content_id), "token": int(token), "playback_id": str(playback_id),
            "playback_lease": lease_value, "generation": generation, "status": "queued",
        }
        self._audio_timeout_tasks[session_id] = asyncio.create_task(
            self._audio_playback_watchdog(session_id, str(content_id), int(token), lease_value, generation),
            name="mishka-audio-timeout-" + session_id,
        )

    def _audio_callback_is_current(self, event: Event) -> bool:
        lease = self._active_audio.get(event.session_id)
        payload = event.payload if isinstance(event.payload, dict) else {}
        response_id = str(payload.get("response_id") or payload.get("content_id") or "")
        token = payload.get("token")
        generation = payload.get("generation")
        if lease is None:
            return False
        try:
            token_matches = int(token) == int(lease["token"])
        except (TypeError, ValueError, OverflowError):
            token_matches = False
        try:
            generation_matches = int(generation) == int(lease["generation"])
        except (TypeError, ValueError, OverflowError):
            generation_matches = False
        current = (
            response_id == lease["content_id"] and token_matches and generation_matches
            and int(lease.get("generation", -1)) == int(self._session_epochs.get(event.session_id, 0))
            and payload.get("playback_lease") == lease.get("playback_lease")
        )
        if not current:
            self._record_audio_completion_diagnostic(
                "stale_audio_callback", session_id=event.session_id,
                correlation_id=event.correlation_id, generation=lease.get("generation"),
                operation=event.event_type, content_id=response_id or None,
                token=token, reason="playback_lease_mismatch",
            )
        return current

    def _invalidate_audio_lease(self, session_id: str, status: str = "cancelled") -> None:
        self._cancel_audio_timeout(session_id)
        lease = self._active_audio.pop(session_id, None)
        if lease is not None:
            self.record_audio_diagnostic(
                "audio_lease_invalidated", session_id=session_id,
                content_id=lease.get("content_id"), generation=lease.get("generation"),
                reason=status,
            )

    def _mark_audio_effect_lifecycle(self, response_id: str, status: str, *, reason: str | None = None) -> None:
        """Project actual adapter lifecycle back to the canonical effect."""
        allowed = {"queued", "playing", "finished", "cancelled", "failed", "unavailable"}
        if status not in allowed:
            return
        for effect in self.last_canonical_effects:
            kind = str(effect.get("type") or "")
            matches = (
                str(effect.get("phrase_id") or "") == str(response_id)
                or str(effect.get("story_id") or "") == str(response_id)
                or str(effect.get("effect_id") or "") == str(response_id)
            )
            if not matches and kind not in {"play_fixed_story", "resume_fixed_story", "play_placeholder", "speak", "support_question", "support_response"}:
                continue
            if not matches and kind in {"play_fixed_story", "resume_fixed_story", "play_placeholder"}:
                matches = str(effect.get("story_id") or "") == str(response_id).split(":")[-1]
            if not matches:
                continue
            effect["audio_status"] = status
            effect["audio_started"] = status in {"playing", "finished", "cancelled"}
            effect["audio_played"] = status == "finished"
            effect["audio_cancelled"] = status == "cancelled"
            effect["audio_failed"] = status == "failed"
            effect["audio_unavailable"] = status == "unavailable"
            if reason:
                effect["audio_failure_reason"] = str(reason)[:80]
            break

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
        self._cancel_welcome_timer(session_id)
        self._welcome_timer_sequences.pop(session_id, None)
        self._cancel_menu_tutorial(session_id)
        self._menu_tutorial_sequences.pop(session_id, None)
        self._cancel_lighting_wave(session_id)
        completion_cleanup = await self._cancel_audio_completions(session_id, operation="session_cleanup")
        self._invalidate_audio_lease(session_id)
        if self.s2s_audio is not None:
            await self.s2s_audio.cancel_session(session_id)
        elif self.bounded_s2s is not None:
            await self.bounded_s2s.cancel_session(session_id)
        self.conversation_contexts.pop(session_id, None)
        self._canonical_profiles.pop(session_id, None)
        self._canonical_schedule_refs.pop(session_id, None)
        self._last_audio_ack_at.pop(session_id, None)
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
        now = self.monotonic_now()
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
                now = self.monotonic_now()
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
                     "last_audio_cleanup": self._last_audio_cleanup,
                     "audio_runtime_diagnostics": list(self.audio_runtime_diagnostics)})
        return base

    def _cancel_menu_tutorial(self, session_id: str) -> None:
        task = self._menu_tutorial_tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()

    def _cancel_lighting_wave(self, session_id: str) -> None:
        task = self._lighting_wave_tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()
        # CanonicalRuntime keeps the last sequence per source for the whole
        # session. Keep this counter monotonic when replacing/canceling a
        # wave, otherwise the next completion is rejected as stale.

    def _schedule_lighting_wave_completion(
        self, session_id: str, token: int, event_type: str, delay_ms: int,
    ) -> None:
        self._cancel_lighting_wave(session_id)
        sequence = self._lighting_wave_sequences.get(session_id, 0) + 1
        self._lighting_wave_sequences[session_id] = sequence

        async def complete() -> None:
            try:
                await asyncio.sleep(max(0.0, float(delay_ms) / 1000.0))
                state = self.canonical_runtime.sessions.get(session_id) if self.canonical_runtime is not None else None
                record = self.sessions.get(session_id)
                if state is None or record is None or record.cancellation_token != token:
                    return
                next_sequence = self._lighting_wave_sequences.get(session_id, 0) + 1
                self._lighting_wave_sequences[session_id] = next_sequence
                await self._receive_canonical_event(Event(
                    event_type, session_id, next_sequence, self.now(),
                    "canonical_lighting_wave", {"token": token},
                    f"lighting-wave:{session_id}:{sequence}:{event_type}",
                    self.monotonic_now(),
                ))
            except asyncio.CancelledError:
                return
            finally:
                if self._lighting_wave_tasks.get(session_id) is asyncio.current_task():
                    self._lighting_wave_tasks.pop(session_id, None)

        if not self.background_scheduler:
            return
        self._lighting_wave_tasks[session_id] = asyncio.create_task(
            complete(), name=f"mishka-lighting-wave-{session_id}"
        )

    def _schedule_menu_tutorial_steps(self, session_id: str, token: int, duration_ms: int) -> None:
        self._cancel_menu_tutorial(session_id)
        sequence = self._menu_tutorial_sequences.get(session_id, 0) + 1
        self._menu_tutorial_sequences[session_id] = sequence

        async def advance() -> None:
            try:
                started_at = self.monotonic_now()
                for button, phase, offset_ms in EXHIBITION_MENU_PHASE_TIMINGS_MS:
                    deadline = started_at + float(offset_ms) / 1000.0
                    await asyncio.sleep(max(0.0, deadline - self.monotonic_now()))
                    state = self.canonical_runtime.sessions.get(session_id) if self.canonical_runtime is not None else None
                    record = self.sessions.get(session_id)
                    if (state is None or record is None or record.cancellation_token != token
                            or state.mode not in {"exhibition_menu_tutorial_buttons", "exhibition_menu_replay_buttons"}):
                        return
                    next_sequence = self._menu_tutorial_sequences.get(session_id, 0) + 1
                    self._menu_tutorial_sequences[session_id] = next_sequence
                    await self._receive_canonical_event(Event(
                        "MenuTutorialStep", session_id, next_sequence, self.now(),
                        "canonical_menu_tutorial_clock",
                        {"button": button, "phase": phase, "token": token},
                        f"menu-tutorial:{session_id}:{sequence}:{button}:{phase}",
                        self.monotonic_now(),
                    ))
            except asyncio.CancelledError:
                return
            finally:
                if self._menu_tutorial_tasks.get(session_id) is asyncio.current_task():
                    self._menu_tutorial_tasks.pop(session_id, None)

        if not self.background_scheduler:
            return
        self._menu_tutorial_tasks[session_id] = asyncio.create_task(
            advance(), name=f"mishka-menu-tutorial-{session_id}"
        )

    def _cancel_welcome_timer(self, session_id: str) -> None:
        task = self._welcome_timers.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()

    async def _fire_exhibition_welcome(self, session_id: str, deadline: float) -> dict[str, Any] | None:
        state = self.canonical_runtime.sessions.get(session_id) if self.canonical_runtime is not None else None
        if state is None or state.welcome_deadline_monotonic != deadline or state.welcome_status != "pending":
            return None
        sequence = self._welcome_timer_sequences.get(session_id, 0) + 1
        self._welcome_timer_sequences[session_id] = sequence
        return await self._receive_canonical_event(Event(
            "WelcomeTimerFired", session_id, sequence, self.now(),
            "canonical_welcome_timer", {"deadline_monotonic": deadline},
            f"welcome:{session_id}:{deadline}", self.monotonic_now(),
        ))

    async def _wait_for_exhibition_welcome(self, session_id: str, deadline: float) -> None:
        try:
            # Production wakeup only; fake-clock tests call
            # flush_welcome_timers() to advance this canonical deadline
            # without waiting on wall time.
            await asyncio.sleep(max(0.0, deadline - self.monotonic_now()))
            await self._fire_exhibition_welcome(session_id, deadline)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("exhibition welcome timer failed", extra={"session_id": session_id})
        finally:
            if self._welcome_timers.get(session_id) is asyncio.current_task():
                self._welcome_timers.pop(session_id, None)

    def _schedule_exhibition_welcome(self, session_id: str, deadline: float) -> None:
        self._cancel_welcome_timer(session_id)
        if not self.background_scheduler:
            return
        self._welcome_timers[session_id] = asyncio.create_task(
            self._wait_for_exhibition_welcome(session_id, deadline),
            name=f"mishka-exhibition-welcome-{session_id}",
        )

    async def flush_welcome_timers(self) -> list[dict[str, Any]]:
        """Fire due exhibition timers; deterministic seam for fake-clock tests."""
        due = []
        sessions = self.canonical_runtime.sessions if self.canonical_runtime is not None else {}
        for session_id, state in tuple(sessions.items()):
            deadline = state.welcome_deadline_monotonic
            if state.welcome_status == "pending" and deadline is not None and self.monotonic_now() >= deadline:
                result = await self._fire_exhibition_welcome(session_id, deadline)
                if result is not None:
                    due.append(result)
        return due

    async def _receive_canonical_event(self, event: Event, transient_text: str | None = None) -> dict[str, Any]:
        """Bridge canonical decisions to application-owned effects.

        The contract engine never touches audio, capture, LEDs or providers.
        This boundary is the sole place where declared effects become calls.
        """
        profile = None
        tag_id = None
        if event.event_type == "RfidRemoved":
            self._canonical_profiles.pop(event.session_id, None)
            self._canonical_schedule_refs.pop(event.session_id, None)
        if event.event_type == "RfidPresented":
            try:
                profile = self.profiles.resolve(str(event.payload.get("tag_id", "")))
                tag_id = str(event.payload.get("tag_id", ""))
            except ProfileProviderError:
                return {"reason": "profile_unavailable", "outputs": [{"action": "safe_stop"}]}
        current = self.canonical_runtime.sessions.get(event.session_id)
        requested_version = event.payload.get("state_version") if isinstance(event.payload, dict) else None
        state_version = int(requested_version) if isinstance(requested_version, int) else (current.state_version if current else 0)
        previous_state = copy.deepcopy(current)
        event_type = "VoiceIntentReady" if event.event_type == "SpeechReceived" else event.event_type
        payload = dict(event.payload)
        accepted_monotonic = event.monotonic_timestamp if event.monotonic_timestamp is not None else self.monotonic_now()
        if event.event_type == "SpeechReceived":
            text = transient_text or ""
            safety = self.safety_classifier.classify(text)
            payload.update({"intent": classify_intent(text), "safety_severity": safety.severity,
                            "safety_reason": safety.reason})
        decision = self.canonical_runtime.handle(CanonicalEvent(
            event_type=event_type, session_id=event.session_id, sequence=event.sequence,
            state_version=state_version, source=event.source, payload=payload,
            correlation_id=event.correlation_id, timestamp=event.timestamp, monotonic_timestamp=accepted_monotonic,
        ), profile, transient_text=transient_text)
        snapshot, effects = decision["state"], decision["effects"]
        exhibition_reset = bool(decision.get("reset_session"))
        self.last_canonical_state = dict(snapshot)
        self.last_canonical_event = {"type": event_type, "source": event.source, "sequence": event.sequence, "session_id": event.session_id, "timestamp": event.timestamp, "monotonic_timestamp": accepted_monotonic, "correlation_id": event.correlation_id, "payload": dict(payload)}
        voice_open = snapshot.get("handset") == "up"
        authorization = self.transition_authorizer.authorize(
            previous_state.mode if previous_state is not None else "idle",
            str(snapshot.get("mode") or "idle"),
            event_type,
        )
        if not authorization["ok"]:
            if previous_state is None:
                self.canonical_runtime.sessions.pop(event.session_id, None)
                rejected_state = {"mode": "idle", "state_version": 0}
            else:
                self.canonical_runtime.sessions[event.session_id] = previous_state
                rejected_state = previous_state.snapshot()
            return {
                "reason": "transition_rejected",
                "state": rejected_state["mode"],
                "state_version": rejected_state["state_version"],
                "outputs": [{"type": "transition_rejected", "authorization": authorization}],
                "canonical_state": rejected_state,
            }
        record = self.sessions.get(event.session_id)
        if record is None:
            record = SessionRecord(event.session_id, self.installation_id, event.correlation_id or str(uuid.uuid4()))
            record.cancellation_token = self._session_epochs.get(event.session_id, 0)
        record.child_id = snapshot.get("profile_ref")
        record.anonymous = snapshot.get("profile_status") != "identified"
        record.handset_up = snapshot.get("handset") == "up"
        record.scenario_id, record.scenario_version = "canonical", "canonical_product/v1"
        record.current_node, record.last_sequence = snapshot.get("mode"), event.sequence
        # Physical RFID values never enter SessionRecord or analytics; only the protected resolver sees them.
        record.rfid_tag = None
        record.last_activity_at = self.now()
        record.state = Lifecycle.ENDED if snapshot.get("mode") in {"soft_end", "session_closed"} else Lifecycle.ACTIVE
        # Keep the same mutable effect objects visible while a playback adapter
        # reports its asynchronous lifecycle.
        self.last_canonical_effects = effects
        audio_failure: dict[str, Any] | None = None
        if not exhibition_reset:
            self.sessions.save(record)
        # Do not populate the profile cache before the first RFID is handled.
        # CanonicalRuntime uses the missing session/profile boundary to detect
        # the first read; pre-populating it made the first tag look repeated.
        if event.event_type == "RfidPresented":
            self._canonical_profiles[event.session_id] = profile
            self._canonical_schedule_refs[event.session_id] = (profile or {}).get("schedule_ref") or (profile or {}).get("profile_ref")
            if (decision["reason"] in {"rfid_identified", "unknown_tag"}
                    and not getattr(self.canonical_runtime, "exhibition_mode", False)):
                await self._run_readiness_wave_async(record, effects)
        for effect_index, effect in enumerate(effects):
            # Canonical effects use ``type``; the readiness wave is an
            # existing diagnostic compatibility projection and uses ``action``.
            # Never expose an opaque ``unknown`` effect id to the UI.
            effect_kind = effect.get("type") or effect.get("action") or "effect"
            effect.setdefault("effect_id", f"{event.session_id}:{snapshot.get('state_version', 0)}:{effect_index}:{effect_kind}")
            kind = effect.get("type")
            if kind in {"speak", "play_fixed_story", "resume_fixed_story", "play_placeholder",
                         "speak_dynamic", "support_question", "support_response"}:
                effect.setdefault("session_id", event.session_id)
                effect.setdefault("generation", int(self._session_epochs.get(event.session_id, 0)))
            if kind == "lighting_wave_intent":
                start_wave = getattr(self.led, "start_wave", None)
                start_age_wave = getattr(self.led, "start_age_selection_wave", None)
                if callable(start_wave):
                    pattern = str(effect.get("pattern") or "")
                    wave_groups = None
                    if pattern == "age_digits_wave" and callable(start_age_wave):
                        lighting = start_age_wave(now=accepted_monotonic)
                    elif pattern == "age_digits_wave":
                        wave_groups = EXHIBITION_DIGIT_WAVE_GROUPS
                        lighting = start_wave(now=accepted_monotonic, groups=wave_groups, hold_on_complete=True)
                    elif pattern == "menu_buttons_wave":
                        wave_groups = EXHIBITION_MENU_WAVE_GROUPS
                        lighting = start_wave(now=accepted_monotonic, groups=wave_groups, hold_on_complete=True)
                    elif pattern == "story_playback_wave":
                        wave_groups = STORY_PLAYBACK_WAVE_GROUPS
                        lighting = start_wave(
                            now=accepted_monotonic, groups=wave_groups, hold_on_complete=True,
                            loop=True, stage_duration_ms=int(effect.get("stage_ms") or 620),
                        )
                    elif pattern == "story_choice_wave":
                        wave_groups = STORY_CHOICE_WAVE_GROUPS
                        lighting = start_wave(
                            now=accepted_monotonic, groups=wave_groups, hold_on_complete=False,
                            base_elements=STORY_MENU_BASE_ELEMENTS,
                            stage_duration_ms=int(effect.get("stage_ms") or 420),
                        )
                    else:
                        lighting = start_wave(now=accepted_monotonic, hold_on_complete=bool(effect.get("hold_on_complete", True)))
                    effect["lighting_effect"] = dict(lighting)
                    effect["generation"] = lighting.get("generation")
                    effect["resolution"] = "canonical_exhibition_wave_started"
                    completion_event = str(effect.get("completion_event") or "")
                    if completion_event in {"ExhibitionOnboardingComplete", "ExhibitionMenuWaveComplete", "StoryChoiceWaveComplete"}:
                        delay_ms = int(effect.get("completion_delay_ms") or
                                       len(wave_groups or READINESS_WAVE_GROUPS) * int(effect.get("stage_ms") or EXHIBITION_WAVE_STAGE_MS))
                        self._schedule_lighting_wave_completion(
                            event.session_id, record.cancellation_token, completion_event, delay_ms,
                        )
                else:
                    effect["resolution"] = "canonical_wave_intent_recorded_without_effect_adapter"
            elif kind == "story_audio_pause":
                pause = getattr(self.audio, "pause", None)
                if callable(pause):
                    outcome = pause()
                    if inspect.isawaitable(outcome):
                        await outcome
                    effect["resolution"] = "story_audio_paused"
                else:
                    effect["resolution"] = "story_audio_pause_unavailable"
            elif kind == "story_audio_resume":
                story_id = str(effect.get("story_id") or "")
                manifest = getattr(self.canonical_runtime, "stories", None)
                verified = manifest.get_verified(story_id) if manifest else None
                placeholder = manifest.get_development_placeholder() if manifest and self.allow_development_placeholders else None
                asset = verified or placeholder
                checkpoint = effect.get("checkpoint") if isinstance(effect.get("checkpoint"), dict) else {}
                offset_ms = int(checkpoint.get("offset_ms") or 0)
                if asset and voice_open:
                    response_id = story_id if verified else f"placeholder:fixed_story:{story_id}"
                    play_from = getattr(self.audio, "play_from", None)
                    if callable(play_from):
                        play_from(response_id, str(asset["audio_file"]), record.cancellation_token, offset_ms)
                    else:
                        self.audio.play(response_id, str(asset["audio_file"]), record.cancellation_token)
                    effect["resolution"] = "story_audio_resumed"
                    effect["offset_ms"] = offset_ms
                else:
                    effect["resolution"] = "story_audio_resume_unavailable"
            elif kind == "lighting_wave_pause":
                engine = getattr(self.led, "exhibition_effects", None)
                pause_effect = getattr(engine, "pause_effect", None)
                effect["lighting_effect"] = dict(pause_effect(now=accepted_monotonic)) if callable(pause_effect) else {"effect": "unavailable"}
                effect["resolution"] = "story_wave_paused"
            elif kind == "lighting_wave_resume":
                engine = getattr(self.led, "exhibition_effects", None)
                resume_effect = getattr(engine, "resume_effect", None)
                effect["lighting_effect"] = dict(resume_effect(now=accepted_monotonic)) if callable(resume_effect) else {"effect": "unavailable"}
                effect["resolution"] = "story_wave_resumed"
            elif kind == "lighting_story_pause_flash":
                fill_ids = tuple(BUTTON_FILL_ELEMENTS.values())
                snapshot = getattr(getattr(self.led, "exhibition_effects", None), "last_frame", None) or {}
                active = set(snapshot.get("active_elements") or ())
                self.led.set_elements(tuple(LightingCommand(element_id, 0, 180) for element_id in fill_ids))
                self.led.set_elements(tuple(LightingCommand(element_id, 1, 180) for element_id in fill_ids))
                self.led.set_elements(tuple(LightingCommand(element_id, 1 if element_id in active else 0, 180) for element_id in fill_ids))
                effect["resolution"] = "story_pause_fill_flash"
            elif kind == "lighting_select_button":
                cancel_effects = getattr(self.led, "cancel_effects", None)
                if callable(cancel_effects):
                    cancel_effects(clear=False)
                button = int(effect.get("button", 0))
                element_id = BUTTON_FILL_ELEMENTS.get(button)
                if element_id is None:
                    effect["resolution"] = "invalid_button"
                else:
                    self.led.set_element(element_id, int(effect.get("state", 1)))
                    effect["resolution"] = "selected_button_fill_lit"
                    effect["element_id"] = element_id
            elif kind == "play_asset":
                asset_id = str(effect.get("content_id") or effect.get("asset_id") or "")
                asset_path = str(effect.get("asset_id") or "")
                if voice_open and asset_id and asset_path:
                    self.audio.play(asset_id, asset_path, record.cancellation_token)
                    effect["asset_status"] = "queued"
                    effect["audio_status"] = "queued"
                    effect["audio_queued"] = True
                    effect["resolution"] = "canonical_asset_queued"
                else:
                    effect["asset_status"] = "absent"
                    effect["audio_status"] = "unavailable"
                    effect["audio_queued"] = False
                    effect["resolution"] = "asset_requires_handset"
            elif kind == "exhibition_effects_cancelled":
                cancel_effects = getattr(self.led, "cancel_effects", None)
                effect["lighting_effect"] = dict(cancel_effects(clear=True)) if callable(cancel_effects) else {"effect": "unavailable"}
                effect["resolution"] = "exhibition_effects_cancelled"
            elif kind == "standby_breathing_started":
                start_breathing = getattr(self.led, "start_breathing", None)
                if callable(start_breathing):
                    lighting = start_breathing(now=accepted_monotonic)
                    effect["lighting_effect"] = dict(lighting)
                    effect["generation"] = lighting.get("generation")
                    effect["resolution"] = "canonical_standby_breathing_started"
                else:
                    effect["resolution"] = "standby_breathing_unavailable"
            elif kind == "exhibition_reset":
                effect["resolution"] = "canonical_exhibition_session_reset"
            elif kind == "welcome_timer_scheduled":
                effect["timer_status"] = "pending"
                self._schedule_exhibition_welcome(event.session_id, float(effect["deadline_monotonic"]))
            elif kind == "welcome_timer_cancelled":
                effect["timer_status"] = "cancelled"
                self._cancel_welcome_timer(event.session_id)
            elif kind == "audio_stop":
                record.cancellation_token += 1
                self._invalidate_audio_lease(event.session_id)
                self.audio.cancel(record.cancellation_token)
                if self.bounded_s2s is not None:
                    await self.bounded_s2s.cancel_session(event.session_id)
            elif kind == "adult_handoff_request":
                if self.adult_handoff is None:
                    effect["resolution"] = "adult_handoff_unavailable"
                    effect["fallback_phrase_id"] = "PHRASE_091"
                    asset = self.voice_manifest.resolve("PHRASE_091")
                    if asset and voice_open:
                        self.audio.play("PHRASE_091", str(asset["asset_path"]), record.cancellation_token)
                    continue
                handoff_id = f"{event.session_id}:{event.correlation_id or event.sequence}"
                request = {"incident_id": handoff_id, "handoff_id": handoff_id, "session_id": event.session_id, "profile_ref": snapshot.get("profile_ref"), "correlation_id": event.correlation_id, "severity": effect.get("severity"), "category": "urgent" if effect.get("severity") == "urgent" else "other", "safe_summary": "Ребёнок попросил взрослого", "location_device_id": self.installation_id, "created_at": self.now(), "delivery": "pending", "state_version": snapshot.get("state_version")}
                try:
                    delivery = self.adult_handoff.request(request)
                    if inspect.isawaitable(delivery):
                        delivery = await delivery
                    if not isinstance(delivery, dict):
                        raise TypeError("adult_handoff response must be an object")
                    effect["delivery"] = dict(delivery)
                    if not delivery.get("acknowledged"):
                        effect["resolution"] = "adult_handoff_pending"
                        continue
                    effect["resolution"] = "adult_handoff_simulated" if delivery.get("simulated") else "adult_handoff_acknowledged"
                    acknowledgement = await self._receive_canonical_event(Event("AdultHandoffAcknowledged", event.session_id, event.sequence, self.now(), "adult_handoff_simulator", {"handoff_id": handoff_id, "simulated": bool(delivery.get("simulated"))}, f"{event.correlation_id or handoff_id}:ack"))
                    effect["acknowledgement"] = {"reason": acknowledgement.get("reason"), "simulated": bool(delivery.get("simulated"))}
                    effects.extend(acknowledgement.get("outputs", []))
                except Exception as exc:
                    effect["resolution"] = "adult_handoff_delivery_failed"
                    effect["fallback_phrase_id"] = "PHRASE_091"
                    asset = self.voice_manifest.resolve("PHRASE_091")
                    if asset and voice_open:
                        self.audio.play("PHRASE_091", str(asset["asset_path"]), record.cancellation_token)
                    effect["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"

            elif kind == "lighting":
                self.led.clear_all()
                for element_id in effect.get("elements", ()):
                    self.led.set_element(int(element_id), int(effect.get("state", 0)))
            elif kind == "lighting_preset":
                # Replace the complete logical frame in one ordered batch.
                # Sending clear_all() first caused a visible off-to-on pulse
                # on the large weather pictogram (element 17).
                cancel_effects = getattr(self.led, "cancel_effects", None)
                if callable(cancel_effects):
                    cancel_effects(clear=False)
                preset = str(effect.get("preset", ""))
                if preset == "EXHIBITION_MENU_TUTORIAL":
                    buttons = effect.get("buttons", ())
                    commands = tuple((element_id, 1) for button in buttons for element_id in BUTTON_ELEMENTS.get(int(button), ()))
                elif preset == "EXHIBITION_MENU_TUTORIAL_DIGIT":
                    buttons = effect.get("buttons", ())
                    digit_button = int(effect.get("digit_button", 0))
                    digit_element = BUTTON_DIGIT_ELEMENTS.get(digit_button, (None, None))[1]
                    commands = tuple(
                        (element_id, 1)
                        for button in buttons
                        for element_id in BUTTON_ELEMENTS.get(int(button), ())
                    )
                    if digit_element is not None:
                        commands += ((digit_element, 1),)
                else:
                    commands = LED_PRESETS.get(preset, ())
                if preset == "TARGET":
                    commands = tuple((element_id, 1) for element_id in BUTTON_ELEMENTS.get(int(effect.get("target_button", 0)), ()))
                desired = {element_id: 0 for element_id in LIGHTING_ELEMENT_IDS}
                for element_id, state in commands:
                    desired[int(element_id)] = int(state)
                self.led.set_elements(tuple(
                    LightingCommand(element_id, state, 0)
                    for element_id, state in desired.items()
                ))
            elif kind == "resume_fixed_story":
                story_id = str(effect.get("story_id") or "")
                manifest = getattr(self.canonical_runtime, "stories", None)
                verified = manifest.get_verified(story_id) if manifest else None
                placeholder = manifest.get_development_placeholder() if manifest and self.allow_development_placeholders else None
                asset = verified or placeholder
                if asset and voice_open:
                    response_id = story_id if verified else f"placeholder:fixed_story:{story_id}"
                    self.audio.play(response_id, str(asset["audio_file"]), record.cancellation_token)
                    _set_audio_effect_status(effect, asset_status="approved" if verified else "placeholder", queued=True)
                    self._register_audio_lease(event.session_id, response_id, record.cancellation_token,
                                               str(effect.get("effect_id")), str(effect.get("playback_lease") or ""))
                    effect["resolution"] = "resumed_verified_story" if verified else "resumed_development_placeholder"
                else:
                    _set_audio_effect_status(effect, asset_status="absent", queued=False, resolution="resume_requires_verified_story_asset")
            elif kind == "play_fixed_story":
                # The runtime has already verified rights and both SHA-256s.
                if voice_open:
                    response_id = str(effect["story_id"])
                    self.audio.play(response_id, str(effect["asset"]), record.cancellation_token)
                    _set_audio_effect_status(effect, asset_status="approved", queued=True)
                    self._register_audio_lease(event.session_id, response_id, record.cancellation_token,
                                               str(effect.get("effect_id")), str(effect.get("playback_lease") or ""))
                else:
                    _set_audio_effect_status(effect, asset_status="absent", queued=False, resolution="story_audio_requires_handset")
            elif kind == "play_placeholder":
                if voice_open:
                    response_id = f"placeholder:{effect['content_kind']}:{effect['story_id']}"
                    self.audio.play(response_id, str(effect["asset"]), record.cancellation_token)
                    _set_audio_effect_status(effect, asset_status="placeholder", queued=True)
                    self._register_audio_lease(event.session_id, response_id, record.cancellation_token,
                                               str(effect.get("effect_id")), str(effect.get("playback_lease") or ""))
                else:
                    _set_audio_effect_status(effect, asset_status="placeholder", queued=False, resolution="placeholder_audio_requires_handset")
                effect["resolution"] = "development_placeholder_no_rights_or_content"
            elif kind == "speak_dynamic":
                text = str(effect.get("text") or "").strip()
                if voice_open and text and callable(getattr(self.audio, "play_text", None)):
                    self.audio.play_text("dynamic", text, record.cancellation_token)
                    _set_audio_effect_status(effect, asset_status="approved", queued=True)
                else:
                    _set_audio_effect_status(effect, asset_status="absent", queued=False, resolution="dynamic_audio_unavailable_visual_only")
            elif kind == "speak":
                # Missing recorded speech never falls through to dynamic TTS.
                # A device/adapter failure is terminal for this declared effect:
                # it is diagnosed and never retried by the exhibition timer.
                phrase_id = str(effect.get("phrase_id", ""))
                content_hash = str(effect.get("content_hash", ""))
                effect["content_id"] = phrase_id
                diagnosis = self.voice_manifest.diagnose(phrase_id, content_hash)
                effect["asset_ref"] = diagnosis.get("asset_ref")
                asset = self.voice_manifest.resolve(phrase_id, content_hash)
                effect["session_id"] = event.session_id
                effect["playback_id"] = f"{event.session_id}:{record.cancellation_token}:{effect.get('effect_id', phrase_id)}"
                if asset and voice_open:
                    try:
                        self.audio.play(phrase_id, str(asset["asset_path"]), record.cancellation_token)
                    except Exception as exc:
                        _set_audio_effect_status(effect, asset_status=str(asset.get("status") or "approved"), queued=False, resolution="audio_dispatch_failed")
                        effect["audio_failure_type"] = type(exc).__name__
                        self.record_audio_diagnostic("audio_dispatch_failed", phrase_id=phrase_id,
                                                     content_id=phrase_id, asset_ref=effect.get("asset_ref"),
                                                     exception_type=type(exc).__name__, detail=str(exc)[:200],
                                                     session_id=event.session_id)
                        audio_failure = {"response_id": phrase_id, "token": record.cancellation_token,
                                         "error_code": "audio_dispatch_failed", "detail": type(exc).__name__}
                        logger.exception("canonical phrase dispatch failed", extra={"phrase_id": phrase_id, "session_id": event.session_id})
                    else:
                        _set_audio_effect_status(effect, asset_status=str(asset.get("status") or "approved"), queued=True)
                        effect["generation"] = int(self._session_epochs.get(event.session_id, 0))
                        self._register_audio_lease(event.session_id, phrase_id, record.cancellation_token,
                                                   str(effect.get("playback_id")), str(effect.get("playback_lease") or ""))
                        if phrase_id == "PHRASE_008" and snapshot.get("mode") in {
                            "exhibition_menu_tutorial_buttons", "exhibition_menu_replay_buttons",
                        }:
                            self._schedule_menu_tutorial_steps(
                                event.session_id, record.cancellation_token,
                                int(asset.get("duration_ms") or 18406),
                            )
                else:
                    _set_audio_effect_status(effect, asset_status="absent", queued=False, resolution="recorded_phrase_unavailable_visual_only")
                    self.record_audio_diagnostic("recorded_phrase_unavailable", phrase_id=phrase_id,
                                                content_id=phrase_id, asset_ref=diagnosis.get("asset_ref"),
                                                content_status=diagnosis.get("status"),
                                                content_code=diagnosis.get("code"), session_id=event.session_id)
                    audio_failure = {"response_id": phrase_id, "token": record.cancellation_token,
                                     "error_code": str(diagnosis.get("code") or "content_unavailable")[:80]}
            elif kind in {"support_question", "support_response"}:
                if kind == "support_question":
                    phrase = {"pain_or_discomfort": "PHRASE_084", "fear": "PHRASE_085"}.get(str(effect.get("question")), "PHRASE_094")
                else:
                    phrase = "PHRASE_071" if str(effect.get("kind")) == "concern" else "PHRASE_094"
                effect["phrase_id"] = phrase
                diagnosis = self.voice_manifest.diagnose(phrase)
                asset = self.voice_manifest.resolve(phrase)
                if asset and voice_open:
                    try:
                        self.audio.play(phrase, str(asset["asset_path"]), record.cancellation_token)
                    except Exception as exc:
                        _set_audio_effect_status(effect, asset_status=str(asset.get("status") or "approved"), queued=False, resolution="audio_dispatch_failed")
                        self.record_audio_diagnostic("audio_dispatch_failed", phrase_id=phrase, content_id=phrase,
                                                     asset_ref=diagnosis.get("asset_ref"), exception_type=type(exc).__name__,
                                                     detail=str(exc)[:200], session_id=event.session_id)
                        audio_failure = {"response_id": phrase, "token": record.cancellation_token,
                                         "error_code": "audio_dispatch_failed", "detail": type(exc).__name__}
                    else:
                        _set_audio_effect_status(effect, asset_status=str(asset.get("status") or "approved"), queued=True)
                        self._register_audio_lease(event.session_id, phrase, record.cancellation_token,
                                                   str(effect.get("effect_id")), str(effect.get("playback_lease") or ""))
                else:
                    _set_audio_effect_status(effect, asset_status="absent", queued=False, resolution="recorded_phrase_unavailable_visual_only")
                    self.record_audio_diagnostic("recorded_phrase_unavailable", phrase_id=phrase, content_id=phrase,
                                                asset_ref=diagnosis.get("asset_ref"), content_status=diagnosis.get("status"),
                                                content_code=diagnosis.get("code"), session_id=event.session_id)
                    audio_failure = {"response_id": phrase, "token": record.cancellation_token,
                                     "error_code": str(diagnosis.get("code") or "content_unavailable")[:80]}
            elif kind == "adult_handoff_offer":
                effect["phrase_id"] = "PHRASE_086"
                effect["resolution"] = "offer_requires_explicit_button_or_speech"
            elif kind == "bounded_dialogue_turn":
                effect["response_policy"] = "bounded_validated_dialogue"
                if self.bounded_s2s is None or not voice_open or not (transient_text or "").strip():
                    effect["resolution"] = "bounded_dialogue_unavailable_visual_only"
                else:
                    try:
                        turn = await self.bounded_s2s.run_text_turn(
                            str(transient_text), self._canonical_audio_context(event.session_id)
                        )
                        effect["resolution"] = "bounded_dialogue_completed" if turn.fallback_reason is None else "bounded_dialogue_fallback"
                        effect["fallback_reason"] = turn.fallback_reason
                        effect["chunks"] = list(turn.chunks)
                    except Exception as exc:
                        effect["resolution"] = "bounded_dialogue_fallback"
                        effect["fallback_reason"] = type(exc).__name__
            elif kind == "generate_personal_story":
                brief = effect.get("brief") if isinstance(effect.get("brief"), dict) else {}
                age_band = brief.get("age_band", "3-5")
                duration = {"1-3": 50, "3-5": 70, "5-7": 100}.get(str(age_band), 70)
                candidate = {"title": "Маленькое доброе путешествие", "text": "Герой нашёл спокойную дорогу и вернулся домой.", "age_band": age_band, "estimated_duration_sec": duration, "safety_tags": ["safe_resolved"], "ending_state": "safe_resolved"}
                valid, reason = validate_generated_story(candidate, brief)
                effect["candidate"] = candidate
                effect["validated"] = valid
                effect["resolution"] = "validated_development_story" if valid else "story_validation_failed"
                if reason:
                    effect["validation_reason"] = reason
                if valid:
                    effect["pipeline_transition"] = await self._receive_canonical_event(Event("VoiceIntentReady", event.session_id, event.sequence, self.now(), "story_pipeline_generated", {"intent": "generated", "safety_severity": "none"}, f"{event.correlation_id or event.session_id}:story-generated"))
                    effects.extend(effect["pipeline_transition"].get("outputs", []))
                else:
                    effect["fallback_phrase_id"] = "PHRASE_080"
                    effect["pipeline_transition"] = await self._receive_canonical_event(Event("VoiceIntentReady", event.session_id, event.sequence, self.now(), "story_pipeline_validator", {"intent": "validation_failed", "safety_severity": "none"}, f"{event.correlation_id or event.session_id}:story-rejected"))
                    effects.extend(effect["pipeline_transition"].get("outputs", []))
            elif kind == "validate_personal_story":
                effect["resolution"] = "validator_boundary_acknowledged"
                effect["pipeline_transition"] = await self._receive_canonical_event(Event("VoiceIntentReady", event.session_id, event.sequence, self.now(), "story_pipeline_validated", {"intent": "validated", "safety_severity": "none"}, f"{event.correlation_id or event.session_id}:story-validated"))
                effects.extend(effect["pipeline_transition"].get("outputs", []))
            elif kind == "play_personal_story":
                effect["resolution"] = "development_only_story_visual_placeholder"
                effect["development_only"] = True
            elif kind in {"capture_open", "capture_close", "audio_stop"}:
                effect["resolution"] = "application_boundary_acknowledged"
            elif kind == "game_retry":
                self.led.clear_all()
                target = int(effect.get("target_button") or 0)
                preset = "HINT" if int(effect.get("hint_stage") or 0) >= 2 else "RETRY"
                for element_id, value in LED_PRESETS.get(preset, ()):
                    self.led.set_element(element_id, value)
                for element_id in BUTTON_ELEMENTS.get(target, ()):
                    self.led.set_element(element_id, 1)
                effect["lighting_preset"] = preset
                effect.setdefault("resolution", "declarative_effect_emitted")
            elif kind == "game_level":
                self.led.clear_all()
                target = int(effect.get("target_button") or 0)
                if target:
                    for element_id in BUTTON_ELEMENTS.get(target, ()):
                        self.led.set_element(element_id, 1)
                effect.setdefault("resolution", "declarative_effect_emitted")
            elif kind in {"exhibition_menu_selected", "menu_tutorial_complete", "exhibition_menu_tutorial_step"}:
                effect.setdefault("resolution", "canonical_exhibition_menu_boundary")
            elif kind in {"content_blocked", "privacy_blocked", "offer_adult", "visual", "clear_private_display", "game_continue_visual", "adult_incident_closed", "game_exit_confirmation", "story_speech_analysis", "hardware_reconnected", "button_edge_ack", "mood_report"}:
                effect.setdefault("resolution", "declarative_effect_emitted")
            elif kind == "typed_info_request":
                try:
                    profile = self._canonical_profiles.get(event.session_id)
                    info = await TypedInfoRenderer(self.schedule_weather, profile, self._canonical_schedule_refs.get(event.session_id)).render(
                        str(effect.get("kind")), allow_demo=getattr(self.settings, "profile", "") == "mock")
                    effect["typed_data"] = info
                except (ScheduleWeatherProviderError, ValueError) as exc:
                    effect["resolution"] = "typed_information_unavailable"
                    effect["error"] = str(exc)
        if audio_failure is not None and event.event_type not in {"AudioFailed", "AudioCompleted"}:
            failure = await self._receive_canonical_event(Event(
                "AudioFailed", event.session_id, event.sequence + 1, self.now(),
                "canonical_audio_failure", audio_failure,
                f"{event.correlation_id or event.session_id}:audio-failed", self.monotonic_now(),
            ))
            failure["audio_origin"] = dict(audio_failure)
            # Preserve the declarative effect that failed alongside the
            # canonical failure effects. Otherwise callers only see the
            # generic safe-stop projection and lose the phrase/asset/error
            # evidence that explains why playback was not started.
            failure_effects = list(failure.get("outputs") or [])
            failure["outputs"] = [dict(item) for item in effects] + failure_effects
            self.last_canonical_effects = [dict(item) for item in failure["outputs"]]
            return failure
        reset_cleanup = None
        if exhibition_reset:
            # Bump before cancelling providers so late audio/STT/model work
            # cannot be accepted by the next physical session.
            self._session_epochs[event.session_id] = self._session_epochs.get(event.session_id, 0) + 1
            reset_cleanup = await self._cleanup_session_resources(event.session_id)
            self.canonical_runtime.reset(event.session_id)
            self._canonical_profiles.pop(event.session_id, None)
            self._canonical_schedule_refs.pop(event.session_id, None)
            self.sessions.delete(event.session_id)

        # Persist the fully enriched effects for the canonical WebPanel/state
        # projection.  Audio status and effect IDs are applied at the
        # application boundary after LangGraph authorization.
        self.last_canonical_effects = [dict(item) for item in effects]
        self.canonical_trace.append({
            "input": payload.get("_input_type") or event.event_type,
            "normalized_event": payload.get("_normalized_event") or ("handset_up" if event_type == "HandsetLifted" else "handset_down" if event_type == "HandsetPlaced" else event_type),
            "canonical_event": event_type,
            "state_before": previous_state.mode if previous_state is not None else "idle",
            "state_after": snapshot.get("mode"),
            "effects": [str(item.get("type") or item.get("action") or "effect") for item in effects],
            "monotonic_timestamp": accepted_monotonic,
            "timer_started": snapshot.get("welcome_t0_monotonic"),
            "timer_deadline": snapshot.get("welcome_deadline_monotonic"),
            "audio": next((item.get("phrase_id") for item in effects if item.get("type") == "speak"), None),
        })
        if not exhibition_reset:
            self.sessions.save(record)
        published_event = event
        if event.event_type == "RfidPresented":
            # The physical tag is resolved only inside the protected profile
            # boundary; it must never enter the common event stream.
            published_event = Event(
                event.event_type, event.session_id, event.sequence, event.timestamp,
                event.source, {
                    "profile_resolved": snapshot.get("profile_status") == "identified",
                    "unknown_tag": snapshot.get("profile_status") != "identified",
                },
                event.correlation_id,
            )
        self.publisher.publish(published_event)
        final_state = dict(snapshot) if exhibition_reset else self.canonical_runtime.sessions.get(event.session_id).snapshot()
        self.last_canonical_state = dict(final_state)
        hook = self.state_update_hook
        if callable(hook):
            try:
                update = hook(event.session_id, {"reason": decision["reason"], "state": final_state.get("mode"), "canonical_state": final_state, "outputs": effects})
                if inspect.isawaitable(update):
                    await update
            except Exception:
                logger.exception("canonical state update hook failed", extra={"session_id": event.session_id})
        return {"reason": decision["reason"], "state": final_state.get("mode"),
                "state_version": final_state.get("state_version"), "outputs": effects,
                "canonical_state": final_state,
                "canonical_trace": list(self.canonical_trace),
                "canonical_diagnostics": list(self.canonical_diagnostics),
                "reset_cleanup": reset_cleanup,
                "pipeline": ["ingress", "normalize_event", "session_guard", "safety_precheck", "route_mode", "policy_validate", "emit_effects", "persist_snapshot", "await_event"],
                "guards": decision.get("guards", {})}
        """Start an anonymous visual session from a pre-auth physical button."""
    async def receive_async(self, event: Event, transient_text: str | None = None) -> dict[str, Any]:
        """Ingress for the single canonical runtime and LangGraph boundary."""
        await self.expire_idle_sessions()
        if self.canonical_runtime is None:
            return {"session_id": event.session_id, "state": Lifecycle.IDLE.value,
                    "node": None, "reason": "canonical_runtime_required",
                    "outputs": [{"action": "safe_stop"}],
                    "correlation_id": event.correlation_id}
        if (event.event_type == "ButtonPressed"
                and getattr(self.canonical_runtime, "exhibition_mode", False)):
            active = self.canonical_runtime.sessions.get(event.session_id)
            if active is None or active.handset != "up":
                return {"session_id": event.session_id, "state": "idle",
                        "reason": "exhibition_age_not_ready", "outputs": [],
                        "canonical_state": {"mode": "idle", "handset": "down"}}
        # In exhibition mode a down edge while no physical session exists is
        # already the neutral state. Do not materialize a new repository or
        # canonical session merely to acknowledge a repeated SpacePressed.
        callback_lease = None
        if event.event_type in {"AudioCompleted", "AudioFailed"}:
            callback_lease = self._active_audio.get(event.session_id)
            if not self._audio_callback_is_current(event):
                state = self.canonical_runtime.sessions.get(event.session_id)
                return {"session_id": event.session_id, "state": state.mode if state else "idle",
                        "reason": "stale_exhibition_audio_completion"
                        if getattr(self.canonical_runtime, "exhibition_mode", False)
                        else "stale_audio_callback",
                        "outputs": [], "canonical_state": state.snapshot()
                        if state else {"mode": "idle", "handset": "down"}}
        if (event.event_type == "AudioCompleted"
                and getattr(self.canonical_runtime, "exhibition_mode", False)):
            active = self.canonical_runtime.sessions.get(event.session_id)
            record = self.sessions.get(event.session_id)
            token = event.payload.get("token")
            try:
                token_matches = token is not None and int(token) == int(record.cancellation_token) if record is not None else False
            except (TypeError, ValueError, OverflowError):
                token_matches = False
            if active is None or record is None or not token_matches:
                return {"session_id": event.session_id, "state": active.mode if active else "idle",
                        "reason": "stale_exhibition_audio_completion", "outputs": [],
                        "canonical_state": active.snapshot() if active else {"mode": "idle", "handset": "down"}}
        if (event.event_type == "MenuTutorialStep"
                and getattr(self.canonical_runtime, "exhibition_mode", False)):
            active = self.canonical_runtime.sessions.get(event.session_id)
            record = self.sessions.get(event.session_id)
            token = event.payload.get("token")
            try:
                token_matches = token is not None and int(token) == int(record.cancellation_token) if record is not None else False
            except (TypeError, ValueError, OverflowError):
                token_matches = False
            if active is None or record is None or not token_matches:
                return {"session_id": event.session_id, "state": active.mode if active else "idle",
                        "reason": "stale_exhibition_menu_tutorial_step", "outputs": [],
                        "canonical_state": active.snapshot() if active else {"mode": "idle", "handset": "down"}}
        if (event.event_type == "ExhibitionMenuWaveComplete"
                and getattr(self.canonical_runtime, "exhibition_mode", False)):
            active = self.canonical_runtime.sessions.get(event.session_id)
            record = self.sessions.get(event.session_id)
            token = event.payload.get("token")
            try:
                token_matches = token is not None and int(token) == int(record.cancellation_token) if record is not None else False
            except (TypeError, ValueError, OverflowError):
                token_matches = False
            if active is None or record is None or not token_matches:
                return {"session_id": event.session_id, "state": active.mode if active else "idle",
                        "reason": "stale_exhibition_menu_wave", "outputs": [],
                        "canonical_state": active.snapshot() if active else {"mode": "idle", "handset": "down"}}

        if (event.event_type == "HandsetPlaced"
                and getattr(self.canonical_runtime, "exhibition_mode", False)
                and event.session_id not in self.canonical_runtime.sessions
                and self.sessions.get(event.session_id) is None):
            return {"session_id": event.session_id, "state": "idle",
                    "node": None, "reason": "handset_already_placed",
                    "outputs": [], "canonical_state": {"mode": "idle", "handset": "down"},
                    "correlation_id": event.correlation_id}
        result = await self._receive_canonical_event(event, transient_text)
        if event.event_type in {"AudioCompleted", "AudioFailed"} and callback_lease is not None:
            self._cancel_audio_timeout(event.session_id)
            if self._active_audio.get(event.session_id) is callback_lease:
                callback_lease["status"] = "finished" if event.event_type == "AudioCompleted" else "failed"
                self._active_audio.pop(event.session_id, None)
        if (event.event_type in {"SpeechReceived", "VoiceIntentReady"}
                and result.get("reason") == "exhibition_onboarding_completed"
                and getattr(self.canonical_runtime, "exhibition_mode", False)):
            current = self.canonical_runtime.sessions.get(event.session_id)
            handoff = await self._receive_canonical_event(Event(
                "ExhibitionOnboardingComplete", event.session_id, event.sequence + 1,
                self.now(), "canonical_gr03_handoff",
                {"state_version": current.state_version if current is not None else 0},
                f"{event.correlation_id or event.session_id}:exhibition-onboarding-complete",
                self.monotonic_now(),
            ))
            handoff["gr03_completion"] = {
                "reason": result.get("reason"), "state": result.get("canonical_state"),
                "outputs": result.get("outputs", []),
            }
            handoff["handoff_event"] = "ExhibitionOnboardingComplete"
            return handoff
        return result

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

    def _canonical_audio_context(self, session_id: str):
        # LocalS2SPipeline needs a bounded context object for VAD/STT
        # bookkeeping. Canonical voice turns never pass through its LLM/TTS
        # conversation path; the STT transcript is re-ingressed as a
        # canonical SpeechReceived event below.
        from .s2s import ConversationContext, ProfileContext
        state = self.canonical_runtime.sessions.get(session_id)
        profile = self._canonical_profiles.get(session_id)
        state_snapshot = state.snapshot() if state is not None else {"mode": "bear_dialogue", "handset": "up"}
        # Onboarding identity is session-scoped RAM, not a persisted profile.
        # Keep it available to the bounded voice context after the anonymous
        # name/age exchange while leaving the public snapshot redacted.
        private_profile = dict(profile or {})
        if state is not None:
            if state.display_name:
                private_profile.setdefault("display_name", state.display_name)
            private_profile["age_band"] = state.age_band
            if state.age_years is not None:
                private_profile["age"] = state.age_years
            state_snapshot["onboarding_name_collected"] = bool(state.onboarding_name_collected)
            state_snapshot["dialogue_memory"] = {
                "status": state.dialogue_memory.status,
                "turns": list(state.dialogue_memory.bounded_turns),
            }
        prompt = build_system_prompt(profile=private_profile or None, state=state_snapshot)
        return ConversationContext(session_id, "canonical_product", str(state_snapshot.get("mode") or "bear_dialogue"), ("greetings", "clinic", "weather", "games", "stories", "feelings", "questions"), ("diagnosis", "medicine", "secret", "personal_data"), 5, 120, 2, profile=ProfileContext.from_profile(private_profile or None), system_prompt=prompt)

    async def _complete_canonical_audio_turn(self, session_id: str, sequence: int, pending: asyncio.Task[Any], epoch: int, correlation_id: str) -> None:
        try:
            audio_result = await pending
        except asyncio.CancelledError:
            return
        session = self.sessions.get(session_id)
        if session is None or session.state == Lifecycle.ENDED or self._session_epochs.get(session_id, 0) != epoch or session.correlation_id != correlation_id:
            return
        if audio_result.stt is None or not audio_result.stt.text.strip():
            return
        payload = await self._receive_canonical_event(
            Event("SpeechReceived", session_id, sequence, self.now(), "uart_audio",
                  {"length": len(audio_result.stt.text)},
                  f"{correlation_id}:audio:{sequence}"),
            audio_result.stt.text,
        )
        await self._invoke_audio_update_hook(session_id, payload, generation=epoch, correlation_id=correlation_id)

    async def _receive_canonical_audio(self, session_id: str, samples: tuple[float, ...], sequence: int, *, wait_for_turn: bool) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if session is None:
            return {"session_id": session_id, "state": Lifecycle.IDLE.value, "outputs": [], "reason": "audio_session_required", "audio_status": "ignored", "pending": False}
        if self.s2s_audio is None:
            return {**self._result(session, [], "audio_unavailable"), "audio_status": "ignored", "barge_in": False, "pending": False}
        if session.state == Lifecycle.ENDED or sequence <= session.last_sequence:
            return {**self._result(session, [], "late_or_duplicate_audio"), "pending": False}
        if session.handset_up is not True:
            return {**self._result(session, [], "audio_not_listening"), "audio_status": "handset_required", "pending": False, "barge_in": False}
        voice_open = True
        session.last_sequence, session.last_activity_at = sequence, self.now()
        context = self._canonical_audio_context(session_id)
        from .s2s import TurnResult
        async def no_llm_turn(_text: str, _generation: int):
            return TurnResult((), (), False, None)
        audio_result = await self.s2s_audio.feed_audio(session_id, samples, context, wait_for_turn=wait_for_turn, turn_handler=no_llm_turn)
        barge_outputs = [{"action": "barge_in", "status": "previous_output_cancelled"}] if audio_result.barge_in else []
        if audio_result.pending is not None:
            epoch = self._session_epochs.get(session_id, 0)
            completion = asyncio.create_task(self._complete_canonical_audio_turn(session_id, sequence, audio_result.pending, epoch, session.correlation_id), name=f"canonical-audio-{session_id}")
            owned = self._audio_completions.setdefault(session_id, set()); owned.add(completion)
            self._audio_completion_started_at[session_id] = self.monotonic_now()
            completion.add_done_callback(self._audio_completion_callback(session_id))
            self.sessions.save(session)
            return {**self._result(session, barge_outputs, "audio_processing"), "audio_status": "processing", "pending": True, "barge_in": audio_result.barge_in}
        if audio_result.stt is not None and audio_result.stt.text.strip():
            result = await self._receive_canonical_event(Event("SpeechReceived", session_id, sequence, self.now(), "uart_audio", {"length": len(audio_result.stt.text)}, f"{session.correlation_id}:audio:{sequence}"), audio_result.stt.text)
            result["audio_status"] = audio_result.ingress_status; result["pending"] = False; result["barge_in"] = audio_result.barge_in
            return result
        if audio_result.turn is not None and audio_result.turn.fallback_reason:
            asset = self.voice_manifest.resolve("PHRASE_091")
            if asset and voice_open: self.audio.play("PHRASE_091", str(asset["asset_path"]), session.cancellation_token)
            return {**self._result(session, barge_outputs + [{"action": "audio_retry", "reason": audio_result.turn.fallback_reason}], "audio_retry"), "audio_status": audio_result.ingress_status, "pending": False, "barge_in": audio_result.barge_in}
        self.sessions.save(session)
        return {**self._result(session, barge_outputs, audio_result.ingress_status), "audio_status": audio_result.ingress_status, "pending": False, "barge_in": audio_result.barge_in}

    async def receive_audio_async(self, session_id: str, samples: tuple[float, ...], sequence: int, *, wait_for_turn: bool = True) -> dict[str, Any]:
        return await self._receive_canonical_audio(session_id, samples, sequence, wait_for_turn=wait_for_turn)

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

    def start_session(self, session_id: str, correlation_id: str | None = None) -> dict[str, Any]:
        current = self.sessions.get(session_id)
        if current is not None:
            return self._result(current, [], "existing_session")
        active = self.sessions.active_for_installation(self.installation_id)
        session = SessionRecord(session_id, self.installation_id, correlation_id or str(uuid.uuid4()), scenario_id="canonical", started_at=self.now(), last_activity_at=self.now())
        if active is not None:
            return self._result(session, [], "installation_busy")
        self.sessions.save(session)
        return self._result(session, [], "session_created")

    async def reset_session(self, session_id: str) -> dict[str, Any]:
        self._cancel_welcome_timer(session_id)
        self._welcome_timer_sequences.pop(session_id, None)
        self._cancel_menu_tutorial(session_id)
        self._menu_tutorial_sequences.pop(session_id, None)
        self._cancel_lighting_wave(session_id)
        self._session_epochs[session_id] = self._session_epochs.get(session_id, 0) + 1
        self._invalidate_audio_lease(session_id)
        cleanup = await self._cancel_audio_completions(session_id, operation="reset")
        if self.bounded_s2s is not None:
            await self.bounded_s2s.cancel_session(session_id)
        if self.s2s_audio is not None:
            await self.s2s_audio.cancel_session(session_id)
        self.conversation_contexts.pop(session_id, None)
        if self.canonical_runtime is not None:
            self.canonical_runtime.reset(session_id)
            self._canonical_profiles.pop(session_id, None)
            self._canonical_schedule_refs.pop(session_id, None)
        # Reset is an application-level boundary, not only a repository
        # operation. Keep fake, PTY and physical LED implementations aligned
        # even when reset is invoked without the Stand wrapper.
        clear_all = getattr(self.led, "clear_all", None)
        if callable(clear_all):
            clear_all()
        self._last_audio_ack_at.pop(session_id, None)
        self.sessions.delete(session_id)
        # Reset is also an observability boundary: do not leave the previous
        # dialogue node visible in health/panel snapshots after a successful
        # installation reset.
        self.last_canonical_state = {
            "session_id": session_id,
            "mode": "idle",
            "handset": "down",
            "expected_inputs": ["handset_up"],
        }
        self.last_canonical_event = {}
        self.last_canonical_effects = []
        return {"session_id":session_id,"state":"RESET","reset":True,"cleanup":cleanup}

    def receive(self, event: Event, transient_text: str | None = None, *, trigger_readiness_wave: bool = True) -> dict[str, Any]:
        raise RuntimeError("canonical runtime accepts events through receive_async")

    def _end(self, session, reason, sequence, emit=True):
        if session.state == Lifecycle.ENDED:
            return []
        session.state = Lifecycle.ENDED
        session.termination_reason = reason
        session.cancellation_token += 1
        self.audio.cancel(session.cancellation_token)
        # Session termination is also a device-state transition. Do not use
        # the legacy pattern projection here: clear canonical 19-element
        # lighting directly.
        clear_all = getattr(self.led, "clear_all", None)
        if callable(clear_all):
            clear_all()
        self.publisher.publish(Event("SessionEnded", session.session_id, sequence, self.now(), "application", {"reason": reason}, session.correlation_id))
        return [{"type": "lighting", "elements": list(LIGHTING_ELEMENT_IDS), "state": 0}, {"action": "session_ended", "reason": reason}]

    def _reconcile_canonical_lighting(self, effects: list[dict]) -> None:
        """Restore the canonical effect after the transient readiness wave.

        The wave is a physical presentation layer and intentionally ends with
        its last group lit. The device snapshot must nevertheless represent
        the canonical state produced by the state machine, otherwise the UI
        and the next input observe a stale all-on projection.
        """
        clear_all = getattr(self.led, "clear_all", None)
        if not callable(clear_all):
            return
        clear_all()
        for effect in effects:
            kind = effect.get("type")
            if kind == "lighting":
                clear_all()
                for element_id in effect.get("elements", ()):
                    self.led.set_element(int(element_id), int(effect.get("state", 0)))
            elif kind == "lighting_preset":
                clear_all()
                preset = str(effect.get("preset", ""))
                commands = LED_PRESETS.get(preset, ())
                if preset == "TARGET":
                    commands = tuple(
                        (element_id, 1)
                        for element_id in BUTTON_ELEMENTS.get(int(effect.get("target_button", 0)), ())
                    )
                for element_id, state in commands:
                    self.led.set_element(element_id, state)

    async def _run_readiness_wave_async(self, session, outputs: list[dict]) -> None:
        if session.readiness_wave_completed:
            return
        wave = getattr(self.led, "readiness_wave_async", None)
        if not callable(wave):
            self._run_readiness_wave(session, outputs)
            return
        session_id = session.session_id
        await wave(
            READINESS_WAVE_DURATION_MS,
            cancelled=lambda: self.sessions.get(session_id) is None,
        )
        complete_wave = getattr(self.led, "complete_readiness_wave", None)
        if callable(complete_wave):
            complete_wave()
        current = self.sessions.get(session_id)
        if current is None:
            return
        current.readiness_wave_completed = True
        self._reconcile_canonical_lighting(outputs)
        self.sessions.save(current)
        outputs.append({
            "action": "led_pattern",
            "pattern": "rfid_wave",
            "duration_ms": READINESS_WAVE_DURATION_MS,
            "groups": [list(group) for group in READINESS_WAVE_GROUPS],
        })

    def _run_readiness_wave(self, session, outputs: list[dict]) -> None:
        if session.readiness_wave_completed:
            return
        wave = getattr(self.led, "readiness_wave", None)
        if not callable(wave):
            raise RuntimeError("canonical LED controller lacks readiness_wave")
        wave(READINESS_WAVE_DURATION_MS)
        session.readiness_wave_completed = True
        self._reconcile_canonical_lighting(outputs)
        outputs.append({
            "action": "led_pattern",
            "pattern": "rfid_wave",
            "duration_ms": READINESS_WAVE_DURATION_MS,
            "groups": [list(group) for group in READINESS_WAVE_GROUPS],
        })

    @staticmethod
    def _result(session, outputs, reason): return {"session_id":session.session_id, "state":session.state.value, "node":session.current_node, "reason":reason, "outputs":outputs, "correlation_id":session.correlation_id}
