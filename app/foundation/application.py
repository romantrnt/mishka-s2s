from __future__ import annotations

import asyncio
import copy
import contextlib
import hashlib
import inspect
import logging
import math
import os
import time
import uuid
from collections import deque
from dataclasses import replace
from typing import Any, Callable
from weakref import WeakSet

from .game_engine import next_deadline as game_deadline
from .engagement_idle import EngagementIdleMixin
from .domain import AudioOutput, DisplayController, Event, EventPublisher, IntentClassifier, LEDController, Lifecycle, PlaybackHandle, ScenarioRepository, SessionRecord, SessionRepository
from .lighting import BUTTON_DIGIT_ELEMENTS, BUTTON_FILL_ELEMENTS, LIGHTING_ELEMENT_IDS, LightingCommand, EXHIBITION_DIGIT_WAVE_GROUPS, EXHIBITION_MENU_WAVE_GROUPS, EXHIBITION_WAVE_STAGE_MS, READINESS_WAVE_DURATION_MS, READINESS_WAVE_GROUPS, PICTOGRAM_ELEMENTS
from .schedule_weather import ScheduleWeatherProviderError, validate_report
from .age_policy import trusted_canonical_age
from .canonical_services import LED_PRESETS, BUTTON_ELEMENTS, RecordedPhraseResolver, SimulatedAdultHandoff, TypedInfoRenderer, classify_intent
from .config import settings_from_compat_env
from .identity import IdentityDecision, IdentityValidationError, validate_identity_decision
from .input_safety import InputSafetyGate
from .canonical_contract import CanonicalEvent, CanonicalRuntime, FixedStoryManifest
from .system_prompt import build_system_prompt
from .private_welcome import PrivateWelcomeMixin
from .owned_speech import OwnedSpeechMixin, TYPED_AUDIO_IDS, MENU_AUDIO_IDS
from .personal_story import PersonalStoryMixin, STORY_MODES, STORY_ID, story_theme
from .dialogue_memory import DialogueMemoryMixin
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


class ScenarioApplication(EngagementIdleMixin, PrivateWelcomeMixin, OwnedSpeechMixin, PersonalStoryMixin, DialogueMemoryMixin):
    """Use-case layer. It depends only on domain ports, never FastAPI/files/UART/LLM."""
    AUDIO_RECOVERABLE_FAILURES = frozenset({
        "vad_failure",
        "stt_failure",
        "stt_low_confidence",
        "audio_too_long",
    })

    def __init__(self, sessions: SessionRepository, scenarios: ScenarioRepository, classifier: IntentClassifier, audio: AudioOutput, led: LEDController, publisher: EventPublisher, now=time.time, installation_id="default", threshold=0.6, bounded_s2s=None, history=None, max_session_seconds=1200, idle_timeout_seconds: float | None = None, display: DisplayController | None = None, schedule_weather: Any = None, allow_quick_replies: bool | None = None, settings: Any = None, adult_handoff: Any = None, monotonic_now=time.monotonic, background_scheduler: bool = True):
        self.sessions, self.scenarios, self.classifier, self.audio, self.led, self.publisher = sessions, scenarios, classifier, audio, led, publisher
        self.now, self.monotonic_now, self.installation_id, self.threshold = now, monotonic_now, installation_id, threshold
        self.background_scheduler = bool(background_scheduler)
        self.settings = settings if settings is not None else settings_from_compat_env()
        settings = self.settings
        self.max_json = str(getattr(settings, "identity_mode", "guest_only")) == "max_json"
        self._max_client_boundary = None
        self.optional_identity = self.max_json
        self._identity_deadline_tasks: dict[str, asyncio.Task[Any]] = {}
        self._identity_requests: dict[str, dict[str, Any]] = {}
        self._identity_shutdown = False
        self._handoff_delivery_tasks: set[asyncio.Task[Any]] = set()
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
        self._dialogue_expiry_tasks: dict[str, asyncio.Task[Any]] = {}
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
        self._init_engagement_idle()
        self._session_epochs: dict[str, int] = {}
        self._session_revisions: dict[str, int] = {}
        # A physical reset owns cleanup until it has finished publishing.
        # New ingress for that session waits without holding a global lock.
        self._session_reset_waiters: dict[str, tuple[asyncio.Task[Any], asyncio.Future[None]]] = {}
        # One active immutable playback lease per session. Adapter callbacks
        # must match it before entering the canonical state machine.
        self._active_audio: dict[str, dict[str, Any]] = {}
        # Browser pause reports position after the pause command is published.
        # Retain only that immutable story execution until its one checkpoint.
        self._paused_story_playback: dict[str, dict[str, Any]] = {}
        self._audio_timeout_tasks: dict[str, asyncio.Task[Any]] = {}
        self._audio_timeout_sequences: dict[str, int] = {}
        self._game_timer_tasks: dict[str, asyncio.Task[Any]] = {}
        self._game_timer_scopes: dict[str, tuple] = {}
        self._game_timer_sequences: dict[str, int] = {}
        self._welcome_timers: dict[str, asyncio.Task[Any]] = {}
        self._private_welcomes: dict[str, Any] = {}
        self._private_welcome_tasks: set[asyncio.Task[Any]] = set()
        self._owned_text_jobs: set[asyncio.Task[Any]] = set()
        self._personal_stories = {}
        self._personal_story_jobs = set()
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
            self.canonical_runtime = CanonicalRuntime(now=self.now, monotonic_now=self.monotonic_now, stories=FixedStoryManifest(f"{root}/stories/fixed_manifest.json", "."), allow_development_placeholders=self.allow_development_placeholders, exhibition_mode=(str(getattr(settings, "runtime_mode", "exhibition")).lower() == "exhibition"), optional_identity=self.optional_identity)
        self.history_repository: Any = history
        # Canonical scenario documents and LangGraph are the single source of
        self.voice_manifest = RecordedPhraseResolver(f"{getattr(settings, 'content_root', 'content')}/audio/voice_manifest.json", allow_development_assets=self.allow_development_placeholders)
        self.safety_classifier = InputSafetyGate(settings)
        # CPU/GPU and physical development profiles are used with real children:
        # availability of placeholder assets does not authorize fake delivery.
        self.adult_handoff = adult_handoff if adult_handoff is not None else (SimulatedAdultHandoff() if profile == "mock" else None)
        self._canonical_profiles: dict[str, dict[str, Any] | None] = {}
        self._canonical_schedule_refs: dict[str, str | None] = {}
        self.transition_authorizer = CanonicalLangGraphAuthorizer() if self.canonical_runtime is not None else None

    def identity_status(self) -> dict[str, Any]:
        """Safe readiness only; starting a client never implies a connected real reader."""
        if self.max_json:
            return {"identity_ready": self._max_client_boundary is not None and self._max_client_boundary._healthy,
                    "reader_ready": bool(self.settings.max_client_source == "local_demo"
                                         and self._max_client_boundary is not None
                                         and self._max_client_boundary._reader_ready),
                    "synthetic": True, "synthetic_data": True, "pending": 0,
                    "source": self.settings.max_client_source,
                    "production_ready": False, "provider": "max_json"}
        return {"identity_ready": False, "reader_ready": False, "synthetic": False,
                "pending": 0, "production_ready": False, "provider": "guest_only"}

    def _identity_result(self, session_id: str, reason: str) -> dict[str, Any]:
        state = self.canonical_runtime.sessions.get(session_id) if self.canonical_runtime else None
        snapshot = state.snapshot() if state is not None else {"mode": "idle", "handset": "down"}
        return {"reason": reason, "state": snapshot["mode"], "outputs": [], "canonical_state": snapshot}

    def _drop_identity_owner(self, session_id: str) -> None:
        self._discard_personal_story(session_id)
        # Drop authority before cancellation can yield. A cancellation-resistant
        # provider may finish, but it no longer owns an accepted request.
        self._discard_private_welcome(session_id)
        self._identity_requests.pop(session_id, None)
        try:
            current_task = asyncio.current_task()
        except RuntimeError:
            current_task = None
        for registry in (self._identity_deadline_tasks,):
            task = registry.pop(session_id, None)
            if task is not None and task is not current_task and not task.done():
                with contextlib.suppress(RuntimeError):
                    task.cancel()
        self._canonical_profiles.pop(session_id, None)
        self._canonical_schedule_refs.pop(session_id, None)
        self._drop_dialogue_context(session_id)

    def _revoke_local_profile(self, session_id: str) -> None:
        """Fence an app-owned termination before any cleanup await.

        Not used for Max's incoming clear/replacement: those must preserve
        the source's newly started request.
        """
        if not self.max_json:
            return
        boundary = self._max_client_boundary
        if boundary is not None and boundary.session_id == session_id:
            boundary.clear_client()
        self._drop_identity_owner(session_id)
        self._session_epochs[session_id] = self._session_epochs.get(session_id, 0) + 1
        self._session_revisions[session_id] = self._session_revisions.get(session_id, 0) + 1
        state = self.canonical_runtime.sessions.get(session_id)
        if state is not None:
            self.canonical_runtime.clear_identity(state)
            state.state_version += 1
            if state.safety_severity != "urgent":
                state.mode = "session_closed" if state.handset == "up" else "idle"
                state.expected_inputs = ["handset_down"] if state.handset == "up" else ["handset_up"]
            self.last_canonical_state = state.snapshot()
        record = self.sessions.get(session_id)
        if record is not None:
            record.child_id, record.anonymous = None, True
            if state is not None:
                record.current_node = state.mode
            self.sessions.save(record)

    def cancel_identity_work(self) -> None:
        """Synchronous authority revocation for Stand.close, also outside a loop."""
        if self._engagement_task is not None:
            self._engagement_task.cancel()
        self._engagement_owners.clear()
        self._identity_shutdown = True
        for session_id in tuple(self.conversation_contexts):
            self._drop_dialogue_context(session_id)
        for session_id in tuple(self._personal_stories):
            self._discard_personal_story(session_id)
        for task in tuple(self._owned_text_jobs):
            task.cancel()
        for session_id in tuple(self._private_welcomes):
            self._discard_private_welcome(session_id)
        for task in tuple(self._handoff_delivery_tasks):
            task.cancel()
        if self.max_json and self._max_client_boundary is not None:
            self._revoke_local_profile(self._max_client_boundary.session_id)
        for session_id in set(self._identity_requests) | set(self._identity_deadline_tasks):
            self._drop_identity_owner(session_id)
    async def shutdown_identity(self) -> dict[str, int]:
        """Revoke first, then drain a bounded amount of provider work."""
        tasks = set(self._identity_deadline_tasks.values()) | self._handoff_delivery_tasks | self._private_welcome_tasks | self._owned_text_jobs | self._personal_story_jobs
        tasks.discard(asyncio.current_task())
        try:
            if self.max_json and self._max_client_boundary is not None:
                await self._max_client_boundary.close()
        finally:
            self.cancel_identity_work()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=self.audio_cleanup_timeout_seconds)
        else:
            pending = set()
        return {"remaining": len(pending)}

    def _identity_scope_live(self, session_id: str, capability: str | None = None) -> bool:
        if not self.optional_identity:
            return True
        state = self.canonical_runtime.sessions.get(session_id) if self.canonical_runtime else None
        request = self._identity_requests.get(session_id)
        return bool(state is not None and request is not None
                    and state.identity_request_id == request["request_id"]
                    and state.identity_expires_monotonic is not None
                    and self.monotonic_now() < state.identity_expires_monotonic
                    and (state.identity_ready_deadline is None or self.monotonic_now() < state.identity_ready_deadline)
                    and (capability is None or capability in state.identity_capabilities))

    def _schedule_identity_deadline(self, session_id: str) -> None:
        old = self._identity_deadline_tasks.pop(session_id, None)
        if old is not None and old is not asyncio.current_task() and not old.done():
            old.cancel()
        if not self.background_scheduler or not self._identity_scope_live(session_id):
            return
        state = self.canonical_runtime.sessions[session_id]
        deadline = min(value for value in (state.identity_ready_deadline, state.identity_expires_monotonic) if value is not None)

        async def expire() -> None:
            try:
                await asyncio.sleep(max(0.0, deadline - self.monotonic_now()))
                await self.expire_identity_leases()
            except asyncio.CancelledError:
                return
            finally:
                if self._identity_deadline_tasks.get(session_id) is asyncio.current_task():
                    self._identity_deadline_tasks.pop(session_id, None)

        self._identity_deadline_tasks[session_id] = asyncio.create_task(expire(), name="mishka-identity-expiry")

    async def expire_identity_leases(self) -> list[dict[str, Any]]:
        """Process due owned deadlines, including simulator fake-clock advances."""
        if not self.optional_identity or self.canonical_runtime is None:
            return []
        results = []
        for session_id, request in tuple(self._identity_requests.items()):
            state = self.canonical_runtime.sessions.get(session_id)
            if state is None or state.identity_request_id != request["request_id"]:
                self._drop_identity_owner(session_id)
                continue
            now = self.monotonic_now()
            if state.mode == "profile_ready" and state.identity_ready_deadline is not None:
                deadline, kind = state.identity_ready_deadline, "IdentityReadyExpired"
            else:
                deadline, kind = state.identity_expires_monotonic, "IdentityAuthorizationExpired"
            if deadline is None or now < deadline:
                continue
            results.append(await self._receive_canonical_event(self._identity_event(
                session_id, kind, {"request_id": request["request_id"]}
            ), identity_event_authorized=True))
        return results

    def _identity_event(self, session_id: str, kind: str, payload: dict[str, Any]) -> Event:
        state = self.canonical_runtime.sessions.get(session_id)
        sequence = state.last_sequence_by_source.get("internal_identity", 0) + 1 if state else 1
        return Event(kind, session_id, sequence, self.now(), "internal_identity", payload,
                     "identity:" + uuid.uuid4().hex, self.monotonic_now())


    async def _set_dialogue_lighting_phase(self, session_id: str, phase: str) -> dict[str, Any] | None:
        """Drive the dialogue LED frame from the actual bounded-runtime phase.

        The bear pictogram and button 7 digit are fixed anchors. Only button
        fills animate, so the serial protocol remains the existing binary
        element/state/transition contract and the same frame is visible in
        the physical stand and the WebPanel.
        """
        runtime = self.canonical_runtime
        state = runtime.sessions.get(session_id) if runtime is not None else None
        if state is None or state.mode != "bear_dialogue" or state.handset != "up":
            return None
        if self.optional_identity and state.identity_request_id and not self._identity_scope_live(session_id):
            await self.expire_identity_leases()
            return None
        phase = str(phase or "listening")
        fills = tuple(BUTTON_FILL_ELEMENTS[button] for button in range(1, 8))
        anchors = (PICTOGRAM_ELEMENTS[6], BUTTON_DIGIT_ELEMENTS[7][1])
        if phase == "processing":
            start_wave = getattr(self.led, "start_wave", None)
            if not callable(start_wave):
                return None
            lighting = start_wave(
                now=self.monotonic_now(),
                groups=tuple((element_id,) for element_id in fills),
                base_elements=anchors,
                loop=True,
                stage_duration_ms=520,
            )
        else:
            phase = "output" if phase == "output" else "listening"
            start_breathing = getattr(self.led, "start_breathing", None)
            if not callable(start_breathing):
                return None
            lighting = start_breathing(
                now=self.monotonic_now(), elements=fills, base_elements=anchors, phase=phase,
            )
        self.runtime_status["dialogue_lighting_phase"] = phase
        self.runtime_status["dialogue_lighting"] = dict(lighting)
        return dict(lighting)

    async def dialogue_lifecycle(self, session_id: str, phase: str, **details: Any) -> None:
        """Callback used by bounded S2S when its backend phase changes."""
        await self._set_dialogue_lighting_phase(session_id, phase)

    def record_audio_diagnostic(self, kind: str, **fields: Any) -> dict[str, Any]:
        item = {"kind": str(kind), "at_monotonic": self.monotonic_now(), **fields}
        self.audio_runtime_diagnostics.append(item)
        self.canonical_diagnostics.append(item)
        self.runtime_status["audio_last_diagnostic"] = dict(item)
        return item

    def _cancel_audio_timeout(self, session_id: str) -> None:
        task = self._audio_timeout_tasks.pop(session_id, None)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    async def _audio_playback_watchdog(self, session_id: str, content_id: str, token: int,
                                       playback_id: str, playback_lease: str,
                                       generation: int, timeout_s: float) -> None:
        try:
            await asyncio.sleep(timeout_s)
            lease = self._active_audio.get(session_id)
            if (not lease or lease.get("content_id") != content_id
                    or lease.get("playback_id") != playback_id
                    or lease.get("playback_lease") != playback_lease):
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
                {"response_id": content_id, "token": token, "playback_id": playback_id,
                 "playback_lease": playback_lease, "generation": generation,
                 "error_code": "audio_playback_timeout", "state_version": state_version},
                "audio-timeout:" + playback_id,
                self.monotonic_now(),
            ))
        except asyncio.CancelledError:
            return
        finally:
            if self._audio_timeout_tasks.get(session_id) is asyncio.current_task():
                self._audio_timeout_tasks.pop(session_id, None)

    def _register_audio_lease(self, session_id: str, content_id: str, token: int, playback_id: str,
                              playback_lease: str | None = None, *, duration_ms: int | None = None) -> None:
        self._cancel_audio_timeout(session_id)
        generation = int(self._session_epochs.get(session_id, 0))
        lease_value = str(playback_lease or "")
        timeout_s = max(0.01, float(getattr(self.settings, "playback_timeout_s", 60.0)))
        if duration_ms is not None:
            timeout_s = max(timeout_s, max(0, int(duration_ms)) / 1000 + 10.0)
        self._active_audio[session_id] = {
            "content_id": str(content_id), "token": int(token), "playback_id": str(playback_id),
            "playback_lease": lease_value, "generation": generation, "status": "queued",
            "timeout_s": timeout_s,
        }
        self._audio_timeout_tasks[session_id] = asyncio.create_task(
            self._audio_playback_watchdog(
                session_id, str(content_id), int(token), str(playback_id),
                lease_value, generation, timeout_s,
            ),
            name="mishka-audio-timeout-" + session_id,
        )

    def _play_owned_audio(self, session_id: str, response_id: str, asset_id: str,
                          token: int, effect: dict[str, Any], *, offset_ms: int = 0,
                          duration_ms: int | None = None) -> PlaybackHandle:
        lease = str(effect.get("playback_lease") or "")
        if not lease:
            raise ValueError("canonical playback lease is required")
        handle = PlaybackHandle(
            session_id=session_id, generation=int(self._session_epochs.get(session_id, 0)),
            playback_id=f"{session_id}:{uuid.uuid4().hex}", playback_lease=lease,
            response_id=response_id, token=int(token),
        )
        self.audio.play_owned(handle, asset_id, offset_ms=offset_ms)
        effect["playback_id"] = handle.playback_id
        effect["generation"] = handle.generation
        self._register_audio_lease(session_id, response_id, token, handle.playback_id, lease,
                                   duration_ms=duration_ms)
        return handle

    def _audio_callback_is_current(self, event: Event) -> bool:
        lease = self._active_audio.get(event.session_id)
        payload = event.payload if isinstance(event.payload, dict) else {}
        response_id = str(payload.get("response_id") or payload.get("content_id") or "")
        token = payload.get("token")
        generation = payload.get("generation")
        if lease is None:
            return False
        if lease.get("content_id") in TYPED_AUDIO_IDS | MENU_AUDIO_IDS | {"PERSONAL_WELCOME", "PERSONAL_STORY"} and not payload.get("playback_id"):
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
            and (payload.get("playback_id") is None
                 or payload.get("playback_id") == lease.get("playback_id"))
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

    def _mark_audio_effect_lifecycle(
        self, response_id: str, status: str, *,
        session_id: str | None = None, generation: int | None = None,
        playback_id: str | None = None, playback_lease: str | None = None,
        reason: str | None = None,
    ) -> bool:
        """Project only the exact playback execution into its effect."""
        allowed = {"queued", "playing", "paused", "finished", "cancelled", "failed", "unavailable"}
        if (status not in allowed or not session_id or generation is None
                or not playback_id or not playback_lease):
            return False
        for effect in self.last_canonical_effects:
            if (effect.get("session_id") != session_id
                    or effect.get("playback_id") != playback_id
                    or effect.get("playback_lease") != playback_lease
                    or effect.get("generation") != generation):
                continue
            if response_id not in {
                str(effect.get("phrase_id") or ""), str(effect.get("story_id") or ""),
                str(effect.get("content_id") or ""),
                str(effect.get("response_id") or ""),
                str(effect.get("story_id") or "") and
                "placeholder:fixed_story:" + str(effect.get("story_id")),
            }:
                continue
            effect["audio_status"] = status
            effect["audio_started"] = status in {"playing", "paused", "finished", "cancelled"}
            effect["audio_played"] = status == "finished"
            effect["audio_cancelled"] = status == "cancelled"
            effect["audio_failed"] = status == "failed"
            effect["audio_unavailable"] = status == "unavailable"
            if reason:
                effect["audio_failure_reason"] = str(reason)[:80]
            return True
        return False

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

    @staticmethod
    def _consume_detached_audio_cancel(task: asyncio.Task[Any]) -> None:
        """Consume a late cancellation result without an unhandled-task warning."""
        if task.cancelled():
            return
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            return

    async def _cancel_audio_owner(self, owner: Any, session_id: str, *, operation: str,
                                  ownership_guard: Callable[[], bool] | None = None) -> dict[str, Any]:
        """Bound provider cleanup so HID/reset cannot be held by native audio I/O."""
        cancel_session = getattr(owner, "cancel_session", None)
        if not callable(cancel_session):
            return {"status": "unavailable", "operation": operation, "session_id": session_id}
        async def cancel_owned() -> None:
            # The cancellation task can start after a newer event has run.
            # Check authority in that task before touching the shared owner.
            if ownership_guard is None or ownership_guard():
                await cancel_session(session_id)

        task = asyncio.create_task(
            cancel_owned(),
            name=f"mishka-audio-cancel-{operation}-{session_id}",
        )
        timeout = self.audio_cleanup_timeout_seconds
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            # Do not let a late cleanup for the old physical session cancel a
            # new session that reuses the same session id. Native PortAudio
            # work may remain shielded inside the provider, but its owner
            # coroutine must stop carrying this session's cancellation intent.
            if not task.done():
                task.cancel()
            task.add_done_callback(self._consume_detached_audio_cancel)
            diagnostic = {
                "kind": "audio_owner_cleanup_timeout",
                "session_id": session_id,
                "operation": operation,
                "timeout_s": timeout,
            }
            self.audio_runtime_diagnostics.append(diagnostic)
            logger.warning("audio owner cleanup timed out; continuing reset", extra=diagnostic)
            return {**diagnostic, "status": "timed_out"}
        except asyncio.CancelledError:
            if not task.done():
                task.cancel()
            raise
        except Exception as exc:
            diagnostic = {
                "kind": "audio_owner_cleanup_failed",
                "session_id": session_id,
                "operation": operation,
                "exception_type": type(exc).__name__,
                "detail": str(exc)[:160],
            }
            self.audio_runtime_diagnostics.append(diagnostic)
            logger.warning("audio owner cleanup failed; continuing reset: %s", diagnostic["detail"])
            return {**diagnostic, "status": "failed"}
        return {"status": "completed", "operation": operation, "session_id": session_id}

    async def _cleanup_session_resources(self, session_id: str, *, identity_request: dict[str, Any] | None = None) -> dict[str, Any]:
        """Idempotently release all application-owned resources for a session."""
        revision = self._session_revisions.get(session_id, 0)
        epoch = self._session_epochs.get(session_id, 0)
        state = self.canonical_runtime.sessions.get(session_id) if self.canonical_runtime else None
        audio_owner = self.s2s_audio if self.s2s_audio is not None else self.bounded_s2s

        def still_current() -> bool:
            current = self.canonical_runtime.sessions.get(session_id) if self.canonical_runtime else None
            return (self._session_revisions.get(session_id, 0) == revision
                    and self._session_epochs.get(session_id, 0) == epoch
                    and current is state)

        # All direct mutations revoke the captured owner's resources before
        # yielding. A later accepted card/call owns whatever it installs next.
        self._cancel_owned_text(session_id)
        self._drop_identity_owner(session_id)
        if identity_request is not None:
            self._identity_requests[session_id] = identity_request
        self._discard_private_welcome(session_id)
        self._drop_engagement(session_id, keys=True)
        self._cancel_game_timer(session_id)
        self._cancel_welcome_timer(session_id)
        self._welcome_timer_sequences.pop(session_id, None)
        self._cancel_menu_tutorial(session_id)
        self._menu_tutorial_sequences.pop(session_id, None)
        self._cancel_lighting_wave(session_id)
        self._invalidate_audio_lease(session_id)
        self._last_audio_ack_at.pop(session_id, None)
        completion_cleanup = await self._cancel_audio_completions(session_id, operation="session_cleanup")
        if not still_current():
            return {"session_id": session_id, "cleanup_stale": True, "audio_completion_cleanup": completion_cleanup}
        if audio_owner is not None:
            await self._cancel_audio_owner(audio_owner, session_id, operation="session_cleanup",
                                           ownership_guard=still_current)
            if not still_current():
                return {"session_id": session_id, "cleanup_stale": True, "audio_completion_cleanup": completion_cleanup}
        snapshot = self.resource_snapshot()
        snapshot["session_id"] = session_id
        snapshot["audio_completion_cleanup"] = completion_cleanup
        return snapshot

    async def expire_idle_sessions(self, now: float | None = None) -> list[dict[str, Any]]:
        """End abandoned sessions and drain their RAM/audio ownership."""
        self.expire_dialogue_memory()
        if self.idle_timeout_seconds is None:
            return []
        current_time = self.now() if now is None else float(now)
        expired: list[dict[str, Any]] = []
        for session in self._iter_sessions():
            if session.session_id in self._engagement_owners:
                continue
            if session.state in {Lifecycle.IDLE, Lifecycle.ENDED} or not session.started_at:
                continue
            last_activity = session.last_activity_at or session.started_at
            if current_time - last_activity < self.idle_timeout_seconds:
                continue
            if self._session_has_active_work(session.session_id):
                continue
            self._revoke_local_profile(session.session_id)
            cleanup_result = await self._cleanup_session_resources(session.session_id)
            if cleanup_result.get("cleanup_stale"):
                continue
            state = self.canonical_runtime.sessions.get(session.session_id)
            if self.max_json and state is not None and state.safety_severity == "urgent":
                # Idle expiry cannot stand in for an adult's incident resolution.
                continue
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
                    self._revoke_local_profile(session_id)
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
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _cancel_lighting_wave(self, session_id: str) -> None:
        task = self._lighting_wave_tasks.pop(session_id, None)
        if task is not None and task is not asyncio.current_task() and not task.done():
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

    def _current_game_timer_scope(self, session_id: str) -> tuple | None:
        state = self.canonical_runtime.sessions.get(session_id) if self.canonical_runtime else None
        engine = state.game_engine_state if state is not None else None
        if (state is None or state.mode != "game_active" or state.substate == "game_exit_confirmation"
                or engine is None or not engine.run_id or game_deadline(engine) is None):
            return None
        return (self._session_epochs.get(session_id, 0), engine.run_id, engine.round_generation,
                engine.timer_generation, game_deadline(engine))

    def _cancel_game_timer(self, session_id: str) -> None:
        self._game_timer_scopes.pop(session_id, None)
        task = self._game_timer_tasks.pop(session_id, None)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    def _sync_game_timer(self, session_id: str) -> None:
        scope = self._current_game_timer_scope(session_id)
        if scope is not None and self._game_timer_scopes.get(session_id) == scope:
            return
        self._cancel_game_timer(session_id)
        if scope is None:
            return
        self._game_timer_scopes[session_id] = scope
        if not self.background_scheduler:
            return
        async def wait() -> None:
            try:
                await asyncio.sleep(max(0., scope[-1] - self.monotonic_now()))
                await self._fire_game_timer(session_id, scope)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("game timer failed")
            finally:
                if self._game_timer_tasks.get(session_id) is asyncio.current_task():
                    self._game_timer_tasks.pop(session_id, None)
        self._game_timer_tasks[session_id] = asyncio.create_task(wait(), name="mishka-game-timer")

    async def _fire_game_timer(self, session_id: str, scope: tuple) -> dict[str, Any] | None:
        if self._current_game_timer_scope(session_id) != scope or self.monotonic_now() < scope[-1]:
            return None
        sequence = self._game_timer_sequences.get(session_id, 0) + 1
        self._game_timer_sequences[session_id] = sequence
        return await self._receive_canonical_event(Event(
            "GameTimer", session_id, sequence, self.now(), "canonical_game_timer",
            {"run_id": scope[1], "round_generation": scope[2], "timer_generation": scope[3], "deadline": scope[4]},
            f"game-timer:{scope[1]}:{scope[2]}:{scope[3]}", self.monotonic_now(),
        ), game_timer_authorized=True, _game_timer_scope=scope)

    async def flush_game_timers(self) -> list[dict[str, Any]]:
        """Deterministic clock seam through the same private authorized delivery."""
        results = []
        for session_id, scope in tuple(self._game_timer_scopes.items()):
            if self.monotonic_now() >= scope[-1]:
                result = await self._fire_game_timer(session_id, scope)
                if result is not None:
                    results.append(result)
        return results

    def _cancel_welcome_timer(self, session_id: str) -> None:
        task = self._welcome_timers.pop(session_id, None)
        if task is not None and task is not asyncio.current_task() and not task.done():
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

    async def _resolve_adult_incident_internal(self, session_id: str, incident_id: str) -> dict[str, Any]:
        """Server-owned resolution seam; no external route or caller identity policy.

        An authenticated adult/incident authorization adapter must be designed
        before this seam is exposed in production.
        """
        state = self.canonical_runtime.sessions.get(session_id) if self.canonical_runtime else None
        if state is None:
            return {"reason": "adult_resolved_ignored", "outputs": [], "canonical_state": {"mode": "idle"}}
        sequence = state.last_sequence_by_source.get("internal_adult_resolution", 0) + 1
        return await self._receive_canonical_event(Event(
            "AdultResolved", session_id, sequence, self.now(), "internal_adult_resolution",
            {"incident_id": incident_id}, f"adult-resolution:{uuid.uuid4().hex}",
            self.monotonic_now(),
        ), adult_resolution_authorized=True)

    async def _receive_canonical_event(self, event: Event, transient_text: str | None = None,
                                       *, adult_resolution_authorized: bool = False,
                                       handoff_ack_authorized: bool = False,
                                       handoff_failure_authorized: bool = False,
                                       dialogue_fallback_authorized: bool = False,
                                       personal_story_authorized: bool = False,
                                       identity_event_authorized: bool = False,
                                       game_timer_authorized: bool = False,
                                       interaction_idle_authorized: bool = False,
                                       _interaction_nonce: str | None = None,
                                       _game_timer_scope: tuple | None = None,
                                       identity_profile: dict[str, Any] | None = None,
                                       _speech_ingress_guard=None, _speech_lifecycle_guard=None) -> dict[str, Any]:
        if event.event_type == "InteractionIdleExpired" and not interaction_idle_authorized:
            return self._identity_result(event.session_id, "interaction_idle_untrusted")
        speech_work = event.event_type == "SpeechReceived"
        speech_scope = (event.session_id, self._engagement_scope(event.session_id))
        if speech_work:
            self._engagement_ingress[speech_scope] = self._engagement_ingress.get(speech_scope,0) + 1
        try:
            waiter = self._session_reset_waiters.get(event.session_id)
            if waiter is not None and waiter[0] is not asyncio.current_task():
                await waiter[1]
            if self.optional_identity and not event.event_type.startswith("Identity") and event.event_type not in {"ClientProfileReady", "ClientProfileCleared"}:
                await self.expire_identity_leases()
            return await self._receive_canonical_event_inner(
                event, transient_text,
                adult_resolution_authorized=adult_resolution_authorized,
                handoff_ack_authorized=handoff_ack_authorized,
                handoff_failure_authorized=handoff_failure_authorized,
                dialogue_fallback_authorized=dialogue_fallback_authorized,
                personal_story_authorized=personal_story_authorized,
                identity_event_authorized=identity_event_authorized,
                game_timer_authorized=game_timer_authorized, _game_timer_scope=_game_timer_scope,
                interaction_idle_authorized=interaction_idle_authorized, _interaction_nonce=_interaction_nonce,
                identity_profile=identity_profile,
                _speech_ingress_guard=_speech_ingress_guard,
                _speech_lifecycle_guard=_speech_lifecycle_guard,
            )
        finally:
            if speech_work:
                count = self._engagement_ingress.get(speech_scope,1) - 1
                if count: self._engagement_ingress[speech_scope] = count
                else: self._engagement_ingress.pop(speech_scope,None)
                self._refresh_engagement_busy(event.session_id)
            waiter = self._session_reset_waiters.get(event.session_id)
            if waiter is not None and waiter[0] is asyncio.current_task():
                self._session_reset_waiters.pop(event.session_id, None)
                if not waiter[1].done():
                    waiter[1].set_result(None)

    def _start_handoff_delivery(self, event: Event, snapshot: dict[str, Any], effect: dict[str, Any]):
        """Own transport by incident, not by the current UI/audio event revision.

        Start before audio cleanup yields: an ordinary button or handset-down
        must not erase an urgent request that has already been accepted. This
        does not grant a late response permission to speak in a new session.
        """
        adapter = self.adult_handoff
        incident_id = str(snapshot.get("incident_id") or "")
        if adapter is None or not incident_id:
            return None
        handoff_id = f"{event.session_id}:{event.correlation_id or event.sequence}"
        request = {"incident_id": incident_id, "handoff_id": handoff_id, "session_id": event.session_id, "profile_ref": snapshot.get("profile_ref"), "correlation_id": event.correlation_id, "severity": effect.get("severity"), "category": "urgent" if effect.get("severity") == "urgent" else "other", "safe_summary": "Ребёнок попросил взрослого", "location_device_id": self.installation_id, "created_at": self.now(), "delivery": "pending", "state_version": snapshot.get("state_version")}

        async def deliver():
            current = self.canonical_runtime.sessions.get(event.session_id)
            if (self._identity_shutdown or current is None or current.mode != "safety_handoff"
                    or current.incident_id != incident_id):
                return {"acknowledged": False}
            delivery = adapter.request(request)
            return await delivery if inspect.isawaitable(delivery) else delivery

        task = asyncio.create_task(deliver(), name="mishka-adult-handoff")
        self._handoff_delivery_tasks.add(task)
        def completed(done):
            self._handoff_delivery_tasks.discard(done)
            # The initiating event can have lost ownership while delivery was
            # pending; consume exceptions even when there is no remaining waiter.
            self._consume_detached_audio_cancel(done)
        task.add_done_callback(completed)
        return task

    async def _receive_canonical_event_inner(self, event: Event, transient_text: str | None = None,
                                             *, adult_resolution_authorized: bool = False,
                                             handoff_ack_authorized: bool = False,
                                             handoff_failure_authorized: bool = False,
                                             dialogue_fallback_authorized: bool = False,
                                       personal_story_authorized: bool = False,
                                             identity_event_authorized: bool = False,
                                             game_timer_authorized: bool = False,
                                             interaction_idle_authorized: bool = False,
                                             _interaction_nonce: str | None = None,
                                             _game_timer_scope: tuple | None = None,
                                             identity_profile: dict[str, Any] | None = None,
                                             _speech_ingress_guard=None, _speech_lifecycle_guard=None) -> dict[str, Any]:
        """Bridge canonical decisions to application-owned effects.

        The contract engine never touches audio, capture, LEDs or providers.
        This boundary is the sole place where declared effects become calls.
        """
        profile = None
        profile_request = None
        verified_voice_activity = False
        current = self.canonical_runtime.sessions.get(event.session_id)
        if event.event_type == "InteractionIdleExpired":
            if not interaction_idle_authorized:
                return self._identity_result(event.session_id, "interaction_idle_untrusted")
            if not self._engagement_due(event.session_id, _interaction_nonce):
                return self._identity_result(event.session_id, "interaction_idle_stale")
        if event.event_type == "GameTimer":
            if not game_timer_authorized:
                return self._identity_result(event.session_id, "game_timer_untrusted")
            if (_game_timer_scope is None or self._current_game_timer_scope(event.session_id) != _game_timer_scope
                    or self.monotonic_now() < _game_timer_scope[-1]):
                return self._identity_result(event.session_id, "game_timer_stale")
        if self.optional_identity:
            if self._identity_shutdown:
                return self._identity_result(event.session_id, "identity_shutdown")
            for other_id, other in tuple(self.canonical_runtime.sessions.items()):
                if other_id != event.session_id and (other.handset == "up"
                        or other.mode == "profile_ready"
                        or other.safety_severity == "urgent"):
                    return self._identity_result(event.session_id, "installation_busy")
        if event.event_type in {"ClientProfileReady", "ClientProfileCleared"}:
            if not self.max_json or not identity_event_authorized:
                return self._identity_result(event.session_id, "client_profile_untrusted")
            if event.event_type == "ClientProfileReady":
                if self._max_client_boundary is None or event.payload.get("client_generation") != self._max_client_boundary.generation:
                    return self._identity_result(event.session_id, "stale_client_generation")
                if current is not None and (current.handset == "up" or current.mode not in {"idle", "profile_ready"}):
                    return self._identity_result(event.session_id, "profile_active_session_ignored")
                checked = validate_identity_decision(IdentityDecision("authorized", identity_profile,
                    frozenset({"personalize"}), event.payload.get("expires_monotonic", 0)), self.monotonic_now())
                if checked.status != "authorized":
                    return self._identity_result(event.session_id, "invalid_client_profile")
                profile = checked.profile
                profile_request = {"request_id": event.payload["request_id"]}
        if event.event_type in {"IdentityReadyExpired", "IdentityAuthorizationExpired"}:
            request = self._identity_requests.get(event.session_id)
            if not identity_event_authorized:
                return self._identity_result(event.session_id, "identity_outcome_untrusted")
            if (not self.optional_identity or request is None or current is None
                    or request["request_id"] != event.payload.get("request_id")
                    or current.identity_request_id != request["request_id"]):
                return self._identity_result(event.session_id, "stale_profile_context")
        requested_version = event.payload.get("state_version") if isinstance(event.payload, dict) else None
        state_version = int(requested_version) if isinstance(requested_version, int) else (current.state_version if current else 0)
        previous_state = copy.deepcopy(current)
        event_type = "VoiceIntentReady" if event.event_type == "SpeechReceived" else event.event_type
        payload = dict(event.payload)
        accepted_monotonic = (self.monotonic_now() if event.event_type in {"GameTimer", "InteractionIdleExpired"} else
                              event.monotonic_timestamp if event.monotonic_timestamp is not None else self.monotonic_now())
        voice_admission = None
        personal_welcome_answer_verified = False
        if event.event_type == "SpeechReceived":
            if _speech_lifecycle_guard is not None and not _speech_lifecycle_guard():
                return self._identity_result(event.session_id, "stale_session_generation")
            voice_admission, rejected = self.canonical_runtime.reserve_voice(CanonicalEvent(
                event_type, event.session_id, event.sequence, state_version, event.source,
                payload, event.correlation_id, event.timestamp, accepted_monotonic,
            ))
            if rejected:
                return self._identity_result(event.session_id, rejected)
            text = transient_text or ""
            parsed_intent = classify_intent(text)
            explicit_adult_request = parsed_intent == "request_adult"
            admitted_handset = current.handset if current is not None else "down"
            epoch = self._session_epochs.get(event.session_id, 0)
            revision = self._session_revisions.get(event.session_id, 0)
            safety_started = self.monotonic_now()
            safety = await self.safety_classifier.classify(text)
            # Retain fixed outcome codes, never a transcript, name, text hash,
            # model response or exception message. Child ACKs may replace
            # last_canonical_event, so diagnostics must be an independent ring.
            self.record_audio_diagnostic(
                "input_safety_decision", code=safety.reason,
                safety_severity=safety.severity, safety_verified=bool(safety.verified),
                stt_trusted=not bool(payload.get("_stt_untrusted", False)),
                latency_ms=round(max(0.0, self.monotonic_now() - safety_started) * 1000, 2),
            )
            # Classification is an async boundary: a hangup, reset or newer
            # event must fence its late result before any canonical mutation.
            if (self._session_epochs.get(event.session_id, 0) != epoch
                    or (_speech_lifecycle_guard is not None and not _speech_lifecycle_guard())):
                return self._identity_result(event.session_id, "stale_session_generation")
            current = self.canonical_runtime.sessions.get(event.session_id)
            if current is None or current.handset != admitted_handset:
                return self._identity_result(event.session_id, "stale_session_generation")
            stale_speech_phase = (_speech_ingress_guard is not None and not _speech_ingress_guard())
            if (self._session_revisions.get(event.session_id, 0) != revision
                    or current.last_sequence_by_source.get(event.source) != event.sequence
                    or stale_speech_phase):
                if safety.severity == "none" and not explicit_adult_request:
                    return self._identity_result(event.session_id, "stale_session_generation")
                # A welcome timer or other ordinary transition must not erase
                # a possible disclosure from the same physical session.
            # Every await may change dedup state even without a transition.
            # Rollback must never restore a pre-await snapshot.
            previous_state = copy.deepcopy(current)
            state_version = current.state_version
            uncertain_stt = payload.pop("_stt_untrusted", False)
            verified_voice_activity = bool(safety.verified and not uncertain_stt and not stale_speech_phase and text.strip())
            if uncertain_stt and safety.severity == "none" and not explicit_adult_request:
                return {**self._identity_result(event.session_id, "audio_retry"),
                        "outputs": [{"action": "audio_retry", "reason": "stt_low_confidence"}]}
            intent = "request_adult" if safety.severity == "concern" else parsed_intent
            if uncertain_stt or not safety.verified or stale_speech_phase:
                # Possible danger is still escalated, without treating the
                # transcript as a reliable name, age, command, or memory.
                transient_text = None
                intent = ("request_adult" if safety.severity == "concern" or explicit_adult_request
                          else "input_retry" if safety.severity == "none" else "free_speech")
            personal_welcome_answer_verified = (safety.verified and safety.severity == "none"
                and not uncertain_stt and bool((transient_text or "").strip()))
            payload.update({"intent": intent, "safety_severity": safety.severity,
                            "safety_reason": safety.reason})
        if event.event_type == "PersonalStoryReady":
            owner = self._personal_stories.get(event.session_id)
            personal_story_authorized = bool(personal_story_authorized and owner and owner.audio is not None
                and event.payload.get("request_id") == owner.request_id
                and self._personal_story_scope(event.session_id, owner))
        if (current is not None and current.mode == "personal_story_theme" and current.scene_menu_phase != "repeat_listen" and transient_text
                and payload.get("safety_severity") == "none" and personal_welcome_answer_verified):
            payload["intent"] = story_theme(transient_text) or "unsupported_story_theme"
        # Transport-supplied timestamps cannot make the real welcome clock due.
        if (event.event_type == "WelcomeTimerFired" and current is not None
                and current.welcome_deadline_monotonic is not None
                and self.monotonic_now() < current.welcome_deadline_monotonic):
            return self._identity_result(event.session_id, "welcome_not_due")
        private_welcome_ready = False
        if (event.event_type == "WelcomeTimerFired" and current is not None
                and current.welcome_status == "pending" and current.mode == "voice_welcome"
                and current.welcome_deadline_monotonic is not None
                and self.monotonic_now() >= current.welcome_deadline_monotonic
                and accepted_monotonic >= current.welcome_deadline_monotonic):
            private_welcome_ready = self._choose_private_welcome(event.session_id, commit=False)
        self.canonical_runtime.menu_voice_capable = self._owned_menu_voice_capable()
        self.canonical_runtime.personal_story_capable = self.personal_story_available()
        from .menu_capabilities import day_menu_items
        self.canonical_runtime.day_menu_items = lambda sid: day_menu_items(self,sid)
        decision = self.canonical_runtime.handle(CanonicalEvent(
            event_type=event_type, session_id=event.session_id, sequence=event.sequence,
            state_version=state_version, source=event.source, payload=payload,
            correlation_id=event.correlation_id, timestamp=event.timestamp, monotonic_timestamp=accepted_monotonic,
        ), profile, transient_text=transient_text,
           _voice_admission=voice_admission,
           private_welcome_ready=private_welcome_ready,
           personal_welcome_answer_verified=personal_welcome_answer_verified,
           adult_resolution_authorized=adult_resolution_authorized,
           handoff_ack_authorized=handoff_ack_authorized,
           handoff_failure_authorized=handoff_failure_authorized,
           dialogue_fallback_authorized=dialogue_fallback_authorized,
           personal_story_authorized=personal_story_authorized,
           identity_event_authorized=identity_event_authorized, game_timer_authorized=game_timer_authorized, interaction_idle_authorized=interaction_idle_authorized)
        if decision["reason"] in {"welcome_started", "personal_welcome_started"}:
            self._choose_private_welcome(event.session_id)
        snapshot, effects = decision["state"], decision["effects"]
        exhibition_reset = bool(decision.get("reset_session"))
        prelift_expired = bool(event.event_type == "IdentityReadyExpired" and decision["reason"] == "identity_expired"
                              and previous_state is not None and previous_state.mode == "profile_ready")
        interaction_stopped = decision["reason"] == "interaction_idle"
        sync_stop = exhibition_reset or prelift_expired or interaction_stopped
        self.last_canonical_state = dict(snapshot)
        self.last_canonical_event = {"type": event_type, "source": event.source, "sequence": event.sequence, "session_id": event.session_id, "timestamp": event.timestamp, "monotonic_timestamp": accepted_monotonic, "correlation_id": event.correlation_id, "payload": dict(payload)}
        voice_open = snapshot.get("handset") == "up"
        authorization = self.transition_authorizer.authorize(
            previous_state.mode if previous_state is not None else "idle",
            str(snapshot.get("mode") or "idle"),
            event_type,
            intent=str(payload.get("intent") or ""),
            safety_level=str(payload.get("safety_severity") or "none"),
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
        # A newer event owns this session as soon as its transition is
        # accepted. Handset reset also fences work that is currently awaiting
        # a provider before any cleanup await can yield.
        # The simulated acknowledgement is a child of the current delivery
        # decision, not a competing ingress event. External acknowledgements
        # still acquire a new revision.
        child_ack = (
            (event.event_type == "AdultHandoffAcknowledged" and handoff_ack_authorized)
            or (event.event_type == "StoryPositionUpdated" and event.source == "audio_pause")
            or (event.event_type in {"PersonalStoryReady", "AudioCompleted"} and personal_story_authorized)
            or (event.event_type == "DialogueFallback" and dialogue_fallback_authorized)
        )
        previous_version = previous_state.state_version if previous_state is not None else -1
        # A physical key release/hold acknowledgement is telemetry, not a
        # replacement for the response triggered by its preceding press.
        semantic_effects = [item for item in effects if item.get("type") != "button_edge_ack"]
        takes_ownership = bool(semantic_effects) or snapshot.get("state_version") != previous_version or exhibition_reset
        if (takes_ownership and self.max_json and self._max_client_boundary is not None
                and event.session_id == self._max_client_boundary.session_id
                and (event.event_type in {"HandsetPlaced", "SessionCancel", "SessionReset", "AnonymousStart"}
                     or (event.event_type == "HandsetLifted" and snapshot.get("profile_status") != "identified"))):
            # Fence Max only after canonical acceptance. Duplicate/stale handset
            # edges must not clear one side while the other retains its context.
            self._max_client_boundary.clear_client()
        revision = self._session_revisions.get(event.session_id, 0) + (1 if takes_ownership and not child_ack else 0)
        self._session_revisions[event.session_id] = revision
        if takes_ownership and not child_ack:
            self._cancel_owned_text(event.session_id)
        next_owner = self.canonical_runtime.sessions.get(event.session_id)
        identity_owner_changed = bool(self.optional_identity and previous_state is not None
            and previous_state.identity_request_id != getattr(next_owner, "identity_request_id", None))
        identity_ended = bool(previous_state is not None and previous_state.profile_ref
                              and getattr(next_owner, "profile_ref", None) is None)
        if (identity_ended and self.max_json and event.event_type != "ClientProfileCleared"
                and self._max_client_boundary is not None
                and self._max_client_boundary.session_id == event.session_id):
            self._max_client_boundary.clear_client()
        if (takes_ownership and event.event_type in {"HandsetPlaced", "SessionCancel", "SessionReset", "AnonymousStart", "HardwareDisconnected", "InteractionIdleExpired"}) or exhibition_reset or identity_owner_changed or identity_ended:
            self._session_epochs[event.session_id] = self._session_epochs.get(event.session_id, 0) + 1
            self._paused_story_playback.pop(event.session_id, None)
            self._discard_private_welcome(event.session_id)
        self._transition_dialogue_memory(
            event.session_id, event.event_type, previous_state,
            self.canonical_runtime.sessions.get(event.session_id),
            takes_ownership=takes_ownership, exhibition_reset=exhibition_reset,
        )
        if exhibition_reset:
            self._session_reset_waiters[event.session_id] = (
                asyncio.current_task(), asyncio.get_running_loop().create_future()
            )
        epoch = self._session_epochs.get(event.session_id, 0)
        state_owner = self.canonical_runtime.sessions.get(event.session_id)
        story_owner = self._personal_stories.get(event.session_id)
        if story_owner and (not state_owner or state_owner.mode not in STORY_MODES or state_owner.handset != "up"
                or self._session_epochs.get(event.session_id,0) != story_owner.epoch
                or (state_owner.profile_ref,state_owner.identity_request_id) != story_owner.identity
                or (story_owner.identity[0] and not self._identity_scope_live(event.session_id,"personalize"))):
            self._discard_personal_story(event.session_id)

        self._sync_game_timer(event.session_id)
        self._engagement_event(event, decision, accepted=takes_ownership, voice_activity=verified_voice_activity)

        reset_sync_results: dict[str, dict[str, Any]] = {}

        def reset_sync_step(resource: str, action) -> None:
            # A failed device must not prevent another device or RAM owner
            # from being revoked. Report failure, never claim physical success.
            try:
                action()
                reset_sync_results[resource] = {"status": "completed"}
            except Exception as exc:
                failure = {"type": "cleanup_failed", "resource": resource,
                           "operation": "interaction_idle" if interaction_stopped else "profile_ready_expiry" if prelift_expired else "exhibition_reset", "status": "failed",
                           "exception_type": type(exc).__name__}
                reset_sync_results[resource] = failure
                effects.append(dict(failure))
                self.canonical_diagnostics.append(dict(failure))
                self.runtime_status["session_cleanup_last_failure"] = dict(failure)
                logger.warning("exhibition reset %s cleanup failed: %s", resource, type(exc).__name__)

        if sync_stop:
            # Physical hangup takes effect synchronously. Native/model audio
            # cleanup can await, fail or be cancelled; none may keep a stale
            # wave or tutorial lit after canonical handset-down was accepted.
            self._cancel_welcome_timer(event.session_id)
            self._cancel_menu_tutorial(event.session_id)
            self._cancel_lighting_wave(event.session_id)
            self._invalidate_audio_lease(event.session_id)
            closing_record = self.sessions.get(event.session_id)
            if closing_record is not None:
                if not interaction_stopped:
                    closing_record.handset_up = False
                closing_record.cancellation_token += 1
                self.sessions.save(closing_record)
                reset_sync_step("audio", lambda: self.audio.cancel(closing_record.cancellation_token))
            cancel_effects = getattr(self.led, "cancel_effects", None)
            reset_sync_step("lighting", lambda: cancel_effects(clear=True)
                            if callable(cancel_effects) else self.led.clear_all())

        def still_current() -> bool:
            return (
                self._session_revisions.get(event.session_id) == revision
                and self._session_epochs.get(event.session_id, 0) == epoch
                and self.canonical_runtime.sessions.get(event.session_id) is state_owner
            )

        def stale_result() -> dict[str, Any]:
            current_state = self.canonical_runtime.sessions.get(event.session_id)
            current_snapshot = current_state.snapshot() if current_state is not None else {"mode": "idle", "handset": "down"}
            return {"reason": "stale_session_generation", "state": current_snapshot["mode"],
                    "state_version": current_snapshot.get("state_version", 0),
                    "outputs": [], "canonical_state": current_snapshot}

        handoff_deliveries = {
            id(effect): self._start_handoff_delivery(event, snapshot, effect)
            for effect in effects if effect.get("type") == "adult_handoff_request"
        }

        if identity_owner_changed or identity_ended:
            # Fence all private context/audio before any provider cleanup await.
            # The replacement profile context is installed only after this event commits.
            self._drop_identity_owner(event.session_id)
            self._invalidate_audio_lease(event.session_id)
            old_record = self.sessions.get(event.session_id)
            if old_record is not None:
                old_record.cancellation_token += 1
                if not sync_stop:
                    self.audio.cancel(old_record.cancellation_token)
            await self._cleanup_session_resources(event.session_id, identity_request=profile_request)
            if not still_current():
                return stale_result()

        if event.event_type == "ClientProfileReady" and decision["reason"] == "profile_ready":
            self._identity_requests[event.session_id] = profile_request

        record = self.sessions.get(event.session_id)
        if record is None:
            record = SessionRecord(event.session_id, self.installation_id, event.correlation_id or str(uuid.uuid4()),
                                   started_at=self.now(), last_activity_at=self.now())
            record.cancellation_token = self._session_epochs.get(event.session_id, 0)
        record.child_id = snapshot.get("profile_ref")
        record.anonymous = snapshot.get("profile_status") != "identified"
        record.handset_up = snapshot.get("handset") == "up"
        record.scenario_id, record.scenario_version = "canonical", "canonical_product/v1"
        record.current_node, record.last_sequence = snapshot.get("mode"), event.sequence
        # The stored reference identifies this context generation, not a patient or card.
        record.last_activity_at = self.now()
        record.state = Lifecycle.ENDED if snapshot.get("mode") in {"soft_end", "session_closed"} else Lifecycle.ACTIVE
        if interaction_stopped or prelift_expired:
            record.state = Lifecycle.IDLE
        # Keep the same mutable effect objects visible while a playback adapter
        # reports its asynchronous lifecycle.
        self.last_canonical_effects = effects
        audio_failure: dict[str, Any] | None = None
        handoff_failure: dict[str, Any] | None = None
        deferred_effects: list[dict[str, Any]] = []

        def dispatch_story_audio(response_id, asset_path, effect, *, asset_status="approved",
                                 offset_ms=0, duration_ms=None):
            nonlocal audio_failure
            try:
                self._play_owned_audio(event.session_id, response_id, asset_path,
                                       record.cancellation_token, effect,
                                       offset_ms=offset_ms, duration_ms=duration_ms)
            except Exception as exc:
                _set_audio_effect_status(effect, asset_status=asset_status, queued=False,
                                         resolution="audio_dispatch_failed")
                effect["audio_failure_type"] = type(exc).__name__
                self.record_audio_diagnostic("audio_dispatch_failed", content_id=response_id,
                                             exception_type=type(exc).__name__, session_id=event.session_id)
                self._invalidate_audio_lease(event.session_id, "failed")
                audio_failure = {"response_id": response_id, "token": record.cancellation_token,
                                 "error_code": "audio_dispatch_failed"}
                return False
            _set_audio_effect_status(effect, asset_status=asset_status, queued=True)
            return True

        if not exhibition_reset:
            self.sessions.save(record)
        if (event.event_type in {"AnonymousStart", "SessionCancel", "SessionReset"}
                and snapshot.get("profile_ref") is None):
            self._canonical_profiles.pop(event.session_id, None)
            self._canonical_schedule_refs.pop(event.session_id, None)
        if event.event_type == "ClientProfileReady" and decision["reason"] == "profile_ready":
            self._canonical_profiles[event.session_id] = dict(profile or {})
            self._canonical_schedule_refs[event.session_id] = (profile or {}).get("schedule_ref")
        if decision["reason"] == "exhibition_handset_lifted":
            self._prepare_private_welcome(event.session_id)
        if snapshot.get("mode") != "voice_welcome":
            self._discard_private_welcome(event.session_id)
        for effect_index, effect in enumerate(effects):
            if not still_current():
                return stale_result()
            # Canonical effects use ``type``; the readiness wave is an
            # existing diagnostic compatibility projection and uses ``action``.
            # Never expose an opaque ``unknown`` effect id to the UI.
            effect_kind = effect.get("type") or effect.get("action") or "effect"
            effect.setdefault("effect_id", f"{event.session_id}:{snapshot.get('state_version', 0)}:{effect_index}:{effect_kind}")
            kind = effect.get("type")
            if kind in {"speak", "play_asset", "play_fixed_story", "resume_fixed_story", "story_audio_resume", "play_placeholder",
                         "speak_dynamic", "support_question", "support_response"}:
                effect.setdefault("session_id", event.session_id)
                effect.setdefault("generation", int(self._session_epochs.get(event.session_id, 0)))
            if sync_stop and kind in {"lighting_dark", "standby_dark_started", "exhibition_effects_cancelled"}:
                effect["cleanup"] = dict(reset_sync_results["lighting"])
                effect["resolution"] = ("canonical_lighting_dark"
                    if reset_sync_results["lighting"]["status"] == "completed" else "lighting_cleanup_failed")
                continue
            if kind == "identity_cleared":
                if profile_request is None:
                    self._drop_identity_owner(event.session_id)
                effect["resolution"] = "private_identity_cleared"
            elif kind in {"lighting_dark", "standby_dark_started"}:
                cancel_effects = getattr(self.led, "cancel_effects", None)
                if callable(cancel_effects):
                    effect["lighting_effect"] = dict(cancel_effects(clear=True))
                else:
                    self.led.clear_all()
                effect["resolution"] = "canonical_lighting_dark"
            elif kind == "profile_ready_lighting":
                start_ready = getattr(self.led, "start_profile_ready", None)
                if callable(start_ready):
                    effect["lighting_effect"] = dict(start_ready(now=accepted_monotonic))
                    effect["resolution"] = "canonical_profile_ready_started"
                else:
                    effect["resolution"] = "profile_ready_lighting_unavailable"
            elif kind == "lighting_wave_intent":
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
                        lighting = start_wave(now=accepted_monotonic, hold_on_complete=bool(effect.get("hold_on_complete", True)), preserve_current=bool(effect.get("preserve_current", False)))
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
                    paused_owner = self._active_audio.get(event.session_id)
                    if (paused_owner is not None
                            and paused_owner.get("content_id") == effect.get("story_id")):
                        self._paused_story_playback[event.session_id] = dict(paused_owner)
                    outcome = pause()
                    if inspect.isawaitable(outcome):
                        outcome = await outcome
                    if not still_current():
                        return stale_result()
                    if isinstance(outcome, dict) and str(outcome.get("story_id") or "") == str(effect.get("story_id") or ""):
                        offset_ms = max(0, int(outcome.get("offset_ms") or 0))
                        if effect.get("story_id") == STORY_ID:
                            owner = self._personal_stories.get(event.session_id)
                            if (self._personal_story_scope(event.session_id,owner,checkpoint=True)
                                    and offset_ms <= int(1000*len(owner.audio.samples)/owner.audio.sample_rate)):
                                owner.checkpoint_ms = offset_ms
                        checkpoint = await self._receive_canonical_event(Event(
                            "StoryPositionUpdated", event.session_id, event.sequence,
                            self.now(), "audio_pause",
                            {"story_id": effect.get("story_id"), "offset_ms": offset_ms},
                            f"{event.correlation_id or event.sequence}:pause-position",
                        ))
                        if not still_current():
                            return stale_result()
                        effect["checkpoint"] = checkpoint.get("canonical_state", {}).get("story_checkpoint")
                        self._paused_story_playback.pop(event.session_id, None)
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
                if not voice_open:
                    _set_audio_effect_status(effect, asset_status=str((asset or {}).get("status") or ("approved" if asset else "absent")),
                                             queued=False, resolution="audio_deferred_handset_down")
                elif asset:
                    response_id = story_id
                    remaining_ms = max(0, int(asset.get("duration_ms") or 0) - offset_ms)
                    queued = dispatch_story_audio(
                        response_id, str(asset["audio_file"]), effect, offset_ms=offset_ms,
                        asset_status="approved" if verified else "placeholder",
                        duration_ms=remaining_ms if verified else None,
                    )
                    self._paused_story_playback.pop(event.session_id, None)
                    if queued:
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
                    try:
                        self._play_owned_audio(event.session_id, asset_id, asset_path,
                                               record.cancellation_token, effect)
                    except Exception as exc:
                        _set_audio_effect_status(effect, asset_status="approved", queued=False,
                                                 resolution="audio_dispatch_failed")
                        effect["audio_failure_type"] = type(exc).__name__
                        self.record_audio_diagnostic("audio_dispatch_failed", content_id=asset_id,
                                                     exception_type=type(exc).__name__, session_id=event.session_id)
                        # The ringtone is optional. Continue to the welcome
                        # timer; a failed recorded welcome has its own safe stop.
                        self._invalidate_audio_lease(event.session_id, "failed")
                        audio_failure = {"response_id": asset_id, "token": record.cancellation_token,
                                         "error_code": "audio_dispatch_failed"}
                    else:
                        _set_audio_effect_status(effect, asset_status="approved", queued=True,
                                                 resolution="canonical_asset_queued")
                else:
                    effect["asset_status"] = "absent"
                    effect["audio_status"] = "unavailable"
                    effect["audio_queued"] = False
                    effect["resolution"] = "asset_requires_handset"
            elif kind == "exhibition_effects_cancelled":
                cancel_effects = getattr(self.led, "cancel_effects", None)
                effect["lighting_effect"] = dict(cancel_effects(clear=True)) if callable(cancel_effects) else {"effect": "unavailable"}
                effect["resolution"] = "exhibition_effects_cancelled"
            elif kind == "exhibition_menu_tutorial_skip":
                # The canonical button edge is the user-facing skip gesture;
                # cancel the scheduled recorded tutorial clock immediately so
                # a late MenuTutorialStep cannot overwrite menu_ready.
                self._cancel_menu_tutorial(event.session_id)
                effect["resolution"] = "exhibition_menu_tutorial_clock_cancelled"
            elif kind == "standby_breathing_started":
                self.led.clear_all()
                effect["resolution"] = "canonical_standby_dark"
            elif kind == "exhibition_reset":
                effect["resolution"] = "canonical_exhibition_session_reset"
            elif kind == "welcome_timer_scheduled":
                effect["timer_status"] = "pending"
                self._schedule_exhibition_welcome(event.session_id, float(effect["deadline_monotonic"]))
            elif kind == "welcome_timer_cancelled":
                effect["timer_status"] = "cancelled"
                self._cancel_welcome_timer(event.session_id)
            elif kind in {"audio_stop", "welcome_ring_stop"}:
                record.cancellation_token += 1
                self._invalidate_audio_lease(event.session_id)
                if sync_stop:
                    effect["sync_cleanup"] = dict(reset_sync_results.get("audio", {"status": "not_owned"}))
                else:
                    try:
                        self.audio.cancel(record.cancellation_token)
                        if event.event_type == "HardwareDisconnected":
                            effect["sync_cleanup"] = {"status": "completed"}
                    except Exception:
                        if event.event_type != "HardwareDisconnected":
                            raise
                        effect["sync_cleanup"] = {"status": "failed", "error_code": "hardware_audio_stop_failed"}
                        self.canonical_diagnostics.append({"kind": "hardware_audio_stop_failed"})
                        logger.warning("hardware disconnect audio stop failed")
                if kind == "welcome_ring_stop":
                    # The ringtone has no S2S turn to drain. The greeting deadline
                    # must not await native preparation cleanup.
                    continue
                if self.bounded_s2s is not None:
                    effect["audio_cleanup"] = await self._cancel_audio_owner(
                        self.bounded_s2s, event.session_id, operation="audio_stop",
                        ownership_guard=still_current if interaction_stopped else None,
                    )
                if not still_current():
                    return stale_result()
                if self.s2s_audio is not None and self.s2s_audio is not self.bounded_s2s:
                    effect["s2s_cleanup"] = await self._cancel_audio_owner(
                        self.s2s_audio, event.session_id, operation="audio_stop",
                        ownership_guard=still_current if interaction_stopped else None,
                    )
            elif kind == "adult_handoff_request":
                incident_id = str(snapshot.get("incident_id") or "")
                if not incident_id:
                    effect["resolution"] = "adult_handoff_incident_missing"
                    continue
                effect["incident_id"] = incident_id
                if self.adult_handoff is None:
                    effect["resolution"] = "adult_handoff_unavailable"
                    effect["fallback_phrase_id"] = "PHRASE_091"
                    handoff_failure = {"incident_id": incident_id}
                    continue
                handoff_id = f"{event.session_id}:{event.correlation_id or event.sequence}"
                try:
                    delivery_task = handoff_deliveries[id(effect)]
                    # Cancellation of a stale audio/HTTP turn cannot cancel a
                    # still-owned incident transport. Shutdown explicitly can.
                    delivery = await asyncio.shield(delivery_task)
                    if not still_current():
                        return stale_result()
                    if not isinstance(delivery, dict):
                        raise TypeError("adult_handoff response must be an object")
                    if type(delivery.get("acknowledged")) is not bool:
                        raise ValueError("adult_handoff acknowledgement must be boolean")
                    if "simulated" in delivery and type(delivery["simulated"]) is not bool:
                        raise ValueError("adult_handoff simulated marker must be boolean")
                    effect["delivery"] = dict(delivery)
                    if delivery.get("simulated") is True or isinstance(self.adult_handoff, SimulatedAdultHandoff):
                        # A local outbox is useful for demos, but no adult has
                        # received anything. Never emit the real receipt phrase.
                        effect["resolution"] = "adult_handoff_simulated_unavailable"
                        effect["fallback_phrase_id"] = "PHRASE_091"
                        handoff_failure = {"incident_id": incident_id}
                        continue
                    if not delivery.get("acknowledged"):
                        effect["resolution"] = "adult_handoff_pending"
                        continue
                    effect["resolution"] = "adult_handoff_acknowledged"
                    acknowledgement = await self._receive_canonical_event(Event("AdultHandoffAcknowledged", event.session_id, event.sequence, self.now(), "adult_handoff_transport", {"handoff_id": handoff_id, "incident_id": incident_id, "simulated": bool(delivery.get("simulated"))}, f"{event.correlation_id or handoff_id}:ack"), handoff_ack_authorized=True)
                    effect["acknowledgement"] = {"reason": acknowledgement.get("reason"), "simulated": bool(delivery.get("simulated"))}
                    effects.extend(acknowledgement.get("outputs", []))
                except Exception as exc:
                    if not still_current():
                        return stale_result()
                    effect["resolution"] = "adult_handoff_delivery_failed"
                    effect["fallback_phrase_id"] = "PHRASE_091"
                    handoff_failure = {"incident_id": incident_id}
                    effect["error"] = type(exc).__name__

            elif kind == "lighting":
                self.led.clear_all()
                for element_id in effect.get("elements", ()):
                    self.led.set_element(int(element_id), int(effect.get("state", 0)))
            elif kind == "scene_menu_page":
                descriptor=effect["descriptor"]
                cancel_effects=getattr(self.led,"cancel_effects",None)
                if callable(cancel_effects): cancel_effects(clear=False)
                buttons=descriptor["buttons"]
                lit={element for button in buttons for element in BUTTON_ELEMENTS.get(int(button),())}
                self.led.set_elements(tuple(LightingCommand(element,int(element in lit),0)
                    for element in LIGHTING_ELEMENT_IDS))
            elif kind == "narrate_scene_menu":
                from .scene_menus import MENU_PCM_MODES
                def menu_current():
                    return bool(still_current() and state_owner.handset=="up"
                        and state_owner.mode==MENU_PCM_MODES.get(effect.get("content_id"))
                        and state_owner.scene_menu_phase=="narrating"
                        and state_owner.playback_lease==effect.get("playback_lease"))
                try:
                    text=str(state_owner.scene_menu_descriptor.get("spoken_text") or "")
                    audio=await self._render_typed_audio(event.session_id,text,effect,
                        record.cancellation_token,menu_current)
                    if not menu_current():return stale_result()
                    if audio is None:raise RuntimeError("menu_audio_unavailable")
                    self._play_typed_audio(event.session_id,audio,effect,record.cancellation_token)
                    _set_audio_effect_status(effect,asset_status="approved",queued=True,resolution="scene_menu_audio_queued")
                except Exception:
                    if not still_current():return stale_result()
                    audio_failure={"response_id":effect["content_id"],"playback_lease":effect["playback_lease"],
                        "error_code":"scene_menu_audio_unavailable"}
                    _set_audio_effect_status(effect,asset_status="unavailable",queued=False,resolution="scene_menu_audio_unavailable")
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
                if preset == "GAME_OUTPUT":
                    commands = tuple((element_id, 1) for button in effect.get("buttons", ())
                                     if button in (1, 2, 3, 4) for element_id in BUTTON_ELEMENTS[button])
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
                if not voice_open:
                    _set_audio_effect_status(effect, asset_status=str((asset or {}).get("status") or ("approved" if asset else "absent")),
                                             queued=False, resolution="audio_deferred_handset_down")
                elif asset:
                    response_id = story_id
                    if dispatch_story_audio(response_id, str(asset["audio_file"]), effect,
                                            asset_status="approved" if verified else "placeholder",
                                            duration_ms=asset.get("duration_ms")):
                        effect["resolution"] = "resumed_verified_story" if verified else "resumed_development_placeholder"
                else:
                    _set_audio_effect_status(effect, asset_status="absent", queued=False, resolution="resume_requires_verified_story_asset")
            elif kind == "play_fixed_story":
                # The runtime has already verified rights and both SHA-256s.
                if voice_open:
                    response_id = str(effect["story_id"])
                    dispatch_story_audio(response_id, str(effect["asset"]), effect,
                                         duration_ms=effect.get("duration_ms"))
                else:
                    _set_audio_effect_status(effect, asset_status="absent", queued=False, resolution="story_audio_requires_handset")
            elif kind == "play_placeholder":
                if voice_open:
                    response_id = str(effect["story_id"])
                    if dispatch_story_audio(response_id, str(effect["asset"]), effect, asset_status="placeholder"):
                        effect["resolution"] = "development_placeholder_no_rights_or_content"
                else:
                    _set_audio_effect_status(effect, asset_status="placeholder", queued=False, resolution="placeholder_audio_requires_handset")
            elif kind == "play_private_welcome":
                owner = self._private_welcomes.get(event.session_id)
                if owner is None or not self._private_welcome_scope(event.session_id, owner) or owner.audio is None:
                    self._discard_private_welcome(event.session_id)
                    return stale_result()
                audio, owner.audio = owner.audio, None
                handle = PlaybackHandle(session_id=event.session_id,
                    generation=self._session_epochs.get(event.session_id, 0),
                    playback_id=f"{event.session_id}:{uuid.uuid4().hex}",
                    playback_lease=effect["playback_lease"],
                    response_id="PERSONAL_WELCOME", token=record.cancellation_token)
                self._register_audio_lease(event.session_id, handle.response_id, handle.token,
                    handle.playback_id, handle.playback_lease,
                    duration_ms=int(len(audio.samples) * 1000 / audio.sample_rate))
                effect.update(playback_id=handle.playback_id, generation=handle.generation)
                try:
                    self.audio.play_pcm_owned(handle, replace(audio, token=handle.token,
                        session_id=event.session_id, generation_id=handle.generation))
                    _set_audio_effect_status(effect, asset_status="approved", queued=True,
                        resolution="personal_welcome_queued")
                except Exception:
                    self._invalidate_audio_lease(event.session_id, "failed")
                    _set_audio_effect_status(effect, asset_status="approved", queued=False,
                        resolution="audio_dispatch_failed")
                    audio_failure = {"response_id": "PERSONAL_WELCOME", "token": handle.token,
                                     "error_code": "audio_dispatch_failed"}
                finally:
                    audio = None
                    self._discard_private_welcome(event.session_id)
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
                if not voice_open:
                    _set_audio_effect_status(effect, asset_status=str((asset or {}).get("status") or ("approved" if asset else "absent")),
                                             queued=False, resolution="audio_deferred_handset_down")
                elif asset:
                    try:
                        if snapshot.get("mode") == "bear_dialogue":
                            await self._set_dialogue_lighting_phase(event.session_id, "output")
                            if not still_current():
                                return stale_result()
                        self._play_owned_audio(event.session_id, phrase_id, str(asset["asset_path"]),
                                               record.cancellation_token, effect)
                    except Exception as exc:
                        _set_audio_effect_status(effect, asset_status=str(asset.get("status") or "approved"), queued=False, resolution="audio_dispatch_failed")
                        effect["audio_failure_type"] = type(exc).__name__
                        self.record_audio_diagnostic("audio_dispatch_failed", phrase_id=phrase_id,
                                                     content_id=phrase_id, asset_ref=effect.get("asset_ref"),
                                                     exception_type=type(exc).__name__, detail=str(exc)[:200],
                                                     session_id=event.session_id)
                        audio_failure = {"response_id": phrase_id, "token": record.cancellation_token,
                                     "playback_lease": effect.get("playback_lease"),
                                         "error_code": "audio_dispatch_failed", "detail": type(exc).__name__}
                        logger.exception("canonical phrase dispatch failed", extra={"phrase_id": phrase_id, "session_id": event.session_id})
                    else:
                        _set_audio_effect_status(effect, asset_status=str(asset.get("status") or "approved"), queued=True)
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
                                     "playback_lease": effect.get("playback_lease"),
                                     "error_code": str(diagnosis.get("code") or "content_unavailable")[:80]}
            elif kind in {"support_question", "support_response"}:
                phrase = str(effect["phrase_id"])
                diagnosis = self.voice_manifest.diagnose(phrase)
                asset = self.voice_manifest.resolve(phrase)
                if not voice_open:
                    _set_audio_effect_status(effect, asset_status=str((asset or {}).get("status") or ("approved" if asset else "absent")),
                                             queued=False, resolution="audio_deferred_handset_down")
                elif asset:
                    try:
                        self._play_owned_audio(event.session_id, phrase, str(asset["asset_path"]),
                                               record.cancellation_token, effect)
                    except Exception as exc:
                        _set_audio_effect_status(effect, asset_status=str(asset.get("status") or "approved"), queued=False, resolution="audio_dispatch_failed")
                        self.record_audio_diagnostic("audio_dispatch_failed", phrase_id=phrase, content_id=phrase,
                                                     asset_ref=diagnosis.get("asset_ref"), exception_type=type(exc).__name__,
                                                     detail=str(exc)[:200], session_id=event.session_id)
                        audio_failure = {"response_id": phrase, "token": record.cancellation_token,
                                         "error_code": "audio_dispatch_failed", "detail": type(exc).__name__}
                    else:
                        _set_audio_effect_status(effect, asset_status=str(asset.get("status") or "approved"), queued=True)

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
                fallback_reason = None
                if not voice_open or not (transient_text or "").strip():
                    effect["resolution"] = "bounded_dialogue_unavailable_visual_only"
                elif self.bounded_s2s is None:
                    effect["resolution"] = "bounded_dialogue_unavailable_visual_only"
                    fallback_reason = "provider_unavailable"
                else:
                    try:
                        await self.expire_identity_leases()
                        if not still_current():
                            return stale_result()
                        turn_context = copy.deepcopy(self._bounded_dialogue_context(event.session_id))
                        turn = await self.bounded_s2s.run_text_turn(str(transient_text), turn_context)
                        await self.expire_identity_leases()
                        if not still_current():
                            return stale_result()
                        # Commit only the current generation. A cancelled or late
                        # provider owns a detached copy, never retained RAM.
                        self.conversation_contexts[event.session_id] = turn_context
                        fallback_reason = turn.fallback_reason or (
                            "dialogue_exit" if turn.return_to_scenario else None
                        )
                        effect["resolution"] = ("bounded_dialogue_completed" if fallback_reason is None
                                                else "bounded_dialogue_fallback")
                        effect["fallback_reason"] = fallback_reason
                        effect["chunks"] = list(turn.chunks)
                    except Exception as exc:
                        if not still_current():
                            return stale_result()
                        logger.exception("bounded dialogue turn failed", extra={"session_id": event.session_id})
                        effect["resolution"] = "bounded_dialogue_fallback"
                        effect["fallback_reason"] = type(exc).__name__
                        fallback_reason = "provider_or_validation_failure"
                if fallback_reason is not None and still_current():
                    fallback = await self._receive_canonical_event(Event(
                        "DialogueFallback", event.session_id, event.sequence, self.now(),
                        "bounded_dialogue", {"reason": fallback_reason},
                        f"{event.correlation_id or event.session_id}:dialogue-fallback",
                        self.monotonic_now(),
                    ), dialogue_fallback_authorized=True)
                    if not still_current():
                        return stale_result()
                    effect["fallback"] = {
                        "reason": fallback.get("reason"), "phrase_id": (
                            "PHRASE_095" if fallback_reason in {"bounded_limit", "dialogue_exit"}
                            else "PHRASE_104"
                        ),
                    }
                    deferred_effects.extend(fallback.get("outputs") or [])
            elif kind == "generate_personal_story":
                owner = await self._prepare_personal_story(event.session_id, effect.get("brief") or {}, record.cancellation_token)
                if not still_current():
                    return stale_result()
                if owner is not None:
                    ready = await self._receive_canonical_event(Event(
                        "PersonalStoryReady",event.session_id,event.sequence,self.now(),"internal_personal_story",
                        {"request_id":owner.request_id},owner.request_id,self.monotonic_now()), personal_story_authorized=True)
                    if not still_current():
                        return stale_result()
                    deferred_effects.extend(ready.get("outputs") or [])
                    effect["resolution"] = "personal_story_prepared"
                    decision["reason"] = ready.get("reason")
                    snapshot = self.canonical_runtime.sessions[event.session_id].snapshot()
                else:
                    fallback = await self._receive_canonical_event(Event(
                        "DialogueFallback",event.session_id,event.sequence,self.now(),"personal_story_unavailable",
                        {"reason":"personal_story_unavailable"},f"{event.correlation_id}:story-unavailable",self.monotonic_now()), dialogue_fallback_authorized=True)
                    if not still_current():
                        return stale_result()
                    deferred_effects.extend(fallback.get("outputs") or [])
                    effect["resolution"] = "personal_story_provider_unavailable"
                    effect["fallback_phrase_id"] = "PHRASE_080"
                    decision["reason"] = fallback.get("reason")
                    snapshot = self.canonical_runtime.sessions[event.session_id].snapshot()
            elif kind == "play_personal_story":
                try:
                    queued = self._play_personal_story(event.session_id,effect,record.cancellation_token)
                except Exception:
                    queued = False
                if queued == "completed":
                    terminal = await self._receive_canonical_event(Event(
                        "AudioCompleted",event.session_id,event.sequence,self.now(),"internal_personal_story_complete",
                        {"response_id":STORY_ID,"token":record.cancellation_token,
                         "generation":effect["generation"],"playback_id":effect["playback_id"],
                         "playback_lease":effect["playback_lease"]},f"{event.correlation_id}:story-already-complete",self.monotonic_now()), personal_story_authorized=True)
                    if not still_current():
                        return stale_result()
                    deferred_effects.extend(terminal.get("outputs") or [])
                    effect["resolution"] = "personal_story_already_complete"
                    decision["reason"] = terminal.get("reason")
                    snapshot = self.canonical_runtime.sessions[event.session_id].snapshot()
                elif queued:
                    effect["resolution"] = "personal_story_pcm_queued"
                else:
                    fallback = await self._receive_canonical_event(Event(
                        "DialogueFallback",event.session_id,event.sequence,self.now(),"personal_story_unavailable",
                        {"reason":"personal_story_unavailable"},f"{event.correlation_id}:story-output-unavailable",self.monotonic_now()), dialogue_fallback_authorized=True)
                    if not still_current():
                        return stale_result()
                    deferred_effects.extend(fallback.get("outputs") or [])
                    effect["resolution"] = "personal_story_output_unavailable"
                    decision["reason"] = fallback.get("reason")
                    snapshot = self.canonical_runtime.sessions[event.session_id].snapshot()
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
                # Telemetry must not overwrite the preceding canonical TARGET.
                effect.setdefault("resolution", "declarative_effect_emitted")
            elif kind in {"exhibition_menu_selected", "menu_tutorial_complete", "exhibition_menu_tutorial_step"}:
                effect.setdefault("resolution", "canonical_exhibition_menu_boundary")
            elif kind in {"content_blocked", "privacy_blocked", "offer_adult", "visual", "clear_private_display", "game_continue_visual", "adult_incident_closed", "game_exit_confirmation", "story_speech_analysis", "hardware_reconnected", "button_edge_ack", "mood_report"}:
                effect.setdefault("resolution", "declarative_effect_emitted")
            elif kind == "typed_info_request":
                if not voice_open:
                    effect["resolution"] = "audio_deferred_handset_down"
                    continue
                tool_kind = str(effect.get("kind") or "")
                tool_name = "date" if tool_kind in {"date", "menu"} else "weather" if tool_kind == "weather" else "schedule"
                unavailable_reason = None
                try:
                    await self.expire_identity_leases()
                    if not still_current():
                        return stale_result()
                    tool_auth = self.transition_authorizer.authorize_tool(
                        snapshot.get("mode"), tool_name,
                        profile_status=str(snapshot.get("profile_status") or "anonymous"))
                    effect["tool_authorization"] = dict(tool_auth)
                    if not tool_auth.get("ok"):
                        unavailable_reason = "typed_information_blocked_by_policy"
                    elif tool_name == "schedule" and not self._identity_scope_live(event.session_id, "schedule"):
                        unavailable_reason = "typed_information_blocked_by_identity"
                    if unavailable_reason is not None:
                        raise ScheduleWeatherProviderError(unavailable_reason)
                    profile = self._canonical_profiles.get(event.session_id)
                    if tool_name != "schedule" and not self._identity_scope_live(event.session_id, "personalize"):
                        profile = None
                    schedule_ref = self._canonical_schedule_refs.get(event.session_id) if self._identity_scope_live(event.session_id, "schedule") else None
                    info = await TypedInfoRenderer(self.schedule_weather, profile, schedule_ref).render(
                        tool_kind, allow_demo=getattr(self.settings, "profile", "") == "mock")
                    def audio_current():
                        return bool(still_current() and state_owner.handset == "up"
                            and state_owner.mode == "schedule_weather"
                            and state_owner.playback_lease == effect.get("playback_lease")
                            and (tool_name != "schedule" or self._identity_scope_live(event.session_id,"schedule")))
                    if not audio_current():
                        return stale_result()
                    audio = await self._render_typed_audio(event.session_id, info["text"], effect,
                                                          record.cancellation_token, audio_current)
                    info = None  # Never put a private schedule/name in public effects or TTS cache.
                    if not audio_current():
                        return stale_result()
                    if audio is None:
                        raise ScheduleWeatherProviderError("typed_information_audio_unavailable")
                    self._play_typed_audio(event.session_id, audio, effect, record.cancellation_token)
                    audio = None
                    _set_audio_effect_status(effect, asset_status="approved", queued=True,
                                             resolution="typed_information_audio_queued")
                except Exception:
                    if not still_current():
                        return stale_result()
                    effect["resolution"] = unavailable_reason or "typed_information_unavailable"
                    fallback = await self._receive_canonical_event(Event(
                        "DialogueFallback", event.session_id, event.sequence, self.now(),
                        "typed_information", {"reason": "typed_information_unavailable", "kind": tool_kind},
                        f"{event.correlation_id or event.session_id}:typed-unavailable", self.monotonic_now(),
                    ), dialogue_fallback_authorized=True)
                    if not still_current():
                        return stale_result()
                    deferred_effects.extend(fallback.get("outputs") or [])
                    snapshot = state_owner.snapshot()

        if not still_current():
            return stale_result()
        effects.extend(deferred_effects)
        if handoff_failure is not None:
            # Delivery failure is a canonical outcome. Its recorded fallback
            # receives a lease and the same terminal error handling as speech.
            failure = await self._receive_canonical_event(Event(
                "AdultHandoffFailed", event.session_id,
                state_owner.last_sequence_by_source.get("internal_handoff_failure", 0) + 1, self.now(),
                "internal_handoff_failure", handoff_failure,
                f"{event.correlation_id or event.session_id}:handoff-failed", self.monotonic_now(),
            ), handoff_failure_authorized=True)
            failure["outputs"] = [dict(item) for item in effects] + list(failure.get("outputs") or [])
            self.last_canonical_effects = [dict(item) for item in failure["outputs"]]
            return failure
        if audio_failure is not None and event.event_type != "AudioFailed":
            failure = await self._receive_canonical_event(Event(
                "AudioFailed", event.session_id,
                state_owner.last_sequence_by_source.get("canonical_audio_failure", 0) + 1, self.now(),
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
        if not exhibition_reset and not still_current():
            return stale_result()
        self.publisher.publish(published_event)
        final_state = dict(snapshot) if exhibition_reset else self.canonical_runtime.sessions[event.session_id].snapshot()
        self.last_canonical_state = dict(final_state)
        hook = self.state_update_hook
        if callable(hook):
            try:
                update = hook(event.session_id, {"reason": decision["reason"], "state": final_state.get("mode"), "canonical_state": final_state, "outputs": effects})
                if inspect.isawaitable(update):
                    await update
            except Exception:
                logger.exception("canonical state update hook failed", extra={"session_id": event.session_id})
        if not exhibition_reset and not still_current():
            return stale_result()
        if self.optional_identity and self._identity_scope_live(event.session_id):
            self._schedule_identity_deadline(event.session_id)
        self._refresh_engagement_busy(event.session_id)
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
        from .domain import EVENT_TYPES
        if event.event_type not in EVENT_TYPES:
            return self._identity_result(event.session_id, "unsupported_event")
        if event.event_type == "InteractionIdleExpired":
            return self._identity_result(event.session_id, "interaction_idle_untrusted")
        if event.event_type in {"ClientProfileReady", "ClientProfileCleared"}:
            return self._identity_result(event.session_id, "max_json_requires_private_client_boundary")
        waiter = self._session_reset_waiters.get(event.session_id)
        if waiter is not None and waiter[0] is not asyncio.current_task():
            if event.event_type in {"AudioCompleted", "AudioFailed"}:
                return {"session_id": event.session_id, "state": "idle",
                        "reason": "stale_session_generation", "outputs": [],
                        "canonical_state": {"mode": "idle", "handset": "down"}}
            await waiter[1]
        await self.expire_engagement_sessions()
        await self.expire_idle_sessions()
        if self.canonical_runtime is None:
            return {"session_id": event.session_id, "state": Lifecycle.IDLE.value,
                    "node": None, "reason": "canonical_runtime_required",
                    "outputs": [{"action": "safe_stop"}],
                    "correlation_id": event.correlation_id}
        if (event.event_type == "ButtonPressed"
                and getattr(self.canonical_runtime, "exhibition_mode", False)):
            active = self.canonical_runtime.sessions.get(event.session_id)
            authorized_ready = bool(self.optional_identity and active is not None
                and active.handset == "down" and active.identity_request_id
                and (active.mode == "profile_ready" or active.interaction_idle))
            if active is None or (active.handset != "up" and not authorized_ready):
                return {"session_id": event.session_id, "state": "idle",
                        "reason": "exhibition_age_not_ready", "outputs": [],
                        "canonical_state": {"mode": "idle", "handset": "down"}}
        # In exhibition mode a down edge while no physical session exists is
        # already the neutral state. Do not materialize a new repository or
        # canonical session merely to acknowledge a repeated SpacePressed.
        active = self.canonical_runtime.sessions.get(event.session_id)
        if (active is not None and active.interaction_idle and event.event_type == "ButtonPressed"
                and event.payload.get("button_id") in self._engagement_pressed.get(event.session_id,set())):
            return self._identity_result(event.session_id,"interaction_idle_button_held")
        if active is not None and active.interaction_idle and event.event_type == "SpeechReceived":
            return self._identity_result(event.session_id,"interaction_idle_button_required")
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
        if result.get("reason") == "stale_session_generation":
            return result
        if event.event_type in {"AudioCompleted", "AudioFailed"}:
            state_after_audio = self.canonical_runtime.sessions.get(event.session_id)
            if state_after_audio is not None and state_after_audio.mode == "bear_dialogue":
                await self._set_dialogue_lighting_phase(event.session_id, "listening")
        if event.event_type in {"AudioCompleted", "AudioFailed"} and callback_lease is not None:
            if self._active_audio.get(event.session_id) is callback_lease:
                self._cancel_audio_timeout(event.session_id)
                callback_lease["status"] = "finished" if event.event_type == "AudioCompleted" else "failed"
                self._active_audio.pop(event.session_id, None)
        self._refresh_engagement_busy(event.session_id)
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

    def _bounded_dialogue_context(self, session_id: str):
        """Reuse committed RAM within the same authorized physical/profile owner."""
        self.expire_dialogue_memory()
        state = self.canonical_runtime.sessions.get(session_id)
        if (self.optional_identity and state is not None and state.identity_request_id
                and not self._identity_scope_live(session_id, "personalize")):
            self._drop_dialogue_context(session_id)
        fresh = self._canonical_audio_context(session_id)
        owner = (self._session_epochs.get(session_id, 0),
                 state.profile_ref if state is not None else None)
        current = self.conversation_contexts.get(session_id)
        if current is not None and getattr(current, "_owner", None) == owner:
            self._resume_dialogue_context(session_id, current)
            current.node_id = fresh.node_id
            current.profile = fresh.profile
            current.system_prompt = fresh.system_prompt
            current.allowed_topics = fresh.allowed_topics
            current.forbidden_topics = fresh.forbidden_topics
            return current
        fresh._owner = owner
        self.conversation_contexts[session_id] = fresh
        return fresh

    def _canonical_audio_context(self, session_id: str):
        # LocalS2SPipeline needs a bounded context object for VAD/STT
        # bookkeeping. Canonical voice turns never pass through its LLM/TTS
        # conversation path; the STT transcript is re-ingressed as a
        # canonical SpeechReceived event below.
        from .s2s import ConversationContext, ProfileContext
        state = self.canonical_runtime.sessions.get(session_id)
        profile = self._canonical_profiles.get(session_id)
        state_snapshot = state.snapshot() if state is not None else {"mode": "bear_dialogue", "handset": "up"}
        if self.optional_identity and state is not None and state.identity_request_id and not self._identity_scope_live(session_id, "personalize"):
            profile = None
            # Schedule-only grants do not authorize names, age or accessibility
            # context. An expired grant must likewise be unreadable immediately,
            # even before the async expiry cleanup has been scheduled.
            state = None
            state_snapshot = {"mode": state_snapshot.get("mode"), "handset": state_snapshot.get("handset"),
                              "profile_status": "anonymous"}
        # Onboarding identity is session-scoped RAM, not a persisted profile.
        # Keep it available to the bounded voice context after the anonymous
        # name/age exchange while leaving the public snapshot redacted.
        private_profile = dict(profile or {})
        if state is not None:
            if state.display_name:
                private_profile.setdefault("display_name", state.display_name)
            private_profile["age_band"] = state.age_band
            state_snapshot["onboarding_name_collected"] = bool(state.onboarding_name_collected)
            state_snapshot["dialogue_memory"] = {
                "status": state.dialogue_memory.status,
                "turns": list(state.dialogue_memory.bounded_turns),
            }
        prompt = build_system_prompt(profile=private_profile or None, state=state_snapshot)
        profile_context = ProfileContext.from_profile(private_profile or None)
        selected_age = trusted_canonical_age(state_snapshot)
        if selected_age is not None:
            profile_context = replace(
                profile_context, age=selected_age["age_years"],
                age_band=selected_age["age_band"],
                age_source=selected_age["age_band_source"],
            )
        node_id = str(state_snapshot.get("mode") or "bear_dialogue")
        # The dedicated conversation mode accepts ordinary replies and new
        # harmless topics, not only seven lexical keywords. Canonical input
        # safety, forbidden topics, output validation and budgets still apply.
        # Other scenarios retain their existing topic allowlist.
        allowed_topics = (("__all__",) if node_id == "bear_dialogue" else
                          ("greetings", "clinic", "weather", "games", "stories", "feelings", "questions"))
        from .age_policy import POLICIES
        turn_limit = POLICIES.get(profile_context.age_band, POLICIES["3-5"]).dialogue_turn_limit
        return ConversationContext(session_id, "canonical_product", node_id, allowed_topics, ("diagnosis", "medicine", "secret", "personal_data"), turn_limit, 120, 2, profile=profile_context, system_prompt=prompt)

    async def _complete_canonical_audio_turn(self, session_id: str, sequence: int, pending: asyncio.Task[Any], epoch: int, correlation_id: str, *, _ingress_guard=None) -> None:
        try:
            audio_result = await pending
        except asyncio.CancelledError:
            return
        await self.expire_identity_leases()
        session = self.sessions.get(session_id)
        if session is None or session.state == Lifecycle.ENDED or self._session_epochs.get(session_id, 0) != epoch or session.correlation_id != correlation_id:
            return
        if audio_result.stt is None or not audio_result.stt.text.strip():
            return
        payload = await self._receive_canonical_event(
            Event("SpeechReceived", session_id, sequence, self.now(), "uart_audio",
                  {"length": len(audio_result.stt.text), "_stt_untrusted": self._untrusted_stt(audio_result)},
                  f"{correlation_id}:audio:{sequence}"),
            audio_result.stt.text,
            _speech_ingress_guard=_ingress_guard,
            _speech_lifecycle_guard=getattr(_ingress_guard, "lifecycle_current", _ingress_guard),
        )
        if payload.get("reason") != "stale_session_generation" and self._session_epochs.get(session_id, 0) == epoch:
            await self._invoke_audio_update_hook(session_id, payload, generation=epoch, correlation_id=correlation_id)

    def _untrusted_stt(self, result) -> bool:
        threshold = getattr(getattr(self.s2s_audio, "config", None), "stt_confidence_threshold", 0.45)
        stt = result.stt
        return (stt is None or stt.unknown or type(stt.confidence) not in (int, float)
                or not math.isfinite(stt.confidence) or not threshold <= stt.confidence <= 1
                or result.ingress_status in {"stt_unknown", "stt_low_confidence", "stt_failure"})

    async def _receive_canonical_audio(self, session_id: str, samples: tuple[float, ...], sequence: int, *, wait_for_turn: bool, _ingress_guard=None) -> dict[str, Any]:
        await self.expire_engagement_sessions()
        state = self.canonical_runtime.sessions.get(session_id)
        if state is not None and state.interaction_idle:
            return {"session_id": session_id, "reason": "interaction_idle_button_required", "outputs": [], "pending": False}
        if _ingress_guard is not None and not _ingress_guard():
            return {"session_id": session_id, "reason": "stale_session_generation", "outputs": [], "pending": False}
        session = self.sessions.get(session_id)
        if session is None:
            return {"session_id": session_id, "state": Lifecycle.IDLE.value, "outputs": [], "reason": "audio_session_required", "audio_status": "ignored", "pending": False}
        if self.s2s_audio is None:
            return {**self._result(session, [], "audio_unavailable"), "audio_status": "ignored", "barge_in": False, "pending": False}
        if session.state == Lifecycle.ENDED or sequence <= session.last_sequence:
            return {**self._result(session, [], "late_or_duplicate_audio"), "pending": False}
        if session.handset_up is not True:
            return {**self._result(session, [], "audio_not_listening"), "audio_status": "handset_required", "pending": False, "barge_in": False}
        epoch = self._session_epochs.get(session_id, 0)
        session.last_sequence, session.last_activity_at = sequence, self.now()
        context = self._canonical_audio_context(session_id)
        from .s2s import TurnResult, AudioIngressPipeline, LocalS2SPipeline
        # An utterance can span several uploads and a retry phase boundary.
        # Keep its first private-listener owner on the bounded VAD instance,
        # not on the final (possibly fresh-phase silence) transport frame.
        utterance_guard = _ingress_guard
        if isinstance(self.s2s_audio, LocalS2SPipeline):
            pipeline = self.s2s_audio.ingress.setdefault(session_id,
                AudioIngressPipeline(self.s2s_audio.vad, self.s2s_audio.config))
            buffered = pipeline.active or bool(pipeline._vad_pending) or bool(pipeline._pre_roll)
            prior_owner = getattr(pipeline, "_canonical_private_owner", None)
            state = self.canonical_runtime.sessions.get(session_id)
            if buffered and prior_owner is not None:
                utterance_guard = prior_owner
            else:
                utterance_guard = _ingress_guard
                pipeline._canonical_private_owner = (_ingress_guard
                    if state is not None and state.mode == "personal_welcome_listen" else None)
        async def no_llm_turn(_text: str, _generation: int):
            return TurnResult((), (), False, None)
        audio_result = await self.s2s_audio.feed_audio(session_id, samples, context, wait_for_turn=wait_for_turn, turn_handler=no_llm_turn)
        if (isinstance(self.s2s_audio, LocalS2SPipeline)
                and getattr(pipeline, "_canonical_private_owner", None) is utterance_guard
                and audio_result.ingress_status == "collecting"):
            if not pipeline.active and utterance_guard is not None and not utterance_guard():
                # Low-VAD pre-roll can contain speech: keep its provenance until
                # normal bounded rolling has certainly displaced every old sample.
                replacement = getattr(pipeline, "_canonical_private_replacement_owner", None)
                if replacement is None or not replacement():
                    pipeline._canonical_private_stale_samples = 0
                    pipeline._canonical_private_replacement_owner = _ingress_guard
                stale_samples = getattr(pipeline, "_canonical_private_stale_samples", 0) + len(samples)
                pipeline._canonical_private_stale_samples = stale_samples
                if stale_samples >= (pipeline._pre_roll.maxlen or 0) + 512:
                    pipeline._canonical_private_owner = _ingress_guard
                    pipeline._canonical_private_stale_samples = 0
                    pipeline._canonical_private_replacement_owner = None
            else:
                pipeline._canonical_private_stale_samples = 0
                pipeline._canonical_private_replacement_owner = None
        _ingress_guard = utterance_guard
        await self.expire_identity_leases()
        current = self.sessions.get(session_id)
        if (current is not session or session.state == Lifecycle.ENDED
                or not session.handset_up or self._session_epochs.get(session_id, 0) != epoch
                or (_ingress_guard is not None and not getattr(_ingress_guard, "lifecycle_current", _ingress_guard)())):
            return {"session_id": session_id, "state": "idle", "outputs": [],
                    "reason": "stale_session_generation", "audio_status": "ignored",
                    "pending": False, "barge_in": False}
        barge_outputs = [{"action": "barge_in", "status": "previous_output_cancelled"}] if audio_result.barge_in else []
        if audio_result.pending is not None:
            epoch = self._session_epochs.get(session_id, 0)
            completion = asyncio.create_task(self._complete_canonical_audio_turn(session_id, sequence, audio_result.pending, epoch, session.correlation_id, _ingress_guard=_ingress_guard), name=f"canonical-audio-{session_id}")
            owned = self._audio_completions.setdefault(session_id, set()); owned.add(completion)
            self._audio_completion_started_at[session_id] = self.monotonic_now()
            completion.add_done_callback(self._audio_completion_callback(session_id))
            self.sessions.save(session)
            return {**self._result(session, barge_outputs, "audio_processing"), "audio_status": "processing", "pending": True, "barge_in": audio_result.barge_in}
        if audio_result.stt is not None and audio_result.stt.text.strip():
            result = await self._receive_canonical_event(Event("SpeechReceived", session_id, sequence, self.now(), "uart_audio", {"length": len(audio_result.stt.text), "_stt_untrusted": self._untrusted_stt(audio_result)}, f"{session.correlation_id}:audio:{sequence}"), audio_result.stt.text, _speech_ingress_guard=_ingress_guard, _speech_lifecycle_guard=getattr(_ingress_guard, "lifecycle_current", _ingress_guard))
            result["audio_status"] = audio_result.ingress_status; result["pending"] = False; result["barge_in"] = audio_result.barge_in
            return result
        if _ingress_guard is not None and not _ingress_guard():
            return {**self._result(session, [], "stale_session_generation"), "pending": False}
        if audio_result.turn is not None and audio_result.turn.fallback_reason:
            # This is an STT/VAD ingress retry, not an adult-handoff failure.
            # Keep it visual/diagnostic until a canonical retry phrase is
            # authorized for every mode; never dispatch untracked audio.
            return {**self._result(session, barge_outputs + [{"action": "audio_retry", "reason": audio_result.turn.fallback_reason}], "audio_retry"), "audio_status": audio_result.ingress_status, "pending": False, "barge_in": audio_result.barge_in}
        self.sessions.save(session)
        return {**self._result(session, barge_outputs, audio_result.ingress_status), "audio_status": audio_result.ingress_status, "pending": False, "barge_in": audio_result.barge_in}

    async def receive_audio_async(self, session_id: str, samples: tuple[float, ...], sequence: int, *, wait_for_turn: bool = True, _ingress_guard=None) -> dict[str, Any]:
        if _ingress_guard is None:
            record = self.sessions.get(session_id)
            epoch = self._session_epochs.get(session_id, 0)
            handset_up = bool(record and record.handset_up)
            _ingress_guard = lambda: (self.sessions.get(session_id) is record
                                     and bool(record and record.handset_up) == handset_up
                                     and self._session_epochs.get(session_id, 0) == epoch)
        # Ordinary phase authority is narrower than physical/session ownership:
        # a completed retry closes the old private-listener answer, but must not
        # erase a danger disclosure from the same still-live capture owner.
        state = self.canonical_runtime.sessions.get(session_id)
        version = state.state_version if state is not None else None
        private_phase = bool(state and state.mode == "personal_welcome_listen")
        transport_guard = _ingress_guard
        phase_guard = lambda: (transport_guard()
            and (not private_phase or (self.canonical_runtime.sessions.get(session_id) is state
                and state.state_version == version)))
        phase_guard.lifecycle_current = getattr(transport_guard, "lifecycle_current", transport_guard)
        return await self._receive_canonical_audio(session_id, samples, sequence, wait_for_turn=wait_for_turn, _ingress_guard=phase_guard)

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
        tasks = tuple(task for task in self._audio_completions.get(session_id, set())
                      if task is not asyncio.current_task())
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
        boundary = self._max_client_boundary
        if not self.max_json or boundary is None or boundary.session_id != session_id:
            return await self._reset_session_inner(session_id)
        waiter = self._session_reset_waiters.get(session_id)
        if waiter is not None and waiter[0] is not asyncio.current_task():
            await waiter[1]
        completion = asyncio.get_running_loop().create_future()
        self._session_reset_waiters[session_id] = (asyncio.current_task(), completion)
        try:
            self._revoke_local_profile(session_id)
            return await self._reset_session_inner(session_id)
        finally:
            try:
                # Also discard source responses arriving while reset awaited
                # audio cleanup. Only a subsequent scan can own the fresh session.
                if self._max_client_boundary is boundary:
                    boundary.clear_client()
            finally:
                self._session_reset_waiters.pop(session_id, None)
                if not completion.done():
                    completion.set_result(None)

    async def _reset_session_inner(self, session_id: str) -> dict[str, Any]:
        self._drop_identity_owner(session_id)
        incident = self.canonical_runtime.sessions.get(session_id) if self.canonical_runtime is not None else None
        if incident is not None and incident.safety_severity == "urgent":
            # A generic operator reset is not an AdultResolved event. Stop
            # in-flight work while preserving the visible incident.
            self._session_epochs[session_id] = self._session_epochs.get(session_id, 0) + 1
            self._session_revisions[session_id] = self._session_revisions.get(session_id, 0) + 1
            self.canonical_runtime.clear_identity(incident)
            incident.state_version += 1
            record = self.sessions.get(session_id)
            self._invalidate_audio_lease(session_id)
            if record is not None:
                record.cancellation_token += 1
                record.child_id, record.anonymous = None, True
                self.audio.cancel(record.cancellation_token)
                self.sessions.save(record)
            await self._cleanup_session_resources(session_id)
            state = incident.snapshot()
            self.last_canonical_state = dict(state)
            return {"session_id": session_id, "state": state["mode"], "reset": False,
                    "reason": "safety_handoff_active", "canonical_state": state}
        self._discard_private_welcome(session_id)
        self._drop_engagement(session_id, keys=True)
        self._cancel_game_timer(session_id)
        self._cancel_welcome_timer(session_id)
        self._welcome_timer_sequences.pop(session_id, None)
        self._cancel_menu_tutorial(session_id)
        self._menu_tutorial_sequences.pop(session_id, None)
        self._cancel_lighting_wave(session_id)
        self._session_epochs[session_id] = self._session_epochs.get(session_id, 0) + 1
        self._session_revisions[session_id] = self._session_revisions.get(session_id, 0) + 1
        self._invalidate_audio_lease(session_id)
        cleanup = await self._cancel_audio_completions(session_id, operation="reset")
        if self.bounded_s2s is not None:
            await self._cancel_audio_owner(self.bounded_s2s, session_id, operation="reset")
        if self.s2s_audio is not None:
            await self._cancel_audio_owner(self.s2s_audio, session_id, operation="reset")
        self._drop_dialogue_context(session_id)
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
                if preset == "GAME_OUTPUT":
                    commands = tuple((element_id, 1) for button in effect.get("buttons", ())
                                     if button in (1, 2, 3, 4) for element_id in BUTTON_ELEMENTS[button])
                if preset == "TARGET":
                    commands = tuple(
                        (element_id, 1)
                        for element_id in BUTTON_ELEMENTS.get(int(effect.get("target_button", 0)), ())
                    )
                for element_id, state in commands:
                    self.led.set_element(element_id, state)



    @staticmethod
    def _result(session, outputs, reason): return {"session_id":session.session_id, "state":session.state.value, "node":session.current_node, "reason":reason, "outputs":outputs, "correlation_id":session.correlation_id}
