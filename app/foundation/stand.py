from __future__ import annotations

import base64
import binascii
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import math
import os
import numpy as np
import secrets
import struct
import time
import wave
from collections import deque
from io import BytesIO
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode

logger = logging.getLogger("mishka.stand")

from .api import build_application, create_app
from .config import Settings, settings_with_compat_env
from .events import EventValidationError, parse_event
from .domain import EVENT_TYPES, Event, Lifecycle, PlaybackHandle
from .hardware import (HardwareFrame, PTYUARTSimulator, PosixSerialTransport,
                       SerialHardwareAdapter, decode_frame, encode_frame)
from .canonical_contract import FixedStoryManifest, canonical_state_nodes
from .s2s import AudioChunk, SoundDeviceCaptureController, SoundDevicePlaybackController
from .input_adapters import KeyboardHIDInputAdapter, NormalizedInputEvent
from .led_contract import BUTTON_LED_COLORS, button_contract, canonical_led_state, canonical_led_state_from_lighting, fixed_button_colors
from .lighting import (
    LightingFaultLatchedError,
    BUTTON_OUTPUT_ELEMENTS,
    FakeLightingOutput,
    PhysicalLightingOutput,
    SerialLightingAdapter,
    LightingCommand,
    LightingController,
    ExhibitionLightingEngine,
    READINESS_WAVE_DURATION_MS,
    READINESS_WAVE_GROUPS,
    LIGHTING_ELEMENT_IDS,
)


AUDIO_SAMPLE_RATES = {16000, 22050, 24000, 48000}
MAX_AUDIO_SECONDS = 15
MAX_AUDIO_WAV_BYTES = 6 * 1024 * 1024
HEADLESS_AUDIO_NODES = frozenset({
    "personal_welcome_listen", "fixed_story_menu", "game_menu",
    "exhibition_menu_repeat_listen",
    "exhibition_name_capture", "exhibition_age_selection", "bear_dialogue", "support_intake",
    "personal_story_theme", "wellbeing_check", "schedule_weather",
    "fixed_story_playing", "story_paused", "story_speech_analysis",
})
_AUTH_QUERY_KEYS = frozenset({"token", "access_token", "stand_token"})

# Diagnostic codes are application-owned, never model prose or exception text.
_DIALOGUE_FALLBACK_CODES = frozenset({
    "provider_or_validation_failure", "provider_unavailable", "dialogue_exit",
    "bounded_limit", "turn_timeout", "voice_generation_timeout", "tts_queue_overflow",
    "oversized_input", "safety_signal", "blocked_input", "unknown_topic",
    "stt_failure", "stt_low_confidence", "vad_failure", "audio_too_long",
    "personal_story_unavailable",
})
_VALIDATION_SOURCES = frozenset({
    "response_validation", "stream_parser", "regex", "regex_trusted",
    "ollama_mini_llm", "mock_mini_llm", "validator_timeout", "validator_error",
})
_VALIDATION_REASONS = frozenset({
    "empty_text", "medical_advice", "self_harm_or_violence", "sexual_content", "personal_data",
    "secrecy_or_isolation", "unverified_external_contact",
    "regex_allow", "trusted_application_text", "mini_llm_allow", "mini_llm_block",
    "mini_llm_review", "mini_llm_suspicious", "mini_llm_uncertain_length",
    "mini_llm_timeout", "validation_exception",
})
_VALIDATION_ERRORS = frozenset({
    "TimeoutError", "ValueError", "TypeError", "JSONDecodeError", "KeyError",
    "OSError", "URLError", "HTTPError", "ConnectionError", "RuntimeError",
})
_PARSER_REASON_CODES = {
    "unknown or invalid model action": "invalid_model_action",
    "unknown or unclosed tag": "invalid_model_tag", "unknown model tag": "invalid_model_tag",
    "unclosed <say> tag": "unclosed_say_tag", "LLM stream exceeded limit": "model_stream_limit",
    "total model speech exceeded limit": "model_speech_limit",
    "unsafe or oversized model output": "unsafe_model_output",
}
_PIPELINE_EVENTS = frozenset({
    "audio_cancelled", "audio_dropped", "audio_ready", "barge_in", "chunk_closed",
    "first_audio_gate_entered", "first_audio_gate_released", "input_completed",
    "playback_cancelled", "playback_finished", "playback_started", "rvc_v2_fallback",
    "speech_end", "token_received", "tts_started", "turn_started", "vad_failure",
    "validation_block", "validation_error", "validation_failure", "voice_conversion_bypassed",
})


def _diagnostic_number(value):
    return type(value) in (int, float) and 0 <= value <= 2**53 - 1 and math.isfinite(value)


def _validation_reason_code(value):
    if not isinstance(value, str):
        return "validation_detail_redacted"
    if value in _VALIDATION_REASONS or value in _VALIDATION_ERRORS:
        return value
    if value in _PARSER_REASON_CODES:
        return _PARSER_REASON_CODES[value]
    if value.startswith("mini_llm_error:") and value.partition(":")[2] in _VALIDATION_ERRORS:
        return value
    return "validation_detail_redacted"


def _hid_diagnostics(adapter: Any) -> dict[str, Any]:
    diagnostics = getattr(adapter, "diagnostics", None)
    if callable(diagnostics):
        return dict(diagnostics())
    ready = bool(getattr(adapter, "is_ready", lambda: False)())
    return {
        "ready": ready,
        "semantic_input_accepted": ready,
        "physical_ready": False,
        "initial_level_sync": {"status": "unavailable", "error_code": "adapter_diagnostics_unavailable"},
    }


class StandAuthQueryRedactionMiddleware:
    """Remove auth values from the ASGI scope before access logging.

    Canonical auth is header/cookie based. A query token is accepted only for
    the explicitly enabled development console fallback; all other endpoints
    are sanitized defensively before downstream handling and access logging.
    """

    def __init__(self, app, allow_query_token: bool = False):
        self.app = app
        self.allow_query_token = bool(allow_query_token)

    async def __call__(self, scope, receive, send):
        query_token = ""
        console_query_fallback = False
        if scope.get("type") == "http":
            raw_query = scope.get("query_string", b"")
            if raw_query:
                pairs = parse_qsl(raw_query.decode("latin-1"), keep_blank_values=True)
                query_values = {key.lower(): value for key, value in pairs}
                path = str(scope.get("path", ""))
                console_query_fallback = (
                    self.allow_query_token
                    and scope.get("method") == "GET"
                    and path == "/console"
                    and bool(query_values.get("token") or query_values.get("access_token") or query_values.get("stand_token"))
                )
                query_token = (
                    query_values.get("token")
                    or query_values.get("access_token")
                    or query_values.get("stand_token")
                    or ""
                )
                sanitized = [
                    (key, "<redacted>" if key.lower() in _AUTH_QUERY_KEYS else value)
                    for key, value in pairs
                ]
                scope = dict(scope)
                if console_query_fallback:
                    headers = list(scope.get("headers", []))
                    if not any(key.lower() == b"x-stand-token" for key, _ in headers):
                        headers.append((b"x-stand-token", query_token.encode("latin-1")))
                    # Persist the credential that the downstream auth check
                    # actually uses; an explicit header outranks the query.
                    query_token = next(value.decode("latin-1") for key, value in headers
                                       if key.lower() == b"x-stand-token")
                    scope["headers"] = headers
                    scope["_stand_query_token"] = query_token
                if any(key.lower() in _AUTH_QUERY_KEYS for key, _ in pairs):
                    scope["query_string"] = urlencode(sanitized).encode("latin-1")
        async def send_with_cookie(message):
            if (console_query_fallback and message.get("type") == "http.response.start"
                    and message.get("status") == 200):
                headers = list(message.get("headers", []))
                for path in ("/console", "/stand"):
                    cookie = SimpleCookie()
                    cookie["stand_token"] = query_token
                    cookie["stand_token"]["path"] = path
                    cookie["stand_token"]["httponly"] = True
                    cookie["stand_token"]["samesite"] = "Strict"
                    if scope.get("scheme") == "https":
                        cookie["stand_token"]["secure"] = True
                    headers.append((b"set-cookie", cookie.output(header="").strip().encode("latin-1")))
                message = dict(message)
                message["headers"] = headers
            await send(message)
        await self.app(scope, receive, send_with_cookie)


class AudioInputError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


async def read_bounded_audio_body(request, limit: int) -> bytes:
    """Bound application buffering even for chunked requests without a length."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError:
            raise AudioInputError("invalid_format", "invalid audio content length") from None
        if length < 0:
            raise AudioInputError("invalid_format", "invalid audio content length")
        if length > limit:
            raise AudioInputError("oversized_audio", "audio request body exceeds limit")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise AudioInputError("oversized_audio", "audio request body exceeds limit")
        body.extend(chunk)
    return bytes(body)


class StandHub:
    """Broadcast hub with one write lease for the active browser session."""
    MAX_CLIENTS = 16
    SEND_TIMEOUT_S = 0.75

    def __init__(self):
        self.clients: set[Any] = set()
        self._owner: Any | None = None
        self._lease_generation = 0
        self._send_locks: dict[Any, asyncio.Lock] = {}
        self._publish_lock = asyncio.Lock()
        self._accepted = 0
        self._disconnected = 0
        self._send_failures = 0
        self._slow_clients = 0

    async def connect(self, websocket) -> bool:
        if len(self.clients) >= self.MAX_CLIENTS:
            try:
                await websocket.close(code=1013, reason="stand connection limit reached")
            except Exception:
                pass
            return False
        self.clients.add(websocket)
        self._send_locks[websocket] = asyncio.Lock()
        self._accepted += 1
        if self._owner is None:
            self._owner = websocket
            self._lease_generation += 1
        return self.is_owner(websocket)

    def disconnect(self, websocket):
        if websocket in self.clients:
            self.clients.discard(websocket)
            self._send_locks.pop(websocket, None)
            self._disconnected += 1
        if self._owner is websocket:
            self._owner = None
            self._lease_generation += 1

    def is_owner(self, websocket) -> bool:
        return self._owner is websocket

    def lease_snapshot(self) -> dict[str, Any]:
        return {
            "owner_connected": self._owner is not None,
            "client_count": len(self.clients),
            "max_clients": self.MAX_CLIENTS,
            "generation": self._lease_generation,
            "accepted": self._accepted,
            "disconnected": self._disconnected,
            "send_failures": self._send_failures,
            "slow_clients": self._slow_clients,
        }

    async def publish(self, payload: dict[str, Any]) -> list[dict[str, str]]:
        failures: list[dict[str, str]] = []
        async with self._publish_lock:
            clients = tuple(self.clients)

            async def send_one(client):
                lock = self._send_locks.get(client)
                if lock is None:
                    return client, {"exception_class": "UnknownClient", "reason": "client is not registered"}
                try:
                    async with lock:
                        await asyncio.wait_for(client.send_json(payload), timeout=self.SEND_TIMEOUT_S)
                except asyncio.TimeoutError:
                    self._slow_clients += 1
                    return client, {"exception_class": "TimeoutError", "reason": "client send timed out"}
                except Exception as exc:
                    return client, {"exception_class": type(exc).__name__, "reason": str(exc)[:160]}
                return client, None

            results = await asyncio.gather(*(send_one(client) for client in clients))
            for client, failure in results:
                if failure is None:
                    continue
                self._send_failures += 1
                failures.append(failure)
                self.disconnect(client)
        return failures


def _software_runtime_usable(stand: Any, lifecycle: Any) -> bool:
    """Allow fake/simulator diagnostics through resource-only degradation."""
    if str(getattr(stand, "hardware_mode", "physical")).lower() == "physical":
        return False
    snapshot = lifecycle.snapshot() if lifecycle is not None else {}
    return (
        str(snapshot.get("state", "")).upper() == "DEGRADED"
        and str(snapshot.get("reason", "")) == "resource_capacity_insufficient"
    )


def _admission_reason(snapshot: dict[str, Any]) -> str:
    state = str(snapshot.get("state", "DEGRADED")).upper()
    if state == "BOOTING":
        return "booting"
    if state == "WARMING_UP":
        return "warming_up"
    if state == "DEGRADED" and snapshot.get("reason") == "resource_capacity_insufficient":
        return "resource_capacity_insufficient"
    return "degraded"


async def _reject_websocket_admission(websocket, *, code: int, error_code: str, reason: str, state: str) -> None:
    """Complete the WebSocket handshake before sending a protocol rejection."""
    try:
        await websocket.accept()
        await websocket.send_json({
            "type": "admission_error",
            "error": {
                "code": error_code,
                "reason": reason,
                "state": state,
                "user_available": False,
                "retry_after_ms": 5000 if reason in {"booting", "warming_up"} else 30000,
            },
        })
        await websocket.close(code=code, reason=reason)
    except Exception:
        # A client may disconnect between handshake and rejection. There is
        # still no session or audio task to clean up in this path.
        return


class BrowserPlaybackController:
    """Streams real provider PCM to the browser console; no child audio is stored."""
    def __init__(self, hub: StandHub): self.hub=hub; self.active_token=0; self._generation=0; self.metrics=None
    async def play(self, audio: AudioChunk):
        generation=self._generation
        if audio.token!=self.active_token: return
        for offset in range(0,len(audio.samples),4096):
            if generation!=self._generation or audio.token!=self.active_token: return
            values=audio.samples[offset:offset+4096]
            pcm=struct.pack("<"+"h"*len(values),*(round(value*32767) for value in values))
            if self.metrics is not None:
                self.metrics.mark_voice(audio.session_id, "proxy_send", generation_id=audio.generation_id)
            await self.hub.publish({"type":"audio_pcm_output","sample_rate":audio.sample_rate,
                                    "token":audio.token,"data_b64":base64.b64encode(pcm).decode()})
    async def stop(self):
        self._generation+=1
        await self.hub.publish({"type":"audio_stop","generation":self._generation})
    def readiness(self): return {"ready":True,"provider":"browser_web_audio"}


def _pattern_button_ids(pattern_id: str) -> tuple[int, ...]:
    ids = tuple(sorted({int(char) for char in str(pattern_id) if char in "1234567"}))
    return ids or tuple(BUTTON_LED_COLORS)


class UARTLEDController:
    """Canonical fake lighting output plus an isolated legacy projection.

    pattern and pattern_map are retained for the existing application
    protocol, but legacy patterns are diagnostic-only. They must not mutate
    canonical element state; behavior code can use set_element(s) when it is
    ready to select individual elements explicitly.
    """
    def __init__(self, adapter: SerialHardwareAdapter, output=None, legacy_sink=None, stage_hook=None, effect_hook=None, monotonic_now=time.monotonic, background_scheduler: bool = True):
        self.adapter = adapter
        self.output = output or FakeLightingOutput()
        self.legacy_sink = legacy_sink
        self.stage_hook = stage_hook
        self.exhibition_effects = ExhibitionLightingEngine(
            self.output,
            monotonic_now=monotonic_now,
            background_scheduler=background_scheduler,
            frame_hook=effect_hook,
        )

    def set_element(self, element_id: int, state: int, transition_ms: int = 0) -> None:
        self.output.set_element(element_id, state, transition_ms)

    def set_elements(self, commands) -> None:
        self.output.set_elements(commands)

    def clear_all(self, transition_ms: int = 0) -> None:
        self.exhibition_effects.cancel_effects(clear=False)
        self.output.clear_all(transition_ms)

    def start_breathing(self, *, now: float | None = None, elements=None, base_elements=None, phase: str = "breathing", preserve_current: bool = False):
        return self.exhibition_effects.start_breathing(
            now=now, elements=elements, base_elements=base_elements, phase=phase, preserve_current=preserve_current,
        )

    def start_wave(self, *, now: float | None = None, groups=None, follow_up_groups=None,
                   hold_on_complete: bool = True, loop: bool = False, base_elements=None,
                   stage_duration_ms: int | None = None, preserve_current: bool = False):
        return self.exhibition_effects.start_wave(
            now=now, groups=groups, follow_up_groups=follow_up_groups,
            hold_on_complete=hold_on_complete, loop=loop,
            base_elements=base_elements, stage_duration_ms=stage_duration_ms, preserve_current=preserve_current,
        )

    def start_profile_ready(self, *, now: float | None = None):
        return self.exhibition_effects.start_profile_ready(now=now)

    def start_age_selection_wave(self, *, now: float | None = None):
        return self.exhibition_effects.start_wave(
            now=now, groups=EXHIBITION_WAVE_GROUPS,
            follow_up_groups=EXHIBITION_DIGIT_WAVE_GROUPS, hold_on_complete=True,
        )

    def cancel_effects(self, *, clear: bool = True):
        return self.exhibition_effects.cancel_effects(clear=clear)

    def reset_to_standby(self, *, now: float | None = None):
        return self.exhibition_effects.reset_to_standby(now=now)

    def advance_effects(self, now: float | None = None, *, force: bool = False):
        return self.exhibition_effects.advance(now, force=force)

    def effect_snapshot(self):
        return self.exhibition_effects.snapshot()

    def readiness_wave(self, total_duration_ms: int = READINESS_WAVE_DURATION_MS) -> None:
        LightingController(self.output).readiness_wave(total_duration_ms)

    async def readiness_wave_async(self, total_duration_ms: int = READINESS_WAVE_DURATION_MS, *, cancelled=None) -> None:
        await LightingController(self.output).readiness_wave_async(
            total_duration_ms,
            stage_hook=self.stage_hook,
            cancelled=cancelled,
        )

    def complete_readiness_wave(self) -> None:
        if callable(self.stage_hook):
            owner = getattr(self.stage_hook, "__self__", None)
            complete = getattr(owner, "_complete_lighting_wave", None)
            if callable(complete):
                complete()

    def pattern(self, pattern_id, color, duration_ms, token):
        button_ids = ()
        if str(pattern_id) != "off" and str(color).lower() != "off":
            button_ids = _pattern_button_ids(pattern_id)
        if str(pattern_id).lower() == "off" or str(color).lower() == "off":
            self.clear_all(int(duration_ms))
        self._publish_legacy(pattern_id, button_ids, duration_ms, token)

    def pattern_map(self, pattern_id, colors, duration_ms, token):
        button_ids = [
            int(key) for key, value in (colors or {}).items()
            if str(key).isdigit() and int(key) in range(1, 8)
            and str(value).lower() != "off"
        ]
        if str(pattern_id).lower() == "off":
            self.clear_all(int(duration_ms))
        self._publish_legacy(pattern_id, button_ids, duration_ms, token)

    def set_button_compatibility(self, button_ids, state: int, transition_ms: int = 0) -> None:
        """Explicit compatibility helper for callers that really need a group."""
        commands = tuple(
            LightingCommand(element_id, state, transition_ms)
            for button_id in button_ids
            for element_id in BUTTON_OUTPUT_ELEMENTS.get(int(button_id), ())
        )
        self.set_elements(commands)

    def _publish_legacy(self, pattern_id, button_ids, duration_ms, token) -> None:
        # This is intentionally a snapshot/diagnostic projection only.  It is
        # not sent through SerialHardwareAdapter and cannot affect hardware.
        if self.legacy_sink is None:
            return
        self.legacy_sink({
            "pattern": pattern_id,
            "targets": list(button_ids),
            "duration_ms": int(duration_ms),
            "token": token,
        })


class UARTAudioOutput:
    private_pcm_supported = False
    def play_pcm_owned(self, handle: PlaybackHandle, audio: AudioChunk):
        raise RuntimeError("private_pcm_unsupported")

    def __init__(self, adapter: SerialHardwareAdapter): self.adapter=adapter
    def play_owned(self, handle: PlaybackHandle, asset_id: str, offset_ms: int = 0):
        self.adapter.send({"type": "audio_play", "payload": {
            "response_id": handle.response_id, "asset_id": asset_id,
            "asset_path": asset_id, "token": handle.token, "offset_ms": int(offset_ms),
            "session_id": handle.session_id, "generation": handle.generation,
            "playback_id": handle.playback_id, "playback_lease": handle.playback_lease,
        }})
    def play(self, response_id, asset_id, token):
        self.adapter.send({"type":"audio_play","payload":{"response_id":response_id,
                           "asset_id":asset_id,"token":token}})
    def play_from(self, response_id, asset_id, token, offset_ms=0):
        self.adapter.send({"type":"audio_play","payload":{"response_id":response_id,
                           "asset_id":asset_id,"token":token,"offset_ms":int(offset_ms)}})
    def play_text(self, response_id, text, token):
        self.adapter.send({"type":"audio_play","payload":{"response_id":response_id,"resolved_text":text,"content_kind":"action","token":token}})
    def pause(self): self.adapter.send({"type":"audio_pause","payload":{}})
    def resume(self): self.adapter.send({"type":"audio_resume","payload":{}})
    def cancel(self, token): self.adapter.send({"type":"audio_stop","payload":{"token":token}})

class UARTDisplayController:
    def __init__(self, adapter: SerialHardwareAdapter): self.adapter=adapter
    def show(self, asset_id, text, token):
        self.adapter.send({"type":"display","payload":{"asset_id":asset_id,"text":text,"token":token}})


class HeadlessAudioOutput:
    """Scenario audio port that delegates playback to the local USB device."""

    private_pcm_supported = True

    def __init__(self):
        self.on_play = None
        self.on_asset_play = None
        self.on_pcm_play = None
        self.on_pause = None
        self.on_resume = None
        self.on_cancel = None

    def play_pcm_owned(self, handle: PlaybackHandle, audio: AudioChunk):
        if not callable(self.on_pcm_play):
            raise RuntimeError("private_pcm_unavailable")
        self.on_pcm_play(handle, audio)

    def play_owned(self, handle: PlaybackHandle, asset_id: str, offset_ms: int = 0):
        if not callable(self.on_asset_play):
            raise RuntimeError("physical asset playback is unavailable")
        self.on_asset_play(handle.response_id, str(asset_id), handle.token, int(offset_ms), handle)

    def play(self, response_id, asset_id, token):
        if asset_id and callable(self.on_asset_play):
            self.on_asset_play(str(response_id), str(asset_id), int(token), 0)
            return
        if callable(self.on_play):
            self.on_play(str(response_id), None)

    def play_from(self, response_id, asset_id, token, offset_ms=0):
        if asset_id and callable(self.on_asset_play):
            self.on_asset_play(str(response_id), str(asset_id), int(token), int(offset_ms))
            return
        if callable(self.on_play):
            self.on_play(str(response_id), None)

    def play_text(self, response_id, text, token):
        if callable(self.on_play):
            self.on_play(str(response_id), str(text))

    def pause(self):
        if callable(self.on_pause):
            return self.on_pause()

    def resume(self):
        if callable(self.on_resume):
            return self.on_resume()

    def cancel(self, token):
        if callable(self.on_cancel):
            self.on_cancel(token)


class HardwareIntegrationStand:
    """One simulated installation connected to the real serial protocol boundary."""
    def __init__(self, settings: Settings | None = None, content_root: str | Path | None = None, monotonic_now=None, background_scheduler: bool = True):
        self.settings=settings or Settings.from_env(); self.hub=StandHub(); self.pty=PTYUARTSimulator()
        self.background_scheduler = bool(background_scheduler)
        self.monotonic_now = monotonic_now or time.monotonic
        self.hardware_mode = getattr(self.settings, "hardware_mode", "simulator")
        self.lighting_serial = None
        self.input_adapter = None
        self._physical_loop = None
        try:
            self._lighting_owner_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._lighting_owner_loop = None
        self._lighting_fault = None
        self._lighting_fault_generation = 0
        self._lighting_fault_future = None
        self._physical_sequence = 0
        self._admits_physical_users = None
        self._hid_generation = 0
        self._hid_status: dict[str, Any] = {"state": "not_started", "generation": 0}
        self._lighting_status: dict[str, Any] = {"state": "starting"}
        self.audio_input = None
        self._headless_audio_queue: asyncio.Queue[Any] | None = None
        self._headless_audio_task: asyncio.Task[Any] | None = None
        self._headless_audio_dropped = 0
        self._headless_audio_status: dict[str, Any] = {"state": "not_started"}
        self._startup_state = "starting"
        self._startup_diagnostics: deque[dict[str, Any]] = deque(maxlen=50)
        self._input_start_error: dict[str, Any] | None = None
        self._closed = False
        self._audio_event_sequence = 0
        self._lighting_wave = {'active': False, 'stage': 0, 'total_stages': len(READINESS_WAVE_GROUPS), 'elements': []}
        # PTY writes/reads are a transport transaction.  Audio inference is
        # deliberately outside this lock so a long STT/LLM/TTS turn can never
        # block the next UART frame or interleave two JSONL transactions.
        self._transport_lock=asyncio.Lock()
        self._uart_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mishka-uart-io")
        self._audio_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mishka-audio-codec")
        # Serialize only the short PCM ingress/VAD section per session. The
        # expensive recognition and response work is detached afterwards.
        self._audio_ingress_locks: dict[str, asyncio.Lock] = {}
        self.raw_frames: deque[dict[str,Any]]=deque(maxlen=500); self.commands: deque[dict[str,Any]]=deque(maxlen=300)
        self.results: deque[dict[str,Any]]=deque(maxlen=200); self.errors: deque[dict[str,Any]]=deque(maxlen=100)
        self.latencies: deque[dict[str,Any]]=deque(maxlen=300); self.device_sequence=0; self.active_session_id="stand"
        self.transport=PosixSerialTransport(self.pty.slave_path)
        self.adapter=SerialHardwareAdapter(self.transport,raw_observer=self._observe_raw); self.adapter.connect()
        if self.hardware_mode == "physical":
            self.playback = SoundDevicePlaybackController(self.settings.audio_output_device or None)
            self.playback.hardware_current = lambda: not self._physical_output_faulted()
            self.audio_input = SoundDeviceCaptureController(
                self.settings.audio_input_device or None,
                sample_rate=self.settings.audio_sample_rate,
                channels=1,
                blocksize=512,
            )
            self.audio_output = HeadlessAudioOutput()
            try:
                if not getattr(self.settings, "lighting_device", ""):
                    raise RuntimeError("MISHKA_LIGHTING_DEVICE is required in physical hardware mode")
                self.lighting_serial = SerialLightingAdapter(self.settings.lighting_device, baudrate=self.settings.lighting_baudrate, timeout_s=self.settings.lighting_timeout_s)
                self.lighting_serial.open()
                self.lighting_output = PhysicalLightingOutput(self.lighting_serial, auto_dark_recovery=True, on_fault=self._on_lighting_fault)
                self._lighting_status = {"state": "available", "backend": "physical_serial_lighting"}
            except Exception as exc:
                self.lighting_serial = None
                self._lighting_status = {"state": "unavailable", "backend": "physical_serial_lighting", "error_code": "lighting_unavailable", "exception_type": type(exc).__name__}
                self._startup_diagnostics.append({"code": "lighting_unavailable", "component": "lighting"})
                if not getattr(self.settings, "allow_degraded_hardware", False):
                    raise
                # Keep the canonical application alive in degraded mode; no
                # physical write is claimed and health remains degraded.
                self.lighting_output = FakeLightingOutput()
        else:
            self.lighting_output = FakeLightingOutput()
            self._lighting_status = {"state": "simulated", "backend": "fake_lighting"}
            self.audio_input = None
            self.playback=BrowserPlaybackController(self.hub)
            self.audio_output = UARTAudioOutput(self.adapter)
        self.application,self.scenarios,self.metrics,self.publisher=build_application(
            content_root,self.settings,audio_output=self.audio_output,
            led_controller=UARTLEDController(self.adapter, self.lighting_output, legacy_sink=self._record_legacy_led, stage_hook=self._publish_lighting_wave_stage, effect_hook=self._publish_lighting_effect_frame, monotonic_now=self.monotonic_now, background_scheduler=self.background_scheduler),display_controller=UARTDisplayController(self.adapter),playback_override=self.playback,monotonic_now=self.monotonic_now,background_scheduler=self.background_scheduler)
        self.application.audio_update_hook=self._publish_audio_completion
        self.application.state_update_hook=self._publish_canonical_state_update
        reset_to_standby = getattr(self.application.led, "reset_to_standby", None)
        if callable(reset_to_standby):
            reset_to_standby(now=self.application.monotonic_now())
        if self.hardware_mode == "physical":
            self.application.dependency_identity = {"hardware_backend": {"name": "physical_serial_lighting", "state": "physical", "acceptance": "physical_hardware"}}
        else:
            self.application.dependency_identity = {"hardware_backend": {"name": "pty_uart_simulator", "state": "simulator", "acceptance": "not_physical_hardware"}}
        self.playback.metrics=self.application.runtime_manager.engine.metrics
        self._event_cursor=0; self._device_buffer=b""; self.responses,self.stories,self.games=self._load_content(content_root)
        if isinstance(self.audio_output, HeadlessAudioOutput):
            self.audio_output.on_play = self._headless_audio_play
            self.audio_output.on_pcm_play = self._headless_pcm_play
            self.audio_output.on_asset_play = self._headless_asset_play
            self.audio_output.on_pause = self._pause_story_playback
            self.audio_output.on_resume = self._resume_story_playback
            self.audio_output.on_cancel = lambda _token: self._cancel_response_audio()
        self._startup_state = "degraded" if self.hardware_mode == "physical" and self._lighting_status.get("state") != "available" else "ready"
        self._story_wav_cache: dict[str, bytes] = {}
        self._story_audio_tasks: set[asyncio.Task[Any]] = set()
        self._response_audio_tasks: set[asyncio.Task[Any]] = set()
        self._notification_tasks: set[asyncio.Task[Any]] = set()
        self.graph={"contract": "canonical_product/v1", "nodes": sorted(canonical_state_nodes())}
        self.adapter.send({"type":"capabilities_request","payload":{"protocol_version":"1"}})
        self._drain_device_commands()
        self._device_write("capabilities",{"buttons":7,"led":True,"display":True,"audio_stop":True,
                           "firmware":"pty-simulator-1"})
        for frame in self._receive_frames_sync():
            if frame.type=="capabilities":
                self.adapter.capabilities=dict(frame.payload); self.adapter.firmware_version=str(frame.payload.get("firmware",""))

    def _headless_audio_accepting(self) -> bool:
        session = self.application.sessions.get(self.active_session_id)
        runtime = getattr(self.application, "canonical_runtime", None)
        state = runtime.sessions.get(self.active_session_id) if runtime is not None else None
        return bool(
            session is not None
            and getattr(self, "_capture_hid_fault", None) is None
            and not self._physical_output_faulted()
            and getattr(session, "handset_up", False)
            and getattr(state, "handset", None) == "up"
            and not getattr(state, "interaction_idle", False)
            and getattr(session, "current_node", None) in HEADLESS_AUDIO_NODES
            and (getattr(session, "current_node", None) not in {"fixed_story_menu","game_menu","personal_story_theme","schedule_weather"}
                 or (not self.application._active_audio.get(self.active_session_id)
                     and (getattr(state,"scene_menu_phase",None) == "repeat_listen"
                          or (getattr(state,"mode",None) in {"personal_story_theme","schedule_weather"}
                              and getattr(state,"scene_menu_phase",None)=="ready"
                              and bool(getattr(state,"scene_menu_descriptor",{}).get("items"))))))
            and not (getattr(session, "current_node", None) == "personal_welcome_listen"
                     and (self.application._active_audio.get(self.active_session_id)
                          or getattr(state, "audio_status", None) == "failed"))
        )

    def _audio_owner_guard(self, sid: str, *, physical: bool = False):
        record = self.application.sessions.get(sid)
        # A callback/upload admitted while the handset was down must not
        # become owned by the next lift of this same mutable session record.
        handset_up = bool(record and record.handset_up)
        epoch = self.application._session_epochs.get(sid, 0)
        hid_generation = getattr(self, "_hid_generation", 0)
        capture_generation = getattr(self, "_capture_generation", 0)
        lighting_generation = getattr(self, "_lighting_fault_generation", 0)

        def current():
            return (self.application.sessions.get(sid) is record
                    and bool(record and record.handset_up) == handset_up
                    and self.application._session_epochs.get(sid, 0) == epoch
                    and (not physical or (
                        self.active_session_id == sid
                        and getattr(self, "_hid_generation", 0) == hid_generation
                        and getattr(self, "_capture_generation", 0) == capture_generation
                        and getattr(self, "_capture_hid_fault", None) is None
                        and getattr(self, "_lighting_fault_generation", 0) == lighting_generation
                        and not self._physical_output_faulted()
                        and getattr(self.application.canonical_runtime.sessions.get(sid), "handset", None) == "up"
                        and bool(record and record.handset_up))))
        return current

    def _headless_frame_owner(self):
        # Called in the capture callback, before scheduling on the event loop.
        sid = self.active_session_id
        owner_current = self._audio_owner_guard(sid, physical=True)
        accepting = self._headless_audio_accepting()
        record = self.application.sessions.get(sid)
        node = getattr(record, "current_node", None)
        runtime = getattr(self.application, "canonical_runtime", None)
        state = runtime.sessions.get(sid) if runtime is not None else None
        version = getattr(state, "state_version", None)
        lease = self.application._active_audio.get(sid)
        private_phase = (node in {"personal_welcome_listen","fixed_story_menu","game_menu","personal_story_theme","schedule_weather"}
                         or bool(lease and lease.get("content_id") == "PERSONAL_WELCOME"))

        def phase_current():
            current = runtime.sessions.get(sid) if runtime is not None else None
            return (accepting and owner_current() and self._headless_audio_accepting()
                    and (not private_phase or (
                        getattr(record, "current_node", None) == node
                        and getattr(current, "state_version", None) == version
                        and self.application._active_audio.get(sid) is lease)))
        # Distinguish a closed listening phase from a revoked physical owner.
        phase_current.lifecycle_current = owner_current
        return sid, phase_current

    def _enqueue_headless_audio_frame(self, values: Any, owner=None) -> None:
        queue = self._headless_audio_queue
        if queue is None or not self._headless_audio_accepting():
            return
        sid, guard = owner if owner is not None else self._headless_frame_owner()
        if not guard():
            return
        try:
            queue.put_nowait((values, sid, guard))
        except asyncio.QueueFull:
            self._headless_audio_dropped += 1

    async def _headless_audio_worker(self) -> None:
        queue = self._headless_audio_queue
        if queue is None:
            return
        while True:
            item = await queue.get()
            if item is None:
                return
            values, sid, guard = item
            if not guard() or not self._headless_audio_accepting():
                continue
            try:
                result = await self.inject_pcm(
                    tuple(float(value) for value in np.asarray(values, dtype=np.float32).reshape(-1)),
                    self.settings.audio_sample_rate,
                    sid,
                    flush_silence=False,
                    wait_for_turn=False,
                    _ingress_guard=guard,
                )
                if isinstance(result, dict) and isinstance(result.get("error"), dict):
                    self.errors.append({
                        "at": time.time(),
                        "stage": "headless_audio_input",
                        "error_code": result["error"].get("code", "audio_input_failed"),
                    })
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors.append({
                    "at": time.time(),
                    "stage": "headless_audio_input",
                    "error_code": "capture_dispatch_failed",
                    "exception_type": type(exc).__name__,
                })

    def _start_headless_audio_capture(self) -> None:
        capture = getattr(self, "audio_input", None)
        if capture is None or getattr(self, "_headless_audio_task", None) is not None:
            return
        loop = self._physical_loop or asyncio.get_running_loop()
        self._capture_generation = getattr(self, "_capture_generation", 0) + 1
        self._headless_audio_queue = asyncio.Queue(maxsize=64)
        self._headless_audio_dropped = 0
        self._headless_audio_task = asyncio.create_task(
            self._headless_audio_worker(),
            name="mishka-headless-audio-input",
        )
        try:
            capture.start(loop, self._enqueue_headless_audio_frame, frame_context=self._headless_frame_owner)
            self._headless_audio_status = {
                "state": "active",
                "device": self.settings.audio_input_device or "default",
                "sample_rate": self.settings.audio_sample_rate,
                "channels": 1,
            }
        except Exception as exc:
            task, self._headless_audio_task = self._headless_audio_task, None
            if task is not None:
                task.cancel()
            self._headless_audio_queue = None
            self._headless_audio_status = {
                "state": "unavailable",
                "error_code": "audio_input_unavailable",
                "exception_type": type(exc).__name__,
            }
            if not self.settings.allow_degraded_hardware:
                raise
            self._startup_diagnostics.append({
                "code": "audio_input_unavailable",
                "component": "audio_input",
            })

    async def _stop_headless_audio_capture(self) -> None:
        self._capture_generation = getattr(self, "_capture_generation", 0) + 1
        capture = getattr(self, "audio_input", None)
        task, self._headless_audio_task = getattr(self, "_headless_audio_task", None), None
        self._headless_audio_queue = None
        try:
            if capture is not None:
                capture.stop()
        except Exception:
            self._headless_audio_status = {"state": "unavailable", "error_code": "capture_stop_failed"}
            raise
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if capture is not None:
            self._headless_audio_status = {"state": "stopped"}

    def _hardware_lifecycle_lock(self):
        lock = getattr(self, "_hid_lifecycle_lock", None)
        if lock is None:
            lock = self._hid_lifecycle_lock = asyncio.Lock()
        return lock

    async def start_hardware(self):
        async with self._hardware_lifecycle_lock():
            await self._start_hardware_locked()

    async def _start_hardware_locked(self):
        if self.hardware_mode != "physical":
            return
        if getattr(self, "_lighting_fault", None) is not None:
            raise RuntimeError("lighting fault requires a new hardware lifecycle")
        pending_loss = getattr(self, "_hid_loss_future", None)
        if pending_loss is not None:
            await self._settle_hid_loss(pending_loss)
        if self.input_adapter is not None and self.input_adapter.is_ready() and getattr(self, "_capture_hid_fault", None) is None:
            hid_diag = _hid_diagnostics(self.input_adapter); self._hid_status = {"state": "active", "generation": getattr(self, "_hid_generation", 0), "initial_level_sync": hid_diag.get("initial_level_sync"), "semantic_input_accepted": bool(hid_diag.get("semantic_input_accepted")), "physical_ready": bool(hid_diag.get("physical_ready"))}
            return
        if not getattr(self.settings, "hid_device", ""):
            self._input_start_error = {"exception_type": "RuntimeError", "detail": "MISHKA_HID_DEVICE is required in physical hardware mode"}
            self._hid_status = {"state": "device_unavailable", "generation": getattr(self, "_hid_generation", 0), "error_code": "hid_unavailable", **self._input_start_error}
            if hasattr(self, "errors"):
                self.errors.append({"at": time.time(), "stage": "hid_start", **self._input_start_error})
            self._startup_state = "degraded"
            if getattr(self.settings, "allow_degraded_hardware", False):
                self._startup_diagnostics.append({"code": "hid_unavailable", "component": "hid"})
                return
            raise RuntimeError(self._input_start_error["detail"])
        self._physical_loop = asyncio.get_running_loop()
        self._lighting_owner_loop = self._physical_loop
        old_adapter = self.input_adapter
        self.input_adapter = None
        self._hid_generation = getattr(self, "_hid_generation", 0) + 1
        generation = self._hid_generation
        self._capture_hid_fault = None
        self._hid_status = {"state": "starting", "generation": generation}
        if old_adapter is not None:
            old_adapter.close()
        self.input_adapter = KeyboardHIDInputAdapter(
            self.settings.hid_device,
            lambda event, reader_generation=generation: self._on_physical_input(event, reader_generation),
        )
        setattr(self.input_adapter, "on_error", lambda diagnostic, reader_generation=generation: self._on_hid_error(diagnostic, reader_generation))
        try:
            self.input_adapter.open()
        except Exception as exc:
            self._input_start_error = {"exception_type": type(exc).__name__, "detail": "HID device open failed"}
            self._hid_status = {"state": "device_unavailable", "generation": generation, "error_code": "hid_unavailable", **self._input_start_error}
            self.errors.append({"at": time.time(), "stage": "hid_start", "generation": generation, **self._input_start_error})
            self._startup_state = "degraded"
            if getattr(self.settings, "allow_degraded_hardware", False):
                self._startup_diagnostics.append({"code": "hid_unavailable", "component": "hid"})
                return
            raise
        self._input_start_error = None
        if self._capture_hid_fault is not None:
            # The reader can fail from its thread before open() returns.
            # Its pending cleanup owns this generation; never reopen capture.
            self._startup_state = "degraded"
            return
        hid_diag = _hid_diagnostics(self.input_adapter)
        level_sync = hid_diag.get("initial_level_sync") or {}
        hid_state = "active" if hid_diag.get("physical_ready") else ("initial_level_unavailable" if level_sync.get("status") == "unavailable" else "degraded")
        self._hid_status = {"state": hid_state, "generation": generation, "initial_level_sync": level_sync, "semantic_input_accepted": bool(hid_diag.get("semantic_input_accepted")), "physical_ready": bool(hid_diag.get("physical_ready"))}
        self._start_headless_audio_capture()
        self._startup_state = "ready" if getattr(self, "_lighting_status", {"state": "available"}).get("state") == "available" and hid_diag.get("physical_ready") and getattr(self, "_headless_audio_status", {}).get("state") == "active" else "degraded"

    async def stop_hardware(self):
        async with self._hardware_lifecycle_lock():
            await self._stop_hardware_locked()

    async def _stop_hardware_locked(self):
        pending_loss = getattr(self, "_hid_loss_future", None)
        if pending_loss is not None:
            await self._settle_hid_loss(pending_loss)
        await self._stop_headless_audio_capture()
        led = getattr(getattr(self, "application", None), "led", None)
        cancel_effects = getattr(led, "cancel_effects", None)
        if callable(cancel_effects):
            cancel_effects(clear=True)
        self._hid_generation = getattr(self, "_hid_generation", 0) + 1
        stopped_generation = self._hid_generation
        if self.input_adapter is not None:
            self.input_adapter.close()
            self.input_adapter = None
        self._hid_status = {"state": "stopped", "generation": stopped_generation}
        self._physical_loop = None
        lighting_output_close = getattr(getattr(self, "lighting_output", None), "close", None)
        try:
            if callable(lighting_output_close):
                await asyncio.to_thread(lighting_output_close)
        finally:
            if self.lighting_serial is not None:
                self.lighting_serial.close()
                self.lighting_serial = None

    def _headless_audio_play(self, response_id: str, text: str | None):
        resolved = text
        if not resolved:
            response = self.responses.get(response_id, {})
            if isinstance(response, dict):
                resolved = response.get("text")
        if resolved:
            self._schedule_response_audio(self.active_session_id, str(resolved))

    def _pcm_owner_current(self, handle: PlaybackHandle, identity_binding) -> bool:
        # Reader faults revoke output synchronously, before owner-loop teardown.
        if getattr(self, "_capture_hid_fault", None) is not None or self._physical_output_faulted():
            return False
        from .owned_speech import OWNED_PCM_IDS, PRIVATE_TYPED_AUDIO_IDS
        lease = self.application._active_audio.get(handle.session_id)
        state = self.application.canonical_runtime.sessions.get(handle.session_id)
        if not (state is not None and state.handset == "up" and handle.response_id in OWNED_PCM_IDS
                and state.active_audio_id == handle.response_id and state.playback_lease == handle.playback_lease
                and lease and self.active_session_id == handle.session_id
                and self.application._session_epochs.get(handle.session_id,0) == handle.generation
                and all(lease.get(key) == value for key,value in {
                    "content_id":handle.response_id,"token":handle.token,"generation":handle.generation,
                    "playback_id":handle.playback_id,"playback_lease":handle.playback_lease}.items())):
            return False
        if handle.response_id == "PERSONAL_STORY":
            owner = self.application._personal_stories.get(handle.session_id)
            return self.application._personal_story_scope(handle.session_id, owner, playback=True)
        if handle.response_id == "PERSONAL_WELCOME" or handle.response_id in PRIVATE_TYPED_AUDIO_IDS:
            capability = "personalize" if handle.response_id == "PERSONAL_WELCOME" else "schedule"
            return bool(identity_binding and all(identity_binding)
                and (state.profile_ref,state.identity_request_id) == identity_binding
                and self.application._identity_scope_live(handle.session_id,capability))
        from .scene_menus import MENU_PCM_MODES
        if handle.response_id in MENU_PCM_MODES:
            return state.mode == MENU_PCM_MODES[handle.response_id]
        return state.mode == "schedule_weather"

    def _personal_story_checkpoint_current(self, handle, story_owner, hid_generation) -> bool:
        # A paused lease grants one position update, never renewed PCM authority.
        app = self.application
        paused = app._paused_story_playback.get(handle.session_id)
        return bool(handle.response_id == "PERSONAL_STORY" and paused
            and self.active_session_id == handle.session_id
            and getattr(self, "_capture_hid_fault", None) is None
            and not self._physical_output_faulted()
            and getattr(self, "_hid_generation", 0) == hid_generation
            and app._session_epochs.get(handle.session_id, 0) == handle.generation
            and app._personal_story_scope(handle.session_id, story_owner, checkpoint=True)
            and all(paused.get(key) == value for key, value in {
                "content_id": handle.response_id, "token": handle.token,
                "generation": handle.generation, "playback_id": handle.playback_id,
                "playback_lease": handle.playback_lease}.items()))

    def _headless_pcm_play(self, handle: PlaybackHandle, audio: AudioChunk) -> None:
        from .owned_speech import OWNED_PCM_IDS
        if (handle.response_id not in OWNED_PCM_IDS or audio.token != handle.token
                or audio.session_id != handle.session_id or not audio.samples):
            raise RuntimeError("private_pcm_invalid_owner")
        state = self.application.canonical_runtime.sessions.get(handle.session_id)
        identity_binding = (state.profile_ref, state.identity_request_id) if state is not None else None
        previous = tuple(self._response_audio_tasks)
        story_owner = (self.application._personal_stories.get(handle.session_id)
                       if handle.response_id == "PERSONAL_STORY" else None)
        task = asyncio.create_task(self._play_pcm_audio(handle, audio, previous, identity_binding,
            hid_generation=getattr(self, "_hid_generation", 0), story_owner=story_owner),
            name="mishka-private-pcm")
        self._response_audio_tasks.add(task)
        if handle.response_id == "PERSONAL_STORY":
            self._story_pause_requested = False
            self._active_story_task, self._active_story_id = task, handle.response_id
        def observe(done):
            self._response_audio_tasks.discard(done)
            if not done.cancelled():
                # Never retain or report exception messages from private PCM.
                done.exception()
        task.add_done_callback(observe)

    async def _play_pcm_audio(self, handle: PlaybackHandle, audio: AudioChunk,
                              previous=(), identity_binding=None, *, hid_generation=None,
                              story_owner=None):
        def current():
            return bool((hid_generation is None or getattr(self, "_hid_generation", 0) == hid_generation)
                and self._pcm_owner_current(handle, identity_binding)
                and (handle.response_id != "PERSONAL_STORY"
                     or self.application._personal_stories.get(handle.session_id) is story_owner))
        async def report(kind):
            if not current():
                return
            self._audio_event_sequence += 1
            payload = {"response_id": handle.response_id, "token": handle.token,
                       "generation": handle.generation, "playback_id": handle.playback_id,
                       "playback_lease": handle.playback_lease}
            if kind == "AudioFailed":
                payload["error_code"] = "private_pcm_failed"
            await self.application.receive_async(Event(kind, handle.session_id,
                self._audio_event_sequence, time.time(), "physical_audio", payload,
                "private-pcm:" + handle.playback_id))
        def diagnostic(code):
            recorder = getattr(self.application, "record_audio_diagnostic", None)
            if callable(recorder):
                recorder(code, session_id=handle.session_id, response_id=handle.response_id)
        try:
            # Registration may follow the synchronous port call before yield.
            # Stale PCM must never cancel a newer owner's playback.
            if not current():
                return
            for task in previous:
                task.cancel()
            if previous:
                await asyncio.gather(*previous, return_exceptions=True)
            if not current():
                return
            self.playback.active_token = handle.token
            diagnostic("private_pcm_started")
            play_owned = getattr(self.playback, "play_owned_pcm", None)
            if not callable(play_owned):
                raise RuntimeError("private_pcm_guard_unavailable")
            if handle.response_id == "PERSONAL_STORY":
                from .s2s import PCMPlaybackResult
                offset = int(story_owner.offset_ms * audio.sample_rate / 1000)
                result = await play_owned(audio, current, start_position_samples=offset)
                if not isinstance(result, PCMPlaybackResult):
                    raise RuntimeError("personal_story_progress_unavailable")
                if (getattr(self, "_story_pause_requested", False)
                        and self._personal_story_checkpoint_current(handle, story_owner, hid_generation)):
                    self._story_pause_requested = False
                    total = len(story_owner.audio.samples)
                    if not offset <= result.position_samples <= total:
                        raise RuntimeError("personal_story_progress_invalid")
                    return {"story_id": "PERSONAL_STORY",
                            "offset_ms": int(result.position_samples * 1000 / audio.sample_rate)}
                completed = result.completed
            else:
                result = await play_owned(audio, current)
                from .s2s import PCMPlaybackResult
                completed = (result.completed if isinstance(result, PCMPlaybackResult)
                             else bool(getattr(self.playback, "last_playback_completed", False)))
            if not current():
                return
            if not completed:
                diagnostic("private_pcm_incomplete")
                await report("AudioFailed")
                return
            diagnostic("private_pcm_finished")
            await report("AudioCompleted")
        except asyncio.CancelledError:
            # Backend cancellation owns its stream cleanup. Never stop shared
            # playback here: a newer owner may already be using it.
            raise
        except Exception:
            if current():
                diagnostic("private_pcm_failed")
                try:
                    await report("AudioFailed")
                except Exception:
                    pass

    def _headless_asset_play(self, response_id: str, asset_id: str, token: int, offset_ms: int = 0,
                             handle: PlaybackHandle | None = None) -> None:
        """Play an immutable canonical WAV through the physical speaker."""
        self._cancel_response_audio()
        is_story = response_id in self.stories or response_id.startswith("placeholder:fixed_story:")
        self._story_pause_requested = False if is_story else getattr(self, "_story_pause_requested", False)
        session_id = handle.session_id if handle is not None else self.active_session_id
        task = asyncio.create_task(
            self._play_asset_audio(session_id, response_id, asset_id, token, int(offset_ms), handle),
            name=f"mishka-asset-audio-{response_id}",
        )
        self._response_audio_tasks.add(task)
        if is_story:
            self._active_story_task = task
            self._active_story_id = response_id
        task.add_done_callback(
            lambda done: self._observe_detached_audio_task(
                done, self._response_audio_tasks, operation="asset_audio",
                session_id=session_id,
            )
        )

    async def _pause_story_playback(self):
        task = getattr(self, "_active_story_task", None)
        if task is None or task.done():
            return
        self._story_pause_requested = True
        await self.playback.stop()
        return await task

    async def _resume_story_playback(self):
        return

    @staticmethod
    def _playback_lifecycle_identity(handle: PlaybackHandle | None) -> dict[str, Any]:
        if handle is None:
            return {}
        return {
            "session_id": handle.session_id, "generation": handle.generation,
            "playback_id": handle.playback_id, "playback_lease": handle.playback_lease,
        }

    async def _play_asset_audio(self, session_id: str, response_id: str, asset_id: str, token: int,
                                offset_ms: int = 0, handle: PlaybackHandle | None = None) -> None:
        hid_generation = getattr(self, "_hid_generation", 0)
        physical = getattr(self, "hardware_mode", None) == "physical"
        def current():
            if (getattr(self, "_capture_hid_fault", None) is not None
                    or self._physical_output_faulted()
                    or getattr(self, "_hid_generation", 0) != hid_generation):
                return False
            if not physical:
                return True  # Preserve simulator/legacy audio port behavior.
            if handle is None or self.active_session_id != session_id:
                return False
            lease = self.application._active_audio.get(session_id)
            state = self.application.canonical_runtime.sessions.get(session_id)
            return bool(lease and state and state.handset == "up"
                and self.application._session_epochs.get(session_id, 0) == handle.generation
                and state.active_audio_id == response_id and state.playback_lease == handle.playback_lease
                and all(lease.get(key) == value for key, value in {
                    "content_id": response_id, "token": token, "generation": handle.generation,
                    "playback_id": handle.playback_id, "playback_lease": handle.playback_lease}.items()))
        def checkpoint_current():
            # Pause changes the canonical active lease to its announcement.
            # The retained old lease grants only one checkpoint, never PCM.
            if not physical:
                return current()
            state = self.application.canonical_runtime.sessions.get(session_id)
            paused = self.application._paused_story_playback.get(session_id)
            return bool(handle and paused and state and state.handset == "up"
                and state.mode in {"story_pause_announcement", "story_paused"}
                and state.active_story_id == response_id
                and self.active_session_id == session_id
                and getattr(self, "_capture_hid_fault", None) is None
                and not self._physical_output_faulted()
                and getattr(self, "_hid_generation", 0) == hid_generation
                and self.application._session_epochs.get(session_id, 0) == handle.generation
                and all(paused.get(key) == value for key, value in {
                    "content_id": response_id, "token": token, "generation": handle.generation,
                    "playback_id": handle.playback_id, "playback_lease": handle.playback_lease}.items()))
        try:
            if not current():
                return
            path = Path(asset_id).expanduser()
            if not path.is_absolute():
                project_root = Path(__file__).resolve().parents[2]
                candidates = (
                    project_root / path,
                    project_root / "content" / "audio" / path,
                    project_root / "content" / path,
                )
                path = next((candidate for candidate in candidates if candidate.is_file()), candidates[0])
            rendered = await self._offload_audio(path.read_bytes)
            if not current():
                return
            with wave.open(BytesIO(rendered), "rb") as source:
                rate = source.getframerate()
                channels = source.getnchannels()
                width = source.getsampwidth()
                raw_pcm = source.readframes(source.getnframes())
            if channels != 1 or width != 2 or not raw_pcm:
                raise ValueError("canonical WAV must be mono PCM16")
            samples = tuple(value / 32768.0 for value in struct.unpack(
                "<%dh" % (len(raw_pcm) // 2), raw_pcm
            ))
            is_story = response_id in self.stories or response_id.startswith("placeholder:fixed_story:")
            start_sample = min(len(samples), max(0, int(int(offset_ms) * rate / 1000)))
            self.playback.active_token = int(token)
            self.commands.append({
                "at": time.time(), "type": "asset_audio_play", "response_id": response_id,
                "asset_id": str(path), "samples": len(samples) - start_sample, "rate": rate,
                "token": int(token), "offset_ms": int(offset_ms),
            })
            self.application._mark_audio_effect_lifecycle(response_id, "playing", **HardwareIntegrationStand._playback_lifecycle_identity(handle))
            recorder = getattr(self.application, "record_audio_diagnostic", None)
            if callable(recorder):
                recorder("audio_playback_started", session_id=session_id, response_id=response_id,
                         content_id=response_id, asset_ref=str(path), playback_id=f"{session_id}:{response_id}:{token}")
            audio = AudioChunk(samples[start_sample:], rate, int(token), session_id)
            guarded_play = getattr(self.playback, "play_owned_pcm", None)
            from .s2s import PCMPlaybackResult
            result = None
            if callable(guarded_play):
                result = await guarded_play(audio, current, start_position_samples=start_sample)
                if physical and not isinstance(result, PCMPlaybackResult):
                    raise RuntimeError("physical_asset_progress_unavailable")
            elif physical:
                raise RuntimeError("physical_asset_guard_unavailable")
            else:
                self.playback.start_position_samples = start_sample
                await self.playback.play(audio)
            completed = (result.completed if isinstance(result, PCMPlaybackResult)
                         else bool(getattr(self.playback, "last_playback_completed", True)))
            if (is_story and getattr(self, "_story_pause_requested", False)
                    and checkpoint_current()):
                self._story_pause_requested = False
                position = (result.position_samples if isinstance(result, PCMPlaybackResult)
                            else int(getattr(self.playback, "position_samples", start_sample)))
                if not start_sample <= position <= len(samples):
                    raise RuntimeError("physical_asset_progress_invalid")
                checkpoint_ms = int(position * 1000 / rate)
                self.commands.append({"at": time.time(), "type": "story_audio_paused", "story_id": response_id, "offset_ms": checkpoint_ms})
                self.application._mark_audio_effect_lifecycle(response_id, "paused", **HardwareIntegrationStand._playback_lifecycle_identity(handle))
                return {"story_id": response_id, "offset_ms": checkpoint_ms}
            if not current():
                return
            if not completed:
                raise RuntimeError("physical_asset_playback_incomplete")
            self.application._mark_audio_effect_lifecycle(response_id, "finished", **HardwareIntegrationStand._playback_lifecycle_identity(handle))
            if callable(recorder):
                recorder("audio_playback_finished", session_id=session_id, response_id=response_id,
                         content_id=response_id, asset_ref=str(path), playback_id=f"{session_id}:{response_id}:{token}")
            # Physical playback owns completion for fixed stories.  Without
            # this event the canonical state would remain PLAYING forever;
            # browser playback reports the same event through its observer.
            if (response_id in self.stories
                    or response_id.startswith("placeholder:fixed_story:")
                    or response_id.startswith("PHRASE_")):
                self._audio_event_sequence += 1
                if handle is None:
                    return
                await self.application.receive_async(
                    Event("AudioCompleted", session_id, self._audio_event_sequence,
                          time.time(), "physical_audio",
                          {"response_id": response_id, "token": int(token),
                           "playback_lease": handle.playback_lease,
                           "generation": handle.generation,
                           "playback_id": handle.playback_id},
                          f"audio-complete:{handle.playback_id}"),
                )
                self._collect_events()
        except asyncio.CancelledError:
            self.application._mark_audio_effect_lifecycle(response_id, "cancelled", **HardwareIntegrationStand._playback_lifecycle_identity(handle))
            recorder = getattr(self.application, "record_audio_diagnostic", None)
            if callable(recorder):
                recorder("audio_playback_cancelled", session_id=session_id, response_id=response_id,
                         content_id=response_id, asset_ref=asset_id, playback_id=f"{session_id}:{response_id}:{token}")
            raise
        except Exception as exc:
            if not current():
                return
            self.application._mark_audio_effect_lifecycle(response_id, "failed", reason=type(exc).__name__, **HardwareIntegrationStand._playback_lifecycle_identity(handle))
            recorder = getattr(self.application, "record_audio_diagnostic", None)
            if callable(recorder):
                recorder("audio_playback_failed", session_id=session_id, response_id=response_id,
                         content_id=response_id, asset_ref=asset_id, error=type(exc).__name__, detail=str(exc)[:200],
                         playback_id=f"{session_id}:{response_id}:{token}")
            self.errors.append({
                "at": time.time(), "stage": "asset_playback", "response_id": response_id,
                "asset_id": asset_id, "error": type(exc).__name__, "detail": str(exc)[:200],
            })
            if not (response_id in self.stories
                    or response_id.startswith("placeholder:fixed_story:")
                    or response_id.startswith("PHRASE_")):
                return
            self._audio_event_sequence += 1
            if handle is None:
                return
            await self.application.receive_async(
                Event("AudioFailed", session_id, self._audio_event_sequence, time.time(),
                      "physical_audio",
                      {"response_id": response_id, "token": int(token),
                       "playback_lease": handle.playback_lease,
                       "generation": handle.generation,
                       "playback_id": handle.playback_id,
                       "error_code": "audio_playback_failed"},
                      f"audio-failed:{handle.playback_id}"),
            )

    def _physical_output_faulted(self) -> bool:
        if getattr(self, "_lighting_fault", None) is not None:
            return True
        if getattr(self, "hardware_mode", None) != "physical":
            return False
        diagnostics = getattr(getattr(self, "lighting_output", None), "diagnostics", None)
        if not callable(diagnostics):
            return False
        try:
            return not diagnostics().get("output_ready", True)
        except Exception:
            return True

    def _on_lighting_fault(self, output, diagnostic) -> None:
        # Called on the sole serial writer. The latch revokes already-issued
        # capture/playback guards before scheduling any asynchronous teardown.
        if (output is not getattr(self, "lighting_output", None) or getattr(self, "_closed", False)
                or getattr(output, "_closing", False) or getattr(output, "_closed", False)):
            return
        if getattr(self, "_lighting_fault", None) is not None:
            return
        self._lighting_fault_generation = getattr(self, "_lighting_fault_generation", 0) + 1
        fault = {"owner_generation": self._lighting_fault_generation,
                 "output_generation": diagnostic.get("generation"),
                 "stage": "physical_lighting_loss", "at_monotonic": time.monotonic()}
        self._lighting_fault = fault
        loop = getattr(self, "_physical_loop", None) or getattr(self, "_lighting_owner_loop", None)
        if loop is None or loop.is_closed():
            return
        self._lighting_fault_future = asyncio.run_coroutine_threadsafe(
            self._handle_lighting_fault(output, fault, self.active_session_id), loop)
        self._lighting_fault_future.add_done_callback(self._observe_lighting_fault_cleanup)

    def _observe_lighting_fault_cleanup(self, done) -> None:
        if done.cancelled():
            return
        try:
            done.result()
        except Exception:
            self.errors.append({"at": time.time(), "stage": "physical_lighting_loss",
                                "error_code": "lighting_cleanup_failed"})

    async def _handle_lighting_fault(self, output, fault, sid: str) -> None:
        if (getattr(self, "_closed", False) or self._lighting_fault is not fault
                or self.lighting_output is not output):
            return
        app = self.application
        self._cancel_response_audio(); self._cancel_story_audio()
        app._cancel_game_timer(sid); app._cancel_welcome_timer(sid)
        app._cancel_menu_tutorial(sid); app._cancel_lighting_wave(sid)
        app._cancel_owned_text(sid)
        # Capture queues are revoked synchronously on the owner loop. The
        # potentially blocking device stop is bounded and kept off that loop.
        self._capture_generation = getattr(self, "_capture_generation", 0) + 1
        capture, playback = self.audio_input, self.playback
        task, self._headless_audio_task = self._headless_audio_task, None
        self._headless_audio_queue = None
        if task is not None:
            task.cancel(); task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
        self._headless_audio_status = {"state": "unavailable", "error_code": "lighting_unavailable"}
        try:
            # A lighting-device failure is a generic hardware fault, not an
            # invented HID edge. This transition preserves any urgent incident.
            await app._receive_canonical_event(Event("HardwareDisconnected", sid,
                fault["owner_generation"], time.time(), "physical_lighting_loss", {},
                "lighting-loss:" + str(fault["owner_generation"])))
        except LightingFaultLatchedError:
            # SAFE_STOP's lit frame is deliberately forbidden by the physical
            # fault latch. Canonical fault/epoch transition already committed.
            pass
        finally:
            def owns_fault():
                return (not self._closed and self._lighting_fault is fault
                        and self.lighting_output is output and self.application is app)
            if owns_fault():
                app._cancel_game_timer(sid); app._invalidate_audio_lease(sid)
            async def stop_resource(resource):
                if not owns_fault() or (resource == "capture" and capture is None):
                    return
                try:
                    if resource == "playback":
                        await playback.stop()
                    else:
                        def stop_capture():
                            if owns_fault():
                                capture.stop()
                        await asyncio.to_thread(stop_capture)
                except Exception:
                    if owns_fault():
                        self.errors.append({"at": time.time(), "stage": "physical_lighting_loss",
                                            "error_code": "lighting_device_stop_failed", "resource": resource})
            async def stop_owned():
                # Independent attempts on captured objects: a broken or slow
                # speaker must not leave microphone stop unattempted (or vice versa).
                await asyncio.gather(stop_resource("playback"), stop_resource("capture"),
                                     return_exceptions=True)
            stop = asyncio.create_task(stop_owned(), name="mishka-lighting-fault-stop")
            stop.add_done_callback(lambda t: None if t.cancelled() else t.exception())
            try:
                await asyncio.wait_for(asyncio.shield(stop), timeout=1.0)
            except asyncio.TimeoutError:
                if owns_fault():
                    self.errors.append({"at": time.time(), "stage": "physical_lighting_loss",
                                        "error_code": "lighting_cleanup_timeout"})

    def _on_hid_error(self, diagnostic: dict[str, Any], generation: int) -> None:
        if generation != getattr(self, "_hid_generation", 0):
            return
        self._hid_status = {"state": str(diagnostic.get("kind") or "reader_exception"), "generation": generation, "semantic_input_accepted": False, "physical_ready": False, **diagnostic}
        self.errors.append({"at": time.time(), "stage": "hid_reader", "generation": generation, **diagnostic})
        if getattr(self, "_capture_hid_fault", None) == generation:
            return
        # Close ingress immediately, including queued callbacks and frames
        # already waiting for UART/audio locks. Cleanup runs on the owner loop.
        self._capture_hid_fault = generation
        loop = getattr(self, "_physical_loop", None)
        if loop is not None and not loop.is_closed():
            self._hid_loss_future = asyncio.run_coroutine_threadsafe(
                self._handle_hid_loss(self.active_session_id, generation), loop)
            self._hid_loss_future.add_done_callback(
                lambda done, owner=generation: self._observe_physical_input_future(done, owner, cleanup=True))

    async def _settle_hid_loss(self, pending_loss) -> None:
        # Bind ownership before yielding. A completed old future must never
        # dispatch teardown against a replacement reader/session.
        generation = getattr(self, "_hid_generation", 0)
        sid = self.active_session_id

        def owns_loss():
            return (getattr(self, "_hid_loss_future", None) is pending_loss
                    and getattr(self, "_hid_generation", 0) == generation)

        if not owns_loss():
            return
        try:
            await asyncio.shield(asyncio.wrap_future(pending_loss))
        except Exception:
            if not owns_loss():
                return
            # Public start/stop hold the lifecycle lock through this retry.
            await self._handle_hid_loss(sid, generation)
        if owns_loss():
            self._hid_loss_future = None

    async def _handle_hid_loss(self, sid: str, generation: int) -> None:
        if generation != getattr(self, "_hid_generation", 0):
            return
        try:
            capture = getattr(self, "audio_input", None)
            if capture is not None:
                try:
                    capture.stop()
                except Exception:
                    # Ingress is already fenced; an input-device failure must
                    # not prevent the independent audio/lighting teardown.
                    self._headless_audio_status = {"state": "unavailable", "error_code": "capture_stop_failed"}
            self._hid_cleanup_sequence = getattr(self, "_hid_cleanup_sequence", 0) + 1
            sequence = self._hid_cleanup_sequence
            result = await self.application.receive_async(Event(
                "HardwareDisconnected", sid, sequence, time.time(), "physical_hid_loss",
                {}, f"hid-loss:{generation}:{sequence}",
            ))
            for effect in result.get("outputs", []):
                if any(isinstance(effect.get(key), dict)
                       and effect[key].get("status") in {"failed", "timed_out", "timeout"}
                       for key in ("sync_cleanup", "audio_cleanup", "s2s_cleanup")):
                    raise RuntimeError("hid_cleanup_failed")
        finally:
            await self._stop_headless_audio_capture()

    def _on_physical_input(self, event: NormalizedInputEvent, generation: int | None = None):
        if generation is not None and generation != getattr(self, "_hid_generation", 0):
            return
        if getattr(self, "_capture_hid_fault", None) is not None:
            return
        loop = self._physical_loop
        if loop is None:
            return
        self._physical_sequence += 1
        payload = dict(event.payload)
        payload["type"] = event.type
        payload["_source"] = "physical_hid"
        payload["_sequence"] = self._physical_sequence
        payload["_hid_generation"] = generation
        future = asyncio.run_coroutine_threadsafe(self.inject_physical(payload), loop)
        future.add_done_callback(
            lambda done, owner=getattr(self, "_hid_generation", 0) if generation is None else generation:
                self._observe_physical_input_future(done, owner))

    def _observe_physical_input_future(self, future, generation=None, *, cleanup=False) -> None:
        if future.cancelled():
            return
        try:
            future.result()
        except Exception:
            owner = getattr(self, "_hid_generation", 0) if generation is None else generation
            if owner != getattr(self, "_hid_generation", 0):
                return
            diagnostic = {"kind": "dispatch_failure", "error_code": "hid_cleanup_failed" if cleanup else "hid_dispatch_failed"}
            # The existing fault path revokes capture immediately. A failed
            # cleanup observer must not recursively schedule another cleanup.
            self._on_hid_error(diagnostic, owner)
            logger.error("physical HID dispatch failed: %s", diagnostic["error_code"])

    async def inject_physical(self, payload: dict):
        normalized = dict(payload)
        source = str(normalized.pop("_source", "hid"))
        input_generation = normalized.pop("_hid_generation", None)
        if input_generation is not None and input_generation != getattr(self, "_hid_generation", 0):
            return {"state": "idle", "reason": "stale_hid_generation", "outputs": []}
        sequence = normalized.pop("_sequence", None)
        if sequence is None:
            self._physical_sequence += 1
            sequence = self._physical_sequence
        edge = normalized.get("type")
        if edge == "SpacePressed":
            normalized = {"type": "handset_down", "_input_type": edge, "_normalized_event": "handset_down"}
        elif edge == "SpaceReleased":
            normalized = {"type": "handset_up", "_input_type": edge, "_normalized_event": "handset_up"}
        event = parse_event(normalized, self.active_session_id, int(sequence), source)
        # The HID path must obey the same admission gate as HTTP/WebSocket.
        # Hangup remains a cleanup operation during warmup, failure and shutdown.
        if event.event_type not in {"HandsetPlaced", "HardwareDisconnected"}:
            if getattr(self, "_capture_hid_fault", None) is not None:
                return {"state": "idle", "reason": "hid_unavailable", "outputs": []}
            if self._physical_output_faulted():
                return {"state": "idle", "reason": "lighting_unavailable", "outputs": []}
            admits = self._admits_physical_users
            if admits is None or not admits():
                return {"state": "idle", "reason": "runtime_unavailable", "outputs": []}
        try:
            result = await self.application.receive_async(event, None)
        except LightingFaultLatchedError:
            # The native writer may latch after admission, including for an
            # ordinary button. Report that known lighting failure, not HID loss.
            current = self.application.canonical_runtime.sessions.get(event.session_id)
            reason = ("lighting_unavailable_cleanup" if event.event_type in
                      {"HandsetPlaced", "HardwareDisconnected"} else "lighting_unavailable")
            result = {"reason": reason, "outputs": [],
                      "canonical_state": current.snapshot() if current else {"mode": "idle", "handset": "down"},
                      "host_write_completed": False, "firmware_acknowledged": False}
        self._collect_events()
        await self.hub.publish({"type": "stand_state", "state": self.panel_snapshot()})
        return result

    async def _offload_uart(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._uart_executor, fn, *args)

    async def _offload_audio(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._audio_executor, fn, *args)

    def _load_content(self, content_root):
        # The stand reads only canonical manifests. Legacy response/story/game
        # obsolete JSON is intentionally not a runtime source.
        project_root = Path(__file__).resolve().parents[2]
        candidate = Path(content_root).expanduser() if content_root else project_root / "content"
        root = candidate if candidate.is_absolute() else project_root / candidate
        manifest = FixedStoryManifest(root / "stories" / "fixed_manifest.json", root.parent)
        stories: dict[str, Any] = {}
        for story_id in manifest.stories:
            verified = manifest.get_verified(story_id)
            if verified is not None:
                stories[story_id] = verified
            elif getattr(self.settings, "profile", "mock") in {"mock", "local_cpu", "local_gpu"}:
                placeholder = manifest.get_development_placeholder()
                if placeholder is not None:
                    stories[story_id] = {**placeholder, "story_id": story_id}
        return {}, stories, {}

    async def phrase_wav(self, phrase_id: str) -> bytes | None:
        asset = self.application.voice_manifest.resolve(str(phrase_id))
        if not asset:
            return None
        path = Path(str(asset["asset_path"]))
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[2] / "content" / "audio" / path
        try:
            return path.read_bytes()
        except OSError:
            return None

    async def asset_wav(self, asset_id: str) -> bytes | None:
        """Serve only the small allowlisted exhibition assets."""
        if str(asset_id) != "RINGTONE":
            return None
        path = Path(__file__).resolve().parents[2] / "assets" / "sounds" / "ringtone.wav"
        try:
            return path.read_bytes()
        except OSError:
            return None

    async def story_wav(self, story_id: str) -> bytes | None:
        """Serve only the immutable verified WAV or explicit dev placeholder."""
        if str(story_id).startswith("placeholder:fixed_story:"):
            story_id = str(story_id).split(":", 2)[-1]
        story = self.stories.get(story_id)
        if not isinstance(story, dict):
            return None
        filename = story.get("audio_file")
        if not isinstance(filename, str):
            return None
        path = Path(filename)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[2] / path
        try:
            return path.read_bytes()
        except OSError:
            return None

    def _cancel_story_audio(self) -> None:
        for task in tuple(self._story_audio_tasks):
            task.cancel()
        self._story_audio_tasks.clear()

    def _cancel_response_audio(self) -> None:
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        for task in tuple(self._response_audio_tasks):
            if task is not current:
                task.cancel()
        self._response_audio_tasks.intersection_update({current} if current is not None else set())

    def _observe_detached_audio_task(self, task: asyncio.Task[Any], owner: set[asyncio.Task[Any]], *,
                                      operation: str, session_id: str | None = None) -> None:
        """Consume every detached audio/notification task outcome exactly once."""
        owner.discard(task)
        recorder = getattr(self.application, "_record_audio_completion_diagnostic", None)
        if task.cancelled():
            if callable(recorder):
                recorder("detached_audio_task_cancelled", session_id=session_id,
                         operation=operation, hook_name=operation, mandatory=False)
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            if callable(recorder):
                recorder("detached_audio_task_cancelled", session_id=session_id,
                         operation=operation, hook_name=operation, mandatory=False)
            return
        if error is not None and callable(recorder):
            recorder("detached_audio_task_error", session_id=session_id,
                     operation=operation, hook_name=operation, mandatory=False,
                     exception_type=type(error).__name__, exception_class=type(error).__name__,
                     reason=str(error))

    def _schedule_notification(self, coro: Any, *, operation: str,
                               session_id: str | None = None) -> None:
        task = asyncio.create_task(coro, name=f"mishka-{operation}")
        self._notification_tasks.add(task)
        task.add_done_callback(
            lambda done: self._observe_detached_audio_task(
                done, self._notification_tasks, operation=operation, session_id=session_id,
            )
        )

    async def _play_response_audio(self, session_id: str, text: str) -> None:
        engine = getattr(self.application, "bounded_s2s", None)
        if engine is None or not text:
            return
        try:
            result = await engine.speak_text(session_id, text)
            if getattr(result, "fallback_reason", None):
                self.errors.append({
                    "at": time.time(),
                    "stage": "response_audio",
                    "error": "fallback",
                    "detail": str(result.fallback_reason),
                })
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.errors.append({
                "at": time.time(),
                "stage": "response_audio",
                "error": type(exc).__name__,
                "detail": str(exc)[:200],
            })

    def _schedule_response_audio(self, session_id: str, text: str) -> None:
        self._cancel_response_audio()
        task = asyncio.create_task(self._play_response_audio(session_id, text), name=f"mishka-response-audio-{session_id}")
        self._response_audio_tasks.add(task)
        task.add_done_callback(
            lambda done: self._observe_detached_audio_task(
                done, self._response_audio_tasks, operation="response_audio", session_id=session_id,
            )
        )

    async def _play_story_audio(self, story_id: str) -> None:
        try:
            rendered = await self.story_wav(story_id)
            if not rendered:
                self.commands.append({"at": time.time(), "type": "story_audio_empty", "story_id": story_id})
                return
            self.commands.append({"at": time.time(), "type": "story_audio_rendered", "story_id": story_id, "bytes": len(rendered)})
            with wave.open(BytesIO(rendered), "rb") as source:
                rate = source.getframerate()
                channels = source.getnchannels()
                width = source.getsampwidth()
                raw_pcm = source.readframes(source.getnframes())
            if channels != 1 or width != 2 or not raw_pcm:
                raise ValueError("story WAV must be mono PCM16")
            samples = tuple(value / 32768.0 for value in struct.unpack("<%dh" % (len(raw_pcm) // 2), raw_pcm))
            token = int(time.time() * 1000)
            self.playback.active_token = token
            self.commands.append({"at": time.time(), "type": "story_audio_play", "story_id": story_id, "samples": len(samples), "rate": rate, "token": token})
            await self.playback.play(AudioChunk(samples, rate, token))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.errors.append({"at": time.time(), "stage": "story_playback", "error": type(exc).__name__, "detail": str(exc)[:200]})

    def _schedule_story_audio(self, story_id: str) -> None:
        self.commands.append({"at": time.time(), "type": "story_audio_schedule", "story_id": story_id})
        self._cancel_story_audio()
        task = asyncio.create_task(self._play_story_audio(story_id), name=f"mishka-story-audio-{story_id}")
        self._story_audio_tasks.add(task)
        task.add_done_callback(
            lambda done: self._observe_detached_audio_task(
                done, self._story_audio_tasks, operation="story_audio", session_id=self.active_session_id,
            )
        )

    def _observe_raw(self, direction: str, raw: bytes):
        item={"at":time.time(),"direction":direction,"raw":"[UART payload omitted from diagnostics]"}
        try:
            frame=decode_frame(raw)
            if frame.type == "audio_pcm":
                # The UART boundary still validates every byte and CRC. A
                # browser does not need hundreds of base64 PCM copies in the
                # diagnostic snapshot; they made each subsequent WS update
                # larger and larger and stalled microphone ingress.
                payload={key:value for key,value in frame.payload.items() if key!="data_b64"}
                payload["pcm_bytes"]=len(str(frame.payload.get("data_b64", "")))*3//4
                item["decoded"]={"type":frame.type,"sequence":frame.sequence,
                                 "payload":{"pcm_bytes":payload["pcm_bytes"]}}
            else:
                allowed = {"button_id", "state", "duration_ms", "encoding",
                           "sample_rate", "channels"}
                payload = {key: value for key, value in frame.payload.items()
                           if key in allowed and isinstance(value, (str, int, float, bool))}
                item["decoded"]={"type":frame.type,"sequence":frame.sequence,"payload":payload}
        except ValueError as exc: item["error"]=str(exc)
        self.raw_frames.append(item)

    def _device_write(self, message_type: str, payload: dict[str,Any]) -> bytes:
        self.device_sequence+=1; raw=encode_frame(message_type,self.device_sequence,payload); self.pty.write_device(raw); return raw

    def _receive_frames_sync(self) -> list[HardwareFrame]:
        for _ in range(20):
            frames=self.adapter.receive_available()
            if frames: return frames
            time.sleep(.001)
        return []

    def _drain_device_commands(self):
        data=self.pty.read_device_available()
        if data: self._device_buffer+=data
        while b"\n" in self._device_buffer:
            raw,self._device_buffer=self._device_buffer.split(b"\n",1)
            try:
                frame=decode_frame(raw+b"\n"); payload=dict(frame.payload)
                if frame.type=="audio_play":
                    response_id = str(payload.get("response_id") or "")
                    response=self.responses.get(response_id,{})
                    story=self.stories.get(str(payload.get("response_id")),{})
                    if response:
                        text=str(response.get("text","")); session=self.application.sessions.get(self.active_session_id)
                        if session and session.child_id and "{name}" in text:
                            profile = self.application._canonical_profiles.get(self.active_session_id) or {}; text = text.replace("{name}", str(profile.get("display_name", "")) if self.application._identity_scope_live(self.active_session_id, "personalize") else "")
                        payload["resolved_text"]=text
                        payload["content_kind"]="response"
                        # Static response ids are metadata/telemetry at the
                        # UART boundary.  Browser playback must use the same
                        # bounded Silero/PCM path as conversational audio;
                        # otherwise the event looks successful but produces
                        # no sound because the console deliberately ignores
                        # non-story resolved_text commands.
                        self._schedule_response_audio(self.active_session_id, text)
                    elif story:
                        payload["resolved_text"]=str(story.get("text",""))
                        payload["asset_path"] = str(payload.get("asset_id") or story.get("audio_file") or "")
                        payload["content_kind"]="story"
                    elif str(payload.get("response_id", "")).startswith("placeholder:fixed_story:"):
                        payload["asset_path"] = str(payload.get("asset_id") or "")
                        payload["content_kind"] = "story"
                    elif str(payload.get("response_id", "")).startswith("PHRASE_"):
                        phrase = self.application.voice_manifest.resolve(str(payload["response_id"]))
                        if phrase:
                            payload["asset_path"] = str(phrase["asset_path"])
                            payload["content_kind"] = "phrase"
                    elif str(payload.get("response_id", "")) == "RINGTONE":
                        payload["asset_path"] = "RINGTONE"
                        payload["content_kind"] = "asset"
                if frame.type in {"audio_pause", "audio_resume"}:
                    self._schedule_notification(
                        self.hub.publish({"type": frame.type, "payload": dict(frame.payload)}),
                        operation=f"{frame.type}_notification", session_id=self.active_session_id,
                    )
                if frame.type=="audio_stop":
                    self._cancel_story_audio()
                    self._cancel_response_audio()
                    self.playback._generation += 1
                    self._schedule_notification(
                        self.hub.publish({"type":"audio_stop","generation":self.playback._generation}),
                        operation="audio_stop_notification", session_id=self.active_session_id,
                    )
                # Story playback has one browser-owned path: the console fetches
                # the allowlisted WAV endpoint for the audio_play command.  Do not
                # also synthesize and publish the same story as PCM here; that
                # created deterministic double playback in child mode.
                self.commands.append({"at":time.time(),"type":frame.type,"sequence":frame.sequence,"payload":payload})
            except ValueError as exc: self.errors.append({"at":time.time(),"stage":"device_command","error":str(exc)})

    async def _publish_canonical_state_update(self, _session_id: str, _result: dict[str, Any]) -> None:
        # Canonical timers and browser audio callbacks enter the application
        # without going through inject().  Flush their UART effects here so a
        # timer-fired phrase is visible to the WebPanel immediately.
        async with self._transport_lock:
            await self.adapter.flush_async(timeout=1.0)
            self._drain_device_commands()
        self._collect_events()
        await self.hub.publish({"type": "stand_state", "state": self.panel_snapshot()})

    async def _publish_audio_completion(self, session_id: str, payload: dict[str, Any]) -> None:
        public = self._public_result(payload)
        self.results.append({"at":time.time(),"input":"audio_completion","result":public})
        failures = await self.hub.publish({"type":"stand_state","state":self.panel_snapshot(),"audio_completion":public})
        if failures:
            recorder = getattr(self.application, "_record_audio_completion_diagnostic", None)
            if callable(recorder):
                first = failures[0]
                recorder(
                    "audio_update_hook_error", session_id=session_id,
                    correlation_id=payload.get("correlation_id"),
                    operation="audio_update_hook", hook_name="StandHub.publish",
                    exception_type=first.get("exception_class"),
                    reason=first.get("reason"), mandatory=False,
                    chunk_index=payload.get("chunk_index"), token=payload.get("token"),
                )

    async def inject(self, message_type: str, payload: dict[str,Any], session_id: str | None = None, *, wait_for_audio: bool = True, _ingress_guard=None):
        sid = session_id or self.active_session_id
        if message_type == "audio_pcm":
            _ingress_guard = _ingress_guard or self._audio_owner_guard(sid)
            if not _ingress_guard():
                return {"reason": "stale_session_generation", "outputs": []}
        if self.application.max_json:
            boundary = self.application._max_client_boundary
            if boundary is None or sid != boundary.session_id:
                return {"error": {"code": "validation_error", "message": "max_json requires its owned session and no RFID ingress"}}
        requested_session_id = session_id
        started=time.perf_counter()
        result=None
        # Text input is a hard browser-audio boundary.  A static response
        # scheduled by the previous device event may still be in the detached
        # response task even after its application turn completed.  Cancel that
        # owned task before dispatching the new input so it cannot enter
        # BoundedConversationEngine.speak_text() and invalidate the foreground
        # text generation.
        if message_type == "text":
            self._cancel_response_audio()
        try:
            async with self._transport_lock:
                if _ingress_guard is not None and not _ingress_guard():
                    return {"reason": "stale_session_generation", "outputs": []}
                await self._offload_uart(self._device_write, message_type, payload)
                frames=await self._offload_uart(self._receive_frames_sync)
            if not frames: raise RuntimeError("serial adapter received no frame from PTY")
            for frame in frames: result=await self._dispatch(frame, sid, wait_for_audio=wait_for_audio, _ingress_guard=_ingress_guard)
            if _ingress_guard is not None and not _ingress_guard():
                return {"reason": "stale_session_generation", "outputs": []}
            # Do not let a rejected concurrent session steal the stand's
            # active-session cursor.  Otherwise a follow-up handset event
            # could be routed to the rejected id and bypass the installation
            # lock as anonymous input.
            if requested_session_id and not (isinstance(result, dict) and result.get("reason") == "installation_busy"):
                self.active_session_id = requested_session_id
        except Exception as exc:
            self.errors.append({"at":time.time(),"stage":"uart_ingress","error":type(exc).__name__,"detail":str(exc)[:200]})
            if isinstance(exc, EventValidationError):
                result={"error":{"type":type(exc).__name__,"code":"validation_error","message":str(exc)}}
            elif isinstance(exc, AudioInputError):
                result={"error":{"type":"audio_input","code":exc.code,"message":str(exc)}}
            else:
                result={"error":{"type":type(exc).__name__,"code":"internal_error","message":"stand input processing failed"}}
        try:
            await self.adapter.flush_async(timeout=1.0)
        except Exception as exc:
            self.errors.append({"at":time.time(),"stage":"uart_egress","error":type(exc).__name__,"detail":str(exc)[:200]})
            result={"error":{"type":type(exc).__name__,"code":"internal_error","message":"stand output processing failed"}}
        async with self._transport_lock:
            self._drain_device_commands()
        self._collect_events()
        quiet_pcm = (
            message_type == "audio_pcm"
            and isinstance(result, dict)
            and result.get("reason") in {"collecting", "audio_session_required"}
        )
        if requested_session_id and (_ingress_guard is None or _ingress_guard()) and not (isinstance(result, dict) and result.get("reason") == "installation_busy"):
            self.active_session_id = requested_session_id
        is_transport_error = isinstance(result, dict) and isinstance(result.get("error"), dict)
        if not quiet_pcm:
            self.latencies.append({"at":time.time(),"operation":message_type,"ms":round((time.perf_counter()-started)*1000,2)})
            if result is not None: self.results.append({"at":time.time(),"input":message_type,"result":self._public_result(result)})
            if not is_transport_error:
                await self.hub.publish({"type":"stand_state","state":self.panel_snapshot()})
        return result

    async def inject_raw(self, raw: bytes):
        sid = self.active_session_id
        ingress_guard = self._audio_owner_guard(sid)
        if not raw.endswith(b"\n"): raw+=b"\n"
        started=time.perf_counter()
        try:
            async with self._transport_lock:
                await self._offload_uart(self.pty.write_device, raw)
                frames=await self._offload_uart(self._receive_frames_sync)
            results=[await self._dispatch(frame, sid, _ingress_guard=ingress_guard) for frame in frames]
            if frames: self.device_sequence=max(self.device_sequence,max(frame.sequence for frame in frames))
            if not frames: raise RuntimeError("no complete UART frame")
            result=results[-1] if results else None
        except EventValidationError as exc:
            self.errors.append({"at":time.time(),"stage":"raw_uart","error":type(exc).__name__,"detail":str(exc)[:200]})
            result={"error":{"type":type(exc).__name__,"code":"validation_error","message":str(exc)}}
        except (binascii.Error, TypeError, ValueError, struct.error) as exc:
            self.errors.append({"at":time.time(),"stage":"raw_uart","error":type(exc).__name__,"detail":str(exc)[:200]})
            result={"error":{"type":type(exc).__name__,"code":"invalid_frame","message":"raw UART frame is invalid"}}
        except Exception as exc:
            self.errors.append({"at":time.time(),"stage":"raw_uart","error":type(exc).__name__,"detail":str(exc)[:200]})
            result={"error":{"type":type(exc).__name__,"code":"internal_error","message":"stand raw input processing failed"}}
        try:
            await self.adapter.flush_async(timeout=1.0)
        except Exception as exc:
            self.errors.append({"at":time.time(),"stage":"uart_egress","error":type(exc).__name__,"detail":str(exc)[:200]})
            result={"error":{"type":type(exc).__name__,"code":"internal_error","message":"stand output processing failed"}}
        async with self._transport_lock:
            self._drain_device_commands()
        self._collect_events()
        self.latencies.append({"at":time.time(),"operation":"raw_uart","ms":round((time.perf_counter()-started)*1000,2)})
        await self.hub.publish({"type":"stand_state","state":self.panel_snapshot()})
        return result

    async def _dispatch(self, frame: HardwareFrame, session_id: str | None = None, *, wait_for_audio: bool = True, _ingress_guard=None):
        sid = session_id or self.active_session_id
        if frame.type=="capabilities":
            self.adapter.capabilities=dict(frame.payload); self.adapter.firmware_version=str(frame.payload.get("firmware","")); return {"capabilities":frame.payload}
        if frame.type=="heartbeat": return {"heartbeat":True}
        if frame.type=="audio_pcm": return await self._dispatch_pcm(frame, sid, wait_for_turn=wait_for_audio, _ingress_guard=_ingress_guard)
        event_payload={"type":frame.type,**frame.payload}
        event=parse_event(event_payload,sid,frame.sequence,"uart_pty")
        return await self.application.receive_async(event,frame.payload.get("text") if frame.type=="text" else None)

    async def _dispatch_pcm(self, frame: HardwareFrame, session_id: str | None = None, *, wait_for_turn: bool = True, _ingress_guard=None):
        sid = session_id or self.active_session_id
        _ingress_guard = _ingress_guard or self._audio_owner_guard(sid)
        payload=frame.payload
        if payload.get("encoding")!="f32le" or payload.get("channels")!=1 or payload.get("sample_rate")!=16000:
            raise ValueError("audio_pcm must be mono f32le at 16000 Hz")
        try: raw=base64.b64decode(str(payload.get("data_b64","")),validate=True)
        except Exception as exc: raise ValueError("invalid audio base64") from exc
        if not raw or len(raw)%4 or len(raw)>2048: raise ValueError("audio PCM frame must contain 1..512 float32 samples")
        samples=struct.unpack("<"+"f"*(len(raw)//4),raw)
        if not all(math.isfinite(value) and -1.0<=value<=1.0 for value in samples): raise ValueError("audio samples must be finite -1..1")
        lock = self._audio_ingress_locks.setdefault(sid, asyncio.Lock())
        async with lock:
            return await self.application.receive_audio_async(sid,tuple(samples),frame.sequence,wait_for_turn=wait_for_turn, _ingress_guard=_ingress_guard)

    async def inject_pcm(self, samples: tuple[float,...], sample_rate: int, session_id: str | None = None, flush_silence: bool = True, *, wait_for_turn: bool = True, _ingress_guard=None):
        sid = session_id or self.active_session_id
        _ingress_guard = _ingress_guard or self._audio_owner_guard(sid)
        if sample_rate not in AUDIO_SAMPLE_RATES:
            raise AudioInputError("unsupported_format", "sample rate is not supported")
        if not samples:
            raise AudioInputError("empty_audio", "audio payload is empty")
        if len(samples)>sample_rate*MAX_AUDIO_SECONDS:
            raise AudioInputError("oversized_audio", "audio exceeds 15 seconds")
        values = np.asarray(samples, dtype=np.float32)
        if not np.isfinite(values).all() or np.any(values < -1.0) or np.any(values > 1.0):
            raise AudioInputError("decode_error", "audio samples must be finite and within -1..1")
        if sample_rate!=16000:
            samples=await self._offload_audio(resample, tuple(float(value) for value in values), sample_rate, 16000)
            sample_rate=16000
        result=None
        turn_completed=False
        for offset in range(0,len(samples),512):
            chunk=samples[offset:offset+512]; raw=struct.pack("<"+"f"*len(chunk),*chunk)
            result=await self.inject("audio_pcm",{"encoding":"f32le","sample_rate":sample_rate,"channels":1,
                                     "data_b64":base64.b64encode(raw).decode()},sid,wait_for_audio=wait_for_turn, _ingress_guard=_ingress_guard)
            if not _ingress_guard():
                return {"reason": "stale_session_generation", "outputs": []}
            # A completed turn changes the scenario node. Stop feeding the
            # remainder of the same upload so it cannot be mistaken for a new
            # turn or overwrite the useful result with a stale tail frame.
            if isinstance(result,dict) and result.get("reason") in {"bounded_s2s_audio", "quick_reply_audio"}:
                turn_completed=True
                break
        if flush_silence and not turn_completed:
            silence=tuple(0.0 for _ in range(320))
            for _ in range(14):
                raw=struct.pack("<"+"f"*len(silence),*silence)
                result=await self.inject("audio_pcm",{"encoding":"f32le","sample_rate":16000,"channels":1,
                                         "data_b64":base64.b64encode(raw).decode()},sid,wait_for_audio=wait_for_turn, _ingress_guard=_ingress_guard)
                if not _ingress_guard():
                    return {"reason": "stale_session_generation", "outputs": []}
                if isinstance(result,dict) and result.get("reason") in {"bounded_s2s_audio","quick_reply_audio","s2s_return"}:
                    turn_completed=True
                    break
        return result

    def _collect_events(self):
        self._event_cursor=len(self.publisher.events)

    async def _publish_lighting_effect_frame(self, _frame: dict[str, Any]) -> None:
        # Background lighting transitions are canonical state changes too.
        # Publish them immediately so the WebPanel cannot remain stuck on the
        # first red wave stage or on the initial breathing phase.
        if self.hub.clients:
            await self.hub.publish({"type": "stand_state", "state": self.panel_snapshot()})

    async def reset(self, session_id: str | None = None):
        boundary = self.application._max_client_boundary
        if self.application.max_json:
            if boundary is None or (session_id is not None and session_id != boundary.session_id):
                return {"error": {"code": "validation_error", "message": "max_json reset requires its owned session"}}
            session_id = boundary.session_id
            # Fence before the Stand's own playback cleanup can yield.
            self.application._revoke_local_profile(session_id)
        self._cancel_story_audio()
        self._cancel_response_audio()
        await self.playback.stop()
        # Reset is an explicit installation-level operator action.  The UI
        # normally sends its stable session id ("stand"), while an API/WS
        # smoke test may have left another id active.  Reset both sides of
        # that binding and make the requested id the fresh cursor; otherwise
        # the next profile is rejected as installation_busy and the panel looks
        # personalized while the runtime is actually waiting for a profile.
        sid=session_id or self.active_session_id
        # The browser uses one stable id ("stand"), while API/WS smoke runs
        # may have created another id in the same installation. Reset is an
        # installation-level operator action: clear every orphan active
        # session before creating the fresh cursor.
        repository_items = getattr(self.application.sessions, "items", {})
        stale_ids = [
            current_id
            for current_id, current in list(repository_items.items())
            if current.installation_id == self.application.installation_id
            and current_id != sid
            and current.state not in {Lifecycle.ENDED, Lifecycle.IDLE}
        ]
        for stale_id in stale_ids:
            stale_result = await self.application.reset_session(stale_id)
            if stale_result.get("reset") is False:
                return stale_result
        result=await self.application.reset_session(sid)
        if result.get("reset") is False:
            return result
        self.active_session_id=sid
        self.results.clear(); self.errors.clear(); self.commands.clear(); self.latencies.clear()
        self.raw_frames.clear()
        self._complete_lighting_wave()
        reset_to_standby = getattr(self.application.led, "reset_to_standby", None)
        if callable(reset_to_standby):
            reset_to_standby(now=self.application.monotonic_now())
        else:
            self.lighting_output.clear_all()
        self.application.publisher.clear()
        metrics=getattr(getattr(self.application, "runtime_manager", None), "engine", None)
        if metrics is not None and hasattr(metrics, "metrics"):
            metrics.metrics.clear()
        self.adapter.send({"type":"audio_stop","payload":{"token":0}})
        self._drain_device_commands(); await self.hub.publish({"type":"stand_state","state":self.panel_snapshot()}); return result

    def _enabled_buttons(self, session):
        """Derive affordances from the canonical mode, never from obsolete JSON."""
        if session is None:
            return list(range(1, 8))
        mode = str(session.current_node or "idle")
        if mode == "fixed_story_menu":
            return [1, 2, 3, 4, 5, 7]
        if mode == "schedule_weather":
            return [1, 2, 3, 4, 7]
        if mode == "wellbeing_check":
            # 1/7 are the explicit no/yes controls; 5 repeats the question.
            return [1, 5, 7]
        if mode in {"session_closed", "safety_handoff"}:
            return []
        return list(range(1, 8))

    async def _publish_lighting_wave_stage(self, stage_index: int, commands) -> None:
        self._lighting_wave = {'active': True, 'stage': stage_index + 1, 'total_stages': len(READINESS_WAVE_GROUPS), 'elements': [command.element_id for command in commands]}
        await self.hub.publish({
            "type": "stand_state",
            "state": self.panel_snapshot(),
            "lighting_wave": dict(self._lighting_wave),
        })

    def _complete_lighting_wave(self) -> None:
        self._lighting_wave = {'active': False, 'stage': len(READINESS_WAVE_GROUPS), 'total_stages': len(READINESS_WAVE_GROUPS), 'elements': []}

    def _record_legacy_led(self, payload: dict[str, Any]) -> None:
        self.commands.append({
            "at": time.time(),
            "type": "led_pattern",
            "sequence": len(self.commands) + 1,
            "payload": dict(payload),
        })

    @staticmethod
    def _safe_trace_item(item: dict[str, Any]) -> dict[str, Any]:
        """Only project fixed protocol names and numeric timing metadata."""
        event_names = EVENT_TYPES | {
            "handset_up", "handset_down", "button", "text",
            "anonymous", "anonymous_start", "button_pressed",
            "button_released", "button_hold", "button_long_hold",
        }
        states = set(canonical_state_nodes()) | {"idle"}
        result: dict[str, Any] = {}
        for key in ("input", "normalized_event", "canonical_event"):
            if item.get(key) in event_names:
                result[key] = item[key]
        for key in ("state_before", "state_after"):
            if item.get(key) in states:
                result[key] = item[key]
        for key in ("monotonic_timestamp", "timer_started", "timer_deadline"):
            value = item.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                result[key] = value
        effects = item.get("effects")
        if isinstance(effects, list):
            safe_effects = {"speak", "speak_dynamic", "stop_audio", "play_story",
                            "led_pattern", "led", "audio", "display"}
            result["effects"] = [effect for effect in effects if effect in safe_effects]
        audio = item.get("audio")
        if isinstance(audio, str) and audio.startswith("PHRASE_") and audio[7:].isdigit():
            result["audio"] = audio
        return result

    @staticmethod
    def _safe_cleanup_failure(item: dict[str, Any]) -> dict[str, Any]:
        result = {"type": "cleanup_failed", "status": "failed"}
        enums = {"resource": {"audio", "lighting"},
                 "operation": {"exhibition_reset", "interaction_idle", "profile_ready_expiry"},
                 "exception_type": {"RuntimeError", "OSError", "ConnectionError",
                                    "TimeoutError", "ValueError", "TypeError", "AttributeError"}}
        for key, allowed in enums.items():
            value = item.get(key)
            result[key] = value if isinstance(value, str) and value in allowed else "unknown"
        return result

    @staticmethod
    def _safe_diagnostic_item(item: dict[str, Any]) -> dict[str, Any]:
        if item.get("type") == "cleanup_failed":
            return HardwareIntegrationStand._safe_cleanup_failure(item)
        allowed = {
            "kind", "code", "status", "component", "stage", "error_code",
            "content_id", "asset_ref", "operation", "source",
            "generation", "at_monotonic",
        }
        result = {key: item[key] for key in allowed if key in item}
        if item.get("kind") == "input_safety_decision":
            # Never expose arbitrary adapter strings through diagnostic fields.
            codes = {"deterministic", "semantic", "mock_deterministic",
                     "input_safety_uncertain", "input_safety_timeout",
                     "input_safety_invalid", "input_safety_unavailable"}
            result["code"] = item.get("code") if item.get("code") in codes else "unknown"
            if item.get("safety_severity") in {"none", "concern", "urgent"}:
                result["safety_severity"] = item["safety_severity"]
            for key in ("safety_verified", "stt_trusted"):
                if type(item.get(key)) is bool:
                    result[key] = item[key]
            latency = item.get("latency_ms")
            if type(latency) in (int, float) and math.isfinite(latency) and latency >= 0:
                result["latency_ms"] = latency
        return result

    @staticmethod
    def _safe_input_diagnostics(value: Any) -> dict[str, Any]:
        """Fixed, bounded HID evidence; never paths, tokens or arbitrary text."""
        if not isinstance(value, dict):
            return {}
        result = {"level_source": "last_observed_hid"}
        for key in ("ready", "reader_alive", "input_desynchronized",
                    "semantic_input_accepted", "physical_ready"):
            if type(value.get(key)) is bool:
                result[key] = value[key]
        for key in ("events", "raw_key_events", "errors", "unverified_events"):
            item = value.get(key)
            if type(item) is int and 0 <= item < 2 ** 63:
                result[key] = item
        status = value.get("status")
        if isinstance(status, str) and status in {"active", "stopped", "device_unavailable", "device_disconnected",
                      "reader_exception", "dispatch_failure", "input_desynchronized"}:
            result["status"] = status
        levels = value.get("pressed_buttons")
        if (isinstance(levels, (list, tuple)) and len(levels) <= 8
                and all(type(item) is int and item in (*range(1, 8), 57) for item in levels)):
            result["pressed_buttons"] = sorted({item for item in levels
                if type(item) is int and item in range(1, 8)})
            result["space_pressed"] = any(type(item) is int and item == 57 for item in levels)
        edge = value.get("last_raw_key")
        if (isinstance(edge, dict) and isinstance(edge.get("kind"), str)
                and edge["kind"] in {"press", "release", "repeat"}):
            control = edge.get("control")
            if isinstance(control, str) and control in {"button", "handset"}:
                safe = {"kind": edge["kind"], "control": control}
                if control == "button" and type(edge.get("button_id")) is int and edge["button_id"] in range(1, 8):
                    safe["button_id"] = edge["button_id"]
                at = edge.get("at_monotonic")
                if type(at) in (int, float) and 0 <= at <= 10 ** 15:
                    safe["at_monotonic"] = at
                result["last_raw_key"] = safe
        return result

    def _input_diagnostics(self) -> dict[str, Any]:
        return self._safe_input_diagnostics(_hid_diagnostics(self.input_adapter))

    def _lighting_diagnostics(self) -> dict[str, Any]:
        runtime = (self.lighting_serial.diagnostics() if self.lighting_serial is not None
                   else dict(getattr(self, "_lighting_status", {})))
        output_diagnostics = getattr(self.lighting_output, "diagnostics", None)
        if callable(output_diagnostics):
            output = output_diagnostics()
            for key in ("output_ready", "generation_failed", "pending_batches", "in_flight",
                        "max_pending_batches", "submitted_batches", "written_batches",
                        "dropped_batches", "failed_batches", "overflow_batches", "generation",
                        "last_failure", "dark_recovery_attempts", "dark_recovery", "fault_latched"):
                if key in output:
                    runtime[key] = output[key]
        runtime["owner_fault_latched"] = getattr(self, "_lighting_fault", None) is not None
        runtime["state_kind"] = "desired_targets"
        # A completed host write is not a firmware acknowledgement or optical
        # measurement. Do not claim observed physical brightness from targets.
        runtime["firmware_acknowledged"] = False
        return runtime

    def physical_hardware_ready(self) -> bool:
        """Live host-side controls/audio readiness, not acoustic/firmware acceptance."""
        if self.hardware_mode != "physical" or self._closed or self._physical_output_faulted():
            return False
        try:
            hid = _hid_diagnostics(self.input_adapter)
            return bool(
                self._hid_status.get("state") == "active"
                and hid.get("physical_ready")
                and self.lighting_serial is not None
                and self._lighting_diagnostics().get("ready")
                and self._lighting_diagnostics().get("output_ready", True)
                and self.playback.readiness().get("ready")
                and self.audio_input is not None
                and self.audio_input.is_ready()
                and self._headless_audio_status.get("state") == "active"
            )
        except Exception:
            return False

    def admission_dependencies_ready(self) -> bool:
        # Only the explicit live-demo profile gains this new hardware gate.
        # It does not depend on RFID availability: anonymous use is supported.
        return (self.settings.max_client_source != "local_demo"
                or self.hardware_mode != "physical"
                or self.physical_hardware_ready())

    def health_snapshot(self) -> dict[str, Any]:
        """Bounded, read-only operational contract with no session content."""
        state = dict(getattr(self.application, "last_canonical_state", {}) or {})
        mode = str(state.get("mode") or "idle")
        handset = "up" if state.get("handset") == "up" else "down"
        audio = dict(self.playback.readiness()) if hasattr(self.playback, "readiness") else {"ready": True}
        audio_state = "available" if audio.get("ready") else "unavailable"
        if self.hardware_mode != "physical" and audio.get("ready"):
            audio_state = "simulated"
        hid_status = dict(getattr(self, "_hid_status", {}) or {})
        hid_state = str(hid_status.get("state") or "unavailable")
        try:
            live_hid = _hid_diagnostics(self.input_adapter) if self.hardware_mode == "physical" else {}
        except Exception:
            live_hid = {}
        if self.hardware_mode != "physical":
            hid_state = "simulated"
        lighting_state = str(getattr(self, "_lighting_status", {}).get("state") or "unavailable")
        lighting_runtime = self._lighting_diagnostics()
        if self.hardware_mode == "physical" and self.lighting_serial is not None and (
                not lighting_runtime.get("ready", False) or not lighting_runtime.get("output_ready", True)
                or lighting_runtime.get("owner_fault_latched", False)):
            # A serial adapter can open successfully and fail later while a
            # USB CDC device is busy.  Readiness must reflect the live
            # transport, otherwise the panel claims production readiness
            # while effects are only being retained in memory.
            lighting_state = "unavailable"
        report = self.scenarios.report() if hasattr(self, "scenarios") else {}
        voice = report.get("voice", {}) if isinstance(report, dict) else {}
        errors = int(voice.get("summary", {}).get("error_count", 0)) if isinstance(voice, dict) else 0
        warnings = int(voice.get("summary", {}).get("warning_count", 0)) if isinstance(voice, dict) else 0
        content_status = "invalid" if errors else "warning" if warnings or int(voice.get("approved_recorded_count", 0)) == 0 else "valid"
        degraded = self._startup_state == "degraded" or any(value == "unavailable" for value in (audio_state, hid_state, lighting_state)) or (self.hardware_mode == "physical" and hid_state != "active")
        last_error = None
        if self._startup_diagnostics:
            last_error = self._startup_diagnostics[-1].get("code")
        if last_error is None and self.errors:
            last_error = self.errors[-1].get("error_code") or self.errors[-1].get("stage")
        if last_error is None and lighting_state == "unavailable" and self.hardware_mode == "physical":
            last_error = "lighting_unavailable"
        local_demo = self.settings.max_client_source == "local_demo"
        hardware_ready = self.physical_hardware_ready()
        runtime_admitted = bool(self._admits_physical_users and self._admits_physical_users())
        if local_demo:
            degraded = degraded or not runtime_admitted
        return {
            "status": "degraded" if degraded else "ready",
            "physical_hardware_ready": hardware_ready,
            **({"software_runtime_usable": runtime_admitted,
                "production_ready": False,
                "identity": self.application.identity_status(),
                "production_blockers": ["synthetic_demo_data", "max_client_production_acceptance_pending"]}
               if local_demo else {}),
            "mode": self.settings.runtime_mode,
            "canonical_state": mode,
            "handset": handset,
            "audio": {"status": audio_state, "provider": audio.get("provider")},
            "hid": {"status": hid_state, "generation": int(hid_status.get("generation") or 0), "semantic_input_accepted": bool(hid_status.get("semantic_input_accepted", self.hardware_mode != "physical" or hid_state == "active")), "initial_level_sync": hid_status.get("initial_level_sync", {"status": "simulated" if self.hardware_mode != "physical" else "not_attempted"}), "physical_ready": bool(self.hardware_mode == "physical" and hid_state == "active" and live_hid.get("physical_ready", False)), "restart_allowed": bool(self.hardware_mode == "physical" and hid_state in {"device_unavailable", "initial_level_unavailable", "device_disconnected", "input_desynchronized", "reader_exception", "dispatch_failure", "stopped"})},
            "lighting": {"status": lighting_state},
            "content": {
                "status": content_status,
                "catalog_version": voice.get("catalog_version"),
                "valid_count": voice.get("valid_count"),
                "approved_count": voice.get("approved_count", voice.get("approved_recorded_count")),
                "pending_count": len(voice.get("pending_approval", [])),
                "placeholder_count": voice.get("status_counts", {}).get("placeholder", 0),
                "error_count": errors,
                "warning_count": warnings,
                "simulator_usable": voice.get("simulator_usable"),
                "catalog_technically_valid": voice.get("catalog_technically_valid"),
                "physical_audio_verified": False,
                "production_ready": False,
                "required_exact_contract": [
                    {
                        key: item[key]
                        for key in ("requirement", "phrase_id", "status", "asset_exists", "approved", "exact")
                        if isinstance(item, dict) and key in item
                    }
                    for item in voice.get("required_exhibition_contract", [])
                    if isinstance(item, dict)
                ],
            },
            "config": self.settings.operational_snapshot(),
            "session": {
                "active": bool(handset == "up"),
                "generation": int(state.get("state_version") or 0),
            },
            "diagnostics": {
                "last_error_code": last_error,
                "startup_diagnostics": [self._safe_diagnostic_item(item) for item in list(self._startup_diagnostics)[-10:]],
            },
        }

    def diagnostics_snapshot(self) -> dict[str, Any]:
        """Read-only bounded trace projection for WebPanel/CI smoke."""
        trace = [self._safe_trace_item(item) for item in list(getattr(self.application, "canonical_trace", ()))[:-51:-1]]
        diagnostics = [self._safe_diagnostic_item(item) for item in list(getattr(self.application, "canonical_diagnostics", ()))[:-51:-1]]
        diagnostic_items = list(reversed(diagnostics))
        diagnostic_items.append({"connection": self.hub.lease_snapshot()})
        return {
            "health": self.health_snapshot(),
            "trace": list(reversed(trace)),
            "diagnostics": diagnostic_items,
            "lighting_hardware": self._lighting_diagnostics(),
            "input_hardware": self._input_diagnostics(),
            # Bounded and redacted, but lossless for every physical lighting ID.
            "physical_lighting": dict(self.lighting_output.snapshot()),
        }

    def snapshot(self):
        # Freeze bounded buffers before Python-level redaction: UART callbacks
        # may append while a projection runs. Never iterate a live deque here.
        session=self.application.sessions.get(self.active_session_id)
        lighting = self.lighting_output.snapshot()
        # Lossless physical projection. The led field below is intentionally
        # only a UI/menu compatibility projection and must not be used as
        # hardware state because it aggregates away the physical channel layout.
        physical_lighting = {str(element_id): int(lighting[str(element_id)]) for element_id in LIGHTING_ELEMENT_IDS}
        led_projection = canonical_led_state_from_lighting(lighting)
        input_diagnostics = self.input_adapter.diagnostics() if self.input_adapter is not None else {"backend": "hid" if self.hardware_mode == "physical" else "fake", "ready": self.hardware_mode != "physical"}
        input_diagnostics["stand_status"] = dict(getattr(self, "_hid_status", {"state": "not_started"}))
        if self._input_start_error is not None:
            input_diagnostics["startup_error"] = dict(self._input_start_error)
        audio_readiness = dict(self.playback.readiness()) if hasattr(self.playback, "readiness") else {"ready": True, "backend": "browser"}
        audio_input = dict(self.audio_input.diagnostics()) if self.audio_input is not None else {"ready": True, "backend": "browser", "direction": "input", "state": "webpanel"}
        audio_input["stand_status"] = dict(self._headless_audio_status)
        audio_input["dropped_frames"] = int(self._headless_audio_dropped)
        audio_readiness["runtime_diagnostics"] = [
            self._safe_diagnostic_item(item) for item in
            list(getattr(self.application, "audio_runtime_diagnostics", ()))[-20:]
            if isinstance(item, dict)
        ]
        audio_readiness["active_playback"] = next((item for item in reversed(audio_readiness["runtime_diagnostics"])
                                                    if item.get("kind") == "audio_playback_started"), None)
        audio_readiness["last_audio_error"] = next((item for item in reversed(audio_readiness["runtime_diagnostics"])
                                                    if item.get("kind") in {"audio_playback_failed", "audio_dispatch_failed", "recorded_phrase_unavailable"}), None)
        identity_status = self.application.identity_status() if hasattr(self.application, "identity_status") else {"identity_ready": False, "reader_ready": False}
        return {"identity": identity_status, "identity_ready": bool(identity_status.get("identity_ready")), "reader_ready": bool(identity_status.get("reader_ready")),
                "transport":{"kind":"pty_uart_jsonl_crc32","slave_path":self.pty.slave_path,
                             "protocol_version":"1","story_audio_mode":"silero_wav","hardware":self.adapter.health()},
                "session":{"session_id":self.active_session_id,"state":session.state.value if session else "NONE",
                           "node":session.current_node if session else None,"scenario":session.scenario_id if session else None,
                           "handset_up":session.handset_up if session else False,
        "enabled_buttons":self._enabled_buttons(session),
                           "anonymous":session.anonymous if session else None,"retries":session.retries if session else 0},
                "visual_only": session.visual_only if session else False,
                "raw_frames":[{"at":item.get("at"), "direction":item.get("direction"),
                               "raw":"[UART payload omitted from diagnostics]",
                               "decoded":{"type":(item.get("decoded") or {}).get("type"),
                                          "sequence":(item.get("decoded") or {}).get("sequence"),
                                          "payload":self._panel_payload((item.get("decoded") or {}).get("payload"))}}
                              for item in tuple(self.raw_frames)],
                "commands":[{"at":item.get("at"), "type":item.get("type"),
                             "sequence":item.get("sequence"),
                             "payload":self._panel_payload(item.get("payload"))}
                            for item in tuple(self.commands)],
                "events":[{key: item[key] for key in ("type", "event_type", "sequence", "timestamp", "source")
                           if key in item} for item in self.publisher.events[-300:] if isinstance(item, dict)],
                "results":list(self.results),
                # lighting is canonical; led is UI compatibility derived
                # from it, never from legacy led_pattern commands.
                "led":led_projection,
                "lighting":lighting,
                "physical_lighting":physical_lighting,
                "lighting_wave": dict(self._lighting_wave),
                "lighting_effects": (self.application.led.effect_snapshot() if callable(getattr(self.application.led, "effect_snapshot", None)) else {}),
                "hardware": {"transport_mode": self.hardware_mode, "lighting": self._lighting_diagnostics(), "input": input_diagnostics, "audio": audio_readiness, "audio_input": audio_input, "acceptance": "physical" if self.hardware_mode == "physical" else "simulator"},
                "button_contract":button_contract(),
                "latencies":list(self.latencies),"pipeline_metrics":self.application.runtime_manager.engine.metrics.snapshot(),
                "graph":self.graph,
                "errors":[self._safe_diagnostic_item(item) for item in tuple(self.errors) if isinstance(item, dict)],
                "profile":self.settings.profile,
                "canonical_state":dict(getattr(self.application, "last_canonical_state", {}) or {}),
                "last_canonical_event":{key: value for key, value in
                    (getattr(self.application, "last_canonical_event", {}) or {}).items()
                    if key in {"type", "sequence", "timestamp", "source"}},
                "effects":[self._panel_payload(item) for item in
                    (getattr(self.application, "last_canonical_effects", []) or [])],
                "canonical_trace":[self._safe_trace_item(item) for item in
                    tuple(getattr(self.application, "canonical_trace", ()) or ()) if isinstance(item, dict)],
                "canonical_diagnostics":[self._safe_diagnostic_item(item) for item in
                    tuple(getattr(self.application, "canonical_diagnostics", ()) or ()) if isinstance(item, dict)],
                "audio_status":next((item.get("audio_status") for item in reversed(getattr(self.application, "last_canonical_effects", []) or []) if item.get("audio_status")), "idle")}

    @classmethod
    def _public_result(cls, value: Any) -> dict[str, Any]:
        """One allowlist for stored results and WebSocket completions."""
        if not isinstance(value, dict):
            return {}
        allowed = {
            "session_id", "state", "state_version", "reason",
            "audio_status", "pending", "barge_in", "correlation_id",
        }
        result = {key: value[key] for key in allowed if key in value
                  and isinstance(value[key], (str, int, float, bool, type(None)))}
        state = value.get("canonical_state")
        if isinstance(state, dict):
            result["canonical_state"] = {
                key: state[key] for key in (
                    "mode", "state_version", "handset", "audio_status",
                    "safety_severity", "incident_id",
                ) if key in state
            }
        if isinstance(value.get("outputs"), list):
            result["outputs"] = [
                cls._panel_payload(item) for item in value["outputs"][-20:]
                if isinstance(item, dict)
            ]
        error = value.get("error")
        if isinstance(error, dict) and isinstance(error.get("code"), str):
            result["error"] = {"code": error["code"]}
        return result

    @staticmethod
    def _panel_payload(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        if value.get("type") == "cleanup_failed":
            return HardwareIntegrationStand._safe_cleanup_failure(value)
        allowed = {
            "button_id", "node_id", "from", "to", "reason", "status",
            "response_id", "session_id", "playback_lease", "playback_id", "token", "offset_ms", "duration_ms",
            "phrase_id", "content_kind", "asset_id", "asset_path",
            "audio_status", "asset_status", "effect",
            "pattern", "state", "generation", "sequence", "source",
            "action", "type", "resolution", "targets", "pcm_bytes",
        }
        result = {key: item for key, item in value.items() if key in allowed}
        fallback = value.get("fallback_reason")
        if isinstance(fallback, str) and fallback in _DIALOGUE_FALLBACK_CODES:
            result["fallback_reason"] = fallback
        return result

    @staticmethod
    def _panel_metrics(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, Any] = {}
        stages = {"llm", "stt", "queue", "silero_tts", "rvc_v2", "playback",
                  "warmup_silero_vad", "warmup_faster_whisper", "warmup_llm",
                  "warmup_silero", "warmup_rvc_v2"}
        for key in stages:
            item = value.get(key)
            if isinstance(item, dict):
                result[key] = {field: item[field] for field in (
                    "count", "last_ms", "max_ms", "avg_ms", "current_sample_count",
                    "failures", "evicted_sample_count", "limit",
                ) if _diagnostic_number(item.get(field))}
        for key in ("chunk_trace_evicted_count", "chunk_trace_limit"):
            if _diagnostic_number(value.get(key)):
                result[key] = value[key]
        if isinstance(value.get("events"), list):
            events = []
            for event in value["events"][-80:]:
                if not isinstance(event, dict):
                    continue
                name = event.get("event")
                if not isinstance(name, str) or name not in _PIPELINE_EVENTS:
                    continue
                projected = {"event": name}
                for field in ("at", "timestamp", "token_index", "chunk_index"):
                    if _diagnostic_number(event.get(field)):
                        projected[field] = event[field]
                if name in {"validation_block", "validation_error", "validation_failure"}:
                    projected["reason"] = _validation_reason_code(event.get("reason"))
                    source = event.get("source")
                    projected["source"] = source if isinstance(source, str) and source in _VALIDATION_SOURCES else "unknown"
                    for field in ("latency_ms", "timeout_ms"):
                        if _diagnostic_number(event.get(field)):
                            projected[field] = event[field]
                events.append(projected)
            result["events"] = events
        # Configuration references, voice-turn IDs and arbitrary scalar keys
        # are intentionally not promoted to public diagnostics.
        return result

    @staticmethod
    def _panel_fallback_effect(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        if value.get("type") == "cleanup_failed":
            return HardwareIntegrationStand._safe_cleanup_failure(value)
        result = {}
        for key in ("type", "action"):
            if isinstance(value.get(key), str) and value[key] in {
                "bounded_dialogue_turn", "speak", "audio_retry", "dialogue_fallback",
            }:
                result[key] = value[key]
        if value.get("phrase_id") in ("PHRASE_095", "PHRASE_104"):
            result["phrase_id"] = value["phrase_id"]
        if value.get("resolution") in ("bounded_dialogue_completed", "bounded_dialogue_fallback",
                                        "bounded_dialogue_unavailable_visual_only"):
            result["resolution"] = value["resolution"]
        fallback = value.get("fallback_reason")
        if isinstance(fallback, str) and fallback in _DIALOGUE_FALLBACK_CODES:
            result["fallback_reason"] = fallback
        reason = value.get("reason")
        if isinstance(reason, str) and reason in _DIALOGUE_FALLBACK_CODES:
            result["reason"] = reason
        return result

    @classmethod
    def _panel_result_item(cls, item: Any) -> dict[str, Any]:
        if not isinstance(item, dict):
            return {}
        # inject/audio_completion store {at, input, result}; do not mistake
        # the envelope for its actual result, or publish its raw nested value.
        payload = item.get("result") if isinstance(item.get("result"), dict) else item
        result = {}
        operation = item.get("input")
        if isinstance(operation, str) and operation in {
            "audio_completion", "audio_pcm", "handset", "button", "button_released",
            "button_hold", "button_long_hold", "text", "reset",
        }:
            result["input"] = operation
        for key in ("at", "sequence"):
            if _diagnostic_number(item.get(key)):
                result[key] = item[key]
        reason = payload.get("reason")
        if isinstance(reason, str) and reason in _DIALOGUE_FALLBACK_CODES | {
            "dialogue_turn", "dialogue_fallback", "dialogue_fallback_ignored",
            "bounded_s2s_audio", "audio_retry", "audio_processing", "s2s_return",
        }:
            result["reason"] = reason
        if payload.get("state") in tuple(state.value for state in Lifecycle):
            result["state"] = payload["state"]
        result.update(cls._panel_fallback_effect(payload))
        if isinstance(payload.get("outputs"), list):
            result["outputs"] = [projection for output in payload["outputs"][-20:]
                                 if (projection := cls._panel_fallback_effect(output))]
        return result

    def panel_snapshot(self) -> dict[str, Any]:
        """Bounded/redacted state projection for WebPanel observers."""
        source = self.snapshot()
        session = source.get("session") if isinstance(source.get("session"), dict) else {}
        canonical = source.get("canonical_state") if isinstance(source.get("canonical_state"), dict) else {}
        canonical_safe = {key: canonical[key] for key in (
            "mode", "handset", "expected_inputs", "node", "onboarding_step",
            "menu_tutorial_step", "active_effect", "lighting_effect",
        ) if key in canonical}
        # The operator guide needs the actual exit choice, not arbitrary
        # internal substate strings or private model/profile metadata.
        if (canonical.get("mode") == "game_active"
                and canonical.get("substate") == "game_exit_confirmation"):
            canonical_safe["substate"] = "game_exit_confirmation"

        def event_item(item):
            if not isinstance(item, dict):
                return {}
            result = {key: item[key] for key in ("type", "event_type", "sequence", "timestamp", "at") if key in item}
            payload = self._panel_payload(item.get("payload"))
            if payload:
                result["payload"] = payload
            return result

        def command_item(item):
            if not isinstance(item, dict):
                return {}
            result = {key: item[key] for key in ("type", "sequence", "at") if key in item}
            payload = item.get("payload")
            if isinstance(payload, dict):
                allowed = {"response_id", "session_id", "playback_lease",
                           "generation", "playback_id", "token", "offset_ms", "duration_ms", "content_kind", "asset_id", "asset_path",
                           "audio_status", "asset_status", "story_audio_mode"}
                result["payload"] = {key: payload[key] for key in allowed if key in payload}
            return result

        def result_item(item):
            return self._panel_result_item(item)

        return {
            "identity": dict(source.get("identity") or {}),
            "identity_ready": bool(source.get("identity_ready")),
            "reader_ready": bool(source.get("reader_ready")),
            "transport": {"kind": source.get("transport", {}).get("kind"), "story_audio_mode": source.get("transport", {}).get("story_audio_mode"), "hardware": {"connected": source.get("transport", {}).get("hardware", {}).get("connected")}},
            "session": {key: session[key] for key in ("state", "node", "scenario", "handset_up", "enabled_buttons", "visual_only", "retries") if key in session},
            "visual_only": bool(source.get("visual_only")),
            "raw_frames": [{"direction": item.get("direction"), "decoded": {key: item.get("decoded", {}).get(key) for key in ("type", "sequence") if isinstance(item.get("decoded"), dict) and key in item.get("decoded", {})}, "error": item.get("error")} for item in source.get("raw_frames", [])[-80:] if isinstance(item, dict)],
            "led": dict(source.get("led") or {}),
            "commands": [command_item(item) for item in source.get("commands", [])[-80:]],
            "events": [event_item(item) for item in source.get("events", [])[-100:]],
            "results": [result_item(item) for item in source.get("results", [])[-50:]],
            "lighting": dict(source.get("lighting") or {}),
            "physical_lighting": dict(source.get("physical_lighting") or source.get("lighting") or {}),
            "lighting_hardware": dict((source.get("hardware") or {}).get("lighting") or {}),
            "input_hardware": self._safe_input_diagnostics((source.get("hardware") or {}).get("input")),
            "audio_input": dict((source.get("hardware") or {}).get("audio_input") or {}),
            "lighting_wave": dict(source.get("lighting_wave") or {}),
            "lighting_effects": dict(source.get("lighting_effects") or {}),
            "button_contract": source.get("button_contract"),
            "latencies": [{key: item[key] for key in ("operation", "ms", "at") if key in item} for item in source.get("latencies", [])[-50:] if isinstance(item, dict)],
            "pipeline_metrics": self._panel_metrics(source.get("pipeline_metrics")),
            "graph": {"contract": source.get("graph", {}).get("contract"), "nodes": list(source.get("graph", {}).get("nodes", []))},
            "errors": [{key: item[key] for key in ("stage", "error_code", "at") if key in item} for item in source.get("errors", [])[-30:] if isinstance(item, dict)],
            "canonical_state": canonical_safe,
            "last_canonical_event": {key: source.get("last_canonical_event", {}).get(key) for key in ("type", "sequence", "source", "at") if key in source.get("last_canonical_event", {})},
            "effects": [result_item(item) for item in source.get("effects", [])[-30:]],
            "canonical_trace": [self._safe_trace_item(item) for item in source.get("canonical_trace", [])[-50:] if isinstance(item, dict)],
            "audio_status": source.get("audio_status", "idle"),
        }

    async def aclose(self, *, hardware_stopped: bool = False):
        """Cancel loop-owned tasks on the loop; release blocking workers off it."""
        if getattr(self, "_closed", False):
            return
        identity_error = None
        shutdown_identity = getattr(self.application, "shutdown_identity", None)
        try:
            if callable(shutdown_identity):
                await shutdown_identity()
        except BaseException as error:
            identity_error = error
        try:
            led = getattr(getattr(self, "application", None), "led", None)
            cancel_effects = getattr(led, "cancel_effects", None)
            if callable(cancel_effects) and not hardware_stopped:
                cancel_effects(clear=True)
            self._cancel_story_audio()
            self._cancel_response_audio()
        finally:
            await asyncio.to_thread(self.close, cancel_tasks=False, hardware_stopped=hardware_stopped)
        if identity_error is not None:
            raise identity_error

    def close(self, *, cancel_tasks: bool = True, hardware_stopped: bool = False):
        if getattr(self, "_closed", False):
            return
        self._closed = True
        cancel_identity = getattr(self.application, "cancel_identity_work", None)
        if callable(cancel_identity):
            cancel_identity()
        if cancel_tasks:
            led = getattr(getattr(self, "application", None), "led", None)
            cancel_effects = getattr(led, "cancel_effects", None)
            if callable(cancel_effects) and not hardware_stopped:
                cancel_effects(clear=True)
            self._cancel_story_audio(); self._cancel_response_audio()
        self._hid_generation = getattr(self, "_hid_generation", 0) + 1
        self._physical_loop = None
        if not hardware_stopped and getattr(self, "audio_input", None) is not None:
            self.audio_input.stop()
        if self.input_adapter is not None:
            self.input_adapter.close()
        try:
            if not hardware_stopped:
                lighting_output_close = getattr(getattr(self, "lighting_output", None), "close", None)
                if callable(lighting_output_close):
                    lighting_output_close()
        finally:
            if self.lighting_serial is not None:
                self.lighting_serial.close()
            self.adapter.close()
            self._uart_executor.shutdown(wait=True, cancel_futures=True)
            self._audio_executor.shutdown(wait=True, cancel_futures=True)
            self.pty.close()


def decode_wav(raw: bytes) -> tuple[tuple[float, ...], int]:
    if not raw:
        raise AudioInputError("empty_audio", "WAV payload is empty")
    if len(raw) > MAX_AUDIO_WAV_BYTES:
        raise AudioInputError("oversized_audio", "WAV payload exceeds 6 MiB")
    try:
        with wave.open(BytesIO(raw), "rb") as source:
            channels=source.getnchannels(); width=source.getsampwidth(); rate=source.getframerate(); frames=source.getnframes()
            if channels not in {1, 2}:
                raise AudioInputError("unsupported_channels", "WAV channels must be mono or stereo")
            if width not in {2, 4}:
                raise AudioInputError("unsupported_sample_width", "WAV sample width must be PCM16 or PCM32")
            if rate not in AUDIO_SAMPLE_RATES:
                raise AudioInputError("unsupported_format", "WAV sample rate is not supported")
            if frames < 1 or frames > rate * MAX_AUDIO_SECONDS:
                raise AudioInputError("oversized_audio", "WAV exceeds 15 seconds")
            expected = frames * channels * width
            data = source.readframes(frames)
            if len(data) != expected:
                raise AudioInputError("truncated_audio", "WAV payload is truncated")
    except AudioInputError:
        raise
    except (wave.Error, EOFError, struct.error) as exc:
        raise AudioInputError("invalid_format", "invalid WAV header or payload") from exc
    if width == 2:
        values = np.frombuffer(data, dtype="<i2").astype(np.float32) / np.float32(32768.0)
    else:
        values = np.frombuffer(data, dtype="<i4").astype(np.float32) / np.float32(2147483648.0)
    if channels == 2:
        values = values.reshape(-1, 2).mean(axis=1, dtype=np.float32)
    values = values.astype(np.float32, copy=False)
    return tuple(float(value) for value in values), rate


def resample(samples: tuple[float, ...], source_rate: int, target_rate: int) -> tuple[float, ...]:
    if source_rate not in AUDIO_SAMPLE_RATES or target_rate not in AUDIO_SAMPLE_RATES:
        raise AudioInputError("unsupported_format", "sample rate is not supported")
    if not samples:
        return ()
    source = np.asarray(samples, dtype=np.float32)
    if not np.isfinite(source).all() or np.any(source < -1.0) or np.any(source > 1.0):
        raise AudioInputError("decode_error", "audio samples must be finite and within -1..1")
    if source_rate == target_rate:
        return tuple(float(value) for value in source)
    length = max(1, round(source.size * target_rate / source_rate))
    positions = np.linspace(0.0, source.size - 1, num=length, dtype=np.float32)
    result = np.interp(positions, np.arange(source.size, dtype=np.float32), source).astype(np.float32, copy=False)
    return tuple(float(value) for value in result)


def create_stand_app(settings: Settings | None = None, *, phone_client=None):
    from fastapi import HTTPException, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import FileResponse, JSONResponse, Response
    globals()["WebSocket"] = WebSocket
    globals()["Request"] = Request
    # Keep a blank explicitly constructed token compatible with the legacy
    # launcher/tests, while never overriding an explicitly configured secret.
    if settings is not None and not settings.stand_access_token and "STAND_ACCESS_TOKEN" in os.environ:
        settings = settings_with_compat_env(settings, ("STAND_ACCESS_TOKEN",))
    settings = settings if settings is not None else Settings.from_env()
    settings.validate()
    if settings.identity_mode == "max_json" and phone_client is None:
        raise ValueError("max_json_requires_explicit_phone_client")
    if settings.identity_mode != "max_json" and phone_client is not None:
        raise ValueError("phone_client_requires_max_json_mode")
    stand=HardwareIntegrationStand(settings); api=create_app(stand.application, admission_guard=stand.admission_dependencies_ready); api.add_middleware(StandAuthQueryRedactionMiddleware, allow_query_token=stand.settings.allow_query_token); api.state.stand=stand
    stand._admits_physical_users = api.state.lifecycle.admits_users
    if phone_client is not None:
        from .max_json import MaxJsonBoundary
        MaxJsonBoundary(stand.application, phone_client, session_id=stand.active_session_id,
                        admits_users=api.state.lifecycle.admits_users)
    console_root=Path(__file__).with_name("console")
    access_token=stand.settings.stand_access_token
    mutation_policy=stand.settings.mutation_auth_policy

    def _mutation_policy_error(request):
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return None
        if mutation_policy != "token_required" or not access_token:
            return JSONResponse({"error": {"code": "mutation_auth_unconfigured", "message": "stand mutation authentication is unavailable"}}, status_code=503)
        return None

    def transport_response(result):
        if not isinstance(result, dict) or not isinstance(result.get("error"), dict):
            return result
        error = result["error"]
        code = str(error.get("code") or "")
        status = {"validation_error":422,"invalid_frame":422,"empty_audio":400,"invalid_format":400,"unsupported_format":400,"decode_error":400,"oversized_audio":413,"runtime_unavailable":503,"conflict":409,"internal_error":500}.get(code, 500)
        if code == "validation_error":
            message = str(error.get("message") or "invalid stand event")
        elif code == "invalid_frame":
            message = "raw UART frame is invalid"
        elif code in {"empty_audio","invalid_format","unsupported_format","decode_error","oversized_audio"}:
            message = str(error.get("message") or "invalid audio input")
        elif code == "runtime_unavailable":
            message = "stand runtime is not ready"
        elif code == "conflict":
            message = "stand input conflicts with the current session"
        else:
            message = "stand input processing failed"
        return JSONResponse({"error":{"code":code or "internal_error","message":message}},status_code=status)

    @api.exception_handler(AudioInputError)
    async def audio_input_error(request: Request, exc: AudioInputError):
        return JSONResponse({"error": {"code": exc.code, "message": str(exc)}}, status_code=400)

    @api.middleware("http")
    async def stand_auth(request:Request,call_next):
        mutation_request = request.method in {"POST", "PUT", "PATCH", "DELETE"}
        protected=(mutation_request
                   or request.url.path.startswith("/stand")
                   or request.url.path.startswith("/console")
                   or request.url.path.startswith("/internal/"))
        supplied=request.headers.get("x-stand-token") or request.cookies.get("stand_token") or ""
        query_fallback = (
            stand.settings.allow_query_token
            and request.method == "GET"
            and request.url.path == "/console"
            and not supplied
        )
        if query_fallback:
            supplied = (
                request.query_params.get("token")
                or request.query_params.get("access_token")
                or request.query_params.get("stand_token")
                or ""
            )
        if protected and access_token and not secrets.compare_digest(supplied,access_token):
            return JSONResponse({"error":{"code":"auth_failure","message":"stand authentication required"}},status_code=401)
        mutation_error = _mutation_policy_error(request)
        if mutation_error is not None:
            return mutation_error
        lifecycle = getattr(api.state, "lifecycle", None)
        if (request.url.path.startswith("/stand/")
                and request.url.path not in {
                    "/stand/reset", "/stand/state", "/stand/graph",
                    "/stand/health", "/stand/diagnostics", "/stand/trace",
                }
                and lifecycle is not None
                and not lifecycle.admits_users()
                and not _software_runtime_usable(stand, lifecycle)):
            snapshot = lifecycle.snapshot()
            return JSONResponse({"error":{"code":"runtime_unavailable","message":"stand runtime is not ready","reason":_admission_reason(snapshot),"state":snapshot.get("state")}},status_code=503)
        response=await call_next(request)
        return response

    if stand.settings.webpanel_enabled:
        @api.get("/console",include_in_schema=False)
        def console(): return FileResponse(console_root/"index.html", headers={"Cache-Control": "no-store"})
        @api.get("/console/console.js",include_in_schema=False)
        def console_js(): return FileResponse(console_root/"console.js",media_type="application/javascript", headers={"Cache-Control": "no-store"})
        @api.get("/console/console.css",include_in_schema=False)
        def console_css(): return FileResponse(console_root/"console.css",media_type="text/css", headers={"Cache-Control": "no-store"})
        @api.get("/console/button-cap.svg",include_in_schema=False)
        def console_button_cap(): return FileResponse(console_root/"button-cap.svg",media_type="image/svg+xml", headers={"Cache-Control": "no-store"})
    @api.get("/stand/state")
    def state(): return stand.panel_snapshot()
    @api.get("/stand/health")
    def operational_health(): return stand.health_snapshot()
    @api.get("/stand/diagnostics")
    def operational_diagnostics(): return stand.diagnostics_snapshot()
    @api.get("/stand/trace")
    def operational_trace(): return {"trace": stand.diagnostics_snapshot().get("trace", [])}
    @api.get("/stand/graph")
    def graph(): return stand.graph
    @api.get("/stand/audio/phrase/{phrase_id}")
    async def phrase_audio(phrase_id: str):
        try:
            rendered = await stand.phrase_wav(phrase_id)
        except Exception as exc:
            stand.errors.append({"at": time.time(), "stage": "phrase_audio", "error": type(exc).__name__})
            raise HTTPException(status_code=503, detail="phrase audio temporarily unavailable") from exc
        if rendered is None:
            raise HTTPException(status_code=404, detail="unknown phrase")
        return Response(content=rendered, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    @api.get("/stand/audio/asset/{asset_id}")
    async def asset_audio(asset_id: str):
        rendered = await stand.asset_wav(asset_id)
        if rendered is None:
            raise HTTPException(status_code=404, detail="unknown audio asset")
        return Response(rendered, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    @api.get("/stand/audio/story/{story_id}")
    async def story_audio(story_id: str):
        try:
            rendered = await stand.story_wav(story_id)
        except Exception as exc:
            stand.errors.append({"at":time.time(),"stage":"story_audio","error":type(exc).__name__})
            raise HTTPException(status_code=503, detail="story audio temporarily unavailable") from exc
        if rendered is None:
            raise HTTPException(status_code=404, detail="unknown story")
        return Response(rendered, media_type="audio/wav", headers={"Cache-Control":"private, max-age=3600"})
    @api.post("/stand/audio/lifecycle")
    async def audio_lifecycle(payload: dict):
        sid = str(payload.get("session_id") or "")
        response_id = str(payload.get("response_id") or "")
        status = str(payload.get("status") or "")
        playback_id = str(payload.get("playback_id") or "")
        playback_lease = str(payload.get("playback_lease") or "")
        try:
            generation = int(payload["generation"])
        except (KeyError, TypeError, ValueError, OverflowError):
            generation = None
        if (not sid or not response_id or not playback_id or not playback_lease
                or generation is None
                or status not in {"queued", "playing", "paused", "finished", "cancelled", "failed", "unavailable"}):
            return JSONResponse({"error": {"code": "validation_error", "message": "playback identity and valid audio status are required"}}, status_code=422)
        projected = stand.application._mark_audio_effect_lifecycle(
            response_id, status, session_id=sid, generation=generation,
            playback_id=playback_id, playback_lease=playback_lease,
            reason=payload.get("reason"),
        )
        if not projected:
            return {"response_id": response_id, "reason": "stale_audio_lifecycle"}
        await stand.hub.publish({"type": "stand_state", "state": stand.panel_snapshot()})
        return {"response_id": response_id, "audio_status": status}
    @api.post("/stand/audio/checkpoint")
    async def audio_checkpoint(payload: dict):
        sid = str(payload.get("session_id") or "")
        story_id = str(payload.get("story_id") or "")
        playback_id = str(payload.get("playback_id") or "")
        playback_lease = str(payload.get("playback_lease") or "")
        try:
            generation = int(payload["generation"])
            offset_ms = int(payload["offset_ms"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return JSONResponse({"error": {"code": "validation_error", "message": "complete story checkpoint is required"}}, status_code=422)
        app = stand.application
        owner = app._paused_story_playback.get(sid)
        state = app.canonical_runtime.sessions.get(sid)
        if (not owner or not state or not story_id or not playback_id or not playback_lease
                or generation != app._session_epochs.get(sid, 0)
                or owner.get("generation") != generation
                or owner.get("content_id") != story_id
                or owner.get("playback_id") != playback_id
                or owner.get("playback_lease") != playback_lease
                or state.active_story_id != story_id
                or state.mode not in {"story_pause_announcement", "story_paused"}
                or offset_ms < 0):
            return {"reason": "stale_story_checkpoint"}
        verified = app.canonical_runtime.stories.get_verified(story_id)
        if verified is not None:
            offset_ms = min(offset_ms, int(verified.get("duration_ms") or offset_ms))
        stand._audio_event_sequence += 1
        result = await app.receive_async(Event(
            "StoryPositionUpdated", sid, stand._audio_event_sequence, time.time(),
            "browser_audio_checkpoint", {"story_id": story_id, "offset_ms": offset_ms},
            f"story-position:{playback_id}",
        ))
        if result.get("reason") == "story_checkpoint_updated" and app._paused_story_playback.get(sid) is owner:
            app._paused_story_playback.pop(sid, None)
        stand._collect_events()
        await stand.hub.publish({"type": "stand_state", "state": stand.panel_snapshot()})
        return result
    @api.post("/stand/audio/failed")
    async def audio_failed(payload: dict):
        sid = str(payload.get("session_id") or "")
        response_id = str(payload.get("response_id") or "")
        playback_lease = payload.get("playback_lease")
        playback_id = str(payload.get("playback_id") or "")
        generation = payload.get("generation")
        callback_token = payload.get("token")
        active_lease = stand.application._active_audio.get(sid)
        # A legacy completion can be enriched only when its unique lease
        # identifies the exact current execution. Lifecycle status never
        # uses this fallback and always requires the supplied playback ID.
        if (not playback_id and isinstance(active_lease, dict)
                and response_id == str(active_lease.get("content_id") or "")
                and playback_lease == active_lease.get("playback_lease")
                and generation is not None
                and int(generation) == int(active_lease.get("generation", -1))):
            playback_id = str(active_lease.get("playback_id") or "")
        if (callback_token is None and isinstance(active_lease, dict)
                and response_id == str(active_lease.get("content_id") or "")
                and playback_lease == active_lease.get("playback_lease")
                and generation is not None
                and int(generation) == int(active_lease.get("generation", -1))):
            callback_token = active_lease.get("token")
        if (not sid or not response_id or not playback_id or callback_token is None
                or playback_lease is None or generation is None):
            return JSONResponse({"error": {"code": "validation_error", "message": "complete playback identity is required"}}, status_code=422)
        stand._audio_event_sequence += 1
        result = await stand.application.receive_async(
            Event("AudioFailed", sid, stand._audio_event_sequence, time.time(), "browser_audio",
                  {"response_id": response_id, "token": callback_token,
                   "playback_id": playback_id, "playback_lease": playback_lease,
                   "generation": generation,
                   "error_code": str(payload.get("error_code") or "audio_playback_failed")[:80]},
                  str(payload.get("correlation_id") or f"browser-audio-failed:{playback_id}"))
        )
        stand._collect_events()
        await stand.hub.publish({"type": "stand_state", "state": stand.panel_snapshot()})
        return result

    @api.post("/stand/audio/completed")
    async def audio_completed(payload: dict):
        sid = str(payload.get("session_id") or "")
        response_id = str(payload.get("response_id") or "")
        playback_lease = payload.get("playback_lease")
        playback_id = str(payload.get("playback_id") or "")
        generation = payload.get("generation")
        callback_token = payload.get("token")
        active_lease = stand.application._active_audio.get(sid)
        # A legacy completion can be enriched only when its unique lease
        # identifies the exact current execution. Lifecycle status never
        # uses this fallback and always requires the supplied playback ID.
        if (not playback_id and isinstance(active_lease, dict)
                and response_id == str(active_lease.get("content_id") or "")
                and playback_lease == active_lease.get("playback_lease")
                and generation is not None
                and int(generation) == int(active_lease.get("generation", -1))):
            playback_id = str(active_lease.get("playback_id") or "")
        if (callback_token is None and isinstance(active_lease, dict)
                and response_id == str(active_lease.get("content_id") or "")
                and playback_lease == active_lease.get("playback_lease")
                and generation is not None
                and int(generation) == int(active_lease.get("generation", -1))):
            callback_token = active_lease.get("token")
        if (not sid or not response_id or not playback_id or callback_token is None
                or playback_lease is None or generation is None):
            return JSONResponse({"error": {"code": "validation_error", "message": "complete playback identity is required"}}, status_code=422)
        stand._audio_event_sequence += 1
        result = await stand.application.receive_async(
            Event("AudioCompleted", sid, stand._audio_event_sequence, time.time(), "browser_audio",
                  {"response_id": response_id, "token": callback_token,
                   "playback_id": playback_id, "playback_lease": playback_lease,
                   "generation": generation},
                  str(payload.get("correlation_id") or f"browser-audio-complete:{playback_id}"))
        )
        stand._collect_events()
        await stand.hub.publish({"type": "stand_state", "state": stand.panel_snapshot()})
        return result
    @api.post("/stand/handset")
    async def handset(payload:dict):
        sid = payload.get("session_id")
        if str(payload.get("state", "")) == "up" and sid:
            existing = stand.application.sessions.get(str(sid))
            if existing is not None and getattr(existing, "current_node", None) and bool(getattr(existing, "handset_up", False)) and str(getattr(existing, "state", "")) in {"Lifecycle.ACTIVE", "ACTIVE"}:
                return {"session_id": str(sid), "state": getattr(existing.state, "value", existing.state), "node": existing.current_node, "reason": "idempotent_duplicate", "outputs": []}
        return transport_response(await stand.inject("handset",{"state":payload.get("state")},sid))
    @api.post("/stand/button")
    async def button(payload:dict): return transport_response(await stand.inject("button",{"button_id":payload.get("button_id"),"state":"pressed"},payload.get("session_id")))
    @api.post("/stand/button-released")
    async def button_released(payload:dict): return transport_response(await stand.inject("button_released",{"button_id":payload.get("button_id"),"state":"released"},payload.get("session_id")))
    @api.post("/stand/button-hold")
    async def button_hold(payload:dict): return transport_response(await stand.inject("button_hold",{"button_id":payload.get("button_id"),"duration_ms":payload.get("duration_ms")},payload.get("session_id")))
    @api.post("/stand/button-long-hold")
    async def button_long_hold(payload:dict): return transport_response(await stand.inject("button_long_hold",{"button_id":payload.get("button_id"),"duration_ms":payload.get("duration_ms")},payload.get("session_id")))
    @api.post("/stand/text")
    async def text(payload:dict): return transport_response(await stand.inject("text",{"text":str(payload.get("text",""))},payload.get("session_id")))
    @api.post("/stand/raw")
    async def raw(payload:dict): return transport_response(await stand.inject_raw(str(payload.get("frame","")).encode()))
    @api.post("/stand/audio/wav")
    async def audio_wav(request:Request,session_id:str="stand"):
        guard = stand._audio_owner_guard(session_id)
        raw = await read_bounded_audio_body(request, MAX_AUDIO_WAV_BYTES)
        samples,rate=await stand._offload_audio(decode_wav, raw)
        return transport_response(await stand.inject_pcm(samples,rate,session_id, _ingress_guard=guard))
    @api.post("/stand/audio/pcm")
    async def audio_pcm(request:Request,session_id:str="stand",sample_rate:int=16000):
        guard = stand._audio_owner_guard(session_id)
        raw=await read_bounded_audio_body(request, 4 * 16000 * MAX_AUDIO_SECONDS)
        if not raw or len(raw)%4: raise AudioInputError("invalid_format", "PCM body must be little-endian float32")
        if len(raw) > 4 * 16000 * MAX_AUDIO_SECONDS: raise AudioInputError("oversized_audio", "PCM exceeds 15 seconds")
        samples=struct.unpack("<"+"f"*(len(raw)//4),raw)
        return transport_response(await stand.inject_pcm(tuple(samples),sample_rate,session_id, _ingress_guard=guard))
    @api.post("/stand/reset")
    async def reset_stand(payload:dict): return transport_response(await stand.reset(payload.get("session_id")))
    @api.websocket("/stand/ws")
    async def stand_ws(websocket:WebSocket):
        supplied=websocket.headers.get("x-stand-token") or websocket.cookies.get("stand_token") or ""
        if mutation_policy != "token_required" or not access_token:
            await _reject_websocket_admission(websocket, code=1013, error_code="mutation_auth_unconfigured", reason="mutation_auth_unconfigured", state="CONFIGURATION_REQUIRED")
            return
        if access_token and not secrets.compare_digest(supplied,access_token):
            await _reject_websocket_admission(
                websocket,
                code=1008,
                error_code="auth_failure",
                reason="invalid_token",
                state="AUTH_REQUIRED",
            )
            return
        # The WebPanel is an authenticated observer. Keep its read-only
        # connection available while runtime warmup or hardware is degraded;
        # mutation messages are rejected below with a structured error.
        await websocket.accept()
        owner = await stand.hub.connect(websocket)
        if websocket not in stand.hub.clients:
            return
        await websocket.send_json({"type":"stand_state","state":stand.panel_snapshot(),"lease":stand.hub.lease_snapshot(),"write_owner":owner})
        session_ids: set[str] = set()
        # Never await STT/LLM/TTS from the WebSocket receive loop.  A
        # browser microphone keeps sending PCM while a previous turn is
        # being recognized or spoken; blocking here used to create a blind
        # window in which barge-in frames accumulated in the socket and were
        # processed only after the old response finished.
        audio_tasks: set[asyncio.Task[Any]] = set()

        def forget_audio_task(task: asyncio.Task[Any]) -> None:
            audio_tasks.discard(task)
            if task.cancelled():
                return
            try:
                error = task.exception()
            except asyncio.CancelledError:
                return
            if error is not None:
                recorder = getattr(stand.application, "_record_audio_completion_diagnostic", None)
                if callable(recorder):
                    recorder("stand_audio_task_error",
                             session_id=next(iter(session_ids), stand.active_session_id),
                             operation="runtime", exception_type=type(error).__name__,
                             reason=str(error))

        def schedule_audio(coro: Any) -> None:
            task = asyncio.create_task(coro)
            audio_tasks.add(task)
            task.add_done_callback(forget_audio_task)
        try:
            while True:
                try:
                    message = await websocket.receive_json()
                    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
                        await websocket.send_json({"type": "error", "error": {
                            "code": "invalid_message", "message": "WebSocket message must be an object with a string type"}})
                        continue
                    kind = message["type"]
                    if kind == "ping":
                        await websocket.send_json({"type": "pong"})
                        continue
                    if kind in {"audio_pcm", "flush_audio", "handset", "button", "button_released", "button_hold", "button_long_hold", "text", "reset"}:
                        lifecycle = getattr(api.state, "lifecycle", None)
                        if (lifecycle is not None
                                and not lifecycle.admits_users()
                                and not _software_runtime_usable(stand, lifecycle)):
                            snapshot = lifecycle.snapshot()
                            await websocket.send_json({"type": "error", "error": {
                                "code": "runtime_unavailable",
                                "message": "stand runtime is not ready",
                                "reason": _admission_reason(snapshot),
                                "state": snapshot.get("state"),
                            }})
                            continue
                    session_id = message.get("session_id")
                    if session_id:
                        session_ids.add(str(session_id))
                    if kind in {"audio_pcm", "flush_audio", "handset", "button", "button_released", "button_hold", "button_long_hold", "text", "reset"} and not stand.hub.is_owner(websocket):
                        await websocket.send_json({"type":"error","error":{"code":"conflict","message":"stand input lease is owned by another client"}})
                        continue
                    if kind=="audio_pcm":
                        sid = str(session_id or stand.active_session_id)
                        audio_pipeline = getattr(stand.application, "s2s_audio", None)
                        trace_generation = getattr(audio_pipeline, "generations", {}).get(sid, 0)
                        stand.application.runtime_manager.engine.metrics.begin_voice_turn(sid, generation_id=trace_generation)
                        stand.application.runtime_manager.engine.metrics.mark_voice(sid, "proxy_receive", generation_id=trace_generation)
                        encoded = message.get("data_b64", "")
                        if not isinstance(encoded, str) or len(encoded) > 4 * ((8192 + 2) // 3):
                            raise ValueError("audio PCM frame exceeds limit")
                        raw=base64.b64decode(encoded,validate=True)
                        sample_rate=int(message.get("sample_rate",16000))
                        if sample_rate<=0 or len(raw)==0 or len(raw)%4 or len(raw)>8192:
                            raise ValueError("audio PCM frame must be finite float32 and <= 2048 samples")
                        samples=struct.unpack("<"+"f"*(len(raw)//4),raw)
                        if not all(math.isfinite(value) and -1.0<=value<=1.0 for value in samples):
                            raise ValueError("audio samples must be finite -1..1")
                        schedule_audio(stand.inject_pcm(tuple(samples),sample_rate,sid,False,wait_for_turn=False,
                                                       _ingress_guard=stand._audio_owner_guard(sid)))
                    elif kind=="flush_audio":
                        # Flush the VAD boundary with one bounded silence frame;
                        # empty payloads remain invalid for the PCM contract.
                        sid = str(session_id or stand.active_session_id)
                        schedule_audio(stand.inject_pcm(tuple(0.0 for _ in range(320)),16000,sid,True,wait_for_turn=False,
                                                       _ingress_guard=stand._audio_owner_guard(sid)))
                    elif kind in {"handset","button","button_released","button_hold","button_long_hold","text"}:
                        result = await stand.inject(kind,{k:v for k,v in message.items() if k not in {"type","session_id"}},message.get("session_id"))
                        if isinstance(result, dict) and result.get("error"):
                            error = result["error"]
                            await websocket.send_json({"type":"error","error":{"code":error.get("code","internal_error"),"message":error.get("message","stand input processing failed")}})
                    elif kind=="reset":
                        await stand.reset(message.get("session_id"))
                    else: await websocket.send_json({"type":"error","error":{"code":"unknown_message_type","message":"unknown stand WebSocket message"}})
                except json.JSONDecodeError:
                    await websocket.send_json({"type":"error","error":{"code":"invalid_json","message":"WebSocket message must be valid JSON"}})
                except EventValidationError:
                    await websocket.send_json({"type":"error","error":{"code":"validation_error","message":"invalid stand event"}})
                except (binascii.Error,TypeError,ValueError,struct.error):
                    # A malformed microphone frame must not tear down the
                    # browser session. Return a structured transport error
                    # and keep accepting later valid frames.
                    await websocket.send_json({"type":"error","error":{"code":"invalid_audio_frame","message":"audio PCM frame is invalid"}})
        except WebSocketDisconnect: pass
        finally:
            stand.hub.disconnect(websocket)
            owned_audio_tasks = tuple(audio_tasks)
            for task in owned_audio_tasks:
                task.cancel()
            if owned_audio_tasks:
                results = await asyncio.gather(*owned_audio_tasks, return_exceptions=True)
                failed = [result for result in results
                          if isinstance(result, BaseException)
                          and not isinstance(result, asyncio.CancelledError)]
                cleanup = {
                    "session_id": next(iter(session_ids), stand.active_session_id),
                    "operation": "disconnect",
                    "task_count": len(owned_audio_tasks),
                    "cancelled": sum(isinstance(result, asyncio.CancelledError) for result in results),
                    "completed": sum(not isinstance(result, BaseException) for result in results),
                    "failed": len(failed),
                    "timed_out": 0,
                    "remaining": 0,
                    "cleanup_completed": True,
                }
                stand.application._last_audio_cleanup = cleanup
                recorder = getattr(stand.application, "_record_audio_completion_diagnostic", None)
                if callable(recorder):
                    for error in failed:
                        recorder("stand_audio_task_error", session_id=cleanup["session_id"],
                                 operation="disconnect", exception_type=type(error).__name__)
                    recorder("stand_audio_cleanup", **cleanup)
            # WebPanel is an observer. Its disconnect is not a physical
            # handset-down event and must never reset the exhibition session.
            # Per-connection audio ingress tasks are cancelled above; the
            # canonical session/effects remain owned by the runtime.
    return api
