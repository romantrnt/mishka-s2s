"""Authenticated virtual peripherals around the existing Stand ingress.

This module owns transport faults and observer cursors, never scenario state.
Synthetic card scans are delegated to Max's unchanged capture runner.
"""
from __future__ import annotations

import asyncio
from collections import deque
import secrets
import time
from typing import Any

from .led_contract import BUTTON_LED_COLORS


SCAN_CODES = {"demo-1": "demo-1", "demo-2": "demo-2", "unknown": "unknown", "archive": "demo-3"}
PRESS_TYPES = {"press": "button", "release": "button_released", "hold": "button_hold", "long_hold": "button_long_hold"}
_CLEANUP_TYPES = {"handset_down", "HandsetPlaced", "SpacePressed", "SessionCancel", "SessionReset", "HardwareDisconnected"}


def is_cleanup(message_type: str, payload: dict[str, Any]) -> bool:
    return message_type in _CLEANUP_TYPES or (message_type == "handset" and payload.get("state") == "down")


def _error(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


class RemoteDeviceSimulator:
    JOURNAL_LIMIT = 300
    MAX_PENDING_INPUTS = 32
    MAX_DELAY_MS = 2000
    SCAN_TIMEOUT_S = 15.0

    def __init__(self, stand, *, scan_callback=None, admits_users=None):
        if stand.hardware_mode not in {"fake", "simulator"}:
            raise ValueError("virtual devices require simulator hardware")
        self.stand = stand
        self.scan_callback = scan_callback
        self.admits_users = admits_users or (lambda: False)
        self.epoch = 0
        self.connected = True
        self.delay_ms = 0
        self.pending_inputs = 0
        self._scan_lock = asyncio.Lock()
        self._scan_task = None
        self._active_scan_epoch = None
        self._scan_quarantined = False
        self._journal: deque[dict[str, Any]] = deque(maxlen=self.JOURNAL_LIMIT)
        self._cursor = 0

    def invalidate(self, reason: str) -> None:
        self.epoch += 1
        self.record("boundary", reason, {})

    def scan_admits_profile(self) -> bool:
        # Max can fence work already inside its frame handler. A synthetic
        # capture queue also needs fencing before that handler is entered.
        return not self._scan_quarantined and (self._active_scan_epoch is None or self._active_scan_epoch == self.epoch)

    def record(self, kind: str, name: str, payload: dict[str, Any]) -> None:
        self._cursor += 1
        # Callers supply application-owned enums and explicitly projected data.
        self._journal.append({"cursor": self._cursor, "at": time.time(), "epoch": self.epoch,
                              "kind": kind, "type": name, "payload": payload})

    def record_event(self, event: dict[str, Any]) -> None:
        payload = event.get("payload") or {}
        allowed = {"button_id", "duration_ms", "response_id", "content_id", "token", "generation",
                   "playback_lease", "playback_id", "state_version", "status"}
        clean = {key: value for key, value in payload.items() if key in allowed}
        clean.update({key: event[key] for key in ("sequence", "source") if key in event})
        self.record("event", event["type"], clean)

    def record_command(self, command: dict[str, Any]) -> None:
        payload = command.get("payload") or {}
        allowed = {"response_id", "asset_id", "token", "generation", "playback_id", "playback_lease",
                   "offset_ms", "element_id", "state", "transition_ms"}
        clean = {key: value for key, value in payload.items() if key in allowed}
        clean["sequence"] = command["sequence"]
        self.record("command", command["type"], clean)

    async def before_input(self, message_type: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        cleanup = is_cleanup(message_type, payload)
        state = self.stand.application.canonical_runtime.sessions.get(self.stand.active_session_id)
        duplicate_down = (state is not None and state.handset == "down"
            and (message_type in {"handset_down", "HandsetPlaced", "SpacePressed"}
                 or (message_type == "handset" and payload.get("state") == "down")))
        if cleanup:
            if not duplicate_down:
                self.invalidate("cleanup_input")
            return None
        if message_type in {"HardwareReconnected", "heartbeat", "capabilities"}:
            return None
        if not self.connected:
            return _error("simulated_input_disconnected", "simulated input device is disconnected")
        if self.pending_inputs >= self.MAX_PENDING_INPUTS:
            return _error("simulator_backpressure", "simulator input queue is full")
        epoch = self.epoch
        self.pending_inputs += 1
        try:
            if self.delay_ms:
                await asyncio.sleep(self.delay_ms / 1000)
            if self.stand._closed or epoch != self.epoch:
                return _error("stale_simulator_epoch", "input belongs to an expired simulator epoch")
            if not self.connected:
                return _error("simulated_input_disconnected", "simulated input device is disconnected")
            return None
        finally:
            self.pending_inputs -= 1

    def projection(self, source: dict[str, Any]) -> dict[str, Any]:
        app = self.stand.application
        sid = self.stand.active_session_id
        state = app.canonical_runtime.sessions.get(sid)
        session = source.get("session") or {}
        enabled = set(session.get("enabled_buttons") or []) if self.connected and self.admits_users() else set()
        pressed = set(getattr(state, "accepted_buttons_down", ()))
        lease = dict(app._active_audio.get(sid) or {})
        active = {key: lease[key] for key in ("content_id", "token", "generation", "playback_id", "playback_lease") if key in lease}
        phrase = app.voice_manifest.resolve(str(lease.get("content_id") or "")) if str(lease.get("content_id") or "").startswith("PHRASE_") else None
        return {
            "contract": "mishka.remote-devices/v1", "simulated": True, "production_ready": False,
            "epoch": self.epoch, "journal_cursor": self._cursor,
            "profile": self.stand.settings.profile, "identity_mode": self.stand.settings.identity_mode,
            "synthetic_profile": bool(app.max_json), "identity": dict(source.get("identity") or {}),
            "session": {"session_id": sid, "state": session.get("state"), "scenario": session.get("scenario"),
                        "context_generation": app._profile_context_generations.get(sid, 0),
                        "node": session.get("node"), "state_version": getattr(state, "state_version", 0),
                        "profile_status": getattr(state, "profile_status", "anonymous"),
                        "interaction_route": getattr(state, "interaction_route", None) if state and state.interaction_open else None,
                        "interaction_active": bool(state and state.interaction_open),
                        "session_actions": self.session_actions(state)},
            "admission": {"input_ready": bool(self.admits_users()), "connected": self.connected},
            "devices": {
                "handset": {"state": "up" if session.get("handset_up") else "down", "space_level": "released" if session.get("handset_up") else "pressed"},
                "buttons": [{"id": key, "color": color, "enabled": key in enabled, "pressed": key in pressed,
                             "press_kinds": list(PRESS_TYPES)} for key, color in BUTTON_LED_COLORS.items()],
                "rfid": {"source": "max_synthetic_capture", "available": self.scan_callback is not None and app.max_json and not self._scan_quarantined,
                         "synthetic": True, "cards": list(SCAN_CODES), "arbitrary_uid_supported": False},
                "lighting": dict(source.get("physical_lighting") or {}),
                "display": next((item for item in reversed(source.get("commands") or []) if item.get("type") == "display"), None),
                "audio": {"status": getattr(state, "audio_status", "idle"), "active_playback": active,
                          "browser_generation": self.stand.playback._generation},
            },
            "microphone": {"eligible": bool(self.connected and self.admits_users() and self.stand._interaction_audio_accepting()),
                           "explicit_enable_required": True},
            "prompt": {"phrase_id": lease.get("content_id"), "text": phrase.get("transcript") if phrase else None},
            "fault": {"input_connected": self.connected, "delay_ms": self.delay_ms, "pending_inputs": self.pending_inputs,
                      "max_pending_inputs": self.MAX_PENDING_INPUTS},
        }

    def session_actions(self, state):
        active = bool(state and state.interaction_open)
        identified = bool(state and state.identity_request_id and state.profile_ref
                          and self.stand.application._identity_scope_live(state.session_id, "personalize"))
        actions = ["end"] if active or identified else []
        ready = (self.connected and self.admits_users() and not self.stand._closed
                 and self.stand.application._runtime_maintenance is None
                 and getattr(self.stand, "_capture_hid_fault", None) is None
                 and not self.stand._physical_output_faulted()
                 and (state is None or (state.safety_severity == "none" and state.mode != "safety_stop"
                     and state.substate != "profile_cleanup_failed")))
        if ready and not active:
            actions.append("start")
        if ready and active and state.interaction_route == "panel" and state.silent_mode and not state.interaction_idle:
            actions.append("enable_audio")
        if (ready and active and state.profile_status != "identified" and state.mode in {
                "exhibition_introduction", "exhibition_name_question", "exhibition_name_capture",
                "exhibition_age_prompt", "exhibition_age_hint", "exhibition_age_selection"}):
            actions.append("skip_interview")
        return actions

    async def _session_action(self, payload):
        action = payload["action"]
        fields = {"action", "instance_id", "epoch", "session_id", "context_generation"}
        if action in {"skip_interview", "enable_audio"}:
            fields.add("state_version")
        if (set(payload) != fields or type(payload.get("epoch")) is not int or payload["epoch"] < 0
                or type(payload.get("context_generation")) is not int or payload["context_generation"] < 0
                or not isinstance(payload.get("session_id"), str)
                or not isinstance(payload.get("instance_id"), str)
                or (action in {"skip_interview", "enable_audio"} and (type(payload["state_version"]) is not int
                    or not 0 <= payload["state_version"] <= 2**63 - 1))):
            return _error("validation_error", "session action requires its exact current owner scope")
        def current():
            return (not self.stand._closed and self.epoch == payload["epoch"]
                    and self.stand.application._profile_context_generations.get(payload["session_id"], 0) == payload["context_generation"]
                    and self.stand.active_session_id == payload["session_id"]
                    and self.stand._panel_instance_id == payload["instance_id"])
        if not current():
            return _error("stale_simulator_epoch", "session action belongs to an expired owner")
        if action != "end":
            if not self.admits_users():
                return _error("runtime_unavailable", "stand runtime is not ready")
            blocked = await self.before_input("panel_session", {})
            if blocked is not None:
                return blocked
            if not current():
                return _error("stale_simulator_epoch", "session action belongs to an expired owner")
            if not self.admits_users():
                return _error("runtime_unavailable", "stand runtime is not ready")
            state = self.stand.application.canonical_runtime.sessions.get(payload["session_id"])
            if (getattr(self.stand, "_capture_hid_fault", None) is not None or self.stand._physical_output_faulted()):
                return _error("runtime_unavailable", "session input is faulted")
        else:
            # Revoke an in-flight Start before cleanup can yield. Stale End
            # never reaches this point and cannot invalidate a newer visit.
            self.invalidate("panel_session_end")
        epoch = self.epoch
        guard = lambda: (self.epoch == epoch and not self.stand._closed
                         and self.stand.application._profile_context_generations.get(payload["session_id"], 0) == payload["context_generation"]
                         and self.stand.active_session_id == payload["session_id"]
                         and self.stand._panel_instance_id == payload["instance_id"])
        return await self.stand.session_action(action, ownership_guard=guard,
                                               state_version=payload.get("state_version"))

    async def _run_scan(self, code: str, epoch: int) -> dict[str, Any]:
        async with self._scan_lock:
            # The child may start after a queued cleanup coroutine. Retain the
            # submission's owner; never adopt a newer epoch before capture.
            if epoch != self.epoch:
                return _error("stale_simulator_epoch", "scan belongs to an expired simulator epoch")
            blocked = await self.before_input("rfid", {})
            if blocked:
                return blocked
            if epoch != self.epoch:
                return _error("stale_simulator_epoch", "scan belongs to an expired simulator epoch")
            self._active_scan_epoch = epoch
            boundary = self.stand.application._max_client_boundary
            generation = boundary.generation
            prior_state = self.stand.application.canonical_runtime.sessions.get(self.stand.active_session_id)
            prior_request_id = prior_state.identity_request_id if prior_state is not None else None
            deadline = time.monotonic() + self.SCAN_TIMEOUT_S
            try:
                scan = await asyncio.wait_for(self.scan_callback(code, self.SCAN_TIMEOUT_S), self.SCAN_TIMEOUT_S)
                while time.monotonic() < deadline:
                    if epoch != self.epoch:
                        return _error("stale_simulator_epoch", "scan belongs to an expired simulator epoch")
                    changed = boundary.generation - generation
                    if not boundary._stepping and ((changed >= 1 and (self.stand.application._canonical_profiles or getattr(scan, "duplicate", False))) or changed >= 2):
                        reason = "duplicate_suppressed_by_max" if getattr(scan, "duplicate", False) else "synthetic_scan_processed"
                        session_state = self.stand.application.canonical_runtime.sessions.get(self.stand.active_session_id)
                        new_request_id = session_state.identity_request_id if session_state is not None else None
                        if getattr(scan, "duplicate", False):
                            auth_status = "duplicate_suppressed"
                        elif (session_state is not None and session_state.profile_status == "identified"
                              and new_request_id and new_request_id != prior_request_id):
                            auth_status = "identified"
                        elif (session_state is not None and session_state.mode not in {"idle", "profile_ready"}
                              and not (session_state.mode == "session_closed" and session_state.substate == "soft_end")):
                            auth_status = "ignored_in_current_state"
                        else:
                            auth_status = "no_personalization"
                        self.record("scan", reason, {"synthetic": True, "auth_status": auth_status})
                        return {"reason": reason, "auth_status": auth_status}
                    await asyncio.sleep(0.01)
                self._scan_quarantined = True
                return _error("synthetic_scan_timeout", "synthetic capture did not settle")
            except asyncio.CancelledError:
                self._scan_quarantined = True
                raise
            except Exception:
                # A timeout/cancelled thread wait does not prove its queued
                # frame cannot arrive later. Keep identity closed until restart.
                self._scan_quarantined = True
                return _error("synthetic_scan_failed", "synthetic capture failed")
            finally:
                if epoch != self.epoch or self._scan_quarantined:
                    try:
                        boundary.clear_client()
                    except Exception:
                        self._scan_quarantined = True
                        self.record("fault", "synthetic_clear_failed", {})
                self._active_scan_epoch = None

    async def aclose(self) -> None:
        task = self._scan_task
        if task is not None and not task.done():
            # The unchanged Max client has its own finite HTTPS deadline.
            # Do not expose a stale queued frame by cancelling only its waiter.
            await asyncio.gather(asyncio.shield(task), return_exceptions=True)

    def journal(self, after: int = 0) -> dict[str, Any]:
        oldest = self._journal[0]["cursor"] if self._journal else self._cursor + 1
        return {"items": [dict(item) for item in self._journal if item["cursor"] > after],
                "cursor": self._cursor, "oldest_cursor": oldest, "limit": self.JOURNAL_LIMIT,
                "gap": after < oldest - 1, "epoch": self.epoch}

    async def action(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = payload.get("action")
        if isinstance(action, str) and action in {"start", "end", "skip_interview", "enable_audio"}:
            return await self._session_action(payload)
        cleanup = action == "reset" or (action == "handset" and payload.get("state") == "down")
        if payload.get("epoch") is not None:
            if type(payload["epoch"]) is not int or payload["epoch"] < 0:
                return _error("validation_error", "epoch must be a nonnegative integer")
            if not cleanup and payload["epoch"] != self.epoch:
                return _error("stale_simulator_epoch", "input belongs to an expired simulator epoch")
        if not cleanup and not self.admits_users():
            return _error("runtime_unavailable", "stand runtime is not ready")
        if action == "handset":
            if (set(payload) - {"action", "state", "epoch"} or not isinstance(payload.get("state"), str)
                    or payload["state"] not in {"up", "down"}):
                return _error("validation_error", "handset requires state up or down")
            return await self.stand.inject("handset", {"state": payload["state"]}, wait_for_audio=False)
        if action == "button":
            if (set(payload) - {"action", "button_id", "press", "duration_ms", "epoch",'cancelled'}
                    or type(payload.get("button_id")) is not int or payload["button_id"] not in range(1, 8)
                    or not isinstance(payload.get("press", "press"), str) or payload.get("press", "press") not in PRESS_TYPES):
                return _error("validation_error", "button requires id 1..7 and a supported press kind")
            press = payload.get("press", "press")
            outgoing = {"button_id": payload["button_id"]}
            if 'cancelled' in payload:
                if press != 'release' or type(payload['cancelled']) is not bool:
                    return _error('validation_error','cancelled must be a boolean on button release')
                outgoing['cancelled'] = payload['cancelled']
            if press in {"hold", "long_hold"}:
                duration = payload.get("duration_ms")
                if type(duration) is not int or not 0 <= duration <= 120000:
                    return _error("validation_error", "hold duration_ms must be 0..120000")
                outgoing["duration_ms"] = duration
            elif "duration_ms" in payload:
                return _error("validation_error", "duration_ms applies only to hold events")
            return await self.stand.inject(PRESS_TYPES[press], outgoing, wait_for_audio=False)
        if action == "reset":
            if set(payload) - {"action", "epoch"}:
                return _error("validation_error", "reset accepts no session or profile override")
            return await self.stand.reset()
        if action == "rfid":
            if (set(payload) - {"action", "card", "epoch"} or not isinstance(payload.get("card"), str)
                    or payload["card"] not in SCAN_CODES):
                return _error("validation_error", "only named synthetic cards are accepted")
            if self.scan_callback is None or not self.stand.application.max_json or self._scan_quarantined:
                return _error("scan_unavailable", "synthetic Max capture is not configured")
            if self._scan_task is not None and not self._scan_task.done():
                return _error("simulator_backpressure", "synthetic scan is already in progress")
            submitted_epoch = self.epoch
            self._scan_task = asyncio.create_task(self._run_scan(SCAN_CODES[payload["card"]], submitted_epoch), name="mishka-synthetic-scan")
            return await asyncio.shield(self._scan_task)
        return _error("validation_error", "unknown simulator action")

    async def fault(self, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) - {"device", "mode", "delay_ms"} or payload.get("device") != "input":
            return _error("validation_error", "only the virtual input adapter accepts fault injection")
        mode = payload.get("mode")
        if mode == "delay":
            if type(payload.get("delay_ms")) is not int or not 0 <= payload["delay_ms"] <= self.MAX_DELAY_MS:
                return _error("validation_error", "delay_ms must be 0..2000")
            self.delay_ms = payload["delay_ms"]
            self.record("fault", "input_delay", {"delay_ms": self.delay_ms})
            return {"reason": "simulator_delay_configured"}
        if "delay_ms" in payload:
            return _error("validation_error", "delay_ms applies only to delay mode")
        if mode == "disconnect":
            if not self.connected:
                return {"reason": "simulator_already_disconnected"}
            self.connected = False
            return await self.stand.inject("HardwareDisconnected", {}, wait_for_audio=False)
        if mode == "reconnect":
            if self.connected:
                return {"reason": "simulator_already_connected"}
            # Reconnect is metadata; canonical SAFE_STOP still needs hangup.
            self.connected = True
            self.invalidate("input_reconnected")
            return await self.stand.inject("HardwareReconnected", {}, wait_for_audio=False)
        return _error("validation_error", "unsupported simulator fault")


def install_remote_devices(api, stand, *, scan_callback=None, admits_users=None):
    """Register under Stand authentication; physical hardware has no such API."""
    if stand.hardware_mode not in {"fake", "simulator"}:
        if scan_callback is not None:
            raise ValueError("synthetic capture controls require simulator hardware")
        return None
    from fastapi import Request
    from fastapi.responses import JSONResponse
    globals()["Request"] = Request
    simulator = RemoteDeviceSimulator(stand, scan_callback=scan_callback, admits_users=admits_users)
    stand.remote_devices = simulator
    api.state.remote_devices = simulator
    boundary = stand.application._max_client_boundary
    if boundary is not None:
        existing_admission = boundary.admits_users
        boundary.admits_users = lambda: (simulator.scan_admits_profile()
                                         and (existing_admission is None or existing_admission()))

    def authenticate(request):
        token = stand.settings.stand_access_token
        if not token or stand.settings.mutation_auth_policy != "token_required":
            return JSONResponse(_error("mutation_auth_unconfigured", "stand mutation authentication is unavailable"), status_code=503)
        supplied = request.headers.get("x-stand-token") or request.cookies.get("stand_token") or ""
        if not secrets.compare_digest(supplied, token):
            return JSONResponse(_error("auth_failure", "stand authentication required"), status_code=401)
        return None

    def response(result):
        if isinstance(result.get("error"), dict):
            code = result["error"].get("code")
            status = 422 if code == "validation_error" else 503 if code in {"runtime_unavailable", "synthetic_scan_failed", "synthetic_scan_timeout"} else 500 if code == "internal_error" else 409
            return JSONResponse(result, status_code=status)
        return {"result": stand._public_result(result), "epoch": simulator.epoch, "state": stand.panel_snapshot()}

    @api.get("/stand/simulator/state")
    async def state(request: Request):
        denied = authenticate(request)
        return denied if denied is not None else stand.panel_snapshot()

    @api.get("/stand/simulator/journal")
    async def journal(request: Request, after: int = 0):
        denied = authenticate(request)
        if denied is not None:
            return denied
        if after < 0:
            return JSONResponse(_error("validation_error", "journal cursor must be nonnegative"), status_code=422)
        return simulator.journal(after)

    @api.post("/stand/simulator/action")
    async def action(request: Request, payload: dict):
        denied = authenticate(request)
        return denied if denied is not None else response(await simulator.action(payload))

    @api.post("/stand/simulator/fault")
    async def fault(request: Request, payload: dict):
        denied = authenticate(request)
        return denied if denied is not None else response(await simulator.fault(payload))
    return simulator
