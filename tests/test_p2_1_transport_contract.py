import json
import os

import pytest
from fastapi.testclient import TestClient

from app.foundation.config import Settings
from app.foundation.hardware import encode_frame
from app.foundation.stand import create_stand_app


def make_api(monkeypatch):
    monkeypatch.setenv("STAND_ACCESS_TOKEN", "transport-secret")
    api = create_stand_app(Settings(profile="mock"))
    api.state.lifecycle.admits_users = lambda: True
    return api


@pytest.mark.hardware
def test_invalid_button_inputs_are_transport_failures_not_success(monkeypatch):
    api = make_api(monkeypatch)
    stand = api.state.stand
    try:
        with TestClient(api) as client:
            for value in ("abc", "", 1.5, -1, 8, None):
                response = client.post(
                    "/stand/button",
                    headers={"x-stand-token": "transport-secret"},
                    json={"session_id": "matrix", "button_id": value},
                )
                assert response.status_code == 422
                assert response.json()["error"]["code"] == "validation_error"
                assert "ValueError" not in response.text
    finally:
        stand.close()


@pytest.mark.hardware
def test_raw_frame_validation_is_422_and_valid_frame_remains_200(monkeypatch):
    api = make_api(monkeypatch)
    stand = api.state.stand
    try:
        with TestClient(api) as client:
            headers = {"x-stand-token": "transport-secret"}
            for value in ("", "garbage", '{"type":"button"}'):
                response = client.post("/stand/raw", headers=headers, json={"frame": value})
                assert response.status_code == 422
                assert response.json()["error"]["code"] == "invalid_frame"
            valid = encode_frame("button", stand.adapter._last_received_sequence + 1, {"button_id": 1, "state": "pressed"}).decode()
            response = client.post("/stand/raw", headers=headers, json={"frame": valid})
            assert response.status_code == 200
            assert "error" not in response.json()
    finally:
        stand.close()


@pytest.mark.hardware
def test_malformed_json_and_auth_are_distinct_transport_errors(monkeypatch):
    api = make_api(monkeypatch)
    stand = api.state.stand
    try:
        with TestClient(api) as client:
            response = client.post(
                "/stand/button",
                headers={"x-stand-token": "transport-secret", "content-type": "application/json"},
                content=b"{bad",
            )
            assert response.status_code == 422
            assert response.json()["detail"][0]["type"] == "json_invalid"
            response = client.post("/stand/button", json={"button_id": 1})
            assert response.status_code == 401
            assert response.json()["error"]["code"] == "auth_failure"
    finally:
        stand.close()


@pytest.mark.hardware
def test_not_ready_http_boundary_is_503(monkeypatch):
    api = make_api(monkeypatch)
    stand = api.state.stand
    try:
        api.state.lifecycle.admits_users = lambda: False
        with TestClient(api) as client:
            response = client.post(
                "/stand/button",
                headers={"x-stand-token": "transport-secret"},
                json={"button_id": 1},
            )
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "runtime_unavailable"
    finally:
        stand.close()


@pytest.mark.hardware
def test_websocket_invalid_inputs_are_structured_and_connection_survives(monkeypatch):
    api = make_api(monkeypatch)
    stand = api.state.stand
    try:
        with TestClient(api) as client:
            with client.websocket_connect("/stand/ws", headers={"x-stand-token": "transport-secret"}) as ws:
                first = ws.receive_json()
                assert first["type"] == "stand_state"
                ws.send_json({"type": "button", "button_id": 8, "session_id": "ws-matrix"})
                error = ws.receive_json()
                assert error == {
                    "type": "error",
                    "error": {"code": "validation_error", "message": "button_id must be 1..7"},
                }
                ws.send_text("{bad")
                error = ws.receive_json()
                assert error == {
                    "type": "error",
                    "error": {"code": "invalid_json", "message": "WebSocket message must be valid JSON"},
                }
                ws.send_json({"type": "not-an-event"})
                error = ws.receive_json()
                assert error["type"] == "error"
                assert error["error"]["code"] == "unknown_message_type"
    finally:
        stand.close()


@pytest.mark.hardware
def test_valid_button_ids_keep_success_contract(monkeypatch):
    api = make_api(monkeypatch)
    stand = api.state.stand
    try:
        with TestClient(api) as client:
            headers = {"x-stand-token": "transport-secret"}
            for value in range(1, 8):
                client.post("/stand/reset", headers=headers, json={"session_id": "valid"})
                client.post("/stand/rfid", headers=headers, json={"session_id": "valid", "tag_id": "mock-child-001"})
                client.post("/stand/handset", headers=headers, json={"session_id": "valid", "state": "up"})
                response = client.post(
                    "/stand/button",
                    headers=headers,
                    json={"session_id": "valid", "button_id": value},
                )
                assert response.status_code == 200
                assert "error" not in response.json()
    finally:
        stand.close()
