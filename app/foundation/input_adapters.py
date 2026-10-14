"""Headless physical input adapters for Linux keyboard/HID devices."""

from __future__ import annotations

import errno
import fcntl
import os
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class NormalizedInputEvent:
    type: str
    payload: dict
    source: str = "hid"
    timestamp: float = 0.0


class KeyboardHIDInputAdapter:
    """Read Linux ``evdev`` keyboard events without a browser dependency.

    The parser intentionally uses the stable Linux input_event ABI and has no
    dependency on python-evdev, which keeps the adapter usable in a minimal
    appliance image. ``feed_key`` is also available for deterministic tests.
    """

    EVENT_STRUCT = struct.Struct("llHHI")
    EV_KEY = 0x01
    KEY_CODES = {2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6, 8: 7}
    KEY_SPACE = 57
    KEY_MAX = 0x2FF
    SUPPORTED_KEY_CODES = frozenset(KEY_CODES) | {KEY_SPACE}

    def __init__(self, device_path: str, callback: Callable[[NormalizedInputEvent], None], *,
                 debounce_ms: int = 35, poll_s: float = 0.05,
                 on_error: Callable[[dict], None] | None = None) -> None:
        if not device_path:
            raise ValueError("HID device path is required")
        self.device_path = str(device_path)
        self.callback = callback
        self.on_error = on_error
        self.debounce_s = max(0, int(debounce_ms)) / 1000.0
        self.poll_s = max(0.005, float(poll_s))
        self._fd: int | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._pressed: set[int] = set()
        self._known_levels: set[int] = set()
        self._last_edge: dict[int, float] = {}
        self._ready = False
        self._closed = False
        self._events = 0
        self._errors = 0
        self._last_error: dict | None = None
        self._status = "stopped"
        self._initial_level_sync: dict = {"status": "not_attempted", "first_event_policy": "requires_level_sync"}
        self._unknown_level_blocked = False
        self._unverified_events = 0

    @classmethod
    def _ev_iocgkey(cls) -> tuple[int, int]:
        size = (cls.KEY_MAX + 1 + 7) // 8
        request = (2 << 30) | (size << 14) | (ord("E") << 8) | 0x18
        return request, size

    def _sync_initial_levels(self) -> None:
        fd = self._fd
        if fd is None:
            self._unknown_level_blocked = True
            self._initial_level_sync = {"status": "unavailable", "error_code": "initial_level_unavailable", "first_event_policy": "reject_unverified_first_event"}
            return
        request, size = self._ev_iocgkey()
        bitmap = bytearray(size)
        try:
            fcntl.ioctl(fd, request, bitmap, True)
        except (OSError, ValueError) as exc:
            self._unknown_level_blocked = True
            self._initial_level_sync = {"status": "unavailable", "error_code": "initial_level_unavailable", "exception_type": type(exc).__name__, "first_event_policy": "reject_unverified_first_event"}
            self._known_levels.clear()
            return
        self._unknown_level_blocked = False
        self._pressed.clear()
        self._known_levels = set(self.SUPPORTED_KEY_CODES)
        for code in self.SUPPORTED_KEY_CODES:
            if bitmap[code // 8] & (1 << (code % 8)):
                self._pressed.add(self.KEY_CODES.get(code, code))
        self._initial_level_sync = {"status": "synced", "space_pressed": self.KEY_SPACE in self._pressed, "pressed_buttons": sorted(self.KEY_CODES[code] for code in self.KEY_CODES if code in self._pressed), "first_event_policy": "accept_edges_against_synced_level"}

    def open(self) -> None:
        with self._lock:
            if self._ready:
                return
            try:
                self._fd = os.open(self.device_path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError as exc:
                self._report_error("device_unavailable", exc)
                raise
            self._stop.clear()
            self._closed = False
            self._pressed.clear()
            self._known_levels.clear()
            self._last_edge.clear()
            self._unknown_level_blocked = False
            self._sync_initial_levels()
            self._ready = True
            self._status = "active"
            self._thread = threading.Thread(target=self._run, name="mishka-hid-reader", daemon=True)
            self._thread.start()

    def close(self) -> None:
        with self._lock:
            self._ready = False
            self._closed = True
            self._status = "stopped"
            self._stop.set()
            fd, thread = self._fd, self._thread
            self._fd = None
            self._thread = None
        if thread is not None:
            thread.join(timeout=max(0.2, self.poll_s * 4))
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    shutdown = close

    def is_ready(self) -> bool:
        return self._ready and not self._closed

    def _report_error(self, kind: str, exc: BaseException | None = None) -> None:
        self._errors += 1
        self._status = kind
        self._last_error = {"kind": kind, "exception_type": type(exc).__name__} if exc else {"kind": kind}
        callback = self.on_error
        if callback is not None:
            try:
                callback(dict(self._last_error))
            except Exception:
                pass

    def _emit(self, event_type: str, payload: dict) -> None:
        self._events += 1
        try:
            self.callback(NormalizedInputEvent(event_type, payload, "hid", time.time()))
        except Exception as exc:
            self._report_error("dispatch_failure", exc)

    def feed_key(self, key_code: int, value: int, *, timestamp: float | None = None) -> None:
        """Inject a Linux key edge (1=down, 0=up, 2=autorepeat)."""
        now = time.monotonic() if timestamp is None else float(timestamp)
        # EVIOCGKEY failure leaves the current level unknowable. The first
        # event may be a buffered/current event rather than a new physical edge;
        # suppress it (and remain blocked) instead of silently inventing a lift.
        if self._unknown_level_blocked and int(key_code) in self.SUPPORTED_KEY_CODES and value in (0, 1):
            self._unverified_events += 1
            self._last_error = {"kind": "initial_level_unavailable", "error_code": "first_event_unverified", "policy": "reject_unverified_first_event"}
            return
        button_id = self.KEY_CODES.get(int(key_code))
        if button_id is not None:
            if value == 1:
                if button_id in self._pressed:
                    return
                if now - self._last_edge.get(button_id, -1e9) < self.debounce_s:
                    return
                self._pressed.add(button_id)
                self._known_levels.add(int(key_code))
                self._last_edge[button_id] = now
                self._emit("ButtonPressed", {"button_id": button_id})
            elif value == 0:
                if button_id not in self._pressed and int(key_code) in self._known_levels:
                    return
                self._pressed.discard(button_id)
                self._known_levels.add(int(key_code))
                self._last_edge[button_id] = now
                self._emit("ButtonReleased", {"button_id": button_id})
            return
        if int(key_code) == self.KEY_SPACE:
            # Space is a physical level with explicit edges: pressed means
            # handset lifted (off-hook) and released means handset placed (on-hook). Autorepeat (value=2) is
            # deliberately ignored and cannot create another lifecycle edge.
            if value == 1:
                if self.KEY_SPACE in self._pressed:
                    return
                if now - self._last_edge.get(self.KEY_SPACE, -1e9) < self.debounce_s:
                    return
                self._pressed.add(self.KEY_SPACE)
                self._known_levels.add(self.KEY_SPACE)
                self._last_edge[self.KEY_SPACE] = now
                self._emit("SpacePressed", {})
            elif value == 0:
                if self.KEY_SPACE not in self._pressed and self.KEY_SPACE in self._known_levels:
                    return
                self._pressed.discard(self.KEY_SPACE)
                self._known_levels.add(self.KEY_SPACE)
                self._last_edge[self.KEY_SPACE] = now
                self._emit("SpaceReleased", {})

    def _run(self) -> None:
        event_size = self.EVENT_STRUCT.size
        buffer = b""
        while not self._stop.is_set():
            fd = self._fd
            if fd is None:
                break
            try:
                chunk = os.read(fd, event_size * 32)
                if not chunk:
                    self._report_error("device_disconnected")
                    self._stop.set()
                    self._ready = False
                    break
                buffer += chunk
                while len(buffer) >= event_size:
                    raw, buffer = buffer[:event_size], buffer[event_size:]
                    _sec, _usec, event_type, code, value = self.EVENT_STRUCT.unpack(raw)
                    if event_type == self.EV_KEY and value in (0, 1, 2):
                        self.feed_key(code, value)
            except BlockingIOError:
                time.sleep(self.poll_s)
            except OSError as exc:
                if exc.errno in (errno.EBADF, errno.EIO) and self._stop.is_set():
                    break
                self._report_error("reader_exception", exc)
                self._stop.set()
                self._ready = False
                break
            except Exception as exc:
                self._report_error("reader_exception", exc)
                self._stop.set()
                self._ready = False
                break

    def diagnostics(self) -> dict:
        return {
            "ready": self.is_ready(),
            "backend": "hid",
                        "pressed_buttons": sorted(self._pressed),
            "events": self._events,
            "errors": self._errors,
            "last_error": self._last_error,
            "status": self._status,
            "reader_alive": bool(self._thread and self._thread.is_alive()),
            "initial_level_sync": dict(self._initial_level_sync),
            "unverified_events": self._unverified_events,
            "semantic_input_accepted": bool(self.is_ready() and not self._unknown_level_blocked),
            "physical_ready": bool(self.is_ready() and not self._unknown_level_blocked and self._initial_level_sync.get("status") == "synced"),
        }
