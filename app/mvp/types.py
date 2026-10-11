from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SessionState(str, Enum):
    IDLE = "IDLE"
    WAITING_RFID = "WAITING_RFID"
    AUTHENTICATED = "AUTHENTICATED"
    WAITING_HANDSET = "WAITING_HANDSET"
    DIALOGUE = "DIALOGUE"
    GAME = "GAME"
    STORY = "STORY"
    FALLBACK = "FALLBACK"
    ENDING = "ENDING"
    RESET = "RESET"


ALLOWED_INTENTS = frozenset({
    "greeting", "mood_positive", "mood_neutral", "mood_negative",
    "want_game", "want_story", "want_exit", "repeat",
    "pain_or_discomfort", "question", "unknown",
})


@dataclass(frozen=True)
class IntentResult:
    intent: str
    confidence: float
    source: str = "mock"
    reason: str | None = None

    def valid(self, threshold: float) -> bool:
        return self.intent in ALLOWED_INTENTS and 0.0 <= self.confidence <= 1.0 and self.confidence >= threshold


@dataclass(frozen=True)
class HardwareEvent:
    type: str
    tag_id: str | None = None
    state: str | None = None
    button_id: int | None = None
    duration_ms: int | None = None
    text: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "HardwareEvent":
        event_type = str(payload.get("type", ""))
        if event_type not in {"rfid", "handset", "button", "button_hold", "text", "timeout"}:
            raise ValueError(f"unsupported event type: {event_type}")
        button = payload.get("button_id")
        return cls(
            type=event_type,
            tag_id=payload.get("tag_id"),
            state=payload.get("state"),
            button_id=int(button) if button is not None else None,
            duration_ms=int(payload["duration_ms"]) if payload.get("duration_ms") is not None else None,
            text=payload.get("text"),
        )


@dataclass
class TransitionRecord:
    at: float
    from_node: str | None
    to_node: str | None
    reason: str


@dataclass
class GameProgress:
    game_id: str
    expected: list[int]
    index: int = 0
    attempts: int = 0
    max_attempts: int = 3
    started_at: float = 0.0


@dataclass
class Session:
    session_id: str
    state: SessionState = SessionState.WAITING_RFID
    child_id: str | None = None
    anonymous: bool = False
    handset_up: bool = False
    scenario_id: str = "main_demo"
    current_node: str | None = None
    return_node: str | None = None
    errors: int = 0
    max_errors: int = 3
    created_at: float = 0.0
    last_activity_at: float = 0.0
    node_entered_at: float = 0.0
    response_token: int = 0
    game: GameProgress | None = None
    transitions: list[TransitionRecord] = field(default_factory=list)
    outputs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.state not in {SessionState.IDLE, SessionState.ENDING, SessionState.RESET}
