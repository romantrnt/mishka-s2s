from __future__ import annotations

import json
import time
from typing import Any

from .domain import EVENT_TYPES, IDENTITY_INTERNAL_EVENT_TYPES, Event
from .identity import IdentityValidationError, validate_uid

EVENT_LIMIT_BYTES = 16_384
_BUTTON_EVENTS = frozenset({"ButtonPressed", "ButtonReleased", "ButtonHeld", "ButtonLongHeld"})
_PLAYBACK_FIELDS = frozenset({"response_id", "content_id", "token", "generation", "playback_lease", "playback_id"})
_EVENT_FIELDS = {
    "RfidPresented": frozenset({"tag_id", "reader_generation", "connection_epoch"}),
    "SpeechReceived": frozenset({"text"}),
    **{name: frozenset({"button_id", "duration_ms"}) for name in _BUTTON_EVENTS},
    **{name: _PLAYBACK_FIELDS for name in ("AudioStarted", "AudioCompleted")},
    "AudioFailed": _PLAYBACK_FIELDS | {"error_code", "code"},
    "StoryPositionUpdated": _PLAYBACK_FIELDS | {"story_id", "offset_ms"},
    "VoiceIntentReady": frozenset({"intent", "safety_severity"}),
    "ExhibitionOnboardingComplete": frozenset({"phase", "step"}),
    "ExhibitionMenuWaveComplete": frozenset({"phase"}),
    "StoryChoiceWaveComplete": frozenset({"phase"}),
    "MenuTutorialStep": frozenset({"button"}),
    "AdultHandoffAcknowledged": frozenset({"incident_id"}),
}
_INTEGER_FIELDS = frozenset({"button_id", "button", "duration_ms", "offset_ms", "step", "token", "generation", "reader_generation", "connection_epoch", "state_version", "length"})
_PUBLIC_FIELDS = {
    **_EVENT_FIELDS,
    "RfidPresented": frozenset({"profile_resolved", "unknown_tag"}),
    "SpeechReceived": frozenset({"length"}),
    **{name: frozenset({"status"}) for name in IDENTITY_INTERNAL_EVENT_TYPES},
}
_ALIASES = {"anonymous":"AnonymousStart", "anonymous_start":"AnonymousStart", "rfid":"RfidPresented", "handset_up":"HandsetLifted", "handset_down":"HandsetPlaced", "SpacePressed":"HandsetPlaced", "SpaceReleased":"HandsetLifted", "space_pressed":"HandsetPlaced", "space_released":"HandsetLifted", "button":"ButtonPressed", "button_pressed":"ButtonPressed", "button_released":"ButtonReleased", "button_hold":"ButtonHeld", "button_long_hold":"ButtonLongHeld", "text":"SpeechReceived", "hardware_disconnected":"HardwareDisconnected"}


def _safe_field(name: str, value: Any) -> bool:
    if name in _INTEGER_FIELDS:
        return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**63 - 1
    if name in {"profile_resolved", "unknown_tag"}:
        return isinstance(value, bool)
    if name == "status":
        return isinstance(value, str) and value in {"authorized", "unknown", "denied", "unavailable"}
    return (isinstance(value, str) and 0 < len(value) <= 128
            and not any(ord(char) < 32 or 127 <= ord(char) < 160 or 0xD800 <= ord(char) <= 0xDFFF for char in value))


def _project_payload(event_type: str, payload: dict[str, Any], *, public: bool) -> dict[str, Any]:
    fields = (_PUBLIC_FIELDS if public else _EVENT_FIELDS).get(event_type, frozenset())
    clean = {key: payload[key] for key in fields if key in payload and _safe_field(key, payload[key])}
    if "state_version" in payload and _safe_field("state_version", payload["state_version"]):
        clean["state_version"] = payload["state_version"]
    if not public:
        # Trace labels are enums, never arbitrary caller strings.
        for key in ("_input_type", "_normalized_event"):
            if isinstance(payload.get(key), str) and payload[key] in set(_ALIASES) | EVENT_TYPES:
                clean[key] = payload[key]
    return clean


class EventValidationError(ValueError):
    """A user-supplied event failed the protocol validation contract."""

    code = "validation_error"



def parse_event(payload: dict[str, Any], session_id: str, sequence: int, source: str = "api", now: float | None = None) -> Event:
    try:
        valid_size = isinstance(payload, dict) and len(json.dumps(payload, ensure_ascii=True).encode()) <= EVENT_LIMIT_BYTES
    except (TypeError, ValueError, OverflowError):
        valid_size = False
    if not valid_size:
        raise EventValidationError("event payload is invalid or exceeds 16 KiB")
    event_name=str(payload.get("type"))
    if event_name=="handset": event_name="handset_up" if payload.get("state")=="up" else "handset_down"
    name = _ALIASES.get(event_name, event_name)
    if name not in EVENT_TYPES:
        raise EventValidationError("unknown event type")
    if name in {"AdultResolved", "DialogueFallback"} | IDENTITY_INTERNAL_EVENT_TYPES:
        # Generic API/UART frames have no authority to assert internal
        # resolution or provider outcomes, regardless of payload/source.
        raise EventValidationError("internal event is not accepted from generic ingress")
    if "state_version" in payload and not _safe_field("state_version", payload["state_version"]):
        raise EventValidationError("state_version must be a nonnegative integer")
    clean = _project_payload(name, payload, public=False)
    if name == "RfidPresented":
        try:
            clean["tag_id"] = validate_uid(payload.get("tag_id"))
        except IdentityValidationError:
            raise EventValidationError("RFID tag_id must be nonempty and at most 128 characters") from None
    if name in _BUTTON_EVENTS and clean.get("button_id") not in {1,2,3,4,5,6,7}:
        raise EventValidationError("button_id must be 1..7")
    if name in {"ButtonHeld", "ButtonLongHeld"} and (not isinstance(clean.get("duration_ms"), int) or not 0 <= clean["duration_ms"] <= 120_000):
        raise EventValidationError("duration_ms must be 0..120000")
    if name == "SpeechReceived":
        text = payload.get("text")
        if not isinstance(text, str) or len(text) > 512:
            raise EventValidationError("speech text must be at most 512 characters")
        # The application receives text transiently; it must never publish/persist it.
        clean = {"length": len(text)}
    return Event(name, session_id, sequence, now if now is not None else time.time(), source, clean, str(payload.get("correlation_id", "")))


def redacted_event(event: Event) -> dict[str, Any]:
    # Reapply the public projection even for directly constructed/internal
    # Events. No raw UID, profile, speech, schedule or arbitrary nested payload
    # reaches the common publisher merely because its event name is harmless.
    payload = event.payload if isinstance(event.payload, dict) else {}
    return {"type": event.event_type, "session_id": event.session_id, "sequence": event.sequence, "timestamp": event.timestamp, "source": event.source, "correlation_id": event.correlation_id, "payload": _project_payload(event.event_type, payload, public=True)}
