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
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode

logger = logging.getLogger("mishka.stand")

from .api import build_application, create_app
from .config import Settings, settings_with_compat_env
from .events import EventValidationError, parse_event
from .domain import Event, Lifecycle
from .hardware import (HardwareFrame, PTYUARTSimulator, PosixSerialTransport,
                       SerialHardwareAdapter, decode_frame, encode_frame)
from .canonical_contract import FixedStoryManifest, canonical_state_nodes
from .s2s import AudioChunk, SoundDeviceCaptureController, SoundDevicePlaybackController
from .input_adapters import KeyboardHIDInputAdapter, NormalizedInputEvent
from .led_contract import BUTTON_LED_COLORS, button_contract, canonical_led_state, canonical_led_state_from_lighting, fixed_button_colors
from .lighting import (
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
    "exhibition_name_capture", "bear_dialogue", "support_intake",
    "personal_story_theme", "wellbeing_check", "schedule_weather",
})
_AUTH_QUERY_KEYS = frozenset({"token", "access_token", "stand_token"})


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
                    scope["headers"] = headers
                    scope["_stand_query_token"] = query_token
                if any(key.lower() in _AUTH_QUERY_KEYS for key, _ in pairs):
                    scope["query_string"] = urlencode(sanitized).encode("latin-1")
        async def send_with_cookie(message):
            if console_query_fallback and message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"set-cookie", b"stand_token=" + query_token.encode("latin-1") + b"; Path=/console; HttpOnly; SameSite=strict"))
                headers.append((b"set-cookie", b"stand_token=" + query_token.encode("latin-1") + b"; Path=/stand/ws; HttpOnly; SameSite=strict"))
                message = dict(message)
                message["headers"] = headers
            await send(message)
        await self.app(scope, receive, send_with_cookie)


class AudioInputError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


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

    def start_breathing(self, *, now: float | None = None):
        return self.exhibition_effects.start_breathing(now=now)

    def start_wave(self, *, now: float | None = None, groups=None, hold_on_complete: bool = True):
        return self.exhibition_effects.start_wave(
            now=now, groups=groups, hold_on_complete=hold_on_complete,
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
    def __init__(self, adapter: SerialHardwareAdapter): self.adapter=adapter
    def play(self, response_id, asset_id, token):
        self.adapter.send({"type":"audio_play","payload":{"response_id":response_id,
                           "asset_id":asset_id,"token":token}})
    def play_text(self, response_id, text, token):
        self.adapter.send({"type":"audio_play","payload":{"response_id":response_id,"resolved_text":text,"content_kind":"action","token":token}})
    def cancel(self, token): self.adapter.send({"type":"audio_stop","payload":{"token":token}})

class UARTDisplayController:
    def __init__(self, adapter: SerialHardwareAdapter): self.adapter=adapter
    def show(self, asset_id, text, token):
        self.adapter.send({"type":"display","payload":{"asset_id":asset_id,"text":text,"token":token}})


class HeadlessAudioOutput:
    """Scenario audio port that delegates playback to the local USB device."""

    def __init__(self):
        self.on_play = None
        self.on_asset_play = None
        self.on_cancel = None

    def play(self, response_id, asset_id, token):
        if asset_id and callable(self.on_asset_play):
            self.on_asset_play(str(response_id), str(asset_id), int(token))
            return
        if callable(self.on_play):
            self.on_play(str(response_id), None)

    def play_text(self, response_id, text, token):
        if callable(self.on_play):
            self.on_play(str(response_id), str(text))

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
        self._physical_sequence = 0
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
                self.lighting_output = PhysicalLightingOutput(self.lighting_serial)
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
            self.audio_output.on_asset_play = self._headless_asset_play
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
        return bool(
            session is not None
            and getattr(session, "handset_up", False)
            and getattr(session, "current_node", None) in HEADLESS_AUDIO_NODES
        )

    def _enqueue_headless_audio_frame(self, values: Any) -> None:
        queue = self._headless_audio_queue
        if queue is None or not self._headless_audio_accepting():
            return
        try:
            queue.put_nowait(values)
        except asyncio.QueueFull:
            self._headless_audio_dropped += 1

    async def _headless_audio_worker(self) -> None:
        queue = self._headless_audio_queue
        if queue is None:
            return
        while True:
            values = await queue.get()
            if values is None:
                return
            if not self._headless_audio_accepting():
                continue
            try:
                result = await self.inject_pcm(
                    tuple(float(value) for value in np.asarray(values, dtype=np.float32).reshape(-1)),
                    self.settings.audio_sample_rate,
                    self.active_session_id,
                    flush_silence=False,
                    wait_for_turn=False,
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
        self._headless_audio_queue = asyncio.Queue(maxsize=64)
        self._headless_audio_dropped = 0
        self._headless_audio_task = asyncio.create_task(
            self._headless_audio_worker(),
            name="mishka-headless-audio-input",
        )
        try:
            capture.start(loop, self._enqueue_headless_audio_frame)
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
        capture = getattr(self, "audio_input", None)
        if capture is not None:
            capture.stop()
        task, self._headless_audio_task = getattr(self, "_headless_audio_task", None), None
        self._headless_audio_queue = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if capture is not None:
            self._headless_audio_status = {"state": "stopped"}

    async def start_hardware(self):
        if self.hardware_mode != "physical":
            return
        if self.input_adapter is not None and self.input_adapter.is_ready():
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
        old_adapter = self.input_adapter
        self.input_adapter = None
        self._hid_generation = getattr(self, "_hid_generation", 0) + 1
        generation = self._hid_generation
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
        hid_diag = _hid_diagnostics(self.input_adapter)
        level_sync = hid_diag.get("initial_level_sync") or {}
        hid_state = "active" if hid_diag.get("physical_ready") else ("initial_level_unavailable" if level_sync.get("status") == "unavailable" else "degraded")
        self._hid_status = {"state": hid_state, "generation": generation, "initial_level_sync": level_sync, "semantic_input_accepted": bool(hid_diag.get("semantic_input_accepted")), "physical_ready": bool(hid_diag.get("physical_ready"))}
        self._start_headless_audio_capture()
        self._startup_state = "ready" if getattr(self, "_lighting_status", {"state": "available"}).get("state") == "available" and hid_diag.get("physical_ready") and getattr(self, "_headless_audio_status", {}).get("state") == "active" else "degraded"

    async def stop_hardware(self):
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

    def _headless_asset_play(self, response_id: str, asset_id: str, token: int) -> None:
        """Play an immutable canonical WAV through the physical speaker.

        The canonical application deliberately passes the resolved asset path
        to its audio port. Physical mode must consume that path directly;
        looking up legacy response text here would silently turn every
        recorded phrase into a no-op after the old content catalog is removed.
        """
        self._cancel_response_audio()
        task = asyncio.create_task(
            self._play_asset_audio(self.active_session_id, response_id, asset_id, token),
            name=f"mishka-asset-audio-{response_id}",
        )
        self._response_audio_tasks.add(task)
        task.add_done_callback(
            lambda done: self._observe_detached_audio_task(
                done, self._response_audio_tasks, operation="asset_audio",
                session_id=self.active_session_id,
            )
        )

    async def _play_asset_audio(self, session_id: str, response_id: str, asset_id: str, token: int) -> None:
        try:
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
            self.playback.active_token = int(token)
            self.commands.append({
                "at": time.time(), "type": "asset_audio_play", "response_id": response_id,
                "asset_id": str(path), "samples": len(samples), "rate": rate, "token": int(token),
            })
            self.application._mark_audio_effect_lifecycle(response_id, "playing")
            recorder = getattr(self.application, "record_audio_diagnostic", None)
            if callable(recorder):
                recorder("audio_playback_started", session_id=session_id, response_id=response_id,
                         content_id=response_id, asset_ref=str(path), playback_id=f"{session_id}:{response_id}:{token}")
            await self.playback.play(AudioChunk(samples, rate, int(token), session_id))
            self.application._mark_audio_effect_lifecycle(response_id, "finished")
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
                lease = dict(getattr(self.application, "_active_audio", {}).get(session_id) or {})
                await self.application.receive_async(
                    Event("AudioCompleted", session_id, self._audio_event_sequence,
                          time.time(), "physical_audio",
                          {"response_id": response_id, "token": int(token),
                           "playback_lease": lease.get("playback_lease"),
                           "generation": lease.get("generation")},
                          f"audio-complete:{session_id}:{response_id}:{token}"),
                )
                self._collect_events()
        except asyncio.CancelledError:
            self.application._mark_audio_effect_lifecycle(response_id, "cancelled")
            recorder = getattr(self.application, "record_audio_diagnostic", None)
            if callable(recorder):
                recorder("audio_playback_cancelled", session_id=session_id, response_id=response_id,
                         content_id=response_id, asset_ref=asset_id, playback_id=f"{session_id}:{response_id}:{token}")
            raise
        except Exception as exc:
            self.application._mark_audio_effect_lifecycle(response_id, "failed", reason=type(exc).__name__)
            recorder = getattr(self.application, "record_audio_diagnostic", None)
            if callable(recorder):
                recorder("audio_playback_failed", session_id=session_id, response_id=response_id,
                         content_id=response_id, asset_ref=asset_id, error=type(exc).__name__, detail=str(exc)[:200],
                         playback_id=f"{session_id}:{response_id}:{token}")
            self.errors.append({
                "at": time.time(), "stage": "asset_playback", "response_id": response_id,
                "asset_id": asset_id, "error": type(exc).__name__, "detail": str(exc)[:200],
            })
            self._audio_event_sequence += 1
            lease = dict(getattr(self.application, "_active_audio", {}).get(session_id) or {})
            await self.application.receive_async(
                Event("AudioFailed", session_id, self._audio_event_sequence, time.time(),
                      "physical_audio",
                      {"response_id": response_id, "token": int(token),
                       "playback_lease": lease.get("playback_lease"),
                       "generation": lease.get("generation"),
                       "error_code": "audio_playback_failed"},
                      f"audio-failed:{session_id}:{response_id}:{token}"),
            )

    def _on_hid_error(self, diagnostic: dict[str, Any], generation: int) -> None:
        if generation != getattr(self, "_hid_generation", 0):
            return
        self._hid_status = {"state": str(diagnostic.get("kind") or "reader_exception"), "generation": generation, "semantic_input_accepted": False, "physical_ready": False, **diagnostic}
        self.errors.append({"at": time.time(), "stage": "hid_reader", "generation": generation, **diagnostic})

    def _on_physical_input(self, event: NormalizedInputEvent, generation: int | None = None):
        if generation is not None and generation != getattr(self, "_hid_generation", 0):
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
        future.add_done_callback(self._observe_physical_input_future)

    def _observe_physical_input_future(self, future) -> None:
        if future.cancelled():
            return
        try:
            future.result()
        except Exception as exc:
            diagnostic = {"at": time.time(), "stage": "hid_dispatch", "exception_type": type(exc).__name__, "detail": str(exc)[:200]}
            self._hid_status = {"state": "dispatch_failure", "generation": getattr(self, "_hid_generation", 0), **diagnostic}
            self.errors.append(diagnostic)
            logger.error("physical HID dispatch failed: %s", diagnostic["detail"])

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
        result = await self.application.receive_async(event, None)
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
        for task in tuple(self._response_audio_tasks):
            task.cancel()
        self._response_audio_tasks.clear()

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
        item={"at":time.time(),"direction":direction,"raw":raw.decode("utf-8","replace").rstrip()}
        try:
            frame=decode_frame(raw)
            if frame.type == "audio_pcm":
                # The UART boundary still validates every byte and CRC. A
                # browser does not need hundreds of base64 PCM copies in the
                # diagnostic snapshot; they made each subsequent WS update
                # larger and larger and stalled microphone ingress.
                payload={key:value for key,value in frame.payload.items() if key!="data_b64"}
                payload["pcm_bytes"]=len(str(frame.payload.get("data_b64", "")))*3//4
                item["raw"]="[audio_pcm payload omitted from diagnostics]"
                item["decoded"]={"type":frame.type,"sequence":frame.sequence,"payload":payload,"checksum":frame.checksum}
            else:
                payload = dict(frame.payload)
                if frame.type in {"rfid", "RfidPresented"} and "tag_id" in payload:
                    payload.pop("tag_id", None)
                    item["raw"] = "[rfid payload redacted]"
                item["decoded"]={"type":frame.type,"sequence":frame.sequence,"payload":payload,"checksum":frame.checksum}
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
                    # Enrich browser playback with the canonical lease.
                    response_id = str(payload.get("response_id") or "")
                    active_audio = getattr(self.application, "_active_audio", {})
                    lease_session = None
                    lease = None
                    for candidate_session, candidate in active_audio.items():
                        if (str(candidate.get("content_id") or "") == response_id
                                and str(candidate.get("token")) == str(payload.get("token"))):
                            lease_session, lease = candidate_session, candidate
                            break
                    if lease is not None:
                        payload["session_id"] = str(lease_session)
                        payload["playback_lease"] = str(lease.get("playback_lease") or "")
                        payload["generation"] = int(lease.get("generation", 0))
                    response=self.responses.get(response_id,{})
                    story=self.stories.get(str(payload.get("response_id")),{})
                    if response:
                        text=str(response.get("text","")); session=self.application.sessions.get(self.active_session_id)
                        if session and session.child_id and "{name}" in text:
                            profile=self.application.profiles.resolve(session.rfid_tag or "") or {}; text=text.replace("{name}",str(profile.get("display_name","")))
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
                        payload["content_kind"]="story"
                    elif str(payload.get("response_id", "")).startswith("placeholder:fixed_story:"):
                        payload["asset_path"] = str(payload.get("asset_id") or "")
                        payload["content_kind"] = "story"
                    elif str(payload.get("response_id", "")).startswith("PHRASE_"):
                        phrase = self.application.voice_manifest.resolve(str(payload["response_id"]))
                        if phrase:
                            payload["asset_path"] = str(phrase["asset_path"])
                            payload["content_kind"] = "phrase"
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
        self.results.append({"at":time.time(),"input":"audio_completion","result":payload})
        failures = await self.hub.publish({"type":"stand_state","state":self.panel_snapshot(),"audio_completion":payload})
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

    async def inject(self, message_type: str, payload: dict[str,Any], session_id: str | None = None, *, wait_for_audio: bool = True):
        sid = session_id or self.active_session_id
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
                await self._offload_uart(self._device_write, message_type, payload)
                frames=await self._offload_uart(self._receive_frames_sync)
            if not frames: raise RuntimeError("serial adapter received no frame from PTY")
            for frame in frames: result=await self._dispatch(frame, sid, wait_for_audio=wait_for_audio)
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
        if requested_session_id and not (isinstance(result, dict) and result.get("reason") == "installation_busy"):
            self.active_session_id = requested_session_id
        is_transport_error = isinstance(result, dict) and isinstance(result.get("error"), dict)
        if not quiet_pcm:
            self.latencies.append({"at":time.time(),"operation":message_type,"ms":round((time.perf_counter()-started)*1000,2)})
            if result is not None: self.results.append({"at":time.time(),"input":message_type,"result":result})
            if not is_transport_error:
                await self.hub.publish({"type":"stand_state","state":self.panel_snapshot()})
        return result

    async def inject_raw(self, raw: bytes):
        if not raw.endswith(b"\n"): raw+=b"\n"
        started=time.perf_counter()
        try:
            async with self._transport_lock:
                await self._offload_uart(self.pty.write_device, raw)
                frames=await self._offload_uart(self._receive_frames_sync)
            results=[await self._dispatch(frame, self.active_session_id) for frame in frames]
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

    async def _dispatch(self, frame: HardwareFrame, session_id: str | None = None, *, wait_for_audio: bool = True):
        sid = session_id or self.active_session_id
        if frame.type=="capabilities":
            self.adapter.capabilities=dict(frame.payload); self.adapter.firmware_version=str(frame.payload.get("firmware","")); return {"capabilities":frame.payload}
        if frame.type=="heartbeat": return {"heartbeat":True}
        if frame.type=="audio_pcm": return await self._dispatch_pcm(frame, sid, wait_for_turn=wait_for_audio)
        event_payload={"type":frame.type,**frame.payload}
        event=parse_event(event_payload,sid,frame.sequence,"uart_pty")
        return await self.application.receive_async(event,frame.payload.get("text") if frame.type=="text" else None)

    async def _dispatch_pcm(self, frame: HardwareFrame, session_id: str | None = None, *, wait_for_turn: bool = True):
        sid = session_id or self.active_session_id
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
            return await self.application.receive_audio_async(sid,tuple(samples),frame.sequence,wait_for_turn=wait_for_turn)

    async def inject_pcm(self, samples: tuple[float,...], sample_rate: int, session_id: str | None = None, flush_silence: bool = True, *, wait_for_turn: bool = True):
        if sample_rate not in AUDIO_SAMPLE_RATES:
            raise AudioInputError("unsupported_format", "sample rate is not supported")
        if not samples:
            raise AudioInputError("empty_audio", "audio payload is empty")
        if len(samples)>sample_rate*MAX_AUDIO_SECONDS:
            raise AudioInputError("oversized_audio", "audio exceeds 15 seconds")
        values = np.asarray(samples, dtype=np.float32)
        if not np.isfinite(values).all() or np.any(values < -1.0) or np.any(values > 1.0):
            raise AudioInputError("decode_error", "audio samples must be finite and within -1..1")
        if session_id: self.active_session_id=session_id
        if sample_rate!=16000:
            samples=await self._offload_audio(resample, tuple(float(value) for value in values), sample_rate, 16000)
            sample_rate=16000
        result=None
        turn_completed=False
        for offset in range(0,len(samples),512):
            chunk=samples[offset:offset+512]; raw=struct.pack("<"+"f"*len(chunk),*chunk)
            result=await self.inject("audio_pcm",{"encoding":"f32le","sample_rate":sample_rate,"channels":1,
                                     "data_b64":base64.b64encode(raw).decode()},self.active_session_id,wait_for_audio=wait_for_turn)
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
                                         "data_b64":base64.b64encode(raw).decode()},self.active_session_id,wait_for_audio=wait_for_turn)
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
        self._cancel_story_audio()
        self._cancel_response_audio()
        await self.playback.stop()
        # Reset is an explicit installation-level operator action.  The UI
        # normally sends its stable session id ("stand"), while an API/WS
        # smoke test may have left another id active.  Reset both sides of
        # that binding and make the requested id the fresh cursor; otherwise
        # the next RFID is rejected as installation_busy and the panel looks
        # authenticated while the runtime is actually waiting for RFID.
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
            await self.application.reset_session(stale_id)
        result=await self.application.reset_session(sid)
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
        allowed = {
            "input", "normalized_event", "canonical_event", "state_before",
            "state_after", "effects", "monotonic_timestamp",
            "timer_started", "timer_deadline",
        }
        result = {key: item[key] for key in allowed if key in item}
        audio = item.get("audio")
        if isinstance(audio, str):
            result["audio"] = audio[:120]
        elif isinstance(audio, dict):
            result["audio"] = {
                key: audio[key] for key in ("status", "provider", "phrase_id", "response_id")
                if key in audio and isinstance(audio[key], (str, int, float, bool))
            }
        return result

    @staticmethod
    def _safe_diagnostic_item(item: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "kind", "code", "error", "reason", "status", "component",
            "content_id", "asset_ref", "operation", "source",
            "generation", "at_monotonic",
        }
        return {key: item[key] for key in allowed if key in item}

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
        if self.hardware_mode != "physical":
            hid_state = "simulated"
        lighting_state = str(getattr(self, "_lighting_status", {}).get("state") or "unavailable")
        lighting_runtime = (
            self.lighting_serial.diagnostics()
            if self.lighting_serial is not None
            else dict(getattr(self, "_lighting_status", {}))
        )
        if self.hardware_mode == "physical" and self.lighting_serial is not None and not lighting_runtime.get("ready", False):
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
        return {
            "status": "degraded" if degraded else "ready",
            "mode": "exhibition",
            "canonical_state": mode,
            "handset": handset,
            "audio": {"status": audio_state, "provider": audio.get("provider")},
            "hid": {"status": hid_state, "generation": int(hid_status.get("generation") or 0), "semantic_input_accepted": bool(hid_status.get("semantic_input_accepted", self.hardware_mode != "physical" or hid_state == "active")), "initial_level_sync": hid_status.get("initial_level_sync", {"status": "simulated" if self.hardware_mode != "physical" else "not_attempted"}), "physical_ready": bool(self.hardware_mode == "physical" and hid_status.get("physical_ready", False)), "restart_allowed": bool(self.hardware_mode == "physical" and hid_state in {"device_unavailable", "initial_level_unavailable", "device_disconnected", "reader_exception", "dispatch_failure", "stopped"})},
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
            "lighting_hardware": (
                self.lighting_serial.diagnostics()
                if self.lighting_serial is not None
                else dict(getattr(self, "_lighting_status", {}))
            ),
            # Bounded and redacted, but lossless for every physical lighting ID.
            "physical_lighting": dict(self.lighting_output.snapshot()),
        }

    def snapshot(self):
        session=self.application.sessions.get(self.active_session_id)
        lighting = self.lighting_output.snapshot()
        # Lossless physical projection. The led field below is intentionally
        # only a UI/menu compatibility projection and must not be used as
        # hardware state because it aggregates away the physical channel layout.
        physical_lighting = {str(element_id): int(lighting[str(element_id)]) for element_id in LIGHTING_ELEMENT_IDS}
        led_projection = canonical_led_state_from_lighting(lighting)
        # Explicit compatibility calls made through the old diagnostic helper
        # remain visible when no canonical element has been changed. Production
        # scenario effects always have a canonical lighting snapshot.
        if not any(int(value) for value in lighting.values()) and self.commands:
            led_projection = canonical_led_state(self.commands)
        input_diagnostics = self.input_adapter.diagnostics() if self.input_adapter is not None else {"backend": "hid" if self.hardware_mode == "physical" else "fake", "ready": self.hardware_mode != "physical"}
        input_diagnostics["stand_status"] = dict(getattr(self, "_hid_status", {"state": "not_started"}))
        if self._input_start_error is not None:
            input_diagnostics["startup_error"] = dict(self._input_start_error)
        audio_readiness = dict(self.playback.readiness()) if hasattr(self.playback, "readiness") else {"ready": True, "backend": "browser"}
        audio_input = dict(self.audio_input.diagnostics()) if self.audio_input is not None else {"ready": True, "backend": "browser", "direction": "input", "state": "webpanel"}
        audio_input["stand_status"] = dict(self._headless_audio_status)
        audio_input["dropped_frames"] = int(self._headless_audio_dropped)
        audio_readiness["runtime_diagnostics"] = list(getattr(self.application, "audio_runtime_diagnostics", ()))[-20:]
        audio_readiness["active_playback"] = next((item for item in reversed(audio_readiness["runtime_diagnostics"])
                                                    if item.get("kind") == "audio_playback_started"), None)
        audio_readiness["last_audio_error"] = next((item for item in reversed(audio_readiness["runtime_diagnostics"])
                                                    if item.get("kind") in {"audio_playback_failed", "audio_dispatch_failed", "recorded_phrase_unavailable"}), None)
        return {"transport":{"kind":"pty_uart_jsonl_crc32","slave_path":self.pty.slave_path,
                             "protocol_version":"1","story_audio_mode":"silero_wav","hardware":self.adapter.health()},
                "session":{"session_id":self.active_session_id,"state":session.state.value if session else "NONE",
                           "node":session.current_node if session else None,"scenario":session.scenario_id if session else None,
                           "handset_up":session.handset_up if session else False,
        "enabled_buttons":self._enabled_buttons(session),
                           "anonymous":session.anonymous if session else None,"retries":session.retries if session else 0},
                "visual_only": session.visual_only if session else False,
                "raw_frames":list(self.raw_frames),"commands":list(self.commands),
                "events":list(self.publisher.events[-300:]),"results":list(self.results),
                # lighting is canonical; led is UI compatibility derived
                # from it, never from legacy led_pattern commands.
                "led":led_projection,
                "lighting":lighting,
                "physical_lighting":physical_lighting,
                "lighting_wave": dict(self._lighting_wave),
                "lighting_effects": (self.application.led.effect_snapshot() if callable(getattr(self.application.led, "effect_snapshot", None)) else {}),
                "hardware": {"transport_mode": self.hardware_mode, "lighting": self.lighting_serial.diagnostics() if self.lighting_serial is not None else dict(self._lighting_status), "input": input_diagnostics, "audio": audio_readiness, "audio_input": audio_input, "acceptance": "physical" if self.hardware_mode == "physical" else "simulator"},
                "button_contract":button_contract(),
                "latencies":list(self.latencies),"pipeline_metrics":self.application.runtime_manager.engine.metrics.snapshot(),
                "graph":self.graph,
                "errors":list(self.errors),"profile":self.settings.profile, "canonical_state":dict(getattr(self.application, "last_canonical_state", {}) or {}), "last_canonical_event":dict(getattr(self.application, "last_canonical_event", {}) or {}), "effects":list(getattr(self.application, "last_canonical_effects", []) or []), "canonical_trace":list(getattr(self.application, "canonical_trace", []) or []), "canonical_diagnostics":list(getattr(self.application, "canonical_diagnostics", []) or []), "audio_status":next((item.get("audio_status") for item in reversed(getattr(self.application, "last_canonical_effects", []) or []) if item.get("audio_status")), "idle")}

    @staticmethod
    def _panel_payload(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        allowed = {
            "button_id", "node_id", "from", "to", "reason", "status",
            "response_id", "session_id", "playback_lease",
            "phrase_id", "content_kind", "asset_id", "asset_path",
            "audio_status", "asset_status", "effect",
            "pattern", "state", "generation", "sequence", "source",
            "action", "type", "resolution",
        }
        return {key: item for key, item in value.items() if key in allowed}

    @staticmethod
    def _panel_metrics(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key == "events" and isinstance(item, list):
                result[key] = [
                    {field: event[field] for field in ("event", "at", "timestamp", "token_index", "chunk_index") if field in event}
                    for event in item[-80:] if isinstance(event, dict)
                ]
            elif isinstance(item, dict):
                result[key] = {field: item[field] for field in ("count", "last_ms", "max_ms", "avg_ms", "current_sample_count") if field in item}
            elif isinstance(item, (str, int, float, bool)) or item is None:
                result[key] = item
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
                           "generation", "content_kind", "asset_id", "asset_path",
                           "audio_status", "asset_status", "story_audio_mode"}
                result["payload"] = {key: payload[key] for key in allowed if key in payload}
            return result

        def result_item(item):
            if not isinstance(item, dict):
                return {}
            result = {key: item[key] for key in ("input", "reason", "state", "at", "sequence", "correlation_id") if key in item}
            if isinstance(item.get("outputs"), list):
                result["outputs"] = [self._panel_payload(output) for output in item["outputs"][-20:] if isinstance(output, dict)]
            return result

        return {
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

    def close(self):
        if getattr(self, "_closed", False):
            return
        self._closed = True
        cancel_effects = getattr(getattr(self, "application", None), "led", None)
        cancel_effects = getattr(cancel_effects, "cancel_effects", None)
        if callable(cancel_effects):
            cancel_effects(clear=True)
        self._cancel_story_audio(); self._cancel_response_audio()
        self._hid_generation = getattr(self, "_hid_generation", 0) + 1
        self._physical_loop = None
        if getattr(self, "audio_input", None) is not None:
            self.audio_input.stop()
        if self.input_adapter is not None:
            self.input_adapter.close()
        if self.lighting_serial is not None:
            self.lighting_serial.close()
        self.adapter.close(); self._uart_executor.shutdown(wait=True, cancel_futures=True)
        self._audio_executor.shutdown(wait=True, cancel_futures=True); self.pty.close()


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


def create_stand_app(settings: Settings | None = None):
    from fastapi import HTTPException, Request, WebSocket, WebSocketDisconnect
    from starlette.websockets import WebSocketDisconnected
    from fastapi.responses import FileResponse, JSONResponse, Response
    globals()["WebSocket"] = WebSocket
    globals()["Request"] = Request
    # Keep a blank explicitly constructed token compatible with the legacy
    # launcher/tests, while never overriding an explicitly configured secret.
    if settings is not None and not settings.stand_access_token and "STAND_ACCESS_TOKEN" in os.environ:
        settings = settings_with_compat_env(settings, ("STAND_ACCESS_TOKEN",))
    stand=HardwareIntegrationStand(settings); api=create_app(stand.application); api.add_middleware(StandAuthQueryRedactionMiddleware, allow_query_token=stand.settings.allow_query_token); api.state.stand=stand
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
        response_id = str(payload.get("response_id") or "")
        status = str(payload.get("status") or "")
        if not response_id or status not in {"queued", "playing", "finished", "cancelled", "failed", "unavailable"}:
            return JSONResponse({"error": {"code": "validation_error", "message": "response_id and valid audio status are required"}}, status_code=422)
        stand.application._mark_audio_effect_lifecycle(response_id, status, reason=payload.get("reason"))
        await stand.hub.publish({"type": "stand_state", "state": stand.panel_snapshot()})
        return {"response_id": response_id, "audio_status": status}
    @api.post("/stand/audio/failed")
    async def audio_failed(payload: dict):
        sid = str(payload.get("session_id") or "")
        response_id = str(payload.get("response_id") or "")
        playback_lease = payload.get("playback_lease")
        generation = payload.get("generation")
        callback_token = payload.get("token")
        active_lease = stand.application._active_audio.get(sid)
        if (callback_token is None and isinstance(active_lease, dict)
                and response_id == str(active_lease.get("content_id") or "")
                and playback_lease == active_lease.get("playback_lease")
                and generation is not None
                and int(generation) == int(active_lease.get("generation", -1))):
            callback_token = active_lease.get("token")
        if (not sid or not response_id or callback_token is None
                or playback_lease is None or generation is None):
            return JSONResponse({"error": {"code": "validation_error", "message": "session_id, response_id, playback_lease and generation are required"}}, status_code=422)
        stand._audio_event_sequence += 1
        result = await stand.application.receive_async(
            Event("AudioFailed", sid, stand._audio_event_sequence, time.time(), "browser_audio",
                  {"response_id": response_id, "token": callback_token,
                   "playback_lease": playback_lease,
                   "generation": generation,
                   "error_code": str(payload.get("error_code") or "audio_playback_failed")[:80]},
                  str(payload.get("correlation_id") or f"browser-audio-failed:{sid}:{stand._audio_event_sequence}"))
        )
        stand._collect_events()
        await stand.hub.publish({"type": "stand_state", "state": stand.panel_snapshot()})
        return result

    @api.post("/stand/audio/completed")
    async def audio_completed(payload: dict):
        sid = str(payload.get("session_id") or "")
        response_id = str(payload.get("response_id") or "")
        playback_lease = payload.get("playback_lease")
        generation = payload.get("generation")
        callback_token = payload.get("token")
        active_lease = stand.application._active_audio.get(sid)
        if (callback_token is None and isinstance(active_lease, dict)
                and response_id == str(active_lease.get("content_id") or "")
                and playback_lease == active_lease.get("playback_lease")
                and generation is not None
                and int(generation) == int(active_lease.get("generation", -1))):
            callback_token = active_lease.get("token")
        if (not sid or not response_id or callback_token is None
                or playback_lease is None or generation is None):
            return JSONResponse({"error": {"code": "validation_error", "message": "session_id, response_id, playback_lease and generation are required"}}, status_code=422)
        stand._audio_event_sequence += 1
        result = await stand.application.receive_async(
            Event("AudioCompleted", sid, stand._audio_event_sequence, time.time(), "browser_audio",
                  {"response_id": response_id, "token": callback_token,
                   "playback_lease": playback_lease,
                   "generation": generation},
                  str(payload.get("correlation_id") or f"browser-audio:{sid}:{stand._audio_event_sequence}"))
        )
        stand._collect_events()
        await stand.hub.publish({"type": "stand_state", "state": stand.panel_snapshot()})
        return result
    @api.post("/stand/rfid")
    async def rfid(payload:dict): return transport_response(await stand.inject("rfid",{"tag_id":str(payload.get("tag_id",""))},payload.get("session_id")))
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
        raw = await request.body()
        samples,rate=await stand._offload_audio(decode_wav, raw)
        return transport_response(await stand.inject_pcm(samples,rate,session_id))
    @api.post("/stand/audio/pcm")
    async def audio_pcm(request:Request,session_id:str="stand",sample_rate:int=16000):
        raw=await request.body()
        if not raw or len(raw)%4: raise AudioInputError("invalid_format", "PCM body must be little-endian float32")
        if len(raw) > 4 * 16000 * MAX_AUDIO_SECONDS: raise AudioInputError("oversized_audio", "PCM exceeds 15 seconds")
        samples=struct.unpack("<"+"f"*(len(raw)//4),raw)
        return transport_response(await stand.inject_pcm(tuple(samples),sample_rate,session_id))
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
                    message=await websocket.receive_json(); kind=message.get("type")
                    if kind == "ping":
                        await websocket.send_json({"type": "pong"})
                        continue
                    if kind in {"audio_pcm", "flush_audio", "rfid", "handset", "button", "button_released", "button_hold", "button_long_hold", "text", "reset"}:
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
                    if kind in {"audio_pcm", "flush_audio", "rfid", "handset", "button", "button_released", "button_hold", "button_long_hold", "text", "reset"} and not stand.hub.is_owner(websocket):
                        await websocket.send_json({"type":"error","error":{"code":"conflict","message":"stand input lease is owned by another client"}})
                        continue
                    if kind=="audio_pcm":
                        sid = str(session_id or stand.active_session_id)
                        audio_pipeline = getattr(stand.application, "s2s_audio", None)
                        trace_generation = getattr(audio_pipeline, "generations", {}).get(sid, 0)
                        stand.application.runtime_manager.engine.metrics.begin_voice_turn(sid, generation_id=trace_generation)
                        stand.application.runtime_manager.engine.metrics.mark_voice(sid, "proxy_receive", generation_id=trace_generation)
                        raw=base64.b64decode(str(message.get("data_b64","")),validate=True)
                        sample_rate=int(message.get("sample_rate",16000))
                        if sample_rate<=0 or len(raw)==0 or len(raw)%4 or len(raw)>8192:
                            raise ValueError("audio PCM frame must be finite float32 and <= 2048 samples")
                        samples=struct.unpack("<"+"f"*(len(raw)//4),raw)
                        if not all(math.isfinite(value) and -1.0<=value<=1.0 for value in samples):
                            raise ValueError("audio samples must be finite -1..1")
                        schedule_audio(stand.inject_pcm(tuple(samples),sample_rate,message.get("session_id"),False,wait_for_turn=False))
                    elif kind=="flush_audio":
                        # Flush the VAD boundary with one bounded silence frame;
                        # empty payloads remain invalid for the PCM contract.
                        schedule_audio(stand.inject_pcm(tuple(0.0 for _ in range(320)),16000,message.get("session_id"),True,wait_for_turn=False))
                    elif kind in {"rfid","handset","button","button_released","button_hold","button_long_hold","text"}:
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
        except (WebSocketDisconnect, WebSocketDisconnected): pass
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
