"""Acceptance/smoke checks for the live canonical HTTP/WebSocket boundary."""

import asyncio
import json
import os
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest


pytestmark = [pytest.mark.acceptance, pytest.mark.smoke]
BASE = os.getenv("MISHKA_CANONICAL_URL", "http://127.0.0.1:8000").rstrip("/")


def _production_boundary():
    assert os.getenv("MISHKA_TEST_ENVIRONMENT") == "production", (
        "canonical acceptance requires the production test mode; "
        "run scripts/run_tests_production.sh acceptance"
    )
    token = os.getenv("STAND_ACCESS_TOKEN", "")
    assert token, "canonical acceptance requires STAND_ACCESS_TOKEN from explicit production env"
    return token


def _request(method, path, token=None, payload=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Stand-Token"] = token
    body = None if payload is None else json.dumps(payload).encode()
    request = Request(BASE + path, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def test_canonical_health_and_readiness_are_live_and_consistent():
    _production_boundary()
    health_status, health = _request("GET", "/health")
    ready_status, ready = _request("GET", "/ready")
    assert health_status == 200
    assert health.get("status") == "ok"
    assert ready_status == 200
    assert ready.get("ready") is True
    lifecycle = ready.get("lifecycle", {})
    assert lifecycle.get("state") == "READY"
    assert lifecycle.get("user_available") is True


def test_canonical_http_auth_and_validation_boundary():
    token = _production_boundary()
    state_status, state = _request("GET", "/stand/state", token)
    assert state_status == 200
    assert isinstance(state, dict)

    invalid_status, invalid = _request(
        "POST", "/stand/button", token,
        {"session_id": "wp04-acceptance", "button_id": 0},
    )
    assert invalid_status == 422
    assert invalid.get("error", {}).get("code") == "validation_error"

    unauthorized_status, unauthorized = _request("GET", "/stand/state")
    assert unauthorized_status == 401
    assert unauthorized.get("error", {}).get("code") == "auth_failure"


def test_canonical_websocket_auth_and_structured_validation_boundary():
    token = _production_boundary()

    async def flow():
        import websockets

        address = BASE.replace("https://", "wss://").replace("http://", "ws://") + "/stand/ws"
        async with websockets.connect(
            address,
            additional_headers={"X-Stand-Token": token},
            open_timeout=10,
            close_timeout=5,
        ) as websocket:
            first = json.loads(await asyncio.wait_for(websocket.recv(), 10))
            assert first.get("type") == "stand_state"
            await websocket.send(json.dumps({
                "type": "button", "session_id": "wp04-acceptance", "button_id": 0,
            }))
            error = json.loads(await asyncio.wait_for(websocket.recv(), 10))
            assert error.get("type") == "error"
            assert error.get("error", {}).get("code") == "validation_error"

    asyncio.run(flow())
