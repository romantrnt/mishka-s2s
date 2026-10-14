from __future__ import annotations

import time
from typing import Any

from .domain import EVENT_TYPES, Event

EVENT_LIMIT_BYTES = 16_384


class EventValidationError(ValueError):
    """A user-supplied event failed the protocol validation contract."""

    code = "validation_error"



def parse_event(payload: dict[str, Any], session_id: str, sequence: int, source: str = "api", now: float | None = None) -> Event:
    if not isinstance(payload, dict) or len(str(payload).encode()) > EVENT_LIMIT_BYTES:
        raise EventValidationError("event payload is invalid or exceeds 16 KiB")
    event_name=str(payload.get("type"))
    if event_name=="handset": event_name="handset_up" if payload.get("state")=="up" else "handset_down"
    aliases = {"anonymous":"AnonymousStart", "anonymous_start":"AnonymousStart", "rfid":"RfidPresented", "handset_up":"HandsetLifted", "handset_down":"HandsetPlaced", "SpacePressed":"HandsetLifted", "SpaceReleased":"HandsetPlaced", "space_pressed":"HandsetLifted", "space_released":"HandsetPlaced", "button":"ButtonPressed", "button_pressed":"ButtonPressed", "button_released":"ButtonReleased", "button_hold":"ButtonHeld", "button_long_hold":"ButtonLongHeld", "text":"SpeechReceived", "hardware_disconnected":"HardwareDisconnected"}
    name = aliases.get(event_name, event_name)
    if name not in EVENT_TYPES:
        raise EventValidationError("unknown event type")
    clean = {k: v for k, v in payload.items() if k != "audio"}
    if name == "RfidPresented" and (not isinstance(clean.get("tag_id"), str) or len(clean["tag_id"]) > 128):
        raise EventValidationError("RFID tag_id must be a string of at most 128 characters")
    if name in {"ButtonPressed", "ButtonReleased", "ButtonHeld", "ButtonLongHeld"} and clean.get("button_id") not in {1,2,3,4,5,6,7}:
        raise EventValidationError("button_id must be 1..7")
    if name in {"ButtonHeld", "ButtonLongHeld"} and (not isinstance(clean.get("duration_ms"), int) or not 0 <= clean["duration_ms"] <= 120_000):
        raise EventValidationError("duration_ms must be 0..120000")
    if name == "SpeechReceived":
        text = clean.get("text")
        if not isinstance(text, str) or len(text) > 512:
            raise EventValidationError("speech text must be at most 512 characters")
        # The application receives text transiently; it must never publish/persist it.
        clean = {"length": len(text)}
    return Event(name, session_id, sequence, now if now is not None else time.time(), source, clean, str(payload.get("correlation_id", "")))


def redacted_event(event: Event) -> dict[str, Any]:
    return {"type": event.event_type, "session_id": event.session_id, "sequence": event.sequence, "timestamp": event.timestamp, "source": event.source, "correlation_id": event.correlation_id, "payload": event.payload}
