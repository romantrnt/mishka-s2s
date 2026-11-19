"""Private remote gateway: all routes, real canonical auth, and browser CSRF."""
import base64

import pytest
from fastapi import FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.foundation.remote_access import PrivateStandGateway, normalize_origin

TOKEN = "test_only_" + "a" * 48
ORIGIN = "https://stand.example"


def basic(password=TOKEN, username="mishka"):
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


def fixture_app():
    app = FastAPI()

    @app.get("/{path:path}")
    @app.post("/{path:path}")
    async def route(request: Request, path: str):
        return {"canonical_auth": request.headers.get("x-stand-token") == TOKEN}

    @app.websocket("/stand/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        await socket.send_json({"canonical_auth": socket.headers.get("x-stand-token") == TOKEN})
        await socket.close()

    return PrivateStandGateway(app, token=TOKEN, allowed_origins=[ORIGIN])


@pytest.mark.parametrize("path", ["/", "/console", "/simulator", "/simulator/app.js", "/live",
                                  "/ready", "/stand/state", "/stand/simulator/state", "/unknown"])
def test_every_route_is_private(path):
    with TestClient(fixture_app(), base_url=ORIGIN) as client:
        result = client.get(path)
        assert result.status_code == 401
        assert result.headers["www-authenticate"].startswith("Basic ")
        assert TOKEN not in result.text
        assert "set-cookie" not in result.headers


@pytest.mark.parametrize("authorization", ["", "Basic", "Basic !!!", "Basic d2l0aG91dGNvbG9u",
                                            "Bearer " + TOKEN, basic("wrong"), basic(username="other"),
                                            "Basic " + base64.b64encode(b"mishka:\xff").decode()])
def test_malformed_basic_fails_closed_even_with_valid_cookie(authorization):
    with TestClient(fixture_app(), base_url=ORIGIN) as client:
        result = client.get("/console", headers={"Authorization": authorization,
                                                "Cookie": "stand_token=" + TOKEN})
        assert result.status_code == 401


def test_login_sets_private_secure_cookie_and_injects_canonical_header():
    with TestClient(fixture_app(), base_url=ORIGIN) as client:
        response = client.get("/console", headers={"Authorization": basic(), "x-stand-token": "attacker"})
        assert response.json() == {"canonical_auth": True}
        cookie = response.headers["set-cookie"]
        assert all(flag in cookie for flag in ("Path=/", "HttpOnly", "SameSite=Strict", "Secure"))
        assert client.get("/stand/state").json() == {"canonical_auth": True}
        assert TOKEN not in response.text
        assert response.headers["cache-control"] == "no-store"


def test_cookie_mutations_require_exact_origin_basic_curl_may_omit_it():
    with TestClient(fixture_app(), base_url=ORIGIN) as client:
        client.get("/console", headers={"Authorization": basic()})
        assert client.post("/stand/reset").status_code == 403
        assert client.post("/stand/reset", headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.post("/stand/reset", headers={"Origin": "null"}).status_code == 403
        assert client.post("/stand/reset", headers={"Origin": ORIGIN}).status_code == 200
        assert client.post("/stand/reset", headers={"Authorization": basic()}).status_code == 200
        assert client.post("/stand/reset", headers={"Authorization": basic(), "Origin": "https://evil.example"}).status_code == 403


def test_no_query_header_bypass_or_ambiguous_auth():
    with TestClient(fixture_app(), base_url=ORIGIN) as client:
        assert client.get("/console", headers={"x-stand-token": TOKEN}).status_code == 401
        for query in ("token", "access_token", "stand_token", "%74oken"):
            assert client.get(f"/console?{query}={TOKEN}", headers={"Authorization": basic()}).status_code == 401
        assert client.get("/console", headers=[("Authorization", basic()), ("Authorization", basic())]).status_code == 401
        assert client.get("/console", headers={"Cookie": f"stand_token={TOKEN}; stand_token={TOKEN}"}).status_code == 401


def test_websocket_auth_and_origin():
    with TestClient(fixture_app(), base_url=ORIGIN) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("wss://stand.example/stand/ws"):
                pytest.fail("unauthenticated socket accepted")
        client.get("/console", headers={"Authorization": basic()})
        for headers in ({}, {"Origin": "https://evil.example"}):
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect("wss://stand.example/stand/ws", headers=headers):
                    pytest.fail("cookie socket without allowed Origin accepted")
        with client.websocket_connect("wss://stand.example/stand/ws", headers={"Origin": ORIGIN}) as ws:
            assert ws.receive_json() == {"canonical_auth": True}
        with client.websocket_connect("wss://stand.example/stand/ws", headers={"Authorization": basic()}) as ws:
            assert ws.receive_json() == {"canonical_auth": True}


def test_local_cookie_is_http_only_strict_and_secure_on_actual_https():
    gateway = PrivateStandGateway(fixture_app().app, token=TOKEN,
                                 allowed_origins=["http://127.0.0.1:8765"], secure_cookie=False)
    with TestClient(gateway, base_url="http://127.0.0.1:8765") as client:
        cookie = client.get("/console", headers={"Authorization": basic()}).headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Secure" not in cookie
    with TestClient(gateway, base_url=ORIGIN) as client:
        assert "Secure" in client.get("/console", headers={"Authorization": basic()}).headers["set-cookie"]


@pytest.mark.parametrize("origin", ["*", "null", "https://stand.example/path", "https://user:pass@stand.example",
                                    "https://stand.example?x=1", "https://stand.example#fragment", "ftp://stand.example"])
def test_origin_configuration_is_explicit(origin):
    with pytest.raises(ValueError):
        normalize_origin(origin)


def test_existing_canonical_http_and_websocket_accept_internal_auth():
    from app.foundation.config import Settings
    from app.foundation.stand import create_stand_app
    api = create_stand_app(Settings(profile="mock", hardware_mode="simulator", voice_conversion="off",
                                    stand_access_token=TOKEN))
    gateway = PrivateStandGateway(api, token=TOKEN, allowed_origins=[ORIGIN])
    with TestClient(gateway, base_url=ORIGIN) as client:
        assert client.get("/stand/state").status_code == 401
        assert client.get("/stand/state", headers={"Authorization": basic()}).status_code == 200
        with client.websocket_connect("wss://stand.example/stand/ws", headers={"Origin": ORIGIN}) as ws:
            assert ws.receive_json()["type"] == "stand_state"
            ws.send_json({"type": "ping"})
            assert ws.receive_json()["type"] == "pong"


def test_browser_html_login_page_and_cookie_session_without_basic_popup():
    with TestClient(fixture_app(), base_url=ORIGIN, follow_redirects=False) as client:
        page = client.get("/console", headers={"Accept": "text/html,application/xhtml+xml"})
        assert page.status_code == 200
        assert '<form method="post" action="/stand/login"' in page.text
        assert TOKEN not in page.text
        assert "set-cookie" not in page.headers
        # Query auth still fails closed even with Accept: text/html
        assert client.get(f"/console?token={TOKEN}", headers={"Accept": "text/html"}).status_code == 401
        # Foreign or missing Origin on POST /stand/login is rejected
        assert client.post("/stand/login", data={"username": "mishka", "password": TOKEN}).status_code == 403
        assert client.post("/stand/login", data={"username": "mishka", "password": TOKEN}, headers={"Origin": "https://evil.example"}).status_code == 403
        # Wrong password returns 401 without setting cookie
        bad = client.post("/stand/login", data={"username": "mishka", "password": "wrong"}, headers={"Origin": ORIGIN})
        assert bad.status_code == 401 and "set-cookie" not in bad.headers and TOKEN not in bad.text
        # Valid login sets HttpOnly/Strict/Secure cookie and redirects to /console
        ok = client.post("/stand/login", data={"username": "mishka", "password": TOKEN}, headers={"Origin": ORIGIN})
        assert ok.status_code == 303 and ok.headers["location"] == "/console"
        cookie = ok.headers["set-cookie"]
        assert all(flag in cookie for flag in ("Path=/", "HttpOnly", "SameSite=Strict", "Secure"))
        assert client.get("/console").json() == {"canonical_auth": True}
