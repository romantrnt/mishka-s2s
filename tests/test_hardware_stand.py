import asyncio
import base64
import io
import math
import os
import struct
import wave

import pytest
from fastapi.testclient import TestClient

from app.foundation.hardware import SerialHardwareAdapter, decode_frame, encode_frame
from fastapi import WebSocketDisconnect

from app.foundation.stand import HardwareIntegrationStand, create_stand_app, decode_wav
from app.foundation.led_contract import BUTTON_LED_COLORS, button_contract, canonical_led_state, validate_button_contract
from app.foundation.config import Settings
def websocket_address(base: str, token: str = "") -> str:
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):] + "/stand/ws"
    if base.startswith("http://"):
        return "ws://" + base[len("http://"):] + "/stand/ws"
    raise ValueError("--url must start with http:// or https://")


def decode_output_pcm(message: dict) -> tuple[tuple[int, ...], int, int]:
    if message.get("type") != "audio_pcm_output":
        raise ValueError("not an audio_pcm_output frame")
    try:
        sample_rate = int(message["sample_rate"])
        token = int(message["token"])
        raw = base64.b64decode(str(message["data_b64"]), validate=True)
    except (KeyError, TypeError, ValueError, base64.binascii.Error) as exc:
        raise ValueError("malformed audio PCM output") from exc
    if sample_rate <= 0 or not raw or len(raw) % 2:
        raise ValueError("audio output must be non-empty little-endian PCM16")
    samples = struct.unpack("<" + "h" * (len(raw) // 2), raw)
    if not all(math.isfinite(value) and -32768 <= value <= 32767 for value in samples):
        raise ValueError("audio output contains invalid PCM samples")
    return samples, sample_rate, token


pytestmark = pytest.mark.hardware
CHILD_TAG = "mock-child-001"
ADULT_TAG = "mock-adult-dev-001"

def fixture_settings():
    return Settings(profile="mock").validate()

def run(value): return asyncio.run(value)


def test_pty_uart_roundtrip_uses_crc_boundary_for_controls_and_commands():
    stand=HardwareIntegrationStand(settings=fixture_settings())
    try:
        assert stand.snapshot()["transport"]["hardware"]["firmware_version"]=="pty-simulator-1"
        assert stand.snapshot()["transport"]["hardware"]["capabilities"]["buttons"]==7
        assert run(stand.inject("rfid",{"tag_id":CHILD_TAG},"pty"))["state"]=="AUTHENTICATED"
        lifted=run(stand.inject("handset",{"state":"up"},"pty"))
        assert lifted["node"]=="fixture_welcome"
        state=stand.snapshot(); directions={item["direction"] for item in state["raw_frames"]}
        assert directions=={"device_to_host","host_to_device"}
        assert {item["type"] for item in state["commands"]}>={"capabilities_request","led_pattern","audio_play"}
        assert any(command["payload"].get("resolved_text","").startswith("Привет, Fixture") for command in state["commands"])
        latest_audio = next(command for command in reversed(state["commands"]) if command["type"] == "audio_play")
        assert latest_audio["payload"]["resolved_text"].startswith("Привет, Fixture")
        held=run(stand.inject("button_hold",{"button_id":1,"duration_ms":20000},"pty"))
        assert held["state"]=="ENDED"
        assert stand.snapshot()["led"]["pattern"] == "off"
        assert stand.snapshot()["led"]["colors"] == {}
    finally: stand.close()


def test_uart_led_commands_enforce_immutable_button_colours():
    stand=HardwareIntegrationStand(settings=fixture_settings())
    try:
        stand.application.led.pattern("custom", "white", 500, 1)
        stand._drain_device_commands()
        command=stand.snapshot()["commands"][-1]
        assert command["type"] == "led_pattern"
        assert command["payload"]["colors"] == {
            "1": "red", "2": "orange", "3": "yellow", "4": "green",
            "5": "cyan", "6": "blue", "7": "purple",
        }
        stand.application.led.pattern("buttons_2_6", "red", 500, 1)
        stand._drain_device_commands()
        command=stand.snapshot()["commands"][-1]
        assert command["payload"]["colors"] == {"2": "orange", "6": "blue"}
    finally:
        stand.close()



def test_runtime_led_state_is_canonical_for_ui_and_physical_signal():
    stand = HardwareIntegrationStand(settings=fixture_settings())
    try:
        stand.application.led.pattern_map("rfid_wave", {str(k): v for k, v in BUTTON_LED_COLORS.items()}, 1200, 4)
        stand._drain_device_commands()
        state = stand.snapshot()
        assert state["led"]["valid"] is True
        assert state["led"]["pattern"] == "rfid_wave"
        assert state["led"]["colors"] == {str(k): v for k, v in BUTTON_LED_COLORS.items()}
        assert state["led"] == canonical_led_state(state["commands"])

        stand.commands.extend((
            {"type": "led_pattern", "sequence": 99, "payload": {"pattern": "buttons_2", "color": "red"}},
            {"type": "led_pattern", "sequence": 98, "payload": {"pattern": "buttons_6", "color": "blue"}},
        ))
        assert canonical_led_state(stand.commands)["sequence"] == 99
        assert canonical_led_state(stand.commands)["colors"] == {"2": "orange"}
    finally:
        stand.close()


def test_malformed_led_state_is_degraded_not_silent_idle():
    state = canonical_led_state([{
        "type": "led_pattern", "sequence": 12,
        "payload": {"pattern": "invalid", "color": "magenta"},
    }])
    assert state["valid"] is False
    assert state["pattern"] == "invalid"
    assert state["color"] == "invalid"


def test_bad_crc_is_rejected_at_serial_adapter_not_application():
    stand=HardwareIntegrationStand(settings=fixture_settings())
    try:
        raw=encode_frame("button",99,{"button_id":1,"state":"pressed"}).replace(b'"checksum":"',b'"checksum":"0',1)
        result=run(stand.inject_raw(raw))
        assert result["error"]["type"]=="ValueError"
        assert stand.snapshot()["errors"][-1]["stage"]=="raw_uart"
        assert stand.application.sessions.get("stand") is None
    finally: stand.close()


def test_malformed_json_values_are_rejected_as_protocol_errors():
    for raw in (b"null\n", b"1\n", b"[[]]\n", b"{}\n"):
        try:
            decode_frame(raw)
        except ValueError as exc:
            assert str(exc)=="invalid frame schema"
        else:
            raise AssertionError(f"malformed frame accepted: {raw!r}")


def test_text_and_pcm_enter_bounded_s2s_through_uart_frames():
    stand=HardwareIntegrationStand(settings=fixture_settings())
    try:
        run(stand.inject("rfid",{"tag_id":CHILD_TAG},"speech"))
        run(stand.inject("handset",{"state":"up"},"speech"))
        run(stand.inject("button",{"button_id":3,"state":"pressed"},"speech"))
        text=run(stand.inject("text",{"text":"fixture"},"speech"))
        assert text["reason"]=="bounded_s2s" and any(item["action"]=="s2s_text" for item in text["outputs"])
        voiced=tuple(.2*math.sin(index/5) for index in range(6400))
        audio=run(stand.inject_pcm(voiced,16000,"speech"))
        assert audio["reason"]=="bounded_s2s_audio" and audio["audio_status"]=="speech_end"
        # Once the scenario leaves the listening node, a late PCM frame is
        # ignored rather than returned as a transport error.
        run(stand.inject("text",{"text":"Мне нужен диагноз"},"speech"))
        late=run(stand.inject_pcm((0.0,)*512,16000,"speech",flush_silence=False))
        assert late["reason"]=="audio_not_listening" and late["audio_status"]=="ignored"
        assert any(item["decoded"]["type"]=="audio_pcm" for item in stand.snapshot()["raw_frames"] if "decoded" in item)
        assert "stt" in stand.snapshot()["pipeline_metrics"]
    finally: stand.close()


def test_duplicate_pcm_sequence_is_rejected_while_vad_is_collecting():
    stand=HardwareIntegrationStand(settings=fixture_settings())
    try:
        run(stand.inject("rfid",{"tag_id":CHILD_TAG},"audio-duplicate"))
        run(stand.inject("handset",{"state":"up"},"audio-duplicate"))
        run(stand.inject("button",{"button_id":3},"audio-duplicate"))
        first=run(stand.application.receive_audio_async("audio-duplicate",(0.0,)*512,100))
        duplicate=run(stand.application.receive_audio_async("audio-duplicate",(0.0,)*512,100))
        assert first["audio_status"] == "collecting"
        assert duplicate["reason"] == "late_or_duplicate_audio"
    finally: stand.close()


def test_concurrent_pcm_frames_do_not_duplicate_vad_acknowledgement():
    """Adjacent WebSocket frames are serialized before they mutate VAD state."""
    stand=HardwareIntegrationStand(settings=fixture_settings())
    try:
        async def flow():
            await stand.inject("rfid", {"tag_id":CHILD_TAG}, "pcm-order")
            await stand.inject("handset", {"state":"up"}, "pcm-order")
            await stand.inject("button", {"button_id":3}, "pcm-order")
            for _ in range(10):
                await stand.inject("audio_pcm", {"encoding":"f32le","sample_rate":16000,"channels":1,"data_b64":base64.b64encode(struct.pack("<"+"f"*512,*([0.2]*512))).decode()}, "pcm-order", wait_for_audio=False)
            await asyncio.gather(*(
                stand.inject("audio_pcm", {"encoding":"f32le","sample_rate":16000,"channels":1,"data_b64":base64.b64encode(struct.pack("<"+"f"*512,*([0.0]*512))).decode()}, "pcm-order", wait_for_audio=False)
                for _ in range(8)
            ))
        run(flow())
        events=stand.snapshot()["pipeline_metrics"]["events"]
        ack_events = [item for item in events if item["event"] == "audio_ack_requested"]
        assert len(ack_events) <= 1
    finally: stand.close()


def test_live_pcm_collecting_does_not_broadcast_full_state_per_frame():
    stand=HardwareIntegrationStand(settings=fixture_settings())
    published=[]
    async def record(payload): published.append(payload)
    stand.hub.publish=record
    try:
        async def flow():
            await stand.inject("rfid", {"tag_id":CHILD_TAG}, "pcm-bandwidth")
            await stand.inject("handset", {"state":"up"}, "pcm-bandwidth")
            await stand.inject("button", {"button_id":3}, "pcm-bandwidth")
            published.clear()
            for _ in range(20):
                await stand.inject("audio_pcm", {"encoding":"f32le","sample_rate":16000,"channels":1,
                    "data_b64":base64.b64encode(struct.pack("<"+"f"*512,*([0.0]*512))).decode()},
                    "pcm-bandwidth", wait_for_audio=False)
        run(flow())
        # PCM collection must not broadcast full state for every frame.
        # A cancellation command from the preceding transition is valid.
        assert all(item.get("type") == "audio_stop" for item in published)
        state=stand.snapshot()
        assert not [item for item in state["results"] if item["input"]=="audio_pcm"]
        pcm_frames=[item for item in state["raw_frames"] if item.get("decoded",{}).get("type")=="audio_pcm"]
        assert len(pcm_frames)==20
        assert all("data_b64" not in item["raw"] and "data_b64" not in item["decoded"]["payload"] for item in pcm_frames)
    finally: stand.close()


def test_pcm_without_audio_session_is_ignored_without_latency_or_snapshot_flood():
    stand=HardwareIntegrationStand(settings=fixture_settings())
    published=[]
    async def record(payload): published.append(payload)
    stand.hub.publish=record
    try:
        raw=base64.b64encode(struct.pack("<"+"f"*512,*([0.0]*512))).decode()
        result=run(stand.inject("audio_pcm", {
            "encoding":"f32le", "sample_rate":16000, "channels":1,
            "data_b64":raw,
        }, "idle-pcm", wait_for_audio=False))
        assert result["reason"] == "audio_session_required"
        assert not stand.snapshot()["latencies"]
        assert not [item for item in stand.snapshot()["results"] if item["input"] == "audio_pcm"]
        assert published == []
        assert len([item for item in stand.snapshot()["raw_frames"] if item.get("decoded", {}).get("type") == "audio_pcm"]) == 1
    finally:
        stand.close()


def test_new_text_stops_old_browser_pcm_even_after_previous_turn_completed():
    stand=HardwareIntegrationStand(settings=fixture_settings())
    published=[]
    original_publish=stand.hub.publish
    async def record(payload):
        published.append(payload)
        await original_publish(payload)
    stand.hub.publish=record
    try:
        async def flow():
            await stand.inject("rfid", {"tag_id":ADULT_TAG}, "text-barge")
            await stand.inject("handset", {"state":"up"}, "text-barge")
            await stand.inject("text", {"text":"Расскажи про зебру"}, "text-barge")
            first_audio=next(item for item in published if item.get("type")=="audio_pcm_output")
            first_token=first_audio["token"]
            boundary=len(published)
            await stand.inject("text", {"text":"Привет"}, "text-barge")
            later=published[boundary:]
            stop_index=next(index for index,item in enumerate(later) if item.get("type")=="audio_stop")
            assert all(item.get("token")!=first_token for item in later[stop_index+1:] if item.get("type")=="audio_pcm_output")
            new_audio = [item for item in later if item.get("type")=="audio_pcm_output" and item.get("token")!=first_token]
            assert new_audio
            assert not stand._response_audio_tasks
            snapshot = stand.application.bounded_s2s.resource_snapshot()
            assert snapshot["active_tasks"] == 0
            assert snapshot["active_sessions"] == 0
            assert snapshot["queued_chunks"] == 0
            assert snapshot["pcm_bytes"] == 0
        run(flow())
    finally: stand.close()


def test_text_barge_in_returns_structured_cancellation_not_http_500():
    stand=HardwareIntegrationStand(settings=fixture_settings())

    class SlowLLM:
        async def stream(self, _messages, _request_id, cancelled):
            yield "<say>"
            await asyncio.sleep(.15)
            if cancelled(): raise asyncio.CancelledError
            yield "Тестовая реплика.</say>"

    stand.application.bounded_s2s.llm=SlowLLM()
    try:
        async def flow():
            await stand.inject("rfid", {"tag_id":ADULT_TAG}, "overlap")
            await stand.inject("handset", {"state":"up"}, "overlap")
            first=asyncio.create_task(stand.inject("text", {"text":"Расскажи про зебру"}, "overlap"))
            await asyncio.sleep(.02)
            second=await stand.inject("text", {"text":"Привет"}, "overlap")
            return await first,second
        first,second=run(flow())
        assert first["reason"]=="cancelled_by_new_input"
        assert first["outputs"]==[]
        assert second["reason"]=="bounded_s2s"
        assert second["node"]=="dev_dialogue"
        assert not stand.snapshot()["errors"]
    finally: stand.close()


def test_release_fixture_supports_natural_dialogue_and_story_audio_resolution():
    stand=HardwareIntegrationStand(settings=fixture_settings())
    try:
        run(stand.inject("rfid", {"tag_id":CHILD_TAG}, "tester"))
        run(stand.inject("handset", {"state":"up"}, "tester"))
        conversation=run(stand.inject("button", {"button_id":3}, "tester"))
        assert conversation["node"] == "fixture_free"
        turn=run(stand.inject("text", {"text":"Как настроение?"}, "tester"))
        assert turn["reason"] in {"quick_reply", "bounded_s2s"} and any(item["action"] in {"quick_reply", "s2s_text"} for item in turn["outputs"])

        run(stand.reset("tester"))
        run(stand.inject("rfid", {"tag_id":CHILD_TAG}, "tester"))
        run(stand.inject("handset", {"state":"up"}, "tester"))
        run(stand.inject("button", {"button_id":2}, "tester"))
        selected=run(stand.inject("button", {"button_id":1}, "tester"))
        assert selected["node"] == "fixture_story_1"
        assert any(item["action"] == "story" for item in selected["outputs"])
        story_command=next(item for item in stand.snapshot()["commands"] if item["type"] == "audio_play" and item["payload"].get("content_kind") == "story")
        assert "Колобок" in story_command["payload"]["resolved_text"]
        assert not any(item["type"] == "story_audio_play" for item in stand.snapshot()["commands"])
        assert not any(item["type"] == "story_audio_schedule" for item in stand.snapshot()["commands"])
        finished=run(stand.inject("button", {"button_id":7}, "tester"))
        assert finished["node"] == "fixture_choose"
    finally: stand.close()


def test_fixture_dialogue_buttons_switch_modes_without_safety_fallback():
    stand=HardwareIntegrationStand(settings=fixture_settings())
    try:
        run(stand.inject("rfid", {"tag_id":CHILD_TAG}, "mode-switch"))
        run(stand.inject("handset", {"state":"up"}, "mode-switch"))
        run(stand.inject("button", {"button_id":3}, "mode-switch"))
        switched=run(stand.inject("button", {"button_id":2}, "mode-switch"))
        assert switched["node"] == "fixture_story_menu"
        assert not any(item.get("action") == "s2s_fallback" for item in switched["outputs"])
        returned=run(stand.inject("button", {"button_id":1}, "mode-switch"))
        assert returned["node"] == "fixture_story_1"
        run(stand.reset("mode-switch"))
        run(stand.inject("rfid", {"tag_id":CHILD_TAG}, "mode-switch"))
        run(stand.inject("handset", {"state":"up"}, "mode-switch"))
        run(stand.inject("button", {"button_id":3}, "mode-switch"))
        game=run(stand.inject("button", {"button_id":1}, "mode-switch"))
        assert game["node"] == "fixture_game_menu"
    finally: stand.close()


def test_fixture_dialogue_accepts_greeting_and_reports_bounded_fallback_reason():
    stand=HardwareIntegrationStand(settings=fixture_settings())
    try:
        run(stand.inject("rfid", {"tag_id":CHILD_TAG}, "greeting"))
        run(stand.inject("handset", {"state":"up"}, "greeting"))
        run(stand.inject("button", {"button_id":3}, "greeting"))
        greeting=run(stand.inject("text", {"text":"Привет"}, "greeting"))
        assert any(item["action"] in {"quick_reply", "s2s_text"} for item in greeting["outputs"])

        unknown=run(stand.inject("text", {"text":"Какая сегодня температура на Марсе?"}, "greeting"))
        assert {item.get("reason") for item in unknown["outputs"] if item.get("action")=="s2s_fallback"} == {"unknown_topic"}
    finally: stand.close()


def test_repeat_pattern_game_runs_on_seven_buttons_and_returns_to_menu():
    stand=HardwareIntegrationStand(settings=fixture_settings())
    try:
        run(stand.inject("rfid", {"tag_id":CHILD_TAG}, "game-pattern"))
        run(stand.inject("handset", {"state":"up"}, "game-pattern"))
        run(stand.inject("button", {"button_id":1}, "game-pattern"))
        run(stand.inject("button", {"button_id":4}, "game-pattern"))
        state = stand.snapshot()["led"]
        assert state["targets"] == [2]
        assert state["colors"] == {"2": BUTTON_LED_COLORS[2]}
        for button_id, next_button in ((2, 5), (5, 1), (1, 7)):
            result=run(stand.inject("button", {"button_id":button_id}, "game-pattern"))
            state = stand.snapshot()["led"]
            assert state["targets"] == [next_button]
            assert state["colors"] == {str(next_button): BUTTON_LED_COLORS[next_button]}
        result=run(stand.inject("button", {"button_id":7}, "game-pattern"))
        assert result["node"] == "fixture_choose"
        assert any(item["type"] == "GameCompleted" and item["payload"].get("success") for item in stand.snapshot()["events"])
    finally: stand.close()


def test_wav_decoder_is_ram_only_and_bounded():
    buffer=io.BytesIO()
    with wave.open(buffer,"wb") as target:
        target.setnchannels(1); target.setsampwidth(2); target.setframerate(16000)
        target.writeframes(struct.pack("<hhhh",0,1000,-1000,0))
    samples,rate=decode_wav(buffer.getvalue())
    assert rate==16000 and len(samples)==4 and samples[1]>0 and samples[2]<0


def test_allowlisted_story_is_rendered_as_real_wav_without_voice_input_storage():
    api=create_stand_app(Settings(profile="mock")); stand=api.state.stand
    try:
        endpoint=next(route.endpoint for route in api.routes if route.path=="/stand/audio/story/{story_id}")
        response=run(endpoint("story_1"))
        assert response.media_type=="audio/wav"
        with wave.open(io.BytesIO(response.body),"rb") as sound:
            assert sound.getnframes()>0 and sound.getframerate()>0
        assert run(endpoint("story_1")).body==response.body
        assert stand._story_wav_cache.keys()=={"story_1"}
        assert not any(item.get("decoded",{}).get("type")=="audio_pcm" for item in stand.raw_frames)
    finally: stand.close()


def test_stand_api_and_console_assets_are_registered():
    api=create_stand_app(); stand=api.state.stand
    try:
        paths={route.path for route in api.routes}
        assert {"/console","/stand/state","/stand/graph","/stand/rfid","/stand/handset","/stand/button","/stand/button-released","/stand/button-hold","/stand/button-long-hold","/stand/text","/stand/raw","/stand/audio/wav","/stand/audio/pcm","/stand/ws"}<=paths
        endpoint=next(route.endpoint for route in api.routes if route.path=="/console")
        assert str(endpoint().path).endswith("console/index.html")
        ws_route=next(route for route in api.routes if route.path=="/stand/ws")
        assert ws_route.dependant.websocket_param_name=="websocket"
        wav_route=next(route for route in api.routes if route.path=="/stand/audio/wav")
        assert wav_route.dependant.request_param_name=="request"
    finally: stand.close()


def test_websocket_disconnect_cancels_audio_session_and_releases_generation(monkeypatch):
    monkeypatch.setenv("STAND_ACCESS_TOKEN", "disconnect-secret")
    api = create_stand_app(Settings(profile="mock"))
    stand = api.state.stand
    sid = "disconnect-lifecycle"
    try:
        with TestClient(api) as client:
            with client.websocket_connect("/stand/ws", headers={"x-stand-token": "disconnect-secret"}) as websocket:
                websocket.receive_json()
                websocket.send_json({"type": "rfid", "tag_id": CHILD_TAG, "session_id": sid})
                websocket.send_json({"type": "handset", "state": "up", "session_id": sid})
            assert not stand.hub.clients
        assert stand.application.sessions.get(sid) is None
        assert not stand.application._audio_completions.get(sid)
    finally:
        stand.close()


def test_stand_ready_reports_validated_content_instead_of_external_placeholder():
    api=create_stand_app(Settings(profile="mock")); stand=api.state.stand
    try:
        ready_route=next(route.endpoint for route in api.routes if route.path=="/ready")
        result=run(ready_route())
        assert result["content"]["valid_scenarios"] == ["dev_core", "fixture_core"]
        assert result["content"]["issues"] == []
        assert result["ready"] is False
        assert result["lifecycle"]["state"] in {"BOOTING", "DEGRADED"}
        assert result["lifecycle"]["reason"]
        assert result["lifecycle"]["user_available"] is False
    finally: stand.close()


def test_websocket_pcm_framing_rejects_malformed_output_and_uses_auth_header():
    raw = struct.pack("<hh", -12, 34)
    frame = {"type": "audio_pcm_output", "sample_rate": 16000, "token": 7, "data_b64": base64.b64encode(raw).decode()}
    samples, rate, token = decode_output_pcm(frame)
    assert samples == (-12, 34) and rate == 16000 and token == 7
    with pytest.raises(ValueError):
        decode_output_pcm({**frame, "data_b64": "%%%"})
    with pytest.raises(ValueError):
        decode_output_pcm({**frame, "data_b64": base64.b64encode(b"x").decode()})
    assert websocket_address("https://example.test", "a+b") == "wss://example.test/stand/ws"


def test_stand_websocket_requires_auth_and_accepts_authorized_handshake(monkeypatch):
    monkeypatch.setenv("STAND_ACCESS_TOKEN", "smoke-secret")
    api = create_stand_app(Settings(profile="mock"))
    stand = api.state.stand
    try:
        client = TestClient(api)
        with client.websocket_connect("/stand/ws?token=wrong") as websocket:
            admission = websocket.receive_json()
            assert admission == {
                "type": "admission_error",
                "error": {
                    "code": "auth_failure",
                    "reason": "invalid_token",
                    "state": "AUTH_REQUIRED",
                    "user_available": False,
                    "retry_after_ms": 30000,
                },
            }
            with pytest.raises(WebSocketDisconnect) as failure:
                websocket.receive_json()
            assert failure.value.code == 1008

        with client.websocket_connect("/stand/ws", headers={"x-stand-token": "smoke-secret"}) as websocket:
            admission = websocket.receive_json()
            assert admission["type"] == "admission_error"
            assert admission["error"]["code"] == "runtime_unavailable"
            assert admission["error"]["reason"] in {
                "booting", "warming_up", "degraded", "resource_capacity_insufficient",
            }
            lifecycle = api.state.lifecycle.snapshot()
            assert lifecycle["ready"] is False
            assert lifecycle["state"] in {"BOOTING", "WARMING_UP", "DEGRADED"}
            assert lifecycle["reason"]
            assert admission["error"]["state"] == lifecycle["state"]
            with pytest.raises(WebSocketDisconnect) as failure:
                websocket.receive_json()
            assert failure.value.code == 1013

        assert stand.application.sessions.items == {}
        assert not stand.application._audio_completions
        assert not stand._response_audio_tasks
    finally:
        stand.close()


class _ChunkTransport:
    def __init__(self):
        self.chunks = []
        self.opened = 0
        self.writes = []
        self.fail_read = False

    def open(self):
        self.opened += 1

    def write(self, raw):
        self.writes.append(raw)

    def read_available(self):
        if self.fail_read:
            raise OSError("pty disconnected")
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        pass


def test_uart_adapter_accepts_partial_reads_and_multiple_frames_per_read():
    transport = _ChunkTransport()
    adapter = SerialHardwareAdapter(transport)
    adapter.connect()
    first = encode_frame("button", 1, {"button_id": 1})
    second = encode_frame("capabilities", 2, {"buttons": 7})
    transport.chunks = [first[:7], first[7:] + second]
    assert adapter.receive_available() == []
    frames = adapter.receive_available()
    assert [(frame.type, frame.sequence) for frame in frames] == [
        ("button", 1), ("capabilities", 2)
    ]


def test_uart_adapter_rejects_duplicate_and_gap_without_accepting_command():
    transport = _ChunkTransport()
    adapter = SerialHardwareAdapter(transport)
    adapter.connect()
    first = encode_frame("button", 1, {"button_id": 1})
    transport.chunks = [first]
    assert [frame.sequence for frame in adapter.receive_available()] == [1]

    transport.chunks = [first]
    with pytest.raises(ValueError, match="duplicate"):
        adapter.receive_available()

    transport.chunks = [encode_frame("button", 3, {"button_id": 3})]
    with pytest.raises(ValueError, match="gap"):
        adapter.receive_available()


def test_uart_adapter_disconnect_clears_partial_frame_and_reconnects_cleanly():
    transport = _ChunkTransport()
    adapter = SerialHardwareAdapter(transport)
    adapter.connect()
    transport.chunks = [encode_frame("button", 1, {"button_id": 1})[:5]]
    assert adapter.receive_available() == []
    transport.fail_read = True
    with pytest.raises(ConnectionError, match="hardware disconnected"):
        adapter.receive_available()
    assert adapter.connected is False
    transport.fail_read = False
    adapter.connect()
    assert adapter._read_buffer == b""
    transport.chunks = [encode_frame("capabilities", 1, {"buttons": 7})]
    assert adapter.receive_available()[0].type == "capabilities"


@pytest.mark.parametrize("raw", [
    b"not-json\n",
    b'{"protocol_version":"1","type":"button","sequence":1,"payload":{},"checksum":"00000000"}\n',
    encode_frame("button", 1, {"button_id": 1})[:-2],
    b"x" * 4097,
])
def test_uart_decoder_rejects_garbage_bad_crc_truncated_and_oversized_frames(raw):
    with pytest.raises(ValueError):
        decode_frame(raw)


def test_uart_adapter_keeps_valid_boundary_after_malformed_frame():
    transport = _ChunkTransport()
    adapter = SerialHardwareAdapter(transport)
    adapter.connect()
    valid = encode_frame("button", 1, {"button_id": 1})
    transport.chunks = [b"garbage\n" + valid]
    with pytest.raises(ValueError, match="invalid JSON"):
        adapter.receive_available()
    assert [frame.sequence for frame in adapter.receive_available()] == [1]


def test_ui_led_runtime_share_one_validated_button_contract():
    stand = HardwareIntegrationStand(settings=fixture_settings())
    try:
        contract = stand.snapshot()["button_contract"]
        assert validate_button_contract(contract) == []
        assert contract == button_contract()
        assert [(entry["id"], entry["color"]) for entry in contract["buttons"]] == list(BUTTON_LED_COLORS.items())
        duplicate = {**contract, "buttons": [*contract["buttons"], dict(contract["buttons"][0])]}
        assert any("duplicate button" in issue for issue in validate_button_contract(duplicate))
        missing = {**contract, "buttons": contract["buttons"][:-1]}
        assert any("missing buttons" in issue for issue in validate_button_contract(missing))
        invalid_color = {**contract, "buttons": [
            {**entry, "color": "white"} if entry["id"] == 7 else entry
            for entry in contract["buttons"]
        ]}
        assert any("invalid color" in issue for issue in validate_button_contract(invalid_color))
    finally:
        stand.close()
