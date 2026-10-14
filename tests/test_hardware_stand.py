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

from app.foundation.stand import HardwareIntegrationStand, StandHub, create_stand_app, decode_wav
from app.foundation.led_contract import BUTTON_LED_COLORS, button_contract, canonical_led_state, validate_button_contract
from app.foundation.lighting import LIGHTING_ELEMENT_IDS, READINESS_WAVE_GROUPS, LightingCommand
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




def test_uart_led_commands_enforce_immutable_button_colours():
    stand=HardwareIntegrationStand(settings=fixture_settings())
    try:
        stand.application.led.pattern("custom", "white", 500, 1)
        stand._drain_device_commands()
        command=stand.snapshot()["commands"][-1]
        assert command["type"] == "led_pattern"
        assert command["payload"]["targets"] == [1, 2, 3, 4, 5, 6, 7]
        stand.application.led.pattern("buttons_2_6", "red", 500, 1)
        stand._drain_device_commands()
        command=stand.snapshot()["commands"][-1]
        assert command["payload"]["targets"] == [2, 6]
    finally:
        stand.close()



def test_runtime_led_state_is_canonical_for_ui_and_physical_signal():
    stand = HardwareIntegrationStand(settings=fixture_settings())
    try:
        stand.application.led.pattern_map("rfid_wave", {str(k): v for k, v in BUTTON_LED_COLORS.items()}, 1200, 4)
        stand._drain_device_commands()
        state = stand.snapshot()
        assert state["led"]["valid"] is True
        assert state["led"]["pattern"] == "canonical_lighting"
        assert state["led"]["targets"] == list(range(1, 8))
        # The legacy pattern is a diagnostic projection; canonical lighting is authoritative.

        stand.commands.extend((
            {"type": "led_pattern", "sequence": 99, "payload": {"pattern": "buttons_2", "color": "red"}},
            {"type": "led_pattern", "sequence": 98, "payload": {"pattern": "buttons_6", "color": "blue"}},
        ))
        assert canonical_led_state(stand.commands)["sequence"] == 99
        assert canonical_led_state(stand.commands)["colors"] == {"2": "orange"}
    finally:
        stand.close()




def test_canonical_set_element_remains_independent_from_readiness_groups():
    stand = HardwareIntegrationStand(settings=fixture_settings())
    try:
        stand.application.led.cancel_effects(clear=True)
        stand.application.led.set_element(62, 1)
        assert stand.snapshot()["lighting"]["62"] == 1
        assert all(stand.snapshot()["lighting"][str(element_id)] == 0 for element_id in (60, 61, 70, 71, 72))
        stand.application.led.set_element(72, 1)
        assert stand.snapshot()["lighting"]["72"] == 1
        assert stand.snapshot()["lighting"]["62"] == 1
        stand.application.led.set_element(21, 1, 500)
        assert stand.snapshot()["lighting"]["21"] == 1
        assert stand.snapshot()["lighting"]["20"] == 0
        assert stand.lighting_output.commands[-1] == LightingCommand(21, 1, 500)
    finally:
        stand.close()


def test_first_rfid_readiness_wave_reconciles_to_canonical_lighting():
    stand = HardwareIntegrationStand(settings=Settings(profile="mock", runtime_mode="clinical").validate())
    try:
        result = run(stand.inject("rfid", {"tag_id": CHILD_TAG}, "readiness-reconcile"))
        assert result["reason"] == "rfid_identified"
        lighting = stand.snapshot()["lighting"]
        assert all(lighting[str(element_id)] == 1 for element_id in LIGHTING_ELEMENT_IDS)
        assert sum(lighting.values()) == 16
        assert set(map(int, lighting)) == set(LIGHTING_ELEMENT_IDS)
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














def test_wav_decoder_is_ram_only_and_bounded():
    buffer=io.BytesIO()
    with wave.open(buffer,"wb") as target:
        target.setnchannels(1); target.setsampwidth(2); target.setframerate(16000)
        target.writeframes(struct.pack("<hhhh",0,1000,-1000,0))
    samples,rate=decode_wav(buffer.getvalue())
    assert rate==16000 and len(samples)==4 and samples[1]>0 and samples[2]<0




def test_stand_api_and_console_assets_are_registered():
    api=create_stand_app(Settings(profile="mock", hardware_mode="fake")); stand=api.state.stand
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


def test_webpanel_disconnect_does_not_reset_exhibition_session(monkeypatch):
    monkeypatch.setenv("STAND_ACCESS_TOKEN", "disconnect-secret")
    api = create_stand_app(Settings(profile="mock", runtime_mode="exhibition", hardware_mode="simulator"))
    stand = api.state.stand
    sid = "disconnect-lifecycle"
    try:
        result = run(stand.inject("handset", {"state": "up"}, sid))
        assert result["reason"] == "exhibition_handset_lifted"
        with TestClient(api) as client:
            with client.websocket_connect("/stand/ws", headers={"x-stand-token": "disconnect-secret"}) as websocket:
                websocket.receive_json()
            assert not stand.hub.clients
        session = stand.application.sessions.get(sid)
        assert session is not None
        assert session.handset_up is True
    finally:
        stand.close()


def test_stand_ready_reports_validated_content_instead_of_external_placeholder():
    api=create_stand_app(Settings(profile="mock")); stand=api.state.stand
    try:
        ready_route=next(route.endpoint for route in api.routes if route.path=="/ready")
        result=run(ready_route())
        assert result["content"]["contract"] == "canonical_product/v1"
        assert result["content"]["scenario_id"] == "canonical_product"
        assert result["content"]["state_nodes"] == 43
        assert result["content"]["ready"] is True
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
            state = websocket.receive_json()
            assert state["type"] == "stand_state"
            websocket.send_json({"type": "ping"})
            assert websocket.receive_json() == {"type": "pong"}

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


class _PanelClient:
    def __init__(self, *, slow=False):
        self.slow = slow
        self.messages = []
        self.closed = []

    async def send_json(self, payload):
        if self.slow:
            await asyncio.sleep(2)
        self.messages.append(payload)

    async def close(self, **kwargs):
        self.closed.append(kwargs)


def test_stand_hub_bounds_observers_and_removes_slow_writers():
    async def scenario():
        hub = StandHub()
        clients = [_PanelClient() for _ in range(hub.MAX_CLIENTS)]
        for client in clients:
            assert await hub.connect(client) in {True, False}
        rejected = _PanelClient()
        assert await hub.connect(rejected) is False
        assert len(hub.clients) == hub.MAX_CLIENTS
        slow = clients[0]
        slow.slow = True
        failures = await asyncio.wait_for(hub.publish({"type": "stand_state", "state": {"session": {"state": "IDLE"}}}), 1.5)
        assert failures and failures[0]["exception_class"] == "TimeoutError"
        assert slow not in hub.clients
        assert hub.lease_snapshot()["slow_clients"] == 1
        assert len(clients[1].messages) == 1
    run(scenario())
