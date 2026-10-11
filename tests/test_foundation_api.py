import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException, WebSocketDisconnect

from app.foundation.api import create_app

pytestmark = pytest.mark.integration

def test_foundation_api_exposes_liveness_readiness_mock_and_websocket_contract():
    app = create_app(include_test_routes=True)
    paths = {route.path for route in app.routes}
    assert {"/", "/health", "/live", "/ready", "/session/start", "/mock/rfid", "/mock/handset", "/mock/button", "/mock/text", "/mock/session/{session_id}", "/session/reset", "/ws/{session_id}"} <= paths
    route=next(route for route in app.routes if route.path=="/ws/{session_id}")
    assert route.dependant.websocket_param_name=="websocket"



def test_production_dependency_identity_has_no_ambiguous_labels():
    source = (Path(__file__).parents[1] / "app" / "foundation" / "api.py").read_text()
    assert "mock_or_external" not in source
    assert "simulator_or_external" not in source
    assert '"profile_provider"' in source
    assert '"hardware_backend"' in source


def test_production_surface_excludes_mock_and_session_control_routes():
    app = create_app(application=object())
    paths = {route.path for route in app.routes}
    assert "/mock/rfid" not in paths
    assert "/mock/handset" not in paths
    assert "/mock/button" not in paths
    assert "/mock/text" not in paths
    assert "/mock/session/{session_id}" not in paths
    assert "/session/start" not in paths
    assert "/session/reset" not in paths


def test_test_surface_keeps_fixture_control_routes():
    app = create_app(include_test_routes=True)
    paths = {route.path for route in app.routes}
    assert "/mock/rfid" in paths
    assert "/mock/text" in paths
    assert "/session/start" in paths
    assert "/session/reset" in paths

def test_offline_http_transport_start_validation_and_reset():
    app=create_app(include_test_routes=True); routes={route.path:route.endpoint for route in app.routes if hasattr(route,"endpoint")}
    assert routes["/"]()["status"] == "ok"
    assert routes["/health"]()["models_loaded"] is False
    readiness = asyncio.run(routes["/ready"]())
    assert readiness["ready"] is False
    assert readiness["lifecycle"]["state"] == "BOOTING"
    assert readiness["lifecycle"]["user_available"] is False
    assert asyncio.run(routes["/internal/s2s/warmup"]())["ok"] is True
    assert routes["/session/start"]({"session_id":"transport"})["state"]=="WAITING_RFID"
    with pytest.raises(ValueError): asyncio.run(routes["/mock/button"]({"session_id":"transport","button_id":9}))
    assert asyncio.run(routes["/session/reset"]({"session_id":"transport"}))["state"]=="RESET"


def test_offline_websocket_session_binding_and_structured_error():
    app=create_app(include_test_routes=True); endpoint=next(route.endpoint for route in app.routes if route.path=="/ws/{session_id}")
    class OfflineSocket:
        def __init__(self):
            self.incoming=[{"type":"handset_up","session_id":"other"},{"type":"button","button_id":99}]
            self.sent=[]
            self.close_calls=0
            self.close_code=None
            self.close_reason=None
        async def accept(self): pass
        async def receive_json(self):
            if self.incoming: return self.incoming.pop(0)
            raise WebSocketDisconnect()
        async def send_json(self,value): self.sent.append(value)
        async def close(self, code=1000, reason=None):
            self.close_calls += 1
            self.close_code = code
            self.close_reason = reason
    socket=OfflineSocket(); asyncio.run(endpoint(socket,"ws-bound"))
    assert len(socket.sent) == 1
    assert socket.sent[0]["type"] == "admission_error"
    error = socket.sent[0]["error"]
    assert error["code"] == "runtime_unavailable"
    assert error["reason"] == "booting"
    assert error["state"] == "BOOTING"
    assert error["user_available"] is False
    assert socket.close_calls == 1
    assert socket.close_code == 1013
    assert socket.close_reason == "booting"
    assert app.state.application.sessions.items == {}


def test_offline_api_rate_limit_is_explicit_429():
    app=create_app(include_test_routes=True); endpoint=next(route.endpoint for route in app.routes if route.path=="/mock/text")
    for _ in range(30):
        asyncio.run(endpoint({"session_id":"rate-limit", "text":"Привет"}))
    with pytest.raises(HTTPException) as error:
        asyncio.run(endpoint({"session_id":"rate-limit", "text":"Привет"}))
    assert error.value.status_code == 429
    assert error.value.detail["code"] == "rate_limit"
