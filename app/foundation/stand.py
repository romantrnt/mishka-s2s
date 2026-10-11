from __future__ import annotations

import base64
import binascii
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import math
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

from .api import build_application, create_app
from .config import Settings, settings_with_compat_env
from .events import EventValidationError, parse_event
from .domain import Lifecycle
from .hardware import (HardwareFrame, PTYUARTSimulator, PosixSerialTransport,
                       SerialHardwareAdapter, decode_frame, encode_frame)
from app.scenarios.langgraph_tree import describe_scenario
from .s2s import AudioChunk
from .led_contract import BUTTON_LED_COLORS, button_contract, canonical_led_state, fixed_button_colors


AUDIO_SAMPLE_RATES = {16000, 22050, 24000, 48000}
MAX_AUDIO_SECONDS = 15
MAX_AUDIO_WAV_BYTES = 6 * 1024 * 1024
_AUTH_QUERY_KEYS = frozenset({"token", "access_token", "stand_token"})
_QUERY_TOKEN_SCOPE_KEY = "_mishka_query_token"


class StandAuthQueryRedactionMiddleware:
    """Remove auth values from the ASGI scope before access logging.

    Canonical auth is header/cookie based. A query token can be retained only
    in a private scope slot for the explicitly enabled dev console
    compatibility path; the query string itself is always sanitized before
    downstream application handling and access logging.
    """

    def __init__(self, app, allow_query_token: bool = False):
        self.app = app
        self.allow_query_token = bool(allow_query_token)

    async def __call__(self, scope, receive, send):
        if scope.get("type") in {"http", "websocket"}:
            raw_query = scope.get("query_string", b"")
            if raw_query:
                pairs = parse_qsl(raw_query.decode("latin-1"), keep_blank_values=True)
                path = str(scope.get("path", ""))
                console_path = path == "/console" or path.startswith("/console/")
                query_token = next((value for key, value in pairs if key.lower() == "token"), None)
                sanitized = [
                    (key, "<redacted>" if key.lower() in _AUTH_QUERY_KEYS else value)
                    for key, value in pairs
                ]
                scope = dict(scope)
                if self.allow_query_token and scope.get("type") == "http" and console_path and query_token is not None:
                    scope[_QUERY_TOKEN_SCOPE_KEY] = query_token
                if any(key.lower() in _AUTH_QUERY_KEYS for key, _ in pairs):
                    scope["query_string"] = urlencode(sanitized).encode("latin-1")
        await self.app(scope, receive, send)


class AudioInputError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class StandHub:
    """Broadcast hub with one write lease for the active browser session."""
    def __init__(self):
        self.clients: set[Any] = set()
        self._owner: Any | None = None
        self._lease_generation = 0

    async def connect(self, websocket) -> bool:
        self.clients.add(websocket)
        if self._owner is None:
            self._owner = websocket
            self._lease_generation += 1
        return self.is_owner(websocket)

    def disconnect(self, websocket):
        self.clients.discard(websocket)
        if self._owner is websocket:
            self._owner = None
            self._lease_generation += 1

    def is_owner(self, websocket) -> bool:
        return self._owner is websocket

    def lease_snapshot(self) -> dict[str, Any]:
        return {"owner_connected": self._owner is not None, "client_count": len(self.clients), "generation": self._lease_generation}

    async def publish(self, payload: dict[str, Any]) -> list[dict[str, str]]:
        dead=[]; failures: list[dict[str, str]] = []
        for client in tuple(self.clients):
            try: await client.send_json(payload)
            except Exception as exc:
                dead.append(client)
                failures.append({"exception_class": type(exc).__name__, "reason": str(exc)[:160]})
        for client in dead: self.disconnect(client)
        return failures


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
    def __init__(self, adapter: SerialHardwareAdapter): self.adapter=adapter

    @staticmethod
    def _fixed_colors(button_ids):
        return fixed_button_colors(button_ids)

    def pattern(self, pattern_id, color, duration_ms, token):
        # The physical LEDs are single-colour.  Ignore arbitrary requested
        # colours and emit the immutable colour of each addressed button.
        ids = () if str(pattern_id) == "off" or str(color).lower() == "off" else _pattern_button_ids(pattern_id)
        self.adapter.send({"type":"led_pattern","payload":{"pattern":pattern_id,
                           "colors":self._fixed_colors(ids),
                           "duration_ms":duration_ms,"token":token}})

    def pattern_map(self, pattern_id, colors, duration_ms, token):
        # Preserve off entries, but never allow a caller to recolour a button.
        button_ids = [int(key) for key, value in (colors or {}).items()
                      if str(key).isdigit() and int(key) in BUTTON_LED_COLORS and str(value).lower() != "off"]
        self.adapter.send({"type":"led_pattern","payload":{"pattern":pattern_id,
                           "colors":self._fixed_colors(button_ids),
                           "duration_ms":duration_ms,"token":token}})


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


class HardwareIntegrationStand:
    """One simulated installation connected to the real serial protocol boundary."""
    def __init__(self, settings: Settings | None = None, content_root: str | Path | None = None):
        self.settings=settings or Settings.from_env(); self.hub=StandHub(); self.pty=PTYUARTSimulator()
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
        self.playback=BrowserPlaybackController(self.hub)
        self.application,self.scenarios,self.metrics,self.publisher=build_application(
            content_root,self.settings,audio_output=UARTAudioOutput(self.adapter),
            led_controller=UARTLEDController(self.adapter),display_controller=UARTDisplayController(self.adapter),playback_override=self.playback)
        self.application.audio_update_hook=self._publish_audio_completion
        self.application.dependency_identity = {"hardware_backend": {"name": "pty_uart_simulator", "state": "simulator", "acceptance": "not_physical_hardware"}}
        self.playback.metrics=self.application.runtime_manager.engine.metrics
        self._event_cursor=0; self._device_buffer=b""; self.responses,self.stories,self.games=self._load_content(content_root)
        self._story_wav_cache: dict[str, bytes] = {}
        self._story_audio_tasks: set[asyncio.Task[Any]] = set()
        self._response_audio_tasks: set[asyncio.Task[Any]] = set()
        self._notification_tasks: set[asyncio.Task[Any]] = set()
        self.graph=describe_scenario(self.scenarios.get("fixture_core"))
        self.adapter.send({"type":"capabilities_request","payload":{"protocol_version":"1"}})
        self._drain_device_commands()
        self._device_write("capabilities",{"buttons":7,"led":True,"display":True,"audio_stop":True,
                           "firmware":"pty-simulator-1"})
        for frame in self._receive_frames_sync():
            if frame.type=="capabilities":
                self.adapter.capabilities=dict(frame.payload); self.adapter.firmware_version=str(frame.payload.get("firmware",""))

    async def _offload_uart(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._uart_executor, fn, *args)

    async def _offload_audio(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._audio_executor, fn, *args)

    def _load_content(self, content_root):
        project_root = Path(__file__).resolve().parents[2]
        candidate = Path(content_root).expanduser() if content_root else project_root / "content"
        root = candidate if candidate.is_absolute() else project_root / candidate
        def load(folder: str, key: str) -> dict[str, Any]:
            try:
                value=json.loads((root/folder).read_text(encoding="utf-8"))[key]
                return value if isinstance(value,dict) else {}
            except (OSError,KeyError,json.JSONDecodeError): return {}
        return load("responses/phrases.json","responses"),load("stories/stories.json","stories"),load("games/games.json","games")

    async def story_wav(self, story_id: str) -> bytes | None:
        """Render only an allowlisted story; never synthesize arbitrary input."""
        story = self.stories.get(story_id)
        if not isinstance(story, dict) or not isinstance(story.get("text"), str):
            return None
        cached = self._story_wav_cache.get(story_id)
        if cached is not None:
            return cached
        engine = self.application.runtime_manager.engine
        audio = await engine.tts.synthesize(story["text"], 0, lambda: False)
        audio = await engine.voice.convert(audio, lambda: False)
        pcm = struct.pack("<" + "h" * len(audio.samples),
                          *(round(value * 32767) for value in audio.samples))
        output = BytesIO()
        with wave.open(output, "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(audio.sample_rate)
            target.writeframes(pcm)
        result = output.getvalue()
        if len(self._story_wav_cache) >= 5:
            self._story_wav_cache.pop(next(iter(self._story_wav_cache)))
        self._story_wav_cache[story_id] = result
        return result

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
                item["decoded"]={"type":frame.type,"sequence":frame.sequence,"payload":frame.payload,"checksum":frame.checksum}
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
                    response=self.responses.get(str(payload.get("response_id")),{})
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

    async def _publish_audio_completion(self, session_id: str, payload: dict[str, Any]) -> None:
        self.results.append({"at":time.time(),"input":"audio_completion","result":payload})
        failures = await self.hub.publish({"type":"stand_state","state":self.snapshot(),"audio_completion":payload})
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
                await self.hub.publish({"type":"stand_state","state":self.snapshot()})
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
        await self.hub.publish({"type":"stand_state","state":self.snapshot()})
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
        self.application.publisher.clear()
        metrics=getattr(getattr(self.application, "runtime_manager", None), "engine", None)
        if metrics is not None and hasattr(metrics, "metrics"):
            metrics.metrics.clear()
        self.adapter.send({"type":"audio_stop","payload":{"token":0}}); self.adapter.send({"type":"led_pattern","payload":{"pattern":"off","color":"off","duration_ms":0,"token":0}})
        self._drain_device_commands(); await self.hub.publish({"type":"stand_state","state":self.snapshot()}); return result

    def _enabled_buttons(self, session):
        """Return buttons accepted by the active scenario node."""
        guest_buttons = [1, 2, 3, 4, 5, 7]
        if session is not None and session.visual_only:
            return guest_buttons
        if session is None:
            # Physical play buttons remain usable before RFID/handset.
            # This is visual-only and deliberately excludes Mishka/LLM.
            return guest_buttons
        if session.state.value not in ("ACTIVE", "FALLBACK", "ENDING"):
            if session.child_id and session.state.value == "AUTHENTICATED":
                return guest_buttons
            if session.rfid_tag is None and session.current_node is None:
                return guest_buttons
            return []
        if session.child_id and session.scenario_id == "fixture_core" and not session.handset_up:
            return guest_buttons
        if session.current_node in {"fixture_welcome", "fixture_choose"}:
            return list(range(1, 8))
        scenario = self.application.scenarios.get(session.scenario_id or "fixture_core")
        nodes = scenario.get("nodes", []) if isinstance(scenario, dict) else getattr(scenario, "nodes", [])
        node = next((item for item in nodes if item.get("id") == (session.current_node or "")), {}) if nodes else {}
        transitions = node.get("transitions", {}) if isinstance(node, dict) else {}
        buttons = []
        for key in transitions:
            if isinstance(key, str) and key.startswith("button_"):
                try:
                    buttons.append(int(key[7:]))
                except ValueError:
                    continue
        if getattr(session, "game_id", None) and not buttons:
            buttons = list(range(1, 8))
        if session.rfid_tag is not None:
            buttons.extend((6, 7))
        return sorted(set(buttons))

    def snapshot(self):
        session=self.application.sessions.get(self.active_session_id)
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
                "led":canonical_led_state(self.commands),
                "button_contract":button_contract(),
                "latencies":list(self.latencies),"pipeline_metrics":self.application.runtime_manager.engine.metrics.snapshot(),
                "graph":self.graph,
                "errors":list(self.errors),"profile":self.settings.profile}

    def close(self):
        self._cancel_story_audio(); self._cancel_response_audio()
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
    from fastapi.responses import FileResponse, JSONResponse, Response
    globals()["WebSocket"] = WebSocket
    globals()["Request"] = Request
    settings = settings_with_compat_env(settings, ("STAND_ACCESS_TOKEN",)) if settings is not None else None
    stand=HardwareIntegrationStand(settings); api=create_app(stand.application); api.add_middleware(StandAuthQueryRedactionMiddleware, allow_query_token=stand.settings.allow_query_token); api.state.stand=stand
    console_root=Path(__file__).with_name("console")
    access_token=stand.settings.stand_access_token

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
        protected=request.url.path.startswith("/stand") or request.url.path.startswith("/console")
        supplied=request.headers.get("x-stand-token") or request.cookies.get("stand_token") or ""
        if not supplied and stand.settings.allow_query_token and (request.url.path == "/console" or request.url.path.startswith("/console/")):
            supplied=str(request.scope.get(_QUERY_TOKEN_SCOPE_KEY, ""))
            if not supplied:
                raw_query = request.scope.get("query_string", b"")
                pairs = parse_qsl(raw_query.decode("latin-1"), keep_blank_values=True)
                supplied=next((value for key, value in pairs if key.lower() == "token"), "")
                if supplied:
                    request.scope[_QUERY_TOKEN_SCOPE_KEY] = supplied
                    request.scope["query_string"] = urlencode([(key, "<redacted>" if key.lower() in _AUTH_QUERY_KEYS else value) for key, value in pairs]).encode("latin-1")
        if protected and access_token and not secrets.compare_digest(supplied,access_token):
            return JSONResponse({"error":{"code":"auth_failure","message":"stand authentication required"}},status_code=401)
        lifecycle = getattr(api.state, "lifecycle", None)
        if (request.url.path.startswith("/stand/")
                and request.url.path not in {"/stand/reset", "/stand/state", "/stand/graph"}
                and lifecycle is not None
                and not lifecycle.admits_users()):
            snapshot = lifecycle.snapshot()
            return JSONResponse({"error":{"code":"runtime_unavailable","message":"stand runtime is not ready","reason":_admission_reason(snapshot),"state":snapshot.get("state")}},status_code=503)
        response=await call_next(request)
        if stand.settings.allow_query_token and request.scope.get(_QUERY_TOKEN_SCOPE_KEY) and request.url.path == "/console" and access_token:
            response.set_cookie("stand_token", access_token, path="/console", httponly=True, samesite="lax")
        return response

    @api.get("/console",include_in_schema=False)
    def console(): return FileResponse(console_root/"index.html")
    @api.get("/console/console.js",include_in_schema=False)
    def console_js(): return FileResponse(console_root/"console.js",media_type="application/javascript")
    @api.get("/console/console.css",include_in_schema=False)
    def console_css(): return FileResponse(console_root/"console.css",media_type="text/css")
    @api.get("/console/button-cap.svg",include_in_schema=False)
    def console_button_cap(): return FileResponse(console_root/"button-cap.svg",media_type="image/svg+xml")
    @api.get("/stand/state")
    def state(): return stand.snapshot()
    @api.get("/stand/graph")
    def graph(): return stand.graph
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
    @api.post("/stand/rfid")
    async def rfid(payload:dict): return transport_response(await stand.inject("rfid",{"tag_id":str(payload.get("tag_id",""))},payload.get("session_id")))
    @api.post("/stand/handset")
    async def handset(payload:dict):
        sid = payload.get("session_id")
        if str(payload.get("state", "")) == "up" and sid:
            existing = stand.application.sessions.get(str(sid))
            if existing is not None and getattr(existing, "current_node", None) and bool(getattr(existing, "handset_up", False)) and str(getattr(existing, "state", "")) in {"Lifecycle.ACTIVE", "Lifecycle.FALLBACK", "ACTIVE", "FALLBACK"}:
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
        if access_token and not secrets.compare_digest(supplied,access_token):
            await _reject_websocket_admission(
                websocket,
                code=1008,
                error_code="auth_failure",
                reason="invalid_token",
                state="AUTH_REQUIRED",
            )
            return
        lifecycle = getattr(api.state, "lifecycle", None)
        if lifecycle is not None and not lifecycle.admits_users():
            snapshot = lifecycle.snapshot()
            await _reject_websocket_admission(
                websocket,
                code=1013,
                error_code="runtime_unavailable",
                reason=_admission_reason(snapshot),
                state=str(snapshot.get("state", "DEGRADED")),
            )
            return
        await websocket.accept(); owner = await stand.hub.connect(websocket); await websocket.send_json({"type":"stand_state","state":stand.snapshot(),"lease":stand.hub.lease_snapshot(),"write_owner":owner})
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
                    session_id = message.get("session_id")
                    if session_id:
                        session_ids.add(str(session_id))
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
                        if not stand.hub.is_owner(websocket):
                            await websocket.send_json({"type":"error","error":{"code":"conflict","message":"stand input lease is owned by another client"}})
                            continue
                        result = await stand.inject(kind,{k:v for k,v in message.items() if k not in {"type","session_id"}},message.get("session_id"))
                        if isinstance(result, dict) and result.get("error"):
                            error = result["error"]
                            await websocket.send_json({"type":"error","error":{"code":error.get("code","internal_error"),"message":error.get("message","stand input processing failed")}})
                    elif kind=="reset": await stand.reset(message.get("session_id"))
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
            # A disconnected browser is an explicit audio/session boundary.
            # Cancel and await all owned work before releasing the session;
            # otherwise a late STT/TTS completion can publish into a future
            # browser session or keep the old PCM generation alive.
            if not stand.hub.clients:
                for session_id in session_ids or {stand.active_session_id}:
                    await stand.application.reset_session(session_id)
                await stand.playback.stop()
    return api
