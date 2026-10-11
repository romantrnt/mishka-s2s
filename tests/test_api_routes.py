from app.mvp.api import create_app
from app.mvp.service import MishkaMVPService


def test_mock_http_and_websocket_routes_are_registered():
    app = create_app(MishkaMVPService())
    paths = {route.path for route in app.routes}
    assert {"/health", "/ready", "/mock/rfid", "/mock/handset", "/mock/button", "/mock/text", "/mock/session/{session_id}", "/session/reset", "/ws/{session_id}"} <= paths
