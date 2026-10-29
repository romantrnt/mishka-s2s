from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol

SCHEMA_VERSION = "1.0"
ALLOWED_INTENTS = frozenset({"greeting", "mood_positive", "mood_neutral", "mood_negative", "want_game", "want_story", "want_exit", "repeat", "pain_or_discomfort", "question", "unknown"})


class Lifecycle(str, Enum):
    """Physical session lifecycle; scenario modes live in CanonicalState."""
    IDLE = "IDLE"
    WAITING_RFID = "WAITING_RFID"
    ACTIVE = "ACTIVE"
    ENDED = "ENDED"


@dataclass(frozen=True)
class PlaybackHandle:
    session_id: str
    generation: int
    playback_id: str
    playback_lease: str
    response_id: str
    token: int


@dataclass(frozen=True)
class Event:
    event_type: str
    session_id: str
    sequence: int
    timestamp: float
    source: str
    payload: dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""
    monotonic_timestamp: float | None = None


IDENTITY_INTERNAL_EVENT_TYPES = frozenset({"IdentityLookupSucceeded", "IdentityLookupFailed", "IdentityReadyExpired", "IdentityAuthorizationExpired"})
EVENT_TYPES = frozenset({"RfidPresented", "RfidRemoved", "AnonymousStart", "SessionStarted", "SessionReset", "SessionCancel", "HandsetLifted", "HandsetPlaced", "ButtonPressed", "ButtonReleased", "ButtonHeld", "ButtonLongHeld", "SpeechReceived", "VoiceIntentReady", "AudioStarted", "AudioCompleted", "AudioFailed", "StoryPositionUpdated", "DialogueFallback", "TimeoutFired", "WelcomeTimerFired", "AdultHandoffAcknowledged", "AdultResolved", "HardwareDisconnected", "HardwareReconnected", "ExhibitionOnboardingComplete", "ExhibitionMenuWaveComplete", "StoryChoiceWaveComplete", "MenuTutorialStep"}) | IDENTITY_INTERNAL_EVENT_TYPES


@dataclass
class SessionRecord:
    session_id: str
    installation_id: str
    correlation_id: str
    state: Lifecycle = Lifecycle.WAITING_RFID
    child_id: str | None = None
    anonymous: bool = False
    handset_up: bool = False
    # Visual guest mode may be entered by a physical button before RFID/handset.
    # It deliberately suppresses audio while preserving LED/display and scenario routing.
    visual_only: bool = False
    rfid_tag: str | None = None
    scenario_id: str | None = None
    scenario_version: str | None = None
    current_node: str | None = None
    started_at: float = 0.0
    last_activity_at: float = 0.0
    last_sequence: int = 0
    cancellation_token: int = 0
    termination_reason: str | None = None
    error_state: str | None = None
    retries: int = 0
    game_id: str | None = None
    game_expected: list[int] = field(default_factory=list)
    game_index: int = 0
    game_attempts: int = 0
    game_max_attempts: int = 0
    readiness_wave_completed: bool = False


@dataclass(frozen=True)
class Intent:
    value: str
    confidence: float
    request_id: str
    source: str
    latency_ms: float = 0.0

    def is_valid(self, threshold: float) -> bool:
        return self.value in ALLOWED_INTENTS and threshold <= self.confidence <= 1.0


@dataclass(frozen=True)
class ValidationIssue:
    scenario_id: str
    node_id: str | None
    field: str
    problem: str
    suggested_fix: str
    # Structured content diagnostics are bounded by the repository.
    category: str = "schema"
    resource: str | None = None
    index: int | None = None
    expected: str | None = None
    severity: str = "error"


class Clock(Protocol):
    def now(self) -> float: ...

class HardwareAdapter(Protocol):
    def send(self, message: dict[str, Any]) -> None: ...
    def health(self) -> dict[str, Any]: ...

class RFIDReader(Protocol): pass
class HandsetReader(Protocol): pass
class ButtonReader(Protocol): pass

class LEDController(Protocol):
    # Canonical lighting port: callers address explicit logical elements.
    def set_element(self, element_id: int, state: int, transition_ms: int = 0) -> None: ...
    def set_elements(self, commands: Any) -> None: ...
    def clear_all(self, transition_ms: int = 0) -> None: ...
    def readiness_wave(self, total_duration_ms: int = 1200) -> None: ...
    async def readiness_wave_async(self, total_duration_ms: int = 1200, *, cancelled: Any = None) -> None: ...

    # Legacy compatibility projection; it must not be used as canonical
    # lighting state and must not be sent to the lighting hardware path.
    def pattern(self, pattern_id: str, color: str, duration_ms: int, token: int) -> None: ...
    def pattern_map(self, pattern_id: str, colors: dict[str, str], duration_ms: int, token: int) -> None: ...

class AudioOutput(Protocol):
    def play(self, response_id: str, asset_id: str | None, token: int) -> None: ...
    def play_owned(self, handle: PlaybackHandle, asset_id: str, offset_ms: int = 0) -> None: ...
    def cancel(self, token: int) -> None: ...

class DisplayController(Protocol):
    def show(self, asset_id: str, text: str | None, token: int) -> None: ...

class SpeechRecognizer(Protocol): pass
class SpeechSynthesizer(Protocol): pass

class ProfileProvider(Protocol):
    def resolve(self, tag_id: str) -> dict[str, Any] | None: ...

class ScenarioRepository(Protocol):
    def get(self, scenario_id: str) -> dict[str, Any]: ...
    def game(self, game_id: str) -> dict[str, Any] | None: ...
    def story(self, story_id: str) -> dict[str, Any] | None: ...

class SessionRepository(Protocol):
    def get(self, session_id: str) -> SessionRecord | None: ...
    def save(self, session: SessionRecord) -> None: ...
    def active_for_installation(self, installation_id: str) -> SessionRecord | None: ...
    def delete(self, session_id: str) -> None: ...

class HistoryRepository(Protocol):
    def append(self, entity: str, child_id: str, item_id: str, payload: dict[str, Any]) -> None: ...
    def has(self, entity: str, child_id: str, item_id: str) -> bool: ...

class IntentClassifier(Protocol):
    def classify(self, text: str, request_id: str, cancelled: Callable[[], bool] | None = None) -> Intent: ...

class WeatherProvider(Protocol):
    def current(self) -> dict[str, Any]: ...

class EventPublisher(Protocol):
    def publish(self, event: Event) -> None: ...
