"""Real synthetic Max TLS/SQLite ownership; no GPU/model downloads."""
import asyncio
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from scripts.run_remote_stand import RemoteLaunchError, _private_directory, load_or_create_token, remote_session

ROOT = Path(__file__).resolve().parents[1]
MAX_SRC = Path(os.environ.get("MAX_JSON_TEST_SRC", ROOT.parent / "max_files/src")).resolve()
HAS_MAX = (MAX_SRC / "phone_local_demo.py").is_file()


def test_help_is_dependency_free_and_profile_source_are_explicit():
    completed = subprocess.run([sys.executable, "-S", str(ROOT / "scripts/run_remote_stand.py"), "--help"],
                               capture_output=True, text=True, timeout=5)
    assert completed.returncode == 0
    assert "--external-origin" in completed.stdout
    assert "local_gpu" in completed.stdout and "mock" in completed.stdout
    assert "--whisper-model" in completed.stdout


def test_state_permissions_token_reuse_and_symlink_failure(tmp_path, monkeypatch):
    monkeypatch.delenv("STAND_ACCESS_TOKEN", raising=False)
    directory = _private_directory(tmp_path / "runtime")
    token = load_or_create_token(directory)
    assert len(token) >= 32
    assert (directory / "stand-token").stat().st_mode & 0o777 == 0o600
    assert load_or_create_token(directory) == token
    monkeypatch.setenv("STAND_ACCESS_TOKEN", "different_" + "x" * 40)
    with pytest.raises(RemoteLaunchError):
        load_or_create_token(directory)
    monkeypatch.delenv("STAND_ACCESS_TOKEN")
    (directory / "stand-token").chmod(0o644)
    with pytest.raises(RemoteLaunchError):
        load_or_create_token(directory)
    link = tmp_path / "link"
    link.symlink_to(directory)
    with pytest.raises(RemoteLaunchError):
        _private_directory(link)


def test_weather_settings_reach_actual_remote_factory_without_loading_runtime(tmp_path, monkeypatch):
    import scripts.run_remote_stand as launcher
    closed, reached = [], []

    class FactoryReached(Exception):
        pass

    class Server:
        server_address = ("127.0.0.1", 19443)
        def __init__(self, *args, **kwargs): self.stopped = threading.Event()
        def serve_forever(self): self.stopped.wait()
        def shutdown(self): self.stopped.set()
        def server_close(self): pass

    phone = SimpleNamespace(close=lambda: closed.append(True))
    demo = SimpleNamespace(DemoServer=Server, connect_demo=lambda *args, **kwargs: phone)
    monkeypatch.setattr(launcher, "_load_max", lambda *args: (demo, lambda *args: None, {}))
    monkeypatch.setattr(launcher, "_max_state", lambda demo, directory: directory)
    monkeypatch.setenv("MISHKA_VOICE_CONVERSION", "off")
    for name, value in {"MISHKA_WEATHER_PROVIDER": "yandex", "YANDEX_WEATHER_API_KEY": "synthetic-weather-key",
                        "YANDEX_WEATHER_URL": "https://weather.example.test/forecast",
                        "YANDEX_WEATHER_LATITUDE": "55", "YANDEX_WEATHER_LONGITUDE": "37",
                        "YANDEX_WEATHER_TIMEOUT_S": "1.5"}.items():
        monkeypatch.setenv(name, value)

    def factory(settings, **kwargs):
        reached.append(True)
        config = settings.weather_configuration()
        assert config.provider == "yandex" and config.available
        assert config.api_key == "synthetic-weather-key"
        assert config.timeout_s == 1.5 and config.latitude == 55 and config.longitude == 37
        assert settings.voice_conversion == "off" and settings.tts_provider == "silero"
        assert settings.mutation_auth_policy == "token_required" and not settings.allow_query_token
        raise FactoryReached

    monkeypatch.setattr("app.foundation.stand.create_stand_app", factory)
    async def scenario():
        with pytest.raises(FactoryReached):
            async with remote_session(max_source=tmp_path, state_dir=tmp_path / "private", profile="local_gpu"):
                pytest.fail("runtime loading should not be reached")
    asyncio.run(scenario())
    assert reached == [True] and closed == [True]
    assert "synthetic-weather-key" not in (tmp_path / "private/launch.json").read_text()


@pytest.mark.skipif(not HAS_MAX, reason="Explicit local Max source fixture is absent")
def test_real_max_lifespan_private_http_honest_readiness_and_owned_shutdown(tmp_path, monkeypatch):
    monkeypatch.delenv("STAND_ACCESS_TOKEN", raising=False)

    async def scenario():
        directory = tmp_path / "private"
        # These tiny files only prove explicit path selection in mock mode;
        # they never count as a loaded real model or trigger a download.
        model = tmp_path / "pinned-whisper"
        model.mkdir()
        for name in ("model.bin", "config.json", "tokenizer.json"):
            (model / name).write_text("mock_test_path_selection_only")
        receipt = tmp_path / "operator-story-receipt.json"
        receipt.write_text("{}")
        monkeypatch.setenv("MISHKA_STORY_BACKEND_RECEIPT_PATH", str(receipt))
        monkeypatch.setenv("LLM_NUM_CTX", "16384")
        monkeypatch.setenv("VALIDATION_NUM_CTX", "16384")
        async with remote_session(max_source=MAX_SRC, state_dir=directory, whisper_model=model) as runtime:
            assert runtime.api.state.stand.settings.stt_model == str(model)
            assert runtime.api.state.stand.settings.story_backend_receipt_path == str(receipt)
            assert runtime.api.state.stand.settings.llm_num_ctx == 16384
            assert runtime.api.state.stand.settings.validation_num_ctx == 16384
            assert runtime.api.state.stand.application.bounded_s2s.story_backend_contracts == {}, "path binding does not approve an invalid receipt"
            assert runtime.manifest["whisper_local_assets"] is True
            assert runtime.manifest["whisper_model"] == str(model)
            with pytest.raises(RemoteLaunchError, match="remote_state_already_in_use"):
                async with remote_session(max_source=MAX_SRC, state_dir=directory):
                    pytest.fail("a concurrent owner reused private Max state")
            token = (directory / "stand-token").read_text().strip()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=runtime.gateway),
                                         base_url="http://127.0.0.1:8000", auth=("mishka", token)) as client:
                ready = await client.get("/ready")
                assert ready.status_code == 503  # Mock does not invent model readiness.
                assert ready.json()["production_ready"] is False
                state = await client.get("/stand/simulator/state")
                assert state.status_code == 200
                assert runtime.api.state.lifecycle.snapshot()["state"] != "READY"
                assert runtime.capture.started.is_set()
                assert runtime.server_thread.is_alive()
            manifest = (directory / "launch.json").read_text()
            assert token not in manifest
            assert "demo_credential" not in manifest
        assert runtime.api.state.stand._closed
        assert runtime.capture.stopped.is_set()
        assert not runtime.server_thread.is_alive()
        stopped = json.loads((directory / "launch.json").read_text())
        assert stopped["event"] == "stopped"
        assert stopped["server_stopped"] is True
        assert stopped["production_ready"] is False
        # Restart must reuse, never initialize/reset the Max state.
        original_db = (directory / "max-demo/demo.sqlite3").stat().st_ino
        monkeypatch.delenv("LLM_NUM_CTX")
        monkeypatch.delenv("VALIDATION_NUM_CTX")
        async with remote_session(max_source=MAX_SRC, state_dir=directory) as restarted:
            assert restarted.api.state.stand.settings.llm_num_ctx == 4096
            assert restarted.api.state.stand.settings.validation_num_ctx == 4096
            assert (directory / "max-demo/demo.sqlite3").stat().st_ino == original_db
            assert (directory / "stand-token").read_text().strip() == token
        # Existing state is validated, never silently repaired/reinitialized.
        database = directory / "max-demo/demo.sqlite3"
        original_bytes = database.read_bytes()
        database.chmod(0o644)
        with pytest.raises(Exception, match="max_demo_state_permissions"):
            async with remote_session(max_source=MAX_SRC, state_dir=directory):
                pytest.fail("insecure existing state was accepted")
        assert database.read_bytes() == original_bytes

    asyncio.run(scenario())


@pytest.mark.skipif(not HAS_MAX, reason="Explicit local Max source fixture is absent")
def test_actual_listener_auth_ws_signal_cleanup_and_no_secret_output(tmp_path, monkeypatch):
    monkeypatch.delenv("STAND_ACCESS_TOKEN", raising=False)
    from websockets.sync.client import connect
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    directory = tmp_path / "listener"
    origin = f"http://127.0.0.1:{port}"
    process = subprocess.Popen([sys.executable, "-u", str(ROOT / "scripts/run_remote_stand.py"),
                                "--max-source", str(MAX_SRC), "--state-dir", str(directory),
                                "--port", str(port), "--external-origin", origin],
                               cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            assert process.poll() is None, "launcher exited before listener became available"
            try:
                if httpx.get(origin + "/live", timeout=0.3).status_code == 401:
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.05)
        else:
            pytest.fail("launcher listener did not start")
        token = (directory / "stand-token").read_text().strip()
        with httpx.Client(base_url=origin, auth=("mishka", token)) as client:
            response = client.get("/stand/state")
            assert response.status_code == 200
            assert client.get("/ready").json()["production_ready"] is False
            cookie = "; ".join(f"{key}={value}" for key, value in client.cookies.items())
            with connect(f"ws://127.0.0.1:{port}/stand/ws", origin=origin,
                         additional_headers={"Cookie": cookie}) as ws:
                assert json.loads(ws.recv())["type"] == "stand_state"
                ws.send(json.dumps({"type": "ping"}))
                assert json.loads(ws.recv())["type"] == "pong"
        # The network-level WS handshake must also fail before acceptance.
        from websockets.exceptions import InvalidStatus
        with pytest.raises(InvalidStatus):
            with connect(f"ws://127.0.0.1:{port}/stand/ws", origin=origin):
                pytest.fail("unauthenticated network WS accepted")
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stderr
        assert token not in stdout + stderr
        assert "demo_credential" not in stdout + stderr
        assert json.loads((directory / "launch.json").read_text())["server_stopped"] is True
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=15)
