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
from functools import wraps
from typing import Any, Callable
from weakref import WeakSet

from .game_engine import next_deadline as game_deadline, cancel_exit_gesture
from .engagement_idle import EngagementIdleMixin
from .domain import AudioOutput, DisplayController, Event, EventPublisher, IntentClassifier, LEDController, Lifecycle, PlaybackHandle, ScenarioRepository, SessionRecord, SessionRepository
from .lighting import BUTTON_DIGIT_ELEMENTS, BUTTON_FILL_ELEMENTS, LIGHTING_ELEMENT_IDS, LightingCommand, EXHIBITION_DIGIT_WAVE_GROUPS, EXHIBITION_MENU_WAVE_GROUPS, EXHIBITION_WAVE_STAGE_MS, READINESS_WAVE_DURATION_MS, READINESS_WAVE_GROUPS, PICTOGRAM_ELEMENTS
from .schedule_weather import ScheduleWeatherProviderError, validate_report
from .age_policy import trusted_canonical_age
from .canonical_services import LED_PRESETS, DIALOGUE_FILL_ELEMENTS, DIALOGUE_ANCHOR_ELEMENTS, BUTTON_ELEMENTS, RecordedPhraseResolver, TypedInfoRenderer, classify_intent, game_confirmation_intent, wellbeing_reassurance, weather_request_scope
from .config import settings_from_compat_env
from .identity import IdentityDecision, IdentityValidationError, validate_identity_decision
from .input_safety import InputSafetyGate
from .lifecycle import RuntimeBusyError
from .canonical_contract import CanonicalEvent, CanonicalRuntime, FixedStoryManifest
from .system_prompt import build_system_prompt
from .private_welcome import PrivateWelcomeMixin
from .owned_speech import OwnedSpeechMixin, TYPED_AUDIO_IDS, MENU_AUDIO_IDS
from .personal_story import PersonalStoryMixin, STORY_MODES, STORY_ID, story_theme
from .dialogue_memory import DialogueMemoryMixin
from .game_presentation import GamePresentationMixin, game_light_commands
from app.scenarios.canonical_graph import CanonicalLangGraphAuthorizer

logger = logging.getLogger("mishka.application")

# PHRASE_008 is one approved 18.406 s WAV. These offsets are the measured
# starts of each spoken digit and its following action phrase in that file.
STORY_PLAYBACK_WAVE_GROUPS = tuple((BUTTON_FILL_ELEMENTS[button],) for button in range(1, 8))
STORY_CHOICE_WAVE_GROUPS = tuple((BUTTON_FILL_ELEMENTS[button],) for button in range(1, 6))
STORY_MENU_BASE_ELEMENTS = tuple(
    BUTTON_DIGIT_ELEMENTS[button][1] for button in range(1, 8)
)

EXHIBITION_MENU_PHASE_TIMINGS_MS = (
    (1, "digit", 380), (1, "full", 1520),
    (2, "digit", 2800), (2, "full", 3690),
    (3, "digit", 5160), (3, "full", 5960),
    (4, "digit", 7670), (4, "full", 8700),
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


_MAINTENANCE_CLEANUP_EVENTS = frozenset({
    "HandsetPlaced", "SessionCancel", "SessionReset", "HardwareDisconnected",
    "ClientProfileCleared", "IdentityReadyExpired", "IdentityAuthorizationExpired",
    "InteractionIdleExpired", "TimeoutFired", "AudioStarted", "AudioCompleted", "AudioFailed",
    "StoryPositionUpdated",
})


def _runtime_ingress_guard(method):
    @wraps(method)
    async def guarded(self, *args, **kwargs):
        first = args[0] if args else kwargs.get("event", kwargs.get("session_id"))
        event = first if isinstance(first, Event) else None
        with self._runtime_work(cleanup=bool(event and event.event_type in _MAINTENANCE_CLEANUP_EVENTS)) as admitted:
            if not admitted:
                return self._identity_result(event.session_id if event else first, "runtime_unavailable")
            return await method(self, *args, **kwargs)
    return guarded


class ScenarioApplication(EngagementIdleMixin, PrivateWelcomeMixin, OwnedSpeechMixin, PersonalStoryMixin, DialogueMemoryMixin, GamePresentationMixin):
    """Use-case layer. It depends only on domain ports, never FastAPI/files/UART/LLM."""
    AUDIO_RECOVERABLE_FAILURES = frozenset({
        "vad_failure",
        "stt_failure",
        "stt_low_confidence",
        "audio_too_long",
    })

    def __init__(self, sessions: SessionRepository, scenarios: ScenarioRepository, classifier: IntentClassifier, audio: AudioOutput, led: LEDController, publisher: EventPublisher, now=time.time, installation_id="default", threshold=0.6, bounded_s2s=None, history=None, max_session_seconds=1200, idle_timeout_seconds: float | None = None, display: DisplayController | None = None, schedule_weather: Any = None, allow_quick_replies: bool | None = None, settings: Any = None, monotonic_now=time.monotonic, background_scheduler: bool = True):
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
        self._identity_replacements: dict[str, tuple[Any, asyncio.Task[Any] | None]] = {}
        self._identity_shutdown = False
        self._dialogue_journal = None
        self._journal_visits = {}
        self._journal_jobs = set()
        self._journal_shutdown_task = None
        if getattr(settings, "dialogue_journal_enabled", False) is True:
            from pathlib import Path
            from .session_journal import SessionJournal
            self._dialogue_journal = SessionJournal(
                enabled=True, root=Path(settings.dialogue_journal_dir))
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
        self._suspended_dialogue_contexts: dict[str, Any] = {}
        self._game_callers: dict[str, Any] = {}
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
        self._profile_context_generations: dict[str, int] = {}
        self._session_revisions: dict[str, int] = {}
        self._runtime_ingress: dict[asyncio.Task[Any], object] = {}
        self._runtime_maintenance: object | None = None
        # A physical reset owns cleanup until it has finished publishing.
        # New ingress for that session waits without holding a global lock.
        self._session_reset_waiters: dict[str, tuple[asyncio.Task[Any], asyncio.Future[None]]] = {}
        # One active immutable playback lease per session. Adapter callbacks
        # must match it before entering the canonical state machine.
        self._active_audio: dict[str, dict[str, Any]] = {}
        # Browser pause reports position after the pause command is published.
        # Retain only that immutable story execution until its one checkpoint.
        self._paused_story_playback: dict[str, dict[str, Any]] = {}
        self._public_story_checkpoint_transfers: dict[str, dict[str, Any]] = {}
        self._audio_timeout_tasks: dict[str, asyncio.Task[Any]] = {}
        self._audio_timeout_sequences: dict[str, int] = {}
        self._game_timer_tasks: dict[str, asyncio.Task[Any]] = {}
        self._game_timer_scopes: dict[str, tuple] = {}
        self._game_timer_sequences: dict[str, int] = {}
        self._game_speech_checks: dict[asyncio.Task, tuple] = {}
        self._game_audio_waits: dict[object, dict] = {}
        self._welcome_timers: dict[str, asyncio.Task[Any]] = {}
        self._private_welcomes: dict[str, Any] = {}
        self._private_welcome_tasks: set[asyncio.Task[Any]] = set()
        self._owned_text_jobs: set[asyncio.Task[Any]] = set()
        self._game_prompt_cues = {}
        self._game_cue_tasks = {}
        self._game_cue_started = set()
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
        self.technical_state_observer: Any = None
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
        from .name_audio import NameAudioRegistry
        self.name_audio_registry = NameAudioRegistry(getattr(settings, "name_audio_manifest", "assets/names/manifest.json"))
        if self.bounded_s2s is not None:
            self.bounded_s2s.name_audio_registry = self.name_audio_registry
        self.safety_classifier = InputSafetyGate(settings, story_backend_contract=
            getattr(self.bounded_s2s, "story_backend_contracts", {}).get("validator"))
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
        self._game_callers.pop(session_id, None)
        self._drop_dialogue_context(session_id)

    def _has_private_identity_context(self, session_id: str) -> bool:
        state = self.canonical_runtime.sessions.get(session_id)
        return bool((state is not None and self.canonical_runtime.has_private_context(state))
                    or session_id in self.conversation_contexts or session_id in self._game_callers
                    or session_id in self._personal_stories
                    or any(key[0] == session_id and count for key, count in self._engagement_ingress.items())
                    or any(getattr(task, '_mishka_session_id', None) == session_id and not task.done()
                           for task in self._owned_text_jobs))

    async def _prepare_fresh_profile(self, event: Event, identity_current=None) -> str | None:
        """Retire the old scenario before making any new profile readable."""
        sid = event.session_id
        runtime = self.canonical_runtime
        old = runtime.sessions.get(sid)
        fresh = runtime._new_state(sid, None)
        if old is not None:
            fresh.handset = old.handset
            carry_panel = old.interaction_open or (old.substate == "profile_cleared"
                and self._identity_replacements.get(sid, (None,))[0] is old)
            fresh.interaction_route = old.interaction_route if old.interaction_route != "panel" or carry_panel else None
            fresh.silent_mode = bool(old.silent_mode and fresh.handset == "down" and carry_panel)
            fresh.state_version = old.state_version + 1
            fresh.last_sequence_by_source = dict(old.last_sequence_by_source)
            fresh.seen_correlations = set(old.seen_correlations)
            fresh.accepted_buttons_down = list(old.accepted_buttons_down)
            fresh.readiness_held_buttons = list(dict.fromkeys(old.readiness_held_buttons + old.accepted_buttons_down))
            runtime.clear_identity(old)
        fresh.substate = 'profile_replacing'
        runtime.sessions[sid] = fresh
        # A fresh card retires old screen actions before cleanup can yield.
        self._profile_context_generations[sid] = self._profile_context_generations.get(sid, 0) + 1
        self._session_epochs[sid] = self._session_epochs.get(sid, 0) + 1
        self._session_revisions[sid] = self._session_revisions.get(sid, 0) + 1
        epoch, revision = self._session_epochs[sid], self._session_revisions[sid]
        boundary = self._max_client_boundary
        generation = event.payload['client_generation']

        def current():
            return (runtime.sessions.get(sid) is fresh and self._session_epochs.get(sid) == epoch
                    and self._session_revisions.get(sid) == revision
                    and self._max_client_boundary is boundary and boundary.generation == generation)

        self._drop_identity_owner(sid)
        self._identity_replacements[sid] = (fresh, asyncio.current_task())
        self._paused_story_playback.pop(sid, None)
        self.last_canonical_state = fresh.snapshot()
        record = self.sessions.get(sid)
        failed = False
        try:
            if record is not None:
                record.child_id, record.anonymous = None, True
                record.current_node = fresh.mode
                record.cancellation_token += 1
                self.sessions.save(record)
                self.audio.cancel(record.cancellation_token)
            close_capture = getattr(self.s2s_audio, 'close_capture', None)
            if callable(close_capture):
                close_capture(sid)
            cancel_effects = getattr(self.led, 'cancel_effects', None)
            cancel_effects(clear=True) if callable(cancel_effects) else self.led.clear_all()
        except Exception:
            failed = True
        try:
            cleanup = await self._cleanup_session_resources(sid)
            completion = cleanup.get('audio_completion_cleanup', {})
            failed = failed or not completion.get('cleanup_completed', False) or bool(completion.get('failed'))
            failed = failed or cleanup.get('audio_owner_cleanup', {}).get('status') != 'completed'
        except (asyncio.CancelledError, Exception):
            if current():
                fresh.mode, fresh.substate = 'session_closed', 'profile_cleanup_failed'
                self._identity_replacements[sid] = (fresh, None)
                self.last_canonical_state = fresh.snapshot()
            raise
        if not current() or cleanup.get('cleanup_stale'):
            return 'stale_profile_context'
        if failed:
            fresh.mode, fresh.substate = 'session_closed', 'profile_cleanup_failed'
            self._identity_replacements[sid] = (fresh, None)
            self.last_canonical_state = fresh.snapshot()
            return 'profile_cleanup_failed'
        if identity_current is not None and not identity_current():
            self._identity_replacements.pop(sid, None)
            return 'stale_profile_context'
        if (self.monotonic_now() >= float(event.payload['expires_monotonic'])
                or (fresh.handset == 'down' and self.monotonic_now() >= float(event.payload['ready_deadline']))):
            self._identity_replacements.pop(sid, None)
            return 'invalid_client_profile'
        fresh.readiness_held_buttons = list(dict.fromkeys(fresh.readiness_held_buttons + fresh.accepted_buttons_down))
        self._identity_replacements.pop(sid, None)
        return None

    def _profile_replacing(self, session_id):
        owner = self._identity_replacements.get(session_id)
        return bool(owner and owner[0] is self.canonical_runtime.sessions.get(session_id)
                    and owner[1] is not None and not owner[1].done())

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
            if state.mode != "safety_stop":
                state.mode = "session_closed" if state.handset == "up" else "idle"
                state.expected_inputs = ["handset_down"] if state.handset == "up" else ["handset_up"]
            self.last_canonical_state = state.snapshot()
        record = self.sessions.get(session_id)
        if record is not None:
            record.child_id, record.anonymous = None, True
            if state is not None:
                record.current_node = state.mode
            self.sessions.save(record)

    def _journal_visit(self, session_id):
        if self._dialogue_journal is None or self._identity_shutdown:
            return None
        visit = self._journal_visits.get(session_id)
        if visit is None:
            visit = {"id": uuid.uuid4().hex, "lock": asyncio.Lock(), "closed": False}
            self._journal_visits[session_id] = visit
        return visit

    async def _journal_io(self, operation, *args):
        # Keep native write ownership until it settles, even if ingress is cancelled.
        work = asyncio.create_task(asyncio.to_thread(operation, *args))
        interrupted = False
        while True:
            try:
                result = await asyncio.shield(work)
                break
            except asyncio.CancelledError:
                if work.cancelled():
                    raise
                interrupted = True
            except Exception:
                result = False
                break
        if interrupted:
            raise asyncio.CancelledError
        return result

    async def _append_journal_turn(self, session_id, visit, role, text, current):
        if visit is None or self._dialogue_journal is None:
            return False
        async with visit["lock"]:
            if (self._identity_shutdown or visit["closed"] or asyncio.current_task().cancelling()
                    or self._journal_visits.get(session_id) is not visit or not current()):
                return False
            return await self._journal_io(self._dialogue_journal.append_turn, visit["id"], role, text)

    async def _finish_journal_visit(self, visit):
        async with visit["lock"]:
            return await self._journal_io(self._dialogue_journal.finish, visit["id"])

    def _end_journal_visit(self, session_id):
        # Canonical acceptance revokes authority before any provider/disk await.
        visit = self._journal_visits.pop(session_id, None)
        if visit is None:
            return
        visit["closed"] = True
        task = asyncio.create_task(self._finish_journal_visit(visit))
        self._journal_jobs.add(task)
        def finished(done):
            self._journal_jobs.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)

    def _close_dialogue_journal(self):
        journal = self._dialogue_journal
        if journal is None or self._journal_shutdown_task is not None:
            return
        visits = tuple(self._journal_visits.values())
        self._journal_visits.clear()
        for visit in visits:
            visit["closed"] = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Stand.close also supports a caller with no running event loop.
            with contextlib.suppress(Exception):
                journal.close()
            return
        async def close():
            await asyncio.gather(*(self._finish_journal_visit(visit) for visit in visits),
                                 *tuple(self._journal_jobs), return_exceptions=True)
            await self._journal_io(journal.close)
        self._journal_shutdown_task = loop.create_task(close(), name="mishka-session-journal-close")

    def cancel_identity_work(self) -> None:
        """Synchronous authority revocation for Stand.close, also outside a loop."""
        if self._engagement_task is not None:
            self._engagement_task.cancel()
        self._engagement_owners.clear()
        self._game_callers.clear()
        self._identity_shutdown = True
        self._close_dialogue_journal()
        for session_id in tuple(self.conversation_contexts):
            self._drop_dialogue_context(session_id)
        for session_id in tuple(self._personal_stories):
            self._discard_personal_story(session_id)
        for task in tuple(self._owned_text_jobs):
            task.cancel()
        for session_id in tuple(self._private_welcomes):
            self._discard_private_welcome(session_id)
        if self.max_json and self._max_client_boundary is not None:
            self._revoke_local_profile(self._max_client_boundary.session_id)
        for session_id in set(self._identity_requests) | set(self._identity_deadline_tasks):
            self._drop_identity_owner(session_id)
    async def shutdown_identity(self) -> dict[str, int]:
        """Revoke first, then drain a bounded amount of provider work."""
        self._identity_shutdown = True
        self._close_dialogue_journal()
        tasks = set(self._identity_deadline_tasks.values()) | self._private_welcome_tasks | self._owned_text_jobs | self._personal_story_jobs
        tasks.discard(asyncio.current_task())
        try:
            if self.max_json and self._max_client_boundary is not None:
                await self._max_client_boundary.close()
        finally:
            self.cancel_identity_work()
            if self._journal_shutdown_task is not None:
                await asyncio.shield(self._journal_shutdown_task)
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
        if not (state is not None and request is not None
                and state.identity_request_id == request["request_id"]
                and state.identity_expires_monotonic is not None
                and self.monotonic_now() < state.identity_expires_monotonic
                and (state.identity_ready_deadline is None or self.monotonic_now() < state.identity_ready_deadline)
                and (capability is None or capability in state.identity_capabilities)):
            return False
        if request.get('revoked'):
            return False
        current = request.get('current')
        if current is None:
            return True
        try:
            live = bool(current())
        except Exception:
            live = False
        if not live:
            # A failed source check cannot revive this receipt on a later poll.
            request['revoked'] = True
        return live

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

    def _public_story_expiry(self, event, state, *, authorized):
        request = self._identity_requests.get(event.session_id)
        boundary = self._max_client_boundary
        if (not authorized or not self.max_json or boundary is None
                or boundary.session_id != event.session_id or state is None
                or not state.profile_ref or not request
                or request["request_id"] != state.identity_request_id
                or event.payload.get("request_id") != state.identity_request_id
                or not state.interaction_open or state.safety_severity == "urgent"
                or state.mode not in {"fixed_story_playing", "story_paused",
                                      "story_pause_announcement", "story_resume_announcement"}):
            return None
        if event.event_type == "ClientProfileCleared":
            if event.payload.get("reason_code") != "session_expired":
                return None
        elif (event.event_type != "IdentityAuthorizationExpired"
                or state.identity_expires_monotonic is None
                or self.monotonic_now() < state.identity_expires_monotonic):
            return None
        asset = self.canonical_runtime.stories.get_verified(state.active_story_id)
        if not asset or int(asset.get("duration_ms") or 0) <= 0:
            return None
        offset = None
        checkpoint = state.dialogue_memory.story_checkpoint
        if state.mode != "fixed_story_playing" and state.story_checkpoint_confirmed and checkpoint:
            if checkpoint.get("story_id") == state.active_story_id:
                value = checkpoint.get("offset_ms")
                if type(value) is int and 0 <= value <= int(asset["duration_ms"]):
                    offset = value
        old = (self._active_audio.get(event.session_id) if state.mode == "fixed_story_playing"
               else self._paused_story_playback.get(event.session_id))
        if offset is None and (not old or old.get("content_id") != state.active_story_id
                or old.get("generation") != self._session_epochs.get(event.session_id, 0)):
            return None
        return {"story_id": state.active_story_id, "offset_ms": offset,
                "duration_ms": int(asset["duration_ms"]),
                "resume": state.mode in {"fixed_story_playing", "story_resume_announcement"},
                "old": {key: old[key] for key in ("generation", "token", "playback_id", "playback_lease")}
                       if offset is None else None}

    def _drop_public_story_transfer(self, sid):
        transfer = self._public_story_checkpoint_transfers.pop(sid, None)
        if transfer is not None:
            transfer["timer"].cancel()
            if not transfer["ready"].done():
                transfer["ready"].cancel()

    def _public_story_transfer(self, sid):
        transfer = self._public_story_checkpoint_transfers.get(sid)
        state = self.canonical_runtime.sessions.get(sid)
        if transfer is not None and (self.monotonic_now() >= transfer["expires"]
                or self._session_epochs.get(sid, 0) != transfer["epoch"]
                or self._session_revisions.get(sid, 0) != transfer["revision"]
                or state is not transfer["state"] or not state.public_story_checkpoint_pending
                or not state.interaction_open or state.profile_ref or state.active_story_id != transfer["story_id"]):
            self._drop_public_story_transfer(sid)
            return None
        return transfer

    async def accept_public_story_checkpoint(self, sid, payload):
        transfer = self._public_story_transfer(sid)
        if transfer is None:
            return {"reason": "stale_story_checkpoint"}
        old = transfer["old"]
        if (any(type(payload.get(key)) is not int for key in ("generation", "token", "target_generation"))
                or payload.get("target_generation") != transfer["epoch"]
                or payload.get("story_id") != transfer["story_id"]
                or any(payload.get(key) != old.get(key) for key in
                       ("generation", "token", "playback_id", "playback_lease"))
                or type(payload.get("offset_ms")) is not int
                or not 0 <= payload["offset_ms"] <= transfer["duration_ms"]):
            return {"reason": "stale_story_checkpoint"}
        # Cleanup must finish before the new public lease can dispatch audio.
        await asyncio.wait({transfer["ready"]})
        if self._public_story_transfer(sid) is not transfer:
            return {"reason": "stale_story_checkpoint"}
        self._drop_public_story_transfer(sid)
        return await self._receive_canonical_event(self._identity_event(sid, "StoryPositionUpdated", {
            "story_id": transfer["story_id"], "offset_ms": payload["offset_ms"],
            "resume": transfer["resume"],
        }), public_story_checkpoint_authorized=True)

    def _identity_event(self, session_id: str, kind: str, payload: dict[str, Any]) -> Event:
        state = self.canonical_runtime.sessions.get(session_id)
        sequence = state.last_sequence_by_source.get("internal_identity", 0) + 1 if state else 1
        return Event(kind, session_id, sequence, self.now(), "internal_identity", payload,
                     "identity:" + uuid.uuid4().hex, self.monotonic_now())

    def _internal_followup(self, event: Event, kind: str, source: str, payload: dict, suffix: str) -> Event:
        state = self.canonical_runtime.sessions.get(event.session_id)
        sequence = state.last_sequence_by_source.get(source, 0) + 1 if state else 1
        origin = event.correlation_id or f"admitted:{(event.session_id, self._session_epochs.get(event.session_id, 0), event.source, event.sequence)!r}"
        return Event(kind, event.session_id, sequence, self.now(), source, payload,
                     f"{origin}:{suffix}", self.monotonic_now())


    async def _set_dialogue_lighting_phase(self, session_id: str, phase: str) -> dict[str, Any] | None:
        """Drive the dialogue LED frame from the actual bounded-runtime phase.

        The bear pictogram and button 7 digit are fixed anchors. Only button
        fills animate, so the serial protocol remains the existing binary
        element/state/transition contract and the same frame is visible in
        the physical stand and the WebPanel.
        """
        runtime = self.canonical_runtime
        state = runtime.sessions.get(session_id) if runtime is not None else None
        word_game = bool(state and state.mode == "game_active" and state.active_game_id == "word_game"
                         and state.game_audio_stage == "playing")
        if state is None or (state.mode != "bear_dialogue" and not word_game) or not state.interaction_open:
            return None
        if self.optional_identity and state.identity_request_id and not self._identity_scope_live(session_id):
            await self.expire_identity_leases()
            return None
        phase = str(phase or "listening")
        fills = DIALOGUE_FILL_ELEMENTS
        anchors = DIALOGUE_ANCHOR_ELEMENTS
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
            phase = phase if phase in {"output", "preparing"} else "listening"
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
        if lease.get("content_id") in TYPED_AUDIO_IDS | MENU_AUDIO_IDS | {"PERSONAL_WELCOME", "PERSONAL_STORY", "GAME_PROMPT", "GAME_HINT"} and not payload.get("playback_id"):
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
        self._cancel_game_cues(session_id)
        lease = self._active_audio.pop(session_id, None)
        if lease is not None:
            self.record_audio_diagnostic(
                "audio_lease_invalidated", session_id=session_id,
                content_id=lease.get("content_id"), generation=lease.get("generation"),
                reason=status,
            )

    def _finish_unstarted_safety_guidance(self, session_id, owner, epoch, lease, effect,
                                         *, code, status="failed") -> None:
        """Terminate only this declared, undispatched local guidance lease."""
        if (self.canonical_runtime.sessions.get(session_id) is not owner
                or self._session_epochs.get(session_id, 0) != epoch
                or owner.mode != "safety_stop" or owner.active_audio_id != "PHRASE_091"
                or owner.playback_lease != lease or session_id in self._active_audio):
            return
        owner.active_audio_id = owner.playback_lease = None
        owner.audio_status, owner.audio_failure_code = status, code
        owner.state_version += 1
        _set_audio_effect_status(effect, asset_status="approved", queued=False, resolution=code)
        if status == "cancelled":
            effect.update(audio_status="cancelled", audio_failed=False, audio_cancelled=True)
        self.last_canonical_state = owner.snapshot()
        self.record_audio_diagnostic(code, session_id=session_id)

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
        owner = self._active_audio.get(session_id)
        state = self.canonical_runtime.sessions.get(session_id) if self.canonical_runtime else None
        if (not owner or state is None or not state.interaction_open
                or self._session_epochs.get(session_id, 0) != generation
                or state.active_audio_id != response_id or state.playback_lease != playback_lease
                or any(owner.get(key) != value for key, value in {
                    "content_id": response_id, "generation": generation,
                    "playback_id": playback_id, "playback_lease": playback_lease,
                }.items())):
            return False
        owner["status"] = state.audio_status = status
        if status == "playing" and response_id in {"GAME_PROMPT", "GAME_HINT"}:
            self._game_audio_started(session_id, playback_id, playback_lease, generation)
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
        # A release/lighting acknowledgement can replace the last effect list
        # while this exact playback remains current.
        return True

    def _iter_sessions(self) -> tuple[SessionRecord, ...]:
        iterator = getattr(self.sessions, "iter_sessions", None)
        if callable(iterator):
            return tuple(iterator())
        items = getattr(self.sessions, "items", {})
        return tuple(items.values()) if isinstance(items, dict) else ()

    @contextlib.contextmanager
    def _runtime_work(self, *, cleanup=False):
        if self._runtime_maintenance is not None and not cleanup:
            yield False
            return
        task = asyncio.current_task()
        if task in self._runtime_ingress:
            yield True
            return
        # Reentrance belongs to this live task, never an inherited child context.
        token = object()
        self._runtime_ingress[task] = token
        try:
            yield True
        finally:
            if self._runtime_ingress.get(task) is token:
                self._runtime_ingress.pop(task, None)

    def maintenance_busy(self) -> bool:
        if (self._runtime_maintenance is not None or self._runtime_ingress
                or any(self._engagement_ingress.values()) or self._session_reset_waiters
                or self._active_audio):
            return True
        states = getattr(self.canonical_runtime, "sessions", {}).values()
        if any(state.handset == "up" or state.mode != "idle" or state.identity_request_id for state in states):
            return True
        jobs = (*self._owned_text_jobs, *self._private_welcome_tasks, *self._personal_story_jobs,
                *(task for tasks in self._audio_completions.values() for task in tasks))
        if any(not task.done() for task in jobs):
            return True
        for owner in (self.bounded_s2s, self.s2s_audio):
            for name in ("active_tasks", "pending_tasks"):
                if any(not task.done() for task in getattr(owner, name, {}).values()):
                    return True
            if getattr(owner, "active_queues", {}):
                return True
            if any(getattr(ingress, "active", False) for ingress in getattr(owner, "ingress", {}).values()):
                return True
        manager = self.runtime_manager
        for provider in (getattr(self.s2s_audio, "recognizer", None), getattr(manager, "stt", None),
                         getattr(manager, "tts", None), getattr(manager, "voice", None)):
            snapshot = getattr(provider, "native_worker_snapshot", None)
            if callable(snapshot):
                try:
                    state = snapshot()
                    if state.get("active_jobs") or state.get("queued_jobs"):
                        return True
                except Exception:
                    return True
        return False

    def acquire_runtime_maintenance(self):
        if self.maintenance_busy():
            raise RuntimeBusyError("runtime_busy")
        token = object()
        self._runtime_maintenance = token

        def release():
            if self._runtime_maintenance is token:
                self._runtime_maintenance = None
        return release

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
                                  ownership_guard: Callable[[], bool] | None = None,
                                  cleanup_tasks: set[asyncio.Task[Any]] | None = None) -> dict[str, Any]:
        """Bound provider cleanup so HID/reset cannot be held by native audio I/O."""
        cancel_session = getattr(owner, "cancel_session", None)
        if not callable(cancel_session):
            return {"status": "unavailable", "operation": operation, "session_id": session_id}
        game_audio_waits = tuple(token for token, wait in self._game_audio_waits.items()
            if owner is self.s2s_audio and wait["owner"][0] == session_id)
        captured_lease = self._active_audio.get(session_id)
        def cleanup_current():
            # A transition can queue a new recording without a new revision
            # after its old provider exceeded the cleanup deadline.
            return (self._active_audio.get(session_id) is captured_lease
                and (ownership_guard is None or ownership_guard()))
        async def cancel_owned() -> None:
            # The cancellation task can start after a newer event has run.
            # Check authority in that task before touching the shared owner.
            if cleanup_current():
                if "ownership_guard" in inspect.signature(cancel_session).parameters:
                    await cancel_session(session_id, ownership_guard=cleanup_current)
                else:
                    await cancel_session(session_id)

        task = asyncio.create_task(
            cancel_owned(),
            name=f"mishka-audio-cancel-{operation}-{session_id}",
        )
        if cleanup_tasks is not None:
            cleanup_tasks.add(task)
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
            task.add_done_callback(self._consume_detached_audio_cancel)
            raise
        except Exception as exc:
            diagnostic = {
                "kind": "audio_owner_cleanup_failed",
                "session_id": session_id,
                "operation": operation,
                "exception_type": type(exc).__name__,
                "detail": type(exc).__name__,
            }
            self.audio_runtime_diagnostics.append(diagnostic)
            logger.warning("audio owner cleanup failed; continuing reset: %s", diagnostic["detail"])
            return {**diagnostic, "status": "failed"}
        finally:
            if any(token in self._game_audio_waits for token in game_audio_waits):
                self._sync_game_timer(session_id)
        return {"status": "completed", "operation": operation, "session_id": session_id}

    async def _cleanup_session_resources(self, session_id: str, *, identity_request: dict[str, Any] | None = None,
                                         cleanup_tasks: set[asyncio.Task[Any]] | None = None,
                                         preserve_public_story_transfer: bool = False) -> dict[str, Any]:
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
        if not preserve_public_story_transfer:
            self._drop_public_story_transfer(session_id)
        self._drop_identity_owner(session_id)
        if identity_request is not None:
            self._identity_requests[session_id] = identity_request
        self._discard_private_welcome(session_id)
        self._drop_engagement(session_id, keys=True)
        if state is not None and state.game_engine_state is not None and state.game_engine_state.seven_exit_phase != 'idle':
            state.game_engine_state = cancel_exit_gesture(state.game_engine_state)
        self._cancel_game_timer(session_id)
        self._drop_game_audio_waits(session_id)
        # Resource cleanup can retain canonical admission history. Keep its
        # producer sequences monotonic until a full canonical session reset.
        self._cancel_welcome_timer(session_id)
        self._cancel_menu_tutorial(session_id)
        self._cancel_lighting_wave(session_id)
        self._invalidate_audio_lease(session_id)
        self._last_audio_ack_at.pop(session_id, None)
        completion_cleanup = await self._cancel_audio_completions(session_id, operation="session_cleanup")
        if not still_current():
            return {"session_id": session_id, "cleanup_stale": True, "audio_completion_cleanup": completion_cleanup}
        owner_cleanup = {"status": "completed"}
        if audio_owner is not None:
            owner_cleanup = await self._cancel_audio_owner(audio_owner, session_id, operation="session_cleanup",
                                           ownership_guard=still_current, cleanup_tasks=cleanup_tasks)
            if not still_current():
                return {"session_id": session_id, "cleanup_stale": True, "audio_completion_cleanup": completion_cleanup}
        snapshot = self.resource_snapshot()
        snapshot["session_id"] = session_id
        snapshot["audio_completion_cleanup"] = completion_cleanup
        snapshot["audio_owner_cleanup"] = owner_cleanup
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
            if state is not None and state.mode == "safety_stop":
                # A local stop ends through physical hangup or operator reset.
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

    def _game_speech_check_owner(self, session_id: str) -> tuple | None:
        state = self.canonical_runtime.sessions.get(session_id) if self.canonical_runtime else None
        engine = state.game_engine_state if state is not None else None
        if state is None or state.mode != "game_active" or not state.interaction_open or engine is None:
            return None
        return (session_id, id(state), self._session_epochs.get(session_id, 0), engine.run_id, engine.round_generation)

    def _catalogue_voice_scope(self, session_id):
        state = self.canonical_runtime.sessions.get(session_id) if self.canonical_runtime else None
        descriptor = state.scene_menu_descriptor if state is not None else {}
        if (state is None or state.mode not in {'game_menu','fixed_story_menu'} or not state.interaction_open
                or state.safety_severity != 'none' or state.scene_menu_refresh_required
                or state.scene_menu_active != state.mode or descriptor.get('scene') != state.mode
                or not descriptor.get('items')):
            return None
        # Capture owns this committed entry, not asset authorization. Selection
        # still verifies the exact asset; paging keeps named choices current.
        return (id(state), self._session_epochs.get(session_id, 0), state.mode, state.scene_menu_generation, state.profile_ref,
                state.identity_request_id, state.age_band,
                id(self.canonical_runtime.stories) if state.mode == 'fixed_story_menu' else None)

    def _finish_game_audio_wait(self, wait) -> None:
        if wait is not None and self._game_audio_waits.pop(wait["token"], None) is wait:
            if getattr(wait["pipeline"], "_canonical_game_audio_wait", None) is wait:
                wait["pipeline"]._canonical_game_audio_wait = None
            self._sync_game_timer(wait["owner"][0])

    def _drop_game_audio_waits(self, session_id) -> None:
        for token, wait in tuple(self._game_audio_waits.items()):
            if wait["owner"][0] == session_id:
                self._game_audio_waits.pop(token, None)
                if getattr(wait["pipeline"], "_canonical_game_audio_wait", None) is wait:
                    wait["pipeline"]._canonical_game_audio_wait = None

    @contextlib.contextmanager
    def _game_audio_wait(self, session_id, pipeline, guard):
        owner = self._game_speech_check_owner(session_id)
        wait = None
        if pipeline is not None and owner is not None and self._game_voice_scope(session_id) is not None and guard():
            prior = getattr(pipeline, "_canonical_game_audio_wait", None)
            if (pipeline.active and prior is not None and self._game_audio_waits.get(prior["token"]) is prior
                    and prior["owner"] == owner and prior["guard"]()):
                wait = prior
            else:
                config = self.s2s_audio.config
                wait = {"token": object(), "owner": owner, "pipeline": pipeline, "guard": guard,
                        "deadline": self.monotonic_now() + config.max_speech_ms / 1000 + config.stt_timeout_s,
                        "completion": None}
                self._game_audio_waits[wait["token"]] = wait
                pipeline._canonical_game_audio_wait = wait
            self._sync_game_timer(session_id)
        failed = True
        try:
            yield wait
            failed = False
        finally:
            # Silence/pre-roll never holds a reminder. A confirmed onset may
            # span uploads, but its fixed wall deadline survives every frame.
            if wait is not None and (failed or not (wait["completion"] is not None or
                    (pipeline.active and getattr(pipeline, "_canonical_game_audio_wait", None) is wait))):
                self._finish_game_audio_wait(wait)

    def _game_audio_wait_deadline(self, session_id, owner):
        deadlines = []
        for token, wait in tuple(self._game_audio_waits.items()):
            if wait["owner"][0] != session_id:
                continue
            if (wait["owner"] != owner or self.s2s_audio.ingress.get(session_id) is not wait["pipeline"]
                    or not wait["guard"]()):
                self._game_audio_waits.pop(token, None)
                if getattr(wait["pipeline"], "_canonical_game_audio_wait", None) is wait:
                    wait["pipeline"]._canonical_game_audio_wait = None
            else:
                deadlines.append(wait["deadline"])
        return max(deadlines) if deadlines else None

    def _current_game_timer_scope(self, session_id: str) -> tuple | None:
        state = self.canonical_runtime.sessions.get(session_id) if self.canonical_runtime else None
        engine = state.game_engine_state if state is not None else None
        owner = self._game_speech_check_owner(session_id)
        audio_wait = self._game_audio_wait_deadline(session_id, owner)
        if (state is None or state.mode != "game_active" or state.substate == "game_exit_confirmation"
                or engine is None or not engine.run_id or game_deadline(engine) is None):
            return None
        deadline = game_deadline(engine)
        wakeup = deadline
        if engine.seven_exit_phase != 'idle':
            return (self._session_epochs.get(session_id, 0), engine.run_id, engine.round_generation,
                    engine.timer_generation, deadline, engine.seven_gesture_generation, wakeup)
        speech_wait = owner is not None and owner in self._game_speech_checks.values()
        if speech_wait or audio_wait is not None:
            mechanics = []
            if engine.phase == "demonstrate" and engine.demonstration_deadline is not None:
                mechanics.append(engine.demonstration_deadline)
            if engine.game_id == "hold_light" and engine.phase == "playing" and not engine.hold_ready:
                started = dict(engine.down_times).get(engine.target)
                if started is not None and engine.target in engine.held:
                    mechanics.append(started + engine.hold_seconds)
            if not speech_wait:
                mechanics.append(max(deadline, audio_wait))
            if not mechanics:
                return None
            # A suppressed earlier hint must not hide a later mechanic deadline.
            wakeup = min(mechanics)
        if engine.game_id == "word_game" and state.active_audio_id:
            return None
        return (self._session_epochs.get(session_id, 0), engine.run_id, engine.round_generation,
                engine.timer_generation, deadline, engine.seven_gesture_generation, wakeup)

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
        if self._current_game_timer_scope(session_id) != scope:
            self._sync_game_timer(session_id)
            return None
        if self.monotonic_now() < scope[-1]:
            return None
        sequence = self._game_timer_sequences.get(session_id, 0) + 1
        self._game_timer_sequences[session_id] = sequence
        return await self._receive_canonical_event(Event(
            "GameTimer", session_id, sequence, self.now(), "canonical_game_timer",
            {"run_id": scope[1], "round_generation": scope[2], "timer_generation": scope[3], "deadline": scope[4], 'gesture_generation':scope[5]},
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

    def _current_silent_hold_scope(self, session_id: str) -> tuple | None:
        state = self.canonical_runtime.sessions.get(session_id) if self.canonical_runtime is not None else None
        if (state is None or not getattr(state, "silent_hold_active", False)
                or state.silent_hold_next_deadline is None or state.handset != "down"):
            return None
        return (self._session_epochs.get(session_id, 0), state.silent_hold_generation, state.silent_hold_next_deadline)

    def _sync_silent_hold_timer(self, session_id: str) -> None:
        tasks = getattr(self, "_silent_hold_tasks", None)
        scopes = getattr(self, "_silent_hold_scopes", None)
        if tasks is None:
            tasks = self._silent_hold_tasks = {}
            scopes = self._silent_hold_scopes = {}
        scope = self._current_silent_hold_scope(session_id)
        if scope is not None and scopes.get(session_id) == scope:
            return
        scopes.pop(session_id, None)
        old = tasks.pop(session_id, None)
        if old is not None and old is not asyncio.current_task() and not old.done():
            old.cancel()
        if scope is None:
            return
        scopes[session_id] = scope
        if not self.background_scheduler:
            return
        async def wait() -> None:
            try:
                await asyncio.sleep(max(0.0, scope[-1] - self.monotonic_now()))
                await self._fire_silent_hold_tick(session_id, scope)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("silent hold timer failed")
            finally:
                if tasks.get(session_id) is asyncio.current_task():
                    tasks.pop(session_id, None)
        tasks[session_id] = asyncio.create_task(wait(), name="mishka-silent-hold-timer")

    async def _fire_silent_hold_tick(self, session_id: str, scope: tuple) -> dict[str, Any] | None:
        if self._current_silent_hold_scope(session_id) != scope:
            self._sync_silent_hold_timer(session_id)
            return None
        if self.monotonic_now() < scope[-1]:
            return None
        seq = getattr(self, "_silent_hold_sequences", {}).get(session_id, 0) + 1
        seqs = getattr(self, "_silent_hold_sequences", None)
        if seqs is None:
            seqs = self._silent_hold_sequences = {}
        seqs[session_id] = seq
        return await self._receive_canonical_event(Event(
            "SilentHoldTick", session_id, seq, self.now(), "canonical_silent_hold_timer",
            {"generation": scope[1], "deadline": scope[2]},
            f"silent-hold:{scope[1]}:{seq}", self.monotonic_now(),
        ))

    async def flush_silent_hold_timers(self) -> list[dict[str, Any]]:
        results = []
        for session_id, scope in tuple(getattr(self, "_silent_hold_scopes", {}).items()):
            if self.monotonic_now() >= scope[-1]:
                res = await self._fire_silent_hold_tick(session_id, scope)
                if res is not None:
                    results.append(res)
        return results

    def _cancel_welcome_timer(self, session_id: str) -> None:
        task = self._welcome_timers.pop(session_id, None)
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()

    async def _fire_exhibition_welcome(self, session_id: str, deadline: float, *, epoch=None) -> dict[str, Any] | None:
        if epoch is not None and self._session_epochs.get(session_id, 0) != epoch:
            return None
        state = self.canonical_runtime.sessions.get(session_id) if self.canonical_runtime is not None else None
        if state is None or state.welcome_deadline_monotonic != deadline or state.welcome_status != "pending":
            return None
        sequence = self._welcome_timer_sequences.get(session_id, 0) + 1
        self._welcome_timer_sequences[session_id] = sequence
        return await self._receive_canonical_event(Event(
            "WelcomeTimerFired", session_id, sequence, self.now(),
            "canonical_welcome_timer", {"deadline_monotonic": deadline,
                "generation": self._session_epochs.get(session_id, 0)},
            f"welcome:{session_id}:{deadline}", self.monotonic_now(),
        ))

    async def _wait_for_exhibition_welcome(self, session_id: str, deadline: float, *, epoch=None) -> None:
        try:
            # Production wakeup only; fake-clock tests call
            # flush_welcome_timers() to advance this canonical deadline
            # without waiting on wall time.
            await asyncio.sleep(max(0.0, deadline - self.monotonic_now()))
            await self._fire_exhibition_welcome(session_id, deadline, epoch=epoch)
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
            self._wait_for_exhibition_welcome(session_id, deadline,
                epoch=self._session_epochs.get(session_id, 0)),
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

    async def session_action(self, session_id: str, action: str, *, ownership_guard, state_version=None):
        """Trusted screen controls, scoped by Stand before and after awaited barriers."""
        with self._runtime_work(cleanup=action == "end") as admitted:
            if not admitted:
                return self._identity_result(session_id, "runtime_unavailable")
            if action != "end":
                replacement = self._identity_replacements.get(session_id)
                if replacement and replacement[1] is not None and replacement[1] is not asyncio.current_task():
                    await asyncio.shield(replacement[1])
            if not ownership_guard():
                return self._identity_result(session_id, "stale_session_generation")
            kind = {"start": "SessionStarted", "end": "SessionCancel", "skip_interview": "InterviewSkipped", "enable_audio": "SessionAudioEnabled"}[action]
            state = self.canonical_runtime.sessions.get(session_id)
            sequence = state.last_sequence_by_source.get("panel_session", 0) + 1 if state else 1
            payload = {"state_version": state_version} if state_version is not None else {}
            event = Event(kind, session_id, sequence, self.now(), "panel_session", payload,
                          "panel:" + uuid.uuid4().hex, self.monotonic_now())
            return await self._receive_canonical_event(event, session_action_authorized=True,
                                                        _action_guard=ownership_guard)

    @_runtime_ingress_guard
    async def _receive_canonical_event(self, event: Event, transient_text: str | None = None,
                                       *, dialogue_fallback_authorized: bool = False,
                                       personal_story_authorized: bool = False,
                                       identity_event_authorized: bool = False,
                                       public_story_checkpoint_authorized: bool = False,
                                       game_timer_authorized: bool = False,
                                       interaction_idle_authorized: bool = False,
                                       _interaction_nonce: str | None = None,
                                       _game_timer_scope: tuple | None = None,
                                       identity_profile: dict[str, Any] | None = None,
                                       identity_current=None,
                                       _speech_ingress_guard=None, _speech_lifecycle_guard=None,
                                       _weather_scope: str | None = None, _weather_origin_current=None,
                                       _story_checkpoint_guard=None,
                                       session_action_authorized: bool = False, _action_guard=None) -> dict[str, Any]:
        if event.event_type == "InteractionIdleExpired" and not interaction_idle_authorized:
            return self._identity_result(event.session_id, "interaction_idle_untrusted")
        speech_work = event.event_type == "SpeechReceived"
        speech_scope = (event.session_id, self._engagement_scope(event.session_id))
        safety_guidance = {}
        if speech_work:
            self._engagement_ingress[speech_scope] = self._engagement_ingress.get(speech_scope,0) + 1
        try:
            waiter = self._session_reset_waiters.get(event.session_id)
            if waiter is not None and waiter[0] is not asyncio.current_task():
                await asyncio.shield(waiter[1])
            if self.optional_identity and not event.event_type.startswith("Identity") and event.event_type not in {"ClientProfileReady", "ClientProfileCleared"}:
                await self.expire_identity_leases()
                state = self.canonical_runtime.sessions.get(event.session_id)
                if (event.event_type == 'HandsetLifted' and state is not None and state.identity_request_id
                        and not self._identity_scope_live(event.session_id)):
                    await self._receive_canonical_event(self._identity_event(event.session_id,
                        'ClientProfileCleared', {'request_id':state.identity_request_id,
                            'reason_code':'source_context_changed'}), identity_event_authorized=True)
            if _weather_scope is not None and (not dialogue_fallback_authorized
                    or event.event_type != 'DialogueFallback' or _weather_origin_current is None
                    or not _weather_origin_current()):
                return self._identity_result(event.session_id, 'stale_session_generation')
            if _action_guard is not None and not _action_guard():
                return self._identity_result(event.session_id, "stale_session_generation")
            return await self._receive_canonical_event_inner(
                event, transient_text,
                dialogue_fallback_authorized=dialogue_fallback_authorized,
                personal_story_authorized=personal_story_authorized,
                identity_event_authorized=identity_event_authorized,
                public_story_checkpoint_authorized=public_story_checkpoint_authorized,
                game_timer_authorized=game_timer_authorized, _game_timer_scope=_game_timer_scope,
                interaction_idle_authorized=interaction_idle_authorized, _interaction_nonce=_interaction_nonce,
                identity_profile=identity_profile,
                identity_current=identity_current,
                _speech_ingress_guard=_speech_ingress_guard,
                _speech_lifecycle_guard=_speech_lifecycle_guard,
                _weather_scope=_weather_scope,
                _story_checkpoint_guard=_story_checkpoint_guard,
                _safety_guidance=safety_guidance,
                session_action_authorized=session_action_authorized, _action_guard=_action_guard,
            )
        except (asyncio.CancelledError, Exception) as exc:
            if safety_guidance:
                cancelled = isinstance(exc, asyncio.CancelledError)
                self._finish_unstarted_safety_guidance(event.session_id, **safety_guidance,
                    code="local_safety_guidance_cancelled" if cancelled else "local_safety_guidance_failed",
                    status="cancelled" if cancelled else "failed")
            raise
        finally:
            if speech_work:
                if self._game_speech_checks.pop(asyncio.current_task(), None) is not None:
                    self._sync_game_timer(event.session_id)
                count = self._engagement_ingress.get(speech_scope,1) - 1
                if count: self._engagement_ingress[speech_scope] = count
                else: self._engagement_ingress.pop(speech_scope,None)
                self._refresh_engagement_busy(event.session_id)
            waiter = self._session_reset_waiters.get(event.session_id)
            if waiter is not None and waiter[0] is asyncio.current_task():
                self._session_reset_waiters.pop(event.session_id, None)
                if not waiter[1].done():
                    waiter[1].set_result(None)

    async def _receive_canonical_event_inner(self, event: Event, transient_text: str | None = None,
                                                               *, dialogue_fallback_authorized: bool = False,
                                       personal_story_authorized: bool = False,
                                             identity_event_authorized: bool = False,
                                             public_story_checkpoint_authorized: bool = False,
                                             game_timer_authorized: bool = False,
                                             interaction_idle_authorized: bool = False,
                                             _interaction_nonce: str | None = None,
                                             _game_timer_scope: tuple | None = None,
                                             identity_profile: dict[str, Any] | None = None,
                                             identity_current=None,
                                             _speech_ingress_guard=None, _speech_lifecycle_guard=None,
                                             _weather_scope: str | None = None,
                                             _story_checkpoint_guard=None,
                                             _safety_guidance: dict,
                                             session_action_authorized: bool = False, _action_guard=None) -> dict[str, Any]:
        """Bridge canonical decisions to application-owned effects.

        The contract engine never touches audio, capture, LEDs or providers.
        This boundary is the sole place where declared effects become calls.
        """
        profile = None
        profile_request = None
        fresh_profile_prepared = False
        verified_voice_activity = False
        current = self.canonical_runtime.sessions.get(event.session_id)
        if (current is not None and current.identity_request_id
                and event.event_type not in {'ClientProfileReady','ClientProfileCleared',
                    'IdentityReadyExpired','IdentityAuthorizationExpired','HandsetLifted','HandsetPlaced',
                    'SessionCancel','SessionReset','AnonymousStart','HardwareDisconnected','HardwareReconnected',
                    'ButtonReleased','SpeechReceived'} and not self._identity_scope_live(event.session_id)):
            urgent = (event.event_type == 'VoiceIntentReady' and
                (event.payload.get('safety_severity', 'none') != 'none' or event.payload.get('intent') == 'help_request'))
            public_story_callback = (event.event_type in {'AudioStarted','AudioCompleted','AudioFailed','StoryPositionUpdated'}
                and current.mode in {'fixed_story_playing','story_paused','story_pause_announcement','story_resume_announcement'}
                and current.active_story_id and current.active_story_id != STORY_ID)
            if not urgent and not public_story_callback:
                self._discard_personal_story(event.session_id)
                return self._identity_result(event.session_id, 'stale_profile_context')
        if (event.event_type == 'WelcomeTimerFired' and event.source == 'canonical_welcome_timer'
                and event.payload.get('generation') != self._session_epochs.get(event.session_id, 0)):
            return self._identity_result(event.session_id, 'stale_session_generation')
        if self._profile_replacing(event.session_id) and event.event_type in {'ButtonPressed','ButtonHeld','ButtonLongHeld','HandsetLifted'}:
            candidate = CanonicalEvent(event.event_type, event.session_id, event.sequence,
                current.state_version, event.source, dict(event.payload), event.correlation_id,
                event.timestamp, event.monotonic_timestamp)
            candidate.validate()
            rejected = self.canonical_runtime._admit(candidate, current)
            if not rejected and event.event_type == 'HandsetLifted':
                # The real hook changes during cleanup without replacing its
                # owner. Verified ready starts the welcome after the barrier.
                if current.handset != 'up' and current.interaction_route != 'panel':
                    current.interaction_route = 'handset'
                current.handset = 'up'
                record = self.sessions.get(event.session_id)
                if record is None:
                    record = SessionRecord(event.session_id, self.installation_id,
                        event.correlation_id or str(uuid.uuid4()), anonymous=True,
                        current_node=current.mode, started_at=self.now(),
                        cancellation_token=self._session_epochs.get(event.session_id, 0))
                record.handset_up = True
                record.last_sequence, record.last_activity_at = event.sequence, self.now()
                self.sessions.save(record)
                self.last_canonical_state = current.snapshot()
                self.last_canonical_effects = []
                self.publisher.publish(event)
            if not rejected and event.event_type == 'ButtonPressed':
                button = event.payload['button_id']
                if button not in current.accepted_buttons_down:
                    current.accepted_buttons_down.append(button)
                if button not in current.readiness_held_buttons:
                    current.readiness_held_buttons.append(button)
            return self._identity_result(event.session_id, rejected or 'profile_replacing')
        if (self._profile_replacing(event.session_id) and event.event_type == 'VoiceIntentReady'
                and event.payload.get('safety_severity', 'none') == 'none'
                and event.payload.get('intent') != 'help_request'):
            return self._identity_result(event.session_id, 'profile_replacing')
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
                if other_id != event.session_id and (other.interaction_open
                        or other.mode == "profile_ready"
                        or other.safety_severity == "urgent"):
                    return self._identity_result(event.session_id, "installation_busy")
        if event.event_type in {"ClientProfileReady", "ClientProfileCleared"}:
            if not self.max_json or not identity_event_authorized:
                return self._identity_result(event.session_id, "client_profile_untrusted")
            if (event.event_type == "ClientProfileCleared" and 'request_id' in event.payload
                    and event.payload['request_id'] != getattr(current, 'identity_request_id', None)):
                return self._identity_result(event.session_id, "stale_profile_context")
            if event.event_type == "ClientProfileReady":
                if self._max_client_boundary is None or event.payload.get("client_generation") != self._max_client_boundary.generation:
                    return self._identity_result(event.session_id, "stale_client_generation")
                if current is not None and (current.mode == 'safety_stop' or current.safety_severity == 'urgent'
                        or (current.mode in {'soft_end', 'session_closed'}
                            and self._identity_replacements.get(event.session_id, (None,))[0] is not current
                            and not (current.interaction_route is None and current.substate == 'soft_end'))):
                    return self._identity_result(event.session_id, "profile_active_session_ignored")
                checked = validate_identity_decision(IdentityDecision("authorized", identity_profile,
                    frozenset({"personalize"}), event.payload.get("expires_monotonic", 0)), self.monotonic_now())
                if checked.status != "authorized":
                    return self._identity_result(event.session_id, "invalid_client_profile")
                profile = checked.profile
                profile_request = {"request_id": event.payload["request_id"]}
                if identity_current is not None:
                    profile_request['current'] = lambda: identity_current(False)
                admission_state = copy.deepcopy(current) if current is not None else self.canonical_runtime._new_state(event.session_id, None)
                candidate = CanonicalEvent(event.event_type, event.session_id, event.sequence,
                    event.payload.get('state_version', admission_state.state_version), event.source,
                    dict(event.payload), event.correlation_id, event.timestamp, event.monotonic_timestamp)
                candidate.validate()
                rejected = self.canonical_runtime._admit(candidate, admission_state)
                if rejected:
                    return self._identity_result(event.session_id, rejected)
                rejected = await self._prepare_fresh_profile(event, identity_current)
                if rejected:
                    return self._identity_result(event.session_id, rejected)
                current = self.canonical_runtime.sessions[event.session_id]
                fresh_profile_prepared = True
        if event.event_type in {"IdentityReadyExpired", "IdentityAuthorizationExpired"}:
            request = self._identity_requests.get(event.session_id)
            if not identity_event_authorized:
                return self._identity_result(event.session_id, "identity_outcome_untrusted")
            if (not self.optional_identity or request is None or current is None
                    or request["request_id"] != event.payload.get("request_id")
                    or current.identity_request_id != request["request_id"]):
                return self._identity_result(event.session_id, "stale_profile_context")
        requested_version = event.payload.get("state_version") if isinstance(event.payload, dict) else None
        state_version = (current.state_version if fresh_profile_prepared else
            int(requested_version) if isinstance(requested_version, int) else (current.state_version if current else 0))
        previous_state = copy.deepcopy(current)
        public_story = self._public_story_expiry(event, current, authorized=identity_event_authorized)
        public_engagement = dict(self._engagement_owners[event.session_id]) if public_story and event.session_id in self._engagement_owners else None
        event_type = "VoiceIntentReady" if event.event_type == "SpeechReceived" else event.event_type
        payload = dict(event.payload)
        accepted_monotonic = (self.monotonic_now() if event.event_type in {"GameTimer", "InteractionIdleExpired", "SilentHoldTick"} else
                              event.monotonic_timestamp if event.monotonic_timestamp is not None else self.monotonic_now())
        voice_admission = None
        personal_welcome_answer_verified = False
        game_confirmation_authorized = False
        catalogue_descriptor = None
        weather_scope = _weather_scope
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
            admitted_offer = ((current.game_offer_id, current.game_offer_ready)
                              if current is not None and current.pending_game_offer else None)
            parsed_intent = classify_intent(text)
            explicit_help_request = parsed_intent == "help_request"
            admitted_handset = current.interaction_open if current is not None else False
            epoch = self._session_epochs.get(event.session_id, 0)
            revision = self._session_revisions.get(event.session_id, 0)
            admitted_game_scope = self._game_voice_scope(event.session_id)
            admitted_catalogue_scope = self._catalogue_voice_scope(event.session_id)
            catalogue_descriptor = copy.deepcopy(getattr(_speech_ingress_guard, 'catalogue_descriptor',
                current.scene_menu_descriptor if current is not None else None))
            game_check_owner = self._game_speech_check_owner(event.session_id)
            if game_check_owner is not None:
                # Automatic hints cannot replace a child's admitted speech
                # while its mandatory safety decision is still pending.
                self._game_speech_checks[asyncio.current_task()] = game_check_owner
                self._sync_game_timer(event.session_id)
            safety_started = self.monotonic_now()
            wellbeing_question = (current.support_question_kind if current is not None
                and current.mode == "wellbeing_check" and current.safety_severity == "none" else None)
            reassurance = wellbeing_reassurance(text, wellbeing_question)
            safety = (await self.safety_classifier.classify(text, wellbeing_question=wellbeing_question)
                      if reassurance else await self.safety_classifier.classify(text))
            # Retain fixed outcome codes, never a transcript, name, text hash,
            # model response or exception message. Keep diagnostics independent
            # of the latest canonical event projection.
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
            if current is None or current.interaction_open != admitted_handset:
                return self._identity_result(event.session_id, "stale_session_generation")
            stale_speech_phase = (_speech_ingress_guard is not None and not _speech_ingress_guard())
            same_game_scope = (admitted_game_scope is not None
                and self._game_voice_scope(event.session_id) == admitted_game_scope)
            same_catalogue_scope = (admitted_catalogue_scope is not None
                and self._catalogue_voice_scope(event.session_id) == admitted_catalogue_scope)
            if ((self._session_revisions.get(event.session_id, 0) != revision and not (same_game_scope or same_catalogue_scope))
                    or current.last_sequence_by_source.get(event.source) != event.sequence
                    or stale_speech_phase):
                if safety.severity == "none" and not explicit_help_request:
                    return self._identity_result(event.session_id, "stale_session_generation")
                # A welcome timer or other ordinary transition must not erase
                # a possible disclosure from the same physical session.
            # Every await may change dedup state even without a transition.
            # Rollback must never restore a pre-await snapshot.
            if safety.severity == 'none' and not explicit_help_request:
                if self._profile_replacing(event.session_id):
                    return self._identity_result(event.session_id, 'profile_replacing')
                if current.identity_request_id and not self._identity_scope_live(event.session_id):
                    return self._identity_result(event.session_id, 'stale_profile_context')
            previous_state = copy.deepcopy(current)
            state_version = current.state_version
            uncertain_stt = payload.pop("_stt_untrusted", False)
            verified_voice_activity = bool(safety.verified and not uncertain_stt and not stale_speech_phase and text.strip())
            if uncertain_stt and safety.severity == "none" and not explicit_help_request:
                return {**self._identity_result(event.session_id, "audio_retry"),
                        "outputs": [{"action": "audio_retry", "reason": "stt_low_confidence"}]}
            intent = parsed_intent
            if uncertain_stt or not safety.verified or stale_speech_phase:
                # Possible danger is still escalated, without treating the
                # transcript as a reliable name, age, command, or memory.
                transient_text = None
                intent = ("help_request" if explicit_help_request
                          else "input_retry" if safety.severity == "none" else "free_speech")
            personal_welcome_answer_verified = (safety.verified and safety.severity == "none"
                and not uncertain_stt and bool((transient_text or "").strip()))
            weather_scope = weather_request_scope(transient_text) if personal_welcome_answer_verified else None
            if (personal_welcome_answer_verified and reassurance and current.mode == "wellbeing_check"
                    and current.support_question_kind == wellbeing_question and current.safety_severity == "none"):
                intent = "no"
            if current.pending_game_offer:
                game_confirmation_authorized = bool(personal_welcome_answer_verified and admitted_offer
                    and admitted_offer == (current.game_offer_id, True) and not explicit_help_request)
                if game_confirmation_authorized:
                    intent = game_confirmation_intent(text)
            mic_probe = getattr(self, "_mic_unavailable_probe", None)
            if callable(mic_probe) and mic_probe():
                payload["mic_unavailable"] = True
            payload.update({"intent": intent, "safety_severity": safety.severity,
                            "safety_reason": safety.reason})
        if event.event_type == "PersonalStoryReady":
            owner = self._personal_stories.get(event.session_id)
            personal_story_authorized = bool(personal_story_authorized and owner and owner.audio is not None
                and event.payload.get("request_id") == owner.request_id
                and self._personal_story_scope(event.session_id, owner))
        if (event.event_type == "StoryPositionUpdated" and event.source == "browser_audio_checkpoint"
                and payload.get("story_id") == STORY_ID):
            owner = self._personal_stories.get(event.session_id)
            paused = self._paused_story_playback.get(event.session_id)
            if (_story_checkpoint_guard is None or not _story_checkpoint_guard()
                    or not self._personal_story_scope(event.session_id, owner, checkpoint=True)
                    or owner.checkpoint_ms != payload.get("offset_ms") or not paused
                    or any(paused.get(key) != payload.get(key) for key in
                           ("generation", "token", "playback_id", "playback_lease"))):
                return self._identity_result(event.session_id, "stale_story_checkpoint")
        if (current is not None and current.mode == "story_paused" and current.active_story_id == STORY_ID
                and self._paused_story_playback.get(event.session_id, {}).get("browser_checkpoint_pending")
                and ((event.event_type == "ButtonPressed" and payload.get("button_id") == 6)
                     or (event.event_type == "SpeechReceived" and payload.get("intent") in {"continue", "resume", "resume_story"}))):
            owner = self._personal_stories.get(event.session_id)
            if (not self._personal_story_scope(event.session_id, owner, checkpoint=True)
                    or owner.checkpoint_ms is None or not current.story_checkpoint_confirmed):
                return self._identity_result(event.session_id, "personal_story_checkpoint_pending")
        if (current is not None and current.mode == "personal_story_theme" and transient_text
                and payload.get("safety_severity") == "none" and personal_welcome_answer_verified
                and payload.get("intent") != "help_request"):
            from .menu_content import catalogue_choice
            payload["intent"] = catalogue_choice(transient_text, 'personal_story_theme', current.age_band,
                descriptor=current.scene_menu_descriptor) or "unsupported_story_theme"
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
        if _action_guard is not None and not _action_guard():
            return self._identity_result(event.session_id, "stale_session_generation")
        decision = self.canonical_runtime.handle(CanonicalEvent(
            event_type=event_type, session_id=event.session_id, sequence=event.sequence,
            state_version=state_version, source=event.source, payload=payload,
            correlation_id=event.correlation_id, timestamp=event.timestamp, monotonic_timestamp=accepted_monotonic,
        ), profile, transient_text=transient_text,
           _voice_admission=voice_admission,
           private_welcome_ready=private_welcome_ready,
           personal_welcome_answer_verified=personal_welcome_answer_verified,
           game_confirmation_authorized=game_confirmation_authorized,
           catalogue_descriptor=catalogue_descriptor,
           dialogue_fallback_authorized=dialogue_fallback_authorized,
           personal_story_authorized=personal_story_authorized,
           identity_event_authorized=identity_event_authorized,
           session_action_authorized=session_action_authorized,
           identity_context_present=self._has_private_identity_context(event.session_id),
           public_story_expiry=public_story,
           public_story_checkpoint_authorized=public_story_checkpoint_authorized,
           game_timer_authorized=game_timer_authorized, interaction_idle_authorized=interaction_idle_authorized)
        if decision["reason"] in {"welcome_started", "personal_welcome_started"}:
            self._choose_private_welcome(event.session_id)
        snapshot, effects = decision["state"], decision["effects"]
        if decision["reason"] != "public_story_identity_expired":
            public_story = public_engagement = None
        exhibition_reset = bool(decision.get("reset_session"))
        prelift_expired = bool(event.event_type == "IdentityReadyExpired" and decision["reason"] == "identity_expired"
                              and previous_state is not None and previous_state.mode == "profile_ready")
        interaction_stopped = decision["reason"] == "interaction_idle"
        sync_stop = (exhibition_reset or prelift_expired or interaction_stopped
                     or (session_action_authorized and event.event_type == "SessionCancel"))
        self.last_canonical_state = dict(snapshot)
        self.last_canonical_event = {"type": event_type, "source": event.source, "sequence": event.sequence, "session_id": event.session_id, "timestamp": event.timestamp, "monotonic_timestamp": accepted_monotonic, "correlation_id": event.correlation_id, "payload": dict(payload)}
        voice_open = bool(self.canonical_runtime.sessions[event.session_id].audio_open)
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
            self.last_canonical_state = dict(rejected_state)
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
        # Internal completions may enrich their owning decision without
        # replacing its ingress revision.
        child_ack = (
            (event.event_type == "StoryPositionUpdated" and event.source == "audio_pause")
            or (event.event_type in {"PersonalStoryReady", "AudioCompleted"} and personal_story_authorized)
            or (event.event_type == "DialogueFallback" and dialogue_fallback_authorized)
        )
        previous_version = previous_state.state_version if previous_state is not None else -1
        # A physical key release/hold acknowledgement is telemetry, not a
        # replacement for the response triggered by its preceding press.
        semantic_effects = [item for item in effects if item.get("type") != "button_edge_ack"]
        takes_ownership = bool(semantic_effects) or snapshot.get("state_version") != previous_version or exhibition_reset
        if takes_ownership:
            if event.event_type == 'ClientProfileCleared' and decision['reason'] == 'profile_cleared':
                self._identity_replacements[event.session_id] = (self.canonical_runtime.sessions[event.session_id], None)
            elif event.event_type in {'HandsetLifted', 'HandsetPlaced', 'SessionCancel', 'SessionReset', 'AnonymousStart',
                                      'HardwareDisconnected', 'InteractionIdleExpired'}:
                self._identity_replacements.pop(event.session_id, None)
        if (previous_state is not None and previous_state.mode == "safety_stop"
                and snapshot.get("mode") == "safety_stop" and not effects and not exhibition_reset):
            # Telemetry and a severity upgrade do not replace the initial
            # guidance owner while its old playback cleanup is draining.
            takes_ownership = False
        if (takes_ownership and self.max_json and self._max_client_boundary is not None
                and event.session_id == self._max_client_boundary.session_id
                and event.event_type in {"HandsetPlaced", "SessionCancel", "SessionReset", "AnonymousStart"}):
            # Fence Max only after canonical acceptance. Duplicate/stale handset
            # edges must not clear one side while the other retains its context.
            self._max_client_boundary.clear_client()
        revision = self._session_revisions.get(event.session_id, 0) + (1 if takes_ownership and not child_ack else 0)
        self._session_revisions[event.session_id] = revision
        if takes_ownership and not child_ack:
            self._drop_public_story_transfer(event.session_id)
            self._cancel_owned_text(event.session_id)
            self._cancel_game_cues(event.session_id)
        next_owner = self.canonical_runtime.sessions.get(event.session_id)
        identity_owner_changed = bool(self.optional_identity and previous_state is not None
            and previous_state.identity_request_id != getattr(next_owner, "identity_request_id", None))
        identity_ended = bool(previous_state is not None and previous_state.profile_ref
                              and getattr(next_owner, "profile_ref", None) is None)
        public_identity_preserved = decision["reason"] in {"public_game_identity_expired", "public_menu_identity_expired"}
        identity_cleanup = (not public_identity_preserved) and (
            identity_owner_changed or identity_ended or (
                takes_ownership and event.event_type == "ClientProfileCleared")
        )
        if (identity_ended and self.max_json and event.event_type != "ClientProfileCleared"
                and self._max_client_boundary is not None
                and self._max_client_boundary.session_id == event.session_id):
            self._max_client_boundary.clear_client()
        if (takes_ownership and event.event_type in {"SessionStarted", "HandsetPlaced", "SessionCancel", "SessionReset", "AnonymousStart", "HardwareDisconnected", "InteractionIdleExpired"}) or exhibition_reset or identity_cleanup:
            self._end_journal_visit(event.session_id)
            self._session_epochs[event.session_id] = self._session_epochs.get(event.session_id, 0) + 1
            self._paused_story_playback.pop(event.session_id, None)
            self._discard_private_welcome(event.session_id)
        game_context = self.conversation_contexts.get(event.session_id)
        if (getattr(game_context, "_game_id", None) == "word_game"
                and (next_owner is None or next_owner.mode != "game_active"
                     or next_owner.active_game_id != "word_game")):
            self._drop_dialogue_context(event.session_id)
        self._transition_dialogue_memory(
            event.session_id, event.event_type, previous_state,
            self.canonical_runtime.sessions.get(event.session_id),
            takes_ownership=takes_ownership, exhibition_reset=exhibition_reset,
        )
        if exhibition_reset or (session_action_authorized and event.event_type == "SessionCancel"):
            self._session_reset_waiters[event.session_id] = (
                asyncio.current_task(), asyncio.get_running_loop().create_future()
            )
        epoch = self._session_epochs.get(event.session_id, 0)
        state_owner = self.canonical_runtime.sessions.get(event.session_id)
        if public_story and public_story["offset_ms"] is None:
            transfer = {"old": public_story["old"], "story_id": public_story["story_id"],
                        "duration_ms": public_story["duration_ms"], "resume": public_story["resume"],
                        "epoch": epoch, "revision": revision, "state": state_owner,
                        "expires": self.monotonic_now() + 5.0,
                        "ready": asyncio.get_running_loop().create_future()}
            transfer["timer"] = asyncio.get_running_loop().call_later(
                5.0, lambda: self._drop_public_story_transfer(event.session_id)
                if self._public_story_checkpoint_transfers.get(event.session_id) is transfer else None)
            self._public_story_checkpoint_transfers[event.session_id] = transfer
        story_owner = self._personal_stories.get(event.session_id)
        if story_owner and (not state_owner or state_owner.mode not in STORY_MODES or not state_owner.interaction_open
                or self._session_epochs.get(event.session_id,0) != story_owner.epoch
                or (state_owner.profile_ref,state_owner.identity_request_id) != story_owner.identity
                or (story_owner.identity[0] and not self._identity_scope_live(event.session_id,"personalize"))):
            self._discard_personal_story(event.session_id)

        self._sync_game_timer(event.session_id)
        self._sync_silent_hold_timer(event.session_id)
        self._engagement_event(event, decision, accepted=takes_ownership, voice_activity=verified_voice_activity)

        # A local safety signal must be visible before old audio cleanup awaits.
        if callable(self.technical_state_observer):
            try:
                self.technical_state_observer(snapshot, event)
            except Exception:
                self.canonical_diagnostics.append({"kind": "operator_observation_failed"})

        reset_sync_results: dict[str, dict[str, Any]] = {}
        local_safety_batch = snapshot.get("mode") == "safety_stop" and any(
            effect.get("type") == "audio_stop" for effect in effects)
        local_safety_cleanup_tasks: set[asyncio.Task[Any]] = set()

        def reset_sync_step(resource: str, action) -> None:
            # A failed device must not prevent another device or RAM owner
            # from being revoked. Report failure, never claim physical success.
            try:
                action()
                reset_sync_results[resource] = {"status": "completed"}
            except Exception as exc:
                failure = {"type": "cleanup_failed", "resource": resource,
                           "operation": "local_safety" if local_safety_batch else "interaction_idle" if interaction_stopped else "profile_ready_expiry" if prelift_expired else "exhibition_reset", "status": "failed",
                           "exception_type": type(exc).__name__}
                reset_sync_results[resource] = failure
                effects.append(dict(failure))
                self.canonical_diagnostics.append(dict(failure))
                self.runtime_status["session_cleanup_last_failure"] = dict(failure)
                logger.warning("canonical stop %s cleanup failed: %s", resource, type(exc).__name__)

        if sync_stop or local_safety_batch:
            # Physical hangup takes effect synchronously. Native/model audio
            # cleanup can await, fail or be cancelled; none may keep a stale
            # wave or tutorial lit after canonical handset-down was accepted.
            self._cancel_welcome_timer(event.session_id)
            self._cancel_menu_tutorial(event.session_id)
            self._cancel_lighting_wave(event.session_id)
            self._invalidate_audio_lease(event.session_id)
            closing_record = self.sessions.get(event.session_id)
            if closing_record is None and local_safety_batch:
                closing_record = SessionRecord(event.session_id, self.installation_id,
                    event.correlation_id or str(uuid.uuid4()), handset_up=snapshot.get("handset") == "up")
            if closing_record is not None:
                if not interaction_stopped and not local_safety_batch:
                    closing_record.handset_up = snapshot.get("handset") == "up"
                closing_record.cancellation_token += 1
                self.sessions.save(closing_record)
                reset_sync_step("audio", lambda: self.audio.cancel(closing_record.cancellation_token))
            cancel_effects = getattr(self.led, "cancel_effects", None)
            if local_safety_batch:
                # Commit capture closure and the complete safe frame before
                # the first provider await. Each device cleanup is independent.
                close_capture = getattr(self.s2s_audio, "close_capture", None)
                reset_sync_step("capture", lambda: close_capture(event.session_id)
                                if self.s2s_audio is not None else None)
                reset_sync_step("lighting_effects", lambda: cancel_effects(clear=False)
                                if callable(cancel_effects) else None)
                desired = dict(LED_PRESETS["SAFE_STOP"])
                reset_sync_step("lighting", lambda: self.led.set_elements(tuple(
                    LightingCommand(element, int(desired.get(element, 0)), 0)
                    for element in LIGHTING_ELEMENT_IDS)))
            else:
                reset_sync_step("lighting", lambda: cancel_effects(clear=True)
                                if callable(cancel_effects) else self.led.clear_all())

        def still_current() -> bool:
            return (
                self._session_revisions.get(event.session_id) == revision
                and self._session_epochs.get(event.session_id, 0) == epoch
                and self.canonical_runtime.sessions.get(event.session_id) is state_owner
                and (not fresh_profile_prepared or identity_current is None or identity_current(False))
            )

        def stale_result() -> dict[str, Any]:
            current_state = self.canonical_runtime.sessions.get(event.session_id)
            current_snapshot = current_state.snapshot() if current_state is not None else {"mode": "idle", "handset": "down"}
            return {"reason": "stale_session_generation", "state": current_snapshot["mode"],
                    "state_version": current_snapshot.get("state_version", 0),
                    "outputs": [], "canonical_state": current_snapshot}

        guidance = next((item for item in effects
            if item.get("type") == "speak" and item.get("phrase_id") == "PHRASE_091"), None)
        if local_safety_batch and guidance is not None and voice_open:
            _safety_guidance.update(owner=state_owner, epoch=epoch,
                                   lease=guidance.get("playback_lease"), effect=guidance)

        if ((identity_cleanup and not fresh_profile_prepared)
                or (session_action_authorized and event.event_type == "SessionCancel")):
            # Fence all private context/audio before any provider cleanup await.
            # The replacement profile context is installed only after this event commits.
            self._drop_identity_owner(event.session_id)
            self._invalidate_audio_lease(event.session_id)
            old_record = self.sessions.get(event.session_id)
            if old_record is not None:
                old_record.child_id, old_record.anonymous = None, True
                old_record.cancellation_token += 1
                self.sessions.save(old_record)
                if not sync_stop:
                    self.audio.cancel(old_record.cancellation_token)
            await self._cleanup_session_resources(event.session_id, identity_request=profile_request,
                cleanup_tasks=local_safety_cleanup_tasks if local_safety_batch else None,
                preserve_public_story_transfer=bool(public_story))
            if not still_current():
                return stale_result()
            if public_engagement is not None:
                public_engagement.update(scope=self._engagement_scope(event.session_id),
                    completed_work=False, preserve_deadline=not public_story["resume"])
                self._engagement_owners[event.session_id] = public_engagement
                self._start_engagement_monitor()
            transfer = self._public_story_transfer(event.session_id)
            if transfer is not None and not transfer["ready"].done():
                transfer["ready"].set_result(None)

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
        if decision["reason"] == "exhibition_handset_lifted" or (fresh_profile_prepared
                and decision['reason'] == 'profile_ready' and voice_open) or decision['reason'] == 'panel_session_started':
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
                    elif pattern == "story_preparing_wave":
                        selected = effect.get('button_id', 3)
                        selected = selected if type(selected) is int and 1 <= selected <= 5 else 3
                        order = [selected] + [b for b in range(1,7) if b != selected]
                        lighting = start_wave(now=accepted_monotonic,
                            groups=tuple((BUTTON_FILL_ELEMENTS[b],) for b in order),
                            base_elements=(BUTTON_DIGIT_ELEMENTS[7][1],),
                            hold_on_complete=False, loop=True, stage_duration_ms=900)
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
                        if getattr(self.audio, "browser_story_checkpoints", False):
                            self._paused_story_playback[event.session_id]["browser_checkpoint_pending"] = True
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
                        checkpoint = await self._receive_canonical_event(self._internal_followup(
                            event, "StoryPositionUpdated", "audio_pause",
                            {"story_id": effect.get("story_id"), "offset_ms": offset_ms},
                            "pause-position",
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
                if not local_safety_batch:
                    record.cancellation_token += 1
                    self._invalidate_audio_lease(event.session_id)
                if sync_stop or local_safety_batch:
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
                if hasattr(self, "_cancel_game_prewarm"):
                    self._cancel_game_prewarm(event.session_id)
                if self.bounded_s2s is not None:
                    effect["audio_cleanup"] = await self._cancel_audio_owner(
                        self.bounded_s2s, event.session_id, operation="audio_stop",
                        ownership_guard=still_current,
                        cleanup_tasks=local_safety_cleanup_tasks if local_safety_batch else None,
                    )
                if not still_current():
                    return stale_result()
                if self.s2s_audio is not None and self.s2s_audio is not self.bounded_s2s:
                    effect["s2s_cleanup"] = await self._cancel_audio_owner(
                        self.s2s_audio, event.session_id, operation="audio_stop",
                        ownership_guard=still_current,
                        cleanup_tasks=local_safety_cleanup_tasks if local_safety_batch else None,
                    )
            elif kind == "lighting":
                elements = tuple(int(element_id) for element_id in effect.get("elements", ()))
                target_state = int(effect.get("state", 0))
                if target_state == 1:
                    cancel_effects = getattr(self.led, "cancel_effects", None)
                    if callable(cancel_effects):
                        cancel_effects(clear=False)
                    active_set = set(elements)
                    self.led.set_elements(tuple(
                        LightingCommand(element_id, int(element_id in active_set), 0)
                        for element_id in LIGHTING_ELEMENT_IDS
                    ))
                else:
                    for element_id in elements:
                        self.led.set_element(element_id, target_state)
            elif kind == "scene_menu_page":
                descriptor=effect["descriptor"]
                cancel_effects=getattr(self.led,"cancel_effects",None)
                if callable(cancel_effects): cancel_effects(clear=False)
                buttons=descriptor["buttons"]
                lit={
                    element
                    for button in buttons
                    for element in (
                        (BUTTON_DIGIT_ELEMENTS[7][1],)
                        if int(button) == 7
                        else BUTTON_ELEMENTS.get(int(button), ())
                    )
                }
                self.led.set_elements(tuple(LightingCommand(element,int(element in lit),0)
                    for element in LIGHTING_ELEMENT_IDS))
            elif kind == "narrate_scene_menu":
                from .scene_menus import MENU_PCM_MODES
                def menu_current():
                    return bool(still_current() and state_owner.interaction_open
                        and state_owner.mode==MENU_PCM_MODES.get(effect.get("content_id"))
                        and state_owner.scene_menu_phase=="narrating"
                        and state_owner.playback_lease==effect.get("playback_lease"))
                try:
                    text=str(state_owner.scene_menu_descriptor.get("spoken_text") or "")
                    streamed=await self._stream_menu_audio(event.session_id,text,effect,
                        record.cancellation_token,menu_current)
                    if not menu_current():return stale_result()
                    if streamed is None:
                        audio=await self._render_typed_audio(event.session_id,text,effect,
                            record.cancellation_token,menu_current)
                        if not menu_current():return stale_result()
                        if audio is None:raise RuntimeError("menu_audio_unavailable")
                        self._play_typed_audio(event.session_id,audio,effect,record.cancellation_token)
                    elif not streamed:
                        raise RuntimeError("menu_audio_unavailable")
                    _set_audio_effect_status(effect,asset_status="approved",queued=True,resolution="scene_menu_audio_queued")
                except Exception:
                    if not still_current():return stale_result()
                    audio_failure={"response_id":effect["content_id"],"playback_lease":effect["playback_lease"],
                        "error_code":"scene_menu_audio_unavailable"}
                    _set_audio_effect_status(effect,asset_status="unavailable",queued=False,resolution="scene_menu_audio_unavailable")
            elif kind == "game_prompt":
                def game_current():
                    return bool(still_current() and state_owner.interaction_open
                        and state_owner.mode == "game_active"
                        and state_owner.active_game_id == effect.get("game_id")
                        and state_owner.playback_lease == effect.get("playback_lease"))
                if getattr(self.audio, "private_pcm_supported", False) and hasattr(self.audio, "play_pcm_stream_owned"):
                    try:
                        ok = await self._stream_game_prompt(event.session_id, effect, record.cancellation_token, game_current)
                        if not game_current(): return stale_result()
                        if ok:
                            _set_audio_effect_status(effect, asset_status="approved", queued=True, resolution="game_prompt_pcm_queued")
                        else:
                            audio_failure = {"response_id": effect["content_id"],
                                "playback_lease": effect["playback_lease"], "error_code": "game_prompt_unavailable"}
                            _set_audio_effect_status(effect, asset_status="absent", queued=False, resolution="game_prompt_unavailable")
                    except Exception:
                        if not game_current(): return stale_result()
                        audio_failure = {"response_id": effect["content_id"],
                            "playback_lease": effect["playback_lease"], "error_code": "game_prompt_dispatch_failed"}
                        _set_audio_effect_status(effect, asset_status="approved", queued=False, resolution="game_prompt_dispatch_failed")
                else:
                    rendered = await self._render_game_prompt(event.session_id, effect, record.cancellation_token, game_current)
                    if not game_current(): return stale_result()
                    if rendered is None:
                        audio_failure = {"response_id": effect["content_id"],
                            "playback_lease": effect["playback_lease"], "error_code": "game_prompt_unavailable"}
                        _set_audio_effect_status(effect, asset_status="absent", queued=False, resolution="game_prompt_unavailable")
                    else:
                        try:
                            self._play_game_prompt(event.session_id, rendered, effect, record.cancellation_token)
                            _set_audio_effect_status(effect, asset_status="approved", queued=True, resolution="game_prompt_pcm_queued")
                        except Exception:
                            audio_failure = {"response_id": effect["content_id"],
                                "playback_lease": effect["playback_lease"], "error_code": "game_prompt_dispatch_failed"}
                            _set_audio_effect_status(effect, asset_status="approved", queued=False, resolution="game_prompt_dispatch_failed")
            elif kind == "game_started":
                if (getattr(self.audio, "private_pcm_supported", False)
                        and hasattr(self.audio, "play_pcm_stream_owned")
                        and hasattr(self, "_schedule_game_prompt_prewarm")):
                    self._schedule_game_prompt_prewarm(event.session_id, state_owner)
            elif kind == "dialogue_prompt":
                def prompt_current():
                    return bool(still_current() and state_owner.interaction_open
                        and state_owner.active_audio_id == effect.get("content_id")
                        and state_owner.playback_lease == effect.get("playback_lease"))
                streamed = None
                if effect["content_id"] == "DIALOGUE_INTRO":
                    streamed = await self._stream_menu_audio(event.session_id, effect["text"], effect,
                                                             record.cancellation_token, prompt_current)
                if not prompt_current():
                    return stale_result()
                rendered = None
                if streamed is None:
                    rendered = await self._render_typed_audio(event.session_id, effect["text"], effect,
                                                             record.cancellation_token, prompt_current)
                if not prompt_current():
                    return stale_result()
                if streamed is False or (streamed is None and rendered is None):
                    audio_failure = {"response_id": effect["content_id"],
                        "playback_lease": effect["playback_lease"], "error_code": "dialogue_prompt_unavailable"}
                    _set_audio_effect_status(effect, asset_status="unavailable", queued=False,
                                             resolution="dialogue_prompt_unavailable")
                else:
                    try:
                        if effect["content_id"] in {"DIALOGUE_INTRO", "DIALOGUE_RETURN"}:
                            await self._set_dialogue_lighting_phase(event.session_id, "output")
                            if not prompt_current():
                                return stale_result()
                        if streamed is None:
                            self._play_typed_audio(event.session_id, rendered, effect, record.cancellation_token)
                        _set_audio_effect_status(effect, asset_status="approved", queued=True,
                                                 resolution="dialogue_prompt_pcm_queued")
                    except Exception:
                        audio_failure = {"response_id": effect["content_id"],
                            "playback_lease": effect["playback_lease"], "error_code": "dialogue_prompt_dispatch_failed"}
            elif kind == "game_lighting":
                if effect.get("animation") == "ripple":
                    ripple = getattr(self.led, "start_game_ripple", None)
                    if callable(ripple):
                        effect["lighting_effect"] = ripple(int(effect["origin_button"]), now=self.monotonic_now())
                        effect["resolution"] = "game_ripple_started"
                elif effect.get("animation") == "success":
                    effects_engine = getattr(self.led, "exhibition_effects", None)
                    start_success = getattr(effects_engine, "start_game_success_wave", None)
                    if callable(start_success):
                        next_preset = effect.get("next_preset")
                        if next_preset:
                            preset_cmds = LED_PRESETS.get(str(next_preset), ())
                            desired = {element_id: 0 for element_id in LIGHTING_ELEMENT_IDS}
                            for element_id, st_val in preset_cmds:
                                desired[int(element_id)] = int(st_val)
                            next_cmds = tuple(LightingCommand(eid, st_val, 140) for eid, st_val in desired.items())
                        elif effect.get("next_buttons") is not None:
                            active = dict(game_light_commands(effect.get("next_buttons", ()), effect.get("display", "digits")))
                            next_cmds = tuple(LightingCommand(eid, int(eid in active), 140) for eid in LIGHTING_ELEMENT_IDS)
                        else:
                            next_cmds = ()
                        effect["lighting_effect"] = start_success(
                            effect.get("origin_button"),
                            next_commands=next_cmds,
                            now=self.monotonic_now(),
                        )
                        effect["resolution"] = "game_success_wave_started"
            elif kind == "game_exit":
                self._cancel_game_cues(event.session_id)
                effect["resolution"] = "game_presentation_released"
            elif kind == "lighting_preset":
                preset = str(effect.get("preset", ""))
                if local_safety_batch and preset == "SAFE_STOP":
                    effect["cleanup"] = dict(reset_sync_results["lighting"])
                    effect["resolution"] = "local_safety_frame_committed"
                    continue
                # An explicit, current canonical listening effect owns this
                # frame. Backend dialogue callbacks remain bear-mode scoped.
                start_breathing = getattr(self.led, "start_breathing", None)
                if preset == "BEAR_DIALOGUE" and snapshot.get("mode") == "bear_dialogue" and callable(start_breathing):
                    phase = ("preparing" if state_owner.active_audio_id in {"DIALOGUE_INTRO", "DIALOGUE_RETURN"}
                             else "listening")
                    lighting = await self._set_dialogue_lighting_phase(event.session_id, phase)
                    if not still_current():
                        return stale_result()
                    if lighting is not None:
                        effect["lighting_effect"] = dict(lighting)
                        effect["resolution"] = "dialogue_entry_" + phase
                        continue
                if preset == "LISTENING" and callable(start_breathing):
                    effect["lighting_effect"] = dict(start_breathing(
                        now=self.monotonic_now(), elements=DIALOGUE_FILL_ELEMENTS,
                        base_elements=DIALOGUE_ANCHOR_ELEMENTS, phase="listening",
                        preserve_current=True,
                    ))
                    effect["resolution"] = "canonical_listening_lighting"
                    continue
                # Replace the complete logical frame in one ordered batch.
                # Sending clear_all() first caused a visible off-to-on pulse
                # on the large weather pictogram (element 17).
                effects_engine = getattr(self.led, "exhibition_effects", None)
                if (preset == "ROOT_MENU"
                        and getattr(effects_engine, "effect", None) == "wave"
                        and getattr(effects_engine, "wave_phase", None) == "game_success"):
                    continue
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
                    commands = game_light_commands(effect.get("buttons", ()), effect.get("display", "digits"))
                if preset == 'GAME_EXIT_HOLD':
                    commands = tuple((element, 1) for element in (14,13,10,9,6,3,1)[:int(effect['step'])])
                if preset == "TARGET":
                    commands = tuple((element_id, 1) for element_id in BUTTON_ELEMENTS.get(int(effect.get("target_button", 0)), ()))
                desired = {element_id: 0 for element_id in LIGHTING_ELEMENT_IDS}
                for element_id, state in commands:
                    desired[int(element_id)] = int(state)
                self.led.set_elements(tuple(
                    LightingCommand(element_id, state, 140 if preset in {"GAME_OUTPUT",'GAME_EXIT_HOLD'} else 0)
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
                effect.update(playback_id=handle.playback_id, generation=handle.generation,
                              session_id=handle.session_id)
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
                if local_safety_batch and phrase_id == "PHRASE_091" and voice_open:
                    pending = set()
                    if local_safety_cleanup_tasks:
                        _, pending = await asyncio.wait(local_safety_cleanup_tasks,
                            timeout=self.audio_cleanup_timeout_seconds)
                    if (not still_current() or not state_owner.interaction_open
                            or state_owner.mode != "safety_stop"):
                        return stale_result()
                    failed = any(item.get("status") == "failed" for item in reset_sync_results.values())
                    failed = failed or any(task.cancelled() or task.exception() is not None
                        for task in local_safety_cleanup_tasks if task.done())
                    failed = failed or any(effect.get(key, {}).get("status") in {"failed", "unavailable"}
                        for effect in effects for key in ("audio_cleanup", "s2s_cleanup"))
                    if pending or failed:
                        code = "local_safety_audio_cleanup_pending" if pending else "local_safety_audio_cleanup_failed"
                        self._finish_unstarted_safety_guidance(event.session_id,
                            **_safety_guidance, code=code)
                        continue
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
                        live_context = self._bounded_dialogue_context(event.session_id, game_id=effect.get("game_id"))
                        turn_context = copy.deepcopy(live_context)
                        if effect.get("game_id") == "word_game":
                            def reserve_word(word_state):
                                # Reserve an emitted word across barge-in; stale generations cannot write it.
                                if still_current() and self.conversation_contexts.get(event.session_id) is live_context:
                                    live_context._word_state = replace(live_context._word_state, pending=word_state)
                            turn_context._word_reserve = reserve_word
                        journal_visit = None if effect.get("game_id") == "word_game" else self._journal_visit(event.session_id)
                        if journal_visit is not None:
                            await self._append_journal_turn(event.session_id, journal_visit, "user", str(transient_text), still_current)
                            if not still_current() or self._identity_shutdown:
                                return stale_result()
                        turn = await self.bounded_s2s.run_text_turn(str(transient_text), turn_context)
                        await self.expire_identity_leases()
                        if not still_current():
                            return stale_result()
                        if journal_visit is not None and not turn.fallback_reason and turn.chunks:
                            await self._append_journal_turn(event.session_id, journal_visit, "assistant", " ".join(turn.chunks), still_current)
                            if not still_current() or self._identity_shutdown:
                                return stale_result()
                        # Commit only the current generation. A cancelled or late
                        # provider owns a detached copy, never retained RAM.
                        if effect.get("game_id") == "word_game" and turn.fallback_reason:
                            turn_context._word_state = live_context._word_state
                        turn_context.__dict__.pop("_word_reserve", None)
                        self.conversation_contexts[event.session_id] = turn_context
                        fallback_reason = turn.fallback_reason or (
                            "dialogue_exit" if turn.return_to_scenario else None
                        )
                        from .activity_requests import activity_target, activity_request_matches
                        if any(activity_target(action) is not None and not activity_request_matches(
                                getattr(turn, "activity_request", None), transient_text, action, turn_context)
                               for action in turn.actions):
                            fallback_reason = "activity_request_unverified"
                        effect["resolution"] = ("bounded_dialogue_completed" if fallback_reason is None
                                                else "bounded_dialogue_fallback")
                        effect["fallback_reason"] = fallback_reason
                        effect["chunks"] = list(turn.chunks)
                        offers = [item for item in turn.actions if item.value == "offer_game"]
                        requests = [item for item in turn.actions if item.value in {'open_catalog','play_story','request_information'}]
                        if fallback_reason is None and snapshot.get('mode') == 'bear_dialogue' and len(requests) == 1 and not turn.chunks:
                            request = requests[0]
                            field = {'open_catalog':'catalog','play_story':'story_id','request_information':'kind'}[request.value]
                            target = request.params[field]
                            if self.transition_authorizer.authorize_model_action('bear_dialogue', request.value, target)['ok']:
                                action = request.value
                                if action == 'play_story':
                                    from .menu_content import catalogue_choice
                                    chosen = catalogue_choice(transient_text, 'fixed_story_menu', state_owner.age_band,
                                        stories=self.canonical_runtime.stories)
                                    if chosen != target:
                                        action, target = 'open_catalog', 'stories'
                                if action == 'request_information':
                                    from .canonical_services import conversational_information_intent
                                    if conversational_information_intent(transient_text or '') != target:
                                        fallback_reason = 'model_action_rejected'
                                if fallback_reason is None:
                                    followup = await self._receive_canonical_event(self._internal_followup(event,
                                        'DialogueFallback', 'bounded_catalogue', {'reason':'catalogue_request',
                                            'action':action, 'target':target}, 'catalogue-request'),
                                        dialogue_fallback_authorized=True,
                                        _weather_scope=weather_scope if action == 'request_information' and target == 'weather' else None,
                                        _weather_origin_current=still_current)
                                    return {**followup, 'outputs':[effect, *(followup.get('outputs') or [])]}
                        if (fallback_reason is None and snapshot.get("mode") == "bear_dialogue" and len(offers) == 1
                                and not turn.chunks and self.transition_authorizer.authorize_model_action(
                                    "bear_dialogue", "offer_game", offers[0].params.get("game_id", ""))["ok"]):
                            followup = await self._receive_canonical_event(self._internal_followup(
                                event, "DialogueFallback",
                                "bounded_game_offer", {"reason": "game_offer", "game_id": offers[0].params["game_id"]},
                                "game-offer"),
                                dialogue_fallback_authorized=True)
                            return {**followup, "outputs": [effect, *(followup.get("outputs") or [])]}
                        if fallback_reason is None and effect.get("game_id") == "word_game":
                            followup = await self._receive_canonical_event(self._internal_followup(
                                event, "DialogueFallback",
                                "bounded_word_turn", {"reason": "word_retry_complete" if turn.word_accepted is False else "word_turn_complete"},
                                "word-complete"),
                                dialogue_fallback_authorized=True)
                            return {**followup, "outputs": [effect, *(followup.get("outputs") or [])]}
                    except Exception as exc:
                        if not still_current():
                            return stale_result()
                        logger.warning("bounded dialogue turn failed: %s", type(exc).__name__)
                        effect["resolution"] = "bounded_dialogue_fallback"
                        effect["fallback_reason"] = type(exc).__name__
                        fallback_reason = "provider_or_validation_failure"
                if fallback_reason is not None and still_current():
                    fallback = await self._receive_canonical_event(self._internal_followup(
                        event, "DialogueFallback",
                        "bounded_dialogue", {"reason": fallback_reason},
                        "dialogue-fallback",
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
                # Expose the accepted waiting state before the provider holds
                # its HTTP request, so the browser can deliver scoped cancel.
                hook = self.state_update_hook
                if callable(hook):
                    try:
                        update = hook(event.session_id, {"reason": decision["reason"],
                            "state": snapshot.get("mode"), "canonical_state": state_owner.snapshot(), "outputs": effects})
                        if inspect.isawaitable(update):
                            await update
                    except Exception:
                        logger.exception("personal story state update hook failed", extra={"session_id": event.session_id})
                if not still_current():
                    return stale_result()
                owner = await self._prepare_personal_story(event.session_id, effect.get("brief") or {}, record.cancellation_token)
                if not still_current():
                    return stale_result()
                if owner is not None:
                    ready = await self._receive_canonical_event(self._internal_followup(
                        event,"PersonalStoryReady","internal_personal_story",
                        {"request_id":owner.request_id},"story-ready"), personal_story_authorized=True)
                    if not still_current():
                        return stale_result()
                    deferred_effects.extend(ready.get("outputs") or [])
                    effect["resolution"] = "personal_story_prepared"
                    decision["reason"] = ready.get("reason")
                    snapshot = self.canonical_runtime.sessions[event.session_id].snapshot()
                else:
                    fallback = await self._receive_canonical_event(self._internal_followup(
                        event,"DialogueFallback","personal_story_unavailable",
                        {"reason":"personal_story_unavailable"},"story-unavailable"), dialogue_fallback_authorized=True)
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
                    terminal = await self._receive_canonical_event(self._internal_followup(
                        event,"AudioCompleted","internal_personal_story_complete",
                        {"response_id":STORY_ID,"token":record.cancellation_token,
                         "generation":effect["generation"],"playback_id":effect["playback_id"],
                         "playback_lease":effect["playback_lease"]},"story-already-complete"), personal_story_authorized=True)
                    if not still_current():
                        return stale_result()
                    deferred_effects.extend(terminal.get("outputs") or [])
                    effect["resolution"] = "personal_story_already_complete"
                    decision["reason"] = terminal.get("reason")
                    snapshot = self.canonical_runtime.sessions[event.session_id].snapshot()
                elif queued:
                    effect["resolution"] = "personal_story_pcm_queued"
                else:
                    fallback = await self._receive_canonical_event(self._internal_followup(
                        event,"DialogueFallback","personal_story_unavailable",
                        {"reason":"personal_story_unavailable"},"story-output-unavailable"), dialogue_fallback_authorized=True)
                    if not still_current():
                        return stale_result()
                    deferred_effects.extend(fallback.get("outputs") or [])
                    effect["resolution"] = "personal_story_output_unavailable"
                    decision["reason"] = fallback.get("reason")
                    snapshot = self.canonical_runtime.sessions[event.session_id].snapshot()
            elif kind in {"capture_open", "capture_close", "audio_stop"}:
                if local_safety_batch and kind == "capture_close":
                    effect["cleanup"] = dict(reset_sync_results["capture"])
                    effect["resolution"] = "local_capture_closed"
                else:
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
            elif kind in {"content_blocked", "privacy_blocked", "visual", "clear_private_display", "game_continue_visual", "game_exit_confirmation", "story_speech_analysis", "hardware_reconnected", "button_edge_ack", "mood_report"}:
                effect.setdefault("resolution", "declarative_effect_emitted")
            elif kind == "typed_info_request":
                if not voice_open:
                    effect["resolution"] = "audio_deferred_handset_down"
                    continue
                tool_kind = str(effect.get("kind") or "")
                tool_name = ("date" if tool_kind in {"date", "menu"} else "weather" if tool_kind == "weather"
                             else 'card_doctors' if tool_kind == 'card_doctors' else "schedule")
                private_capability = 'personalize' if tool_name == 'card_doctors' else 'schedule' if tool_name == 'schedule' else None
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
                    elif private_capability and not self._identity_scope_live(event.session_id, private_capability):
                        unavailable_reason = "typed_information_blocked_by_identity"
                    if unavailable_reason is not None:
                        raise ScheduleWeatherProviderError(unavailable_reason)
                    profile = self._canonical_profiles.get(event.session_id)
                    if tool_name != "schedule" and not self._identity_scope_live(event.session_id, "personalize"):
                        profile = None
                    schedule_ref = self._canonical_schedule_refs.get(event.session_id) if self._identity_scope_live(event.session_id, "schedule") else None
                    request_scope = weather_scope
                    if (tool_kind == 'weather'
                            and previous_state is not None and previous_state.mode == 'schedule_weather'
                            and previous_state.scene_menu_descriptor.get('scene') == 'schedule_weather'
                            and any(item.get('id') == 'weather' and (
                                (event.event_type == 'ButtonPressed' and item.get('slot') == payload.get('button_id'))
                                or (event.event_type in {'SessionAudioEnabled', 'HandsetLifted'}
                                    and previous_state.pending_tool == 'weather'))
                                for item in previous_state.scene_menu_descriptor.get('items', ()) if isinstance(item, dict))):
                        request_scope = 'configured_point'
                    info = await TypedInfoRenderer(self.schedule_weather, profile, schedule_ref).render(
                        tool_kind, allow_demo=getattr(self.settings, "profile", "") == "mock",
                        weather_scope=request_scope)
                    def audio_current():
                        return bool(still_current() and state_owner.interaction_open
                            and (state_owner.mode == "schedule_weather" or (state_owner.mode == 'bear_dialogue'
                                and state_owner.dialogue_information_kind == tool_kind))
                            and state_owner.playback_lease == effect.get("playback_lease")
                            and (not private_capability or self._identity_scope_live(event.session_id,private_capability)))
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
                    fallback = await self._receive_canonical_event(self._internal_followup(
                        event, "DialogueFallback",
                        "typed_information", {"reason": "typed_information_unavailable", "kind": tool_kind},
                        "typed-unavailable",
                    ), dialogue_fallback_authorized=True)
                    if not still_current():
                        return stale_result()
                    deferred_effects.extend(fallback.get("outputs") or [])
                    snapshot = state_owner.snapshot()

        if not still_current():
            return stale_result()
        effects.extend(deferred_effects)
        if audio_failure is not None and (event.event_type != "AudioFailed" or decision.get("reason") == "game_prompt_retried"):
            failure = await self._receive_canonical_event(self._internal_followup(
                event, "AudioFailed",
                "canonical_audio_failure", audio_failure,
                "audio-failed",
            ))
            failure["audio_origin"] = dict(audio_failure)
            # Preserve the declarative effect that failed alongside the
            # canonical failure effects. Otherwise callers only see the
            # generic safe-stop projection and lose the phrase/asset/error
            # evidence that explains why playback was not started.
            failure_effects = list(failure.get("outputs") or [])
            failure["outputs"] = [dict(item) for item in effects] + failure_effects
            self.last_canonical_effects = [dict(item) for item in failure["outputs"]]
            if (audio_failure.get("response_id") == "DIALOGUE_INTRO"
                    and failure.get("reason") == "dialogue_prompt_finished"
                    and self._session_epochs.get(event.session_id, 0) == epoch
                    and self.canonical_runtime.sessions.get(event.session_id) is state_owner
                    and state_owner.state_version == failure.get("canonical_state", {}).get("state_version")
                    and state_owner.mode == "bear_dialogue" and state_owner.active_audio_id is None):
                # Internal dispatch failure is already terminal; it bypasses
                # receive_async's ordinary AudioFailed lighting callback.
                await self._set_dialogue_lighting_phase(event.session_id, "listening")
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
        if public_engagement is not None and self._engagement_due(event.session_id, public_engagement["nonce"]):
            return await self._receive_canonical_event(self._identity_event(
                event.session_id, "InteractionIdleExpired", {}), interaction_idle_authorized=True,
                _interaction_nonce=public_engagement["nonce"])
        return {"reason": decision["reason"], "state": final_state.get("mode"),
                "state_version": final_state.get("state_version"), "outputs": effects,
                "canonical_state": final_state,
                "canonical_trace": list(self.canonical_trace),
                "canonical_diagnostics": list(self.canonical_diagnostics),
                "reset_cleanup": reset_cleanup,
                "pipeline": ["ingress", "normalize_event", "session_guard", "safety_precheck", "route_mode", "policy_validate", "emit_effects", "persist_snapshot", "await_event"],
                "guards": decision.get("guards", {})}
        """Start an anonymous visual session from a pre-auth physical button."""
    @_runtime_ingress_guard
    async def receive_async(self, event: Event, transient_text: str | None = None, *,
                            _story_checkpoint_guard=None) -> dict[str, Any]:
        """Ingress for the single canonical runtime and LangGraph boundary."""
        from .domain import EVENT_TYPES
        if event.event_type not in EVENT_TYPES:
            return self._identity_result(event.session_id, "unsupported_event")
        if event.event_type == "InteractionIdleExpired":
            return self._identity_result(event.session_id, "interaction_idle_untrusted")
        if event.event_type in {"SessionStarted", "SessionAudioEnabled", "InterviewSkipped"}:
            return self._identity_result(event.session_id, "session_action_untrusted")
        if event.event_type in {"ClientProfileReady", "ClientProfileCleared"}:
            return self._identity_result(event.session_id, "max_json_requires_private_client_boundary")
        waiter = self._session_reset_waiters.get(event.session_id)
        if waiter is not None and waiter[0] is not asyncio.current_task():
            if event.event_type in {"AudioCompleted", "AudioFailed"}:
                return {"session_id": event.session_id, "state": "idle",
                        "reason": "stale_session_generation", "outputs": [],
                        "canonical_state": {"mode": "idle", "handset": "down"}}
            await asyncio.shield(waiter[1])
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
            silent_hold_candidate = (
                int(event.payload.get("button_id") or 0) == 6
                and (active is None or (
                    active.handset == "down" and not active.interaction_open
                    and (active.mode in {"idle", "profile_ready"}
                         or (active.mode == "session_closed" and active.substate == "soft_end"))
                ))
            )
            if not silent_hold_candidate and (active is None or (not active.interaction_open and not authorized_ready)):
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
        result = await self._receive_canonical_event(event, transient_text,
            _story_checkpoint_guard=_story_checkpoint_guard)
        if result.get("reason") == "stale_session_generation":
            return result
        if event.event_type in {"AudioCompleted", "AudioFailed"}:
            state_after_audio = self.canonical_runtime.sessions.get(event.session_id)
            if state_after_audio is not None and (state_after_audio.mode == "bear_dialogue"
                    or (state_after_audio.mode == "game_active" and state_after_audio.active_game_id == "word_game"
                        and state_after_audio.game_audio_stage == "playing")):
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
            handoff = await self._receive_canonical_event(self._internal_followup(
                event, "ExhibitionOnboardingComplete", "canonical_gr03_handoff",
                {"state_version": current.state_version if current is not None else 0},
                "exhibition-onboarding-complete",
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

    def _bounded_dialogue_context(self, session_id: str, game_id=None):
        """Reuse committed RAM within the same authorized physical/profile owner."""
        self.expire_dialogue_memory()
        state = self.canonical_runtime.sessions.get(session_id)
        if (self.optional_identity and state is not None and state.identity_request_id
                and not self._identity_scope_live(session_id, "personalize")):
            self._drop_dialogue_context(session_id)
        fresh = self._canonical_audio_context(session_id)
        if fresh.node_id == 'bear_dialogue':
            from .menu_content import catalogue_items
            import json
            recordings = tuple(catalogue_items('fixed_story_menu', state.age_band,
                                               stories=self.canonical_runtime.stories))
            fresh.verified_story_ids = tuple(story_id for story_id, _ in recordings)
            fresh.system_prompt += '\nAVAILABLE_FIXED_RECORDINGS_DATA=' + json.dumps(dict(recordings), ensure_ascii=False)
        owner = (self._session_epochs.get(session_id, 0),
                 state.profile_ref if state is not None else None)
        if game_id:
            owner += (game_id, getattr(getattr(state, "game_engine_state", None), "run_id", None))
        if game_id == "word_game":
            fresh._game_id = game_id
            from .word_game import WordState
            fresh._word_state = WordState()
            fresh._word_return_to_dialogue = state.game_return_mode == "bear_dialogue"
            fresh.allowed_topics = ("__all__",)
            fresh.system_prompt += self._word_game_instructions(fresh.profile.age_band)
        current = self.conversation_contexts.get(session_id)
        if game_id == "word_game" and current is not None and getattr(current, "_game_id", None) is None:
            self._suspended_dialogue_contexts[session_id] = current
        if game_id is None and (current is None or getattr(current, "_game_id", None) is not None):
            restored = self._suspended_dialogue_contexts.pop(session_id, None)
            if restored is not None:
                current = restored
                self.conversation_contexts[session_id] = current
        if current is not None and getattr(current, "_owner", None) == owner:
            self._resume_dialogue_context(session_id, current)
            current.node_id = fresh.node_id
            current.profile = fresh.profile
            current.system_prompt = fresh.system_prompt
            current.verified_story_ids = fresh.verified_story_ids
            current.allowed_topics = fresh.allowed_topics
            current.forbidden_topics = fresh.forbidden_topics
            if game_id == "word_game":
                # Thinking between game turns belongs to engagement idle, not
                # the ordinary absolute conversation generation budget.
                current.started_at = time.monotonic()
                remaining = current.max_turns - current.turns
                current.system_prompt += (f" В партии осталось ходов ребёнка: {remaining}."
                    + (" ПОСЛЕДНИЙ ХОД: это правило заменяет просьбу назвать ещё слово из правил игры. Заверши кратко, без нового слова или вопроса."
                       + (" Скажи, что мы вернулись к разговору." if state.game_return_mode == "bear_dialogue"
                          else " Скажи, что теперь можно выбрать другое занятие кнопкой.")
                       if remaining == 1 else ""))
            return current
        fresh._owner = owner
        if game_id == "word_game":
            fresh.system_prompt += f" Небольшая партия: {fresh.max_turns} ходов ребёнка, затем завершим игру."
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
            if state.grammatical_gender in {"masculine", "feminine", "neutral"}:
                private_profile.setdefault("grammatical_gender", state.grammatical_gender)
            if state.interests:
                private_profile.setdefault("interests", list(state.interests))
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
        allowed_topics = (("__all__",) if node_id == "bear_dialogue" or
                          (node_id == "support_intake" and state is not None and state.safety_severity == "none") else
                          ("greetings", "clinic", "weather", "games", "stories", "feelings", "questions"))
        from .age_policy import POLICIES
        turn_limit = POLICIES.get(profile_context.age_band, POLICIES["3-5"]).dialogue_turn_limit
        return ConversationContext(session_id, "canonical_product", node_id, allowed_topics, ("diagnosis", "medicine", "secret", "personal_data"), turn_limit, 120, 2, profile=profile_context, system_prompt=prompt)

    async def _complete_canonical_audio_turn(self, session_id: str, sequence: int, pending: asyncio.Task[Any], epoch: int, correlation_id: str, *, _ingress_guard=None, _game_audio_wait=None) -> None:
        try:
            try:
                audio_result = await pending
            except asyncio.CancelledError:
                return
            await self.expire_identity_leases()
            session = self.sessions.get(session_id)
            if session is None or session.state == Lifecycle.ENDED or self._session_epochs.get(session_id, 0) != epoch or session.correlation_id != correlation_id:
                return
            if audio_result.stt is None or not audio_result.stt.text.strip():
                if audio_result.barge_in:
                    await self._set_dialogue_lighting_phase(session_id, "listening")
                return
            payload = await self._receive_canonical_event(
                Event("SpeechReceived", session_id, sequence, self.now(), "uart_audio",
                      {"length": len(audio_result.stt.text), "_stt_untrusted": self._untrusted_stt(audio_result), "_name_confident": (not self._untrusted_stt(audio_result) and audio_result.stt.confidence >= 0.75)},
                      f"{correlation_id}:audio:{sequence}"),
                audio_result.stt.text,
                _speech_ingress_guard=_ingress_guard,
                _speech_lifecycle_guard=getattr(_ingress_guard, "lifecycle_current", _ingress_guard),
            )
            if payload.get("reason") != "stale_session_generation" and self._session_epochs.get(session_id, 0) == epoch:
                await self._invoke_audio_update_hook(session_id, payload, generation=epoch, correlation_id=correlation_id)
        finally:
            self._finish_game_audio_wait(_game_audio_wait)

    def _untrusted_stt(self, result) -> bool:
        threshold = getattr(getattr(self.s2s_audio, "config", None), "stt_confidence_threshold", 0.45)
        stt = result.stt
        return (stt is None or stt.unknown or type(stt.confidence) not in (int, float)
                or not math.isfinite(stt.confidence) or not threshold <= stt.confidence <= 1
                or result.ingress_status in {"stt_unknown", "stt_low_confidence", "stt_failure"})

    async def _receive_canonical_audio(self, session_id: str, samples: tuple[float, ...], sequence: int, *, wait_for_turn: bool, _ingress_guard=None) -> dict[str, Any]:
        await self.expire_engagement_sessions()
        if self._profile_replacing(session_id):
            return {'session_id':session_id, 'reason':'profile_replacing', 'outputs':[], 'pending':False}
        state = self.canonical_runtime.sessions.get(session_id)
        if state is not None and state.identity_request_id and not self._identity_scope_live(session_id):
            return {'session_id':session_id, 'reason':'stale_profile_context', 'outputs':[], 'pending':False}
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
        active = self.canonical_runtime.sessions.get(session_id)
        if active is None or not active.interaction_open:
            return {**self._result(session, [], "audio_not_listening"), "audio_status": "handset_required", "pending": False, "barge_in": False}
        epoch = self._session_epochs.get(session_id, 0)
        session.last_sequence, session.last_activity_at = sequence, self.now()
        context = self._canonical_audio_context(session_id)
        from .s2s import TurnResult, AudioIngressPipeline, LocalS2SPipeline
        # An utterance can span several uploads and a retry phase boundary.
        # Keep its first private-listener owner on the bounded VAD instance,
        # not on the final (possibly fresh-phase silence) transport frame.
        utterance_guard = _ingress_guard
        pipeline = None
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
                    if state is not None and state.mode in {"personal_welcome_listen", "game_active", 'game_menu', 'fixed_story_menu',
                                                           'exhibition_menu_repeat_listen'} else None)
        async def no_llm_turn(_text: str, _generation: int):
            return TurnResult((), (), False, None)
        with self._game_audio_wait(session_id, pipeline, utterance_guard) as audio_wait:
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
                    or not bool(getattr(self.canonical_runtime.sessions.get(session_id), "interaction_open", False))
                    or self._session_epochs.get(session_id, 0) != epoch
                    or (_ingress_guard is not None and not getattr(_ingress_guard, "lifecycle_current", _ingress_guard)())):
                return {"session_id": session_id, "state": "idle", "outputs": [],
                        "reason": "stale_session_generation", "audio_status": "ignored",
                        "pending": False, "barge_in": False}
            barge_outputs = [{"action": "barge_in", "status": "previous_output_cancelled"}] if audio_result.barge_in else []
            if audio_result.pending is not None:
                epoch = self._session_epochs.get(session_id, 0)
                completion = asyncio.create_task(self._complete_canonical_audio_turn(session_id, sequence, audio_result.pending, epoch, session.correlation_id, _ingress_guard=_ingress_guard, _game_audio_wait=audio_wait), name=f"canonical-audio-{session_id}")
                if audio_wait is not None:
                    audio_wait["completion"] = completion
                owned = self._audio_completions.setdefault(session_id, set()); owned.add(completion)
                self._audio_completion_started_at[session_id] = self.monotonic_now()
                completion.add_done_callback(self._audio_completion_callback(session_id))
                completion.add_done_callback(lambda _done, wait=audio_wait: self._finish_game_audio_wait(wait))
                self.sessions.save(session)
                return {**self._result(session, barge_outputs, "audio_processing"), "audio_status": "processing", "pending": True, "barge_in": audio_result.barge_in}
            if audio_result.stt is not None and audio_result.stt.text.strip():
                logger.info(
                    "speech_received chars=%d conf=%.2f mode=%s",
                    len(audio_result.stt.text.strip()),
                    float(getattr(audio_result.stt, "confidence", 0.0) or 0.0),
                    getattr(active, "mode", None),
                )
                result = await self._receive_canonical_event(Event("SpeechReceived", session_id, sequence, self.now(), "uart_audio", {"length": len(audio_result.stt.text), "_stt_untrusted": self._untrusted_stt(audio_result), "_name_confident": (not self._untrusted_stt(audio_result) and audio_result.stt.confidence >= 0.75)}, f"{session.correlation_id}:audio:{sequence}"), audio_result.stt.text, _speech_ingress_guard=_ingress_guard, _speech_lifecycle_guard=getattr(_ingress_guard, "lifecycle_current", _ingress_guard))
                result["audio_status"] = audio_result.ingress_status; result["pending"] = False; result["barge_in"] = audio_result.barge_in
                return result
            if _ingress_guard is not None and not _ingress_guard():
                return {**self._result(session, [], "stale_session_generation"), "pending": False}
            if audio_result.turn is not None and audio_result.turn.fallback_reason:
                if audio_result.barge_in:
                    await self._set_dialogue_lighting_phase(session_id, "listening")
                # This is an STT/VAD ingress retry, not a local safety reaction.
                # Keep it visual/diagnostic until a canonical retry phrase is
                # authorized for every mode; never dispatch untracked audio.
                return {**self._result(session, barge_outputs + [{"action": "audio_retry", "reason": audio_result.turn.fallback_reason}], "audio_retry"), "audio_status": audio_result.ingress_status, "pending": False, "barge_in": audio_result.barge_in}
            self.sessions.save(session)
            return {**self._result(session, barge_outputs, audio_result.ingress_status), "audio_status": audio_result.ingress_status, "pending": False, "barge_in": audio_result.barge_in}

    @_runtime_ingress_guard
    async def receive_audio_async(self, session_id: str, samples: tuple[float, ...], sequence: int, *, wait_for_turn: bool = True, _ingress_guard=None) -> dict[str, Any]:
        if _ingress_guard is None:
            record = self.sessions.get(session_id)
            epoch = self._session_epochs.get(session_id, 0)
            active = self.canonical_runtime.sessions.get(session_id)
            interaction_open = bool(active and active.interaction_open)
            _ingress_guard = lambda: (self.sessions.get(session_id) is record
                                     and bool(getattr(self.canonical_runtime.sessions.get(session_id), "interaction_open", False)) == interaction_open
                                     and self._session_epochs.get(session_id, 0) == epoch)
        # Ordinary phase authority is narrower than physical/session ownership:
        # a completed retry closes the old private-listener answer, but must not
        # erase a danger disclosure from the same still-live capture owner.
        state = self.canonical_runtime.sessions.get(session_id)
        version = state.state_version if state is not None else None
        private_phase = bool(state and state.mode == "personal_welcome_listen")
        menu_repeat_phase = bool(state and state.mode == 'exhibition_menu_repeat_listen')
        game_phase = bool(state and state.mode == "game_active")
        game_scope = self._game_voice_scope(session_id)
        game_owner = self._game_speech_check_owner(session_id)
        catalogue_scope = self._catalogue_voice_scope(session_id)
        conversation_scene = ((id(state), state.mode) if state is not None and state.mode in
                              {'bear_dialogue','support_intake','schedule_weather'} else None)
        transport_guard = _ingress_guard
        phase_guard = lambda: (transport_guard()
            and (conversation_scene is None or ((id(self.canonical_runtime.sessions.get(session_id)),
                getattr(self.canonical_runtime.sessions.get(session_id), 'mode', None)) == conversation_scene))
            and (catalogue_scope is None or self._catalogue_voice_scope(session_id) == catalogue_scope)
            and (not private_phase or (self.canonical_runtime.sessions.get(session_id) is state
                and state.state_version == version))
            # A finished menu answer cannot become a new dialogue turn or
            # answer a replacement question while its completion awaits expiry.
            and (not menu_repeat_phase or (self.canonical_runtime.sessions.get(session_id) is state
                and state.state_version == version))
            and (not game_phase or (game_scope is not None
                and self._game_speech_check_owner(session_id) == game_owner
                and self._game_voice_scope(session_id) == game_scope)))
        phase_guard.lifecycle_current = getattr(transport_guard, "lifecycle_current", transport_guard)
        if catalogue_scope is not None:
            phase_guard.catalogue_descriptor = copy.deepcopy(getattr(transport_guard, 'catalogue_descriptor', state.scene_menu_descriptor))
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
            boundary = None
        waiter = self._session_reset_waiters.get(session_id)
        if waiter is not None and waiter[0] is not asyncio.current_task():
            await asyncio.shield(waiter[1])
        completion = asyncio.get_running_loop().create_future()
        self._session_reset_waiters[session_id] = (asyncio.current_task(), completion)
        try:
            if boundary is not None:
                self._revoke_local_profile(session_id)
            return await self._reset_session_inner(session_id)
        finally:
            try:
                # Also discard source responses arriving while reset awaited
                # audio cleanup. Only a subsequent scan can own the fresh session.
                if boundary is not None and self._max_client_boundary is boundary:
                    boundary.clear_client()
            finally:
                self._session_reset_waiters.pop(session_id, None)
                if not completion.done():
                    completion.set_result(None)

    async def _reset_session_inner(self, session_id: str) -> dict[str, Any]:
        self._end_journal_visit(session_id)
        self._identity_replacements.pop(session_id, None)
        self._drop_public_story_transfer(session_id)
        self._drop_identity_owner(session_id)
        self._discard_private_welcome(session_id)
        self._drop_engagement(session_id, keys=True)
        self._cancel_game_timer(session_id)
        self._drop_game_audio_waits(session_id)
        self._cancel_welcome_timer(session_id)
        self._welcome_timer_sequences.pop(session_id, None)
        self._cancel_menu_tutorial(session_id)
        self._menu_tutorial_sequences.pop(session_id, None)
        self._cancel_lighting_wave(session_id)
        self._session_epochs[session_id] = self._session_epochs.get(session_id, 0) + 1
        self._session_revisions[session_id] = self._session_revisions.get(session_id, 0) + 1
        self._invalidate_audio_lease(session_id)
        sync_cleanup = {}
        def stop_resource(name, action):
            try:
                action()
                sync_cleanup[name] = {"status": "completed"}
            except Exception as exc:
                failure = {"kind": "session_reset_cleanup_failed", "resource": name,
                           "status": "failed", "exception_type": type(exc).__name__}
                sync_cleanup[name] = failure
                self.canonical_diagnostics.append(failure)
        record = self.sessions.get(session_id)
        if record is not None:
            record.cancellation_token += 1
            record.handset_up = False
            stop_resource("audio", lambda: self.audio.cancel(record.cancellation_token))
        close_capture = getattr(self.s2s_audio, "close_capture", None)
        if callable(close_capture):
            stop_resource("capture", lambda: close_capture(session_id))
        cancel_effects = getattr(self.led, "cancel_effects", None)
        stop_resource("lighting", lambda: cancel_effects(clear=True)
                      if callable(cancel_effects) else self.led.clear_all())
        if self.canonical_runtime is not None:
            self.canonical_runtime.reset(session_id)
        self.last_canonical_state = {
            "session_id": session_id, "mode": "idle", "handset": "down",
            "expected_inputs": ["handset_up"],
        }
        self.last_canonical_effects = []
        epoch, revision = self._session_epochs[session_id], self._session_revisions[session_id]
        def reset_current():
            return (self._session_epochs.get(session_id) == epoch
                and self._session_revisions.get(session_id) == revision
                and (self.canonical_runtime is None or session_id not in self.canonical_runtime.sessions))
        cleanup = await self._cancel_audio_completions(session_id, operation="reset")
        cleanup["sync_cleanup"] = sync_cleanup
        if self.bounded_s2s is not None:
            await self._cancel_audio_owner(self.bounded_s2s, session_id, operation="reset",
                                           ownership_guard=reset_current)
        if self.s2s_audio is not None:
            await self._cancel_audio_owner(self.s2s_audio, session_id, operation="reset",
                                           ownership_guard=reset_current)
        self._drop_dialogue_context(session_id)
        if self.canonical_runtime is not None:
            self._canonical_profiles.pop(session_id, None)
            self._canonical_schedule_refs.pop(session_id, None)
        # Reset is an application-level boundary, not only a repository
        # operation. Keep fake, PTY and physical LED implementations aligned
        # even when reset is invoked without the Stand wrapper.
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
                    commands = game_light_commands(effect.get("buttons", ()), effect.get("display", "digits"))
                if preset == "TARGET":
                    commands = tuple(
                        (element_id, 1)
                        for element_id in BUTTON_ELEMENTS.get(int(effect.get("target_button", 0)), ())
                    )
                for element_id, state in commands:
                    self.led.set_element(element_id, state)



    @staticmethod
    def _result(session, outputs, reason): return {"session_id":session.session_id, "state":session.state.value, "node":session.current_node, "reason":reason, "outputs":outputs, "correlation_id":session.correlation_id}
