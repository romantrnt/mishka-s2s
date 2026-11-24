"""Bounded technical observations only: no conversation journal or disk writes."""
from collections import deque
from copy import deepcopy
import secrets
import threading
import time

from .canonical_contract import STATE_NAMES
from .domain import EVENT_TYPES


COMPONENTS = frozenset({
    "hid_start", "hid_reader", "physical_lighting_loss", "story_playback",
    "device_command", "panel_session", "uart_ingress", "uart_egress",
    "raw_uart", "phrase_audio", "story_audio", "shutdown",
    "headless_audio", "headless_audio_capture", "headless_audio_processing",
})
STATUSES = frozenset({
    "ready", "degraded", "available", "unavailable", "simulated", "active",
    "stopped", "not_started", "device_unavailable", "device_disconnected",
    "input_desynchronized", "reader_exception", "dispatch_failure", "unknown",
})
RUNTIME_STATES = frozenset({"BOOTING", "WARMING_UP", "READY", "DEGRADED", "STOPPING"})


def enum(value, allowed, default="unknown"):
    return value if isinstance(value, str) and value in allowed else default


class OperatorMonitor:
    def __init__(self, capacity=240):
        self._lock = threading.Lock()
        self._items = deque(maxlen=capacity)
        self._cursor = 0
        self._stream = secrets.token_hex(8)  # Process identity, never a visitor identifier.
        self._mode = "idle"
        self._alert = None
        self._health = None
        self._append("started", {})

    def _append(self, kind, data, level="info"):
        self._cursor += 1
        item = {"cursor": self._cursor, "at": time.time(), "kind": kind, "level": level, "data": data}
        self._items.append(item)
        return item

    def event(self, event):
        if not isinstance(event, dict):
            return
        kind = enum(event.get("event_type") or event.get("type"), EVENT_TYPES, None)
        if kind is None or kind in {"SilentHoldTick", "MenuTutorialStep", "StoryPositionUpdated"}:
            return
        data = {"event": kind}
        payload = event.get("payload")
        if kind in {"ButtonPressed", "ButtonReleased", "ButtonHeld", "ButtonLongHeld"} and isinstance(payload, dict):
            button = payload.get("button_id")
            if type(button) is int and 1 <= button <= 7:
                data["button"] = button
        with self._lock:
            self._append("event", data)

    def observe(self, state, event):
        self.state(state)
        self.event({"type": event.event_type, "payload": event.payload})

    def state(self, state):
        if not isinstance(state, dict):
            state = {}
        mode = enum(state.get("mode"), STATE_NAMES | {"idle"})
        kind = enum(state.get("support_alert_kind"), {"help_requested", "urgent_safety"}, None)
        kind = kind if mode == "safety_stop" else None
        with self._lock:
            if mode != self._mode:
                self._append("state", {"from": self._mode or "unknown", "to": mode})
                self._mode = mode
            if self._alert and self._alert["kind"] != kind:
                self._append("alert_ended", {"kind": self._alert["kind"], "adult_acknowledged": False})
                self._alert = None
            if kind and self._alert is None:
                item = self._append("alert_opened", {"kind": kind}, "error")
                self._alert = {"kind": kind, "opened_at": item["at"], "cursor": item["cursor"],
                               "adult_acknowledged": False, "delivery_confirmed": False}

    def error(self, item):
        # Never retain exception messages, paths, IDs or arbitrary diagnostic strings.
        stage = item.get("stage") if isinstance(item, dict) else None
        with self._lock:
            self._append("error", {"component": enum(stage, COMPONENTS)}, "error")

    def reset(self):
        self.state({"mode": "idle"})
        with self._lock:
            self._append("reset", {})

    def snapshot(self, *, after=0, stream="", health=None, runtime=None, lighting=None):
        health = health if isinstance(health, dict) else {}
        safe_health = {"status": enum(health.get("status"), STATUSES),
                       "runtime": enum(runtime, RUNTIME_STATES),
                       "handset": enum(health.get("handset"), {"up", "down"})}
        for key in ("audio", "hid", "lighting"):
            source = health.get(key)
            safe_health[key] = enum(source.get("status") if isinstance(source, dict) else None, STATUSES)
        targets = lighting if isinstance(lighting, dict) else {}
        safe_health["lit_channels"] = sum(type(targets.get(str(i))) is int and targets[str(i)] == 1 for i in range(18))
        with self._lock:
            if safe_health != self._health:
                self._append("health", dict(safe_health))
                self._health = safe_health
            restart = bool(stream and stream != self._stream) or after > self._cursor
            oldest = self._items[0]["cursor"]
            gap = restart or (after > 0 and after < oldest - 1)
            since = 0 if gap else after
            return {"stream": self._stream, "cursor": self._cursor, "gap": gap, "sampled_at": time.time(),
                    "oldest_cursor": oldest, "capacity": self._items.maxlen,
                    "items": deepcopy([item for item in self._items if item["cursor"] > since]),
                    "alert": deepcopy(self._alert), "health": dict(safe_health), "mode": self._mode,
                    "storage": "bounded_process_memory", "transcripts_enabled": False}


class OperatorErrorBuffer(deque):
    """Keep existing error semantics and observe only a fixed component code."""
    def __init__(self, monitor, *, maxlen):
        super().__init__(maxlen=maxlen)
        self._monitor = monitor

    def append(self, item):
        super().append(item)
        self._monitor.error(item)
