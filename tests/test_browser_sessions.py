"""Observable browser-session boundaries; no devices, models or live services."""
import asyncio
from types import SimpleNamespace
import threading

from fastapi import FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from app.foundation.remote_access import PrivateStandGateway

TOKEN = "synthetic_test_" + "s" * 40
ORIGIN = "https://phone.example.test"


def gateway(**kwargs):
    app = FastAPI()
    app.state.writes = []
    app.state.started = threading.Event()
    app.state.finished = threading.Event()
    app.state.physical_session = "unchanged"

    @app.get("/{path:path}")
    @app.post("/{path:path}")
    async def route(request: Request, path: str):
        return {"canonical_auth": request.headers.get("x-stand-token") == TOKEN}

    @app.websocket("/stand/ws")
    async def socket(ws: WebSocket):
        await ws.accept()
        try:
            while True:
                text = await ws.receive_text()
                if text == "slow":
                    app.state.started.set()
                    await asyncio.sleep(0.2)
                app.state.writes.append(text)
                await ws.send_text(text)
        except WebSocketDisconnect:
            pass
        finally:
            app.state.finished.set()

    return PrivateStandGateway(app, token=TOKEN, allowed_origins=[ORIGIN], **kwargs)


def login(client, **data):
    return client.post("/stand/login", data={"username": "mishka", "password": TOKEN, **data},
                       headers={"Origin": ORIGIN})


def test_cookie_is_opaque_not_api_credential_and_old_raw_cookie_is_rejected():
    with TestClient(gateway(), base_url=ORIGIN, follow_redirects=False) as client:
        response = login(client)
        assert response.status_code == 303
        cookie = response.headers["set-cookie"]
        assert TOKEN not in cookie and "stand_session=" in cookie
        assert "Domain=" not in cookie and "Max-Age=3600" in cookie
        assert client.get("/stand/state").json()["canonical_auth"]
        assert client.get("/stand/state", headers={"Cookie": "stand_token=" + TOKEN}).status_code == 401
        sid = client.cookies.get("stand_session")
        assert client.get("/stand/state", headers={"Cookie": f"stand_session={sid}; stand_session={sid}"}).status_code == 401
        assert client.get("/stand/state", headers={"x-stand-token": sid, "Cookie": ""}).status_code == 401


@pytest.mark.parametrize("target,expected", [
    ("/console/operator", "/console/operator"), ("/console", "/console"),
    ("https://evil.example", "/console"), ("//evil.example", "/console"),
    ("/console/../stand/reset", "/console"), ("/console?token=secret", "/console"),
    ("/console/operator#part", "/console"), ("/stand/reset", "/console"),
])
def test_login_return_is_a_small_exact_allowlist(target, expected):
    with TestClient(gateway(), base_url=ORIGIN, follow_redirects=False) as client:
        page = client.get("/console/operator", headers={"Accept": "text/html"})
        assert 'name="return_to" type="hidden" value="/console/operator"' in page.text
        assert 'for="username"' in page.text and 'for="password"' in page.text
        assert 'autocomplete="username"' in page.text and 'autocomplete="current-password"' in page.text
        assert login(client, return_to=target).headers["location"] == expected


def test_absolute_expiry_does_not_slide_on_read_and_fresh_login_rotates():
    gate = gateway()
    clock = [100.0]
    gate._clock = lambda: clock[0]
    with TestClient(gate, base_url=ORIGIN, follow_redirects=False) as client:
        login(client)
        sid = client.cookies.get("stand_session")
        clock[0] = 3699
        assert client.get("/stand/state").status_code == 200
        clock[0] = 3700
        assert client.get("/stand/state").status_code == 401
        login(client)
        assert client.cookies.get("stand_session") != sid
        assert client.get("/stand/state", headers={"Cookie": f"stand_session={sid}"}).status_code == 401


def test_logout_is_post_only_origin_checked_and_revokes_only_browser_session():
    gate = gateway()
    with TestClient(gate, base_url=ORIGIN, follow_redirects=False) as client:
        login(client)
        sid = client.cookies.get("stand_session")
        assert client.get("/stand/logout").status_code == 405
        for origin in (None, "null", "https://evil.example"):
            assert client.post("/stand/logout", headers={} if origin is None else {"Origin": origin}).status_code == 403
        assert client.get("/stand/state").status_code == 200
        result = client.post("/stand/logout", headers={"Origin": ORIGIN})
        assert result.status_code == 303 and "Max-Age=0" in result.headers["set-cookie"]
        assert client.get("/stand/state", headers={"Cookie": f"stand_session={sid}"}).status_code == 401
        assert client.post("/stand/logout", headers={"Origin": ORIGIN}).status_code == 303
        assert gate.app.state.physical_session == "unchanged"


@pytest.mark.parametrize("boundary", ["logout", "expiry", "rotation", "eviction"])
def test_open_websocket_revoked_and_inflight_write_cancelled(boundary):
    gate = gateway(max_sessions=1 if boundary == "eviction" else 64)
    with TestClient(gate, base_url=ORIGIN, follow_redirects=False) as client:
        login(client)
        if boundary == "expiry":
            gate._sessions[client.cookies.get("stand_session")].expires_at = gate._clock() + 0.08
        with client.websocket_connect("wss://phone.example.test/stand/ws", headers={"Origin": ORIGIN}) as ws:
            ws.send_text("first")
            assert ws.receive_text() == "first"
            ws.send_text("slow")
            assert gate.app.state.started.wait(1)
            if boundary == "logout":
                client.post("/stand/logout", headers={"Origin": ORIGIN})
            elif boundary == "rotation":
                login(client)
            elif boundary == "eviction":
                # A second client, not rotation of the existing cookie.
                client.post("/stand/login", data={"username": "mishka", "password": TOKEN},
                            headers={"Origin": ORIGIN, "Cookie": ""})
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_text()
            assert closed.value.code == 1008
        assert gate.app.state.finished.wait(1)
        assert gate.app.state.writes == ["first"]
        assert gate.app.state.physical_session == "unchanged"


def test_bounded_ram_session_eviction_and_api_mode_are_explicit():
    gate = gateway(max_sessions=2, allow_api_token=True)
    with TestClient(gate, base_url=ORIGIN, follow_redirects=False) as client:
        sids = []
        for _ in range(3):
            client.cookies.clear()
            login(client)
            sids.append(client.cookies.get("stand_session"))
        assert len(gate._sessions) == 2
        assert client.get("/stand/state", headers={"Cookie": f"stand_session={sids[0]}"}).status_code == 401
        assert client.post("/stand/reset", headers={"x-stand-token": TOKEN, "Cookie": ""}).status_code == 200
        assert client.post("/stand/reset", headers={"x-stand-token": TOKEN, "Origin": "https://evil.example"}).status_code == 403
        assert client.get("/stand/state", headers=[("x-stand-token", TOKEN), ("x-stand-token", TOKEN)]).status_code == 401
        assert client.get("/stand/state", headers={"x-stand-token": TOKEN, "Authorization": "Basic !!!"}).status_code == 401
        assert client.get("/stand/state", headers={"Authorization": "Bearer " + TOKEN}).status_code == 401
        with client.websocket_connect("wss://phone.example.test/stand/ws", headers={"x-stand-token": TOKEN, "Cookie": ""}) as ws:
            ws.send_text("api")
            assert ws.receive_text() == "api"


@pytest.mark.parametrize("path", ["/stand/login", "/stand/logout", "/console", "/stand/ws"])
def test_url_credentials_never_bypass_any_endpoint(path):
    with TestClient(gateway(), base_url=ORIGIN, follow_redirects=False) as client:
        login(client)
        for key in ("token", "access_token", "stand_token", "stand_session", "%74oken"):
            assert client.post(f"{path}?{key}=ignored", headers={"Origin": ORIGIN}).status_code == 401


@pytest.mark.parametrize("ttl", [0, -1, 86401, float("inf"), float("nan"), True])
def test_ttl_is_bounded(ttl):
    with pytest.raises(ValueError, match="ttl"):
        gateway(session_ttl_s=ttl)


def test_real_launcher_factory_preserves_app_and_canonical_api_auth():
    from app.foundation.remote_access import build_browser_gateway
    settings = SimpleNamespace(stand_access_token=TOKEN, allow_query_token=False, mutation_auth_policy="token_required")
    app = gateway().app
    gate = build_browser_gateway(app, settings, port=18024, environ={})
    assert gate.app is app and gate.allow_api_token
    assert gate.allowed_origins == {"http://127.0.0.1:18024", "http://localhost:18024", "http://[::1]:18024"}
    with TestClient(gate, base_url="http://127.0.0.1:18024", follow_redirects=False) as client:
        assert client.get("/console").status_code == 401
        assert client.get("/console", headers={"Accept": "text/html"}).status_code == 200
        assert client.get("/stand/state", headers={"x-stand-token": TOKEN}).json()["canonical_auth"]
        assert client.post("/stand/login", data={"username": "mishka", "password": TOKEN},
                           headers={"Origin": "http://attacker.invalid:18024", "Host": "attacker.invalid:18024"}).status_code == 403
    secure = build_browser_gateway(app, settings, port=8000, environ={"MISHKA_BROWSER_ORIGINS": ORIGIN})
    assert secure.secure_cookie and secure.allowed_origins == {ORIGIN}
    for environ in ({"MISHKA_BROWSER_ORIGINS": "*"}, {"MISHKA_BROWSER_ORIGINS": "https://*.example.test"}, {"MISHKA_BROWSER_ORIGINS": ""},
                    {"MISHKA_BROWSER_SESSION_TTL_S": "nan"}):
        with pytest.raises(ValueError):
            build_browser_gateway(app, settings, port=8000, environ=environ)
    settings.stand_access_token = ""
    with pytest.raises(ValueError):
        build_browser_gateway(app, settings, port=8000, environ={})
    settings.stand_access_token = TOKEN
    settings.allow_query_token = True
    with pytest.raises(ValueError):
        build_browser_gateway(app, settings, port=8000, environ={})


def test_actual_launcher_passes_gateway_to_uvicorn_and_closes_original_stand(monkeypatch):
    from pathlib import Path
    import runpy
    from app.foundation.config import Settings
    from app.foundation.remote_access import PrivateStandGateway
    from app.foundation import max_client_runtime
    import uvicorn

    calls = []

    class Stand:
        _closed = False
        async def aclose(self):
            self._closed = True
            calls.append("closed")

    stand = Stand()
    app = SimpleNamespace(state=SimpleNamespace(stand=stand))
    settings = Settings(stand_access_token=TOKEN, voice_conversion="off")
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: settings))
    monkeypatch.setattr(max_client_runtime, "build_configured_app", lambda *args: app)
    monkeypatch.setenv("MISHKA_PORT", "8000")
    monkeypatch.delenv("MISHKA_BROWSER_ORIGINS", raising=False)
    monkeypatch.delenv("MISHKA_ENV_FILE", raising=False)

    def run(asgi, **options):
        assert isinstance(asgi, PrivateStandGateway) and asgi.app is app
        assert asgi.allow_api_token and options["port"] == 8000
        assert not stand._closed
        calls.append("run")

    monkeypatch.setattr(uvicorn, "run", run)
    runpy.run_path(str(Path(__file__).parents[1] / "scripts/run_stand.py"), run_name="__main__")
    assert calls == ["run", "closed"] and stand._closed
