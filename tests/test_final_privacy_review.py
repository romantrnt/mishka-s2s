"""Final review regressions. Paths, transports and child data are synthetic."""
import asyncio
import json
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.foundation.api import build_application, create_app
from app.foundation.config import Settings, public_model_reference
from app.foundation.repositories import SQLiteRepository
from app.foundation.s2s import FasterWhisperRecognizer, OllamaLLMProvider, S2SConfig, VLLMLLMProvider


@pytest.mark.parametrize("reference", ["/PRIVATE_MODEL/model", "models/PRIVATE_MODEL", r"C:\PRIVATE_MODEL\model", "https://PRIVATE_MODEL/token"])
def test_public_model_projection_does_not_expose_paths(reference):
    assert public_model_reference(reference) == "[configured]"
    settings = Settings(stt_model=reference, llm_model=reference, validation_model=reference, silero_model=reference)
    assert "PRIVATE_MODEL" not in json.dumps(settings.redacted_snapshot())
    # Projection never changes the actual configured model/loader argument.
    assert settings.stt_model == reference
    assert public_model_reference("qwen3:14b") == "qwen3:14b"


def test_public_readiness_and_health_hide_database_and_model_paths(tmp_path):
    settings = Settings(profile="mock", stt_model="/PRIVATE_STT/model",
                        llm_model="/PRIVATE_LLM/model", validation_model="/PRIVATE_VALIDATION/model")
    application, _, _, _ = build_application(settings=settings, background_scheduler=False)
    repo = SQLiteRepository(str(tmp_path / "PRIVATE_HISTORY.sqlite3"))
    application.history_repository = repo
    api = create_app(application)
    routes = {route.path: route.endpoint for route in api.routes if hasattr(route, "endpoint")}
    try:
        result = asyncio.run(routes["/ready"]())
        serialized = json.dumps({"ready": result, "health": routes["/health"]()})
        assert "PRIVATE_" not in serialized
        assert str(tmp_path) not in serialized
        assert result["dependencies"]["history"]["ready"] is True
        assert result["dependencies"]["history"]["path_configured"] is True
        assert repo.path == str(tmp_path / "PRIVATE_HISTORY.sqlite3")
        # Non-mock configuration previously repeated private references outside
        # effective_settings. This reads config only, without loading models.
        application.runtime_manager.settings = replace(settings, profile="local_cpu")
        assert "PRIVATE_" not in json.dumps(application.runtime_manager.configuration())
    finally:
        repo.db.close()
        asyncio.run(application.runtime_manager.close())


@pytest.mark.parametrize("kind", ["ollama", "vllm"])
def test_llm_readiness_redacts_reference_without_changing_lookup(monkeypatch, kind):
    import httpx
    reference = "/PRIVATE_LLM/model"
    class Client:
        def __init__(self, *args, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {
                "models": [{"name": reference}], "data": [{"id": reference}]})
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    provider = (OllamaLLMProvider("http://unused", reference, 1.0) if kind == "ollama"
                else VLLMLLMProvider("http://unused", reference, 1.0))
    result = asyncio.run(provider.readiness())
    assert result["ready"] is True and result["model_present"] is True
    assert "PRIVATE_LLM" not in json.dumps(result)
    assert provider.model == reference


def test_stt_readiness_redacts_reference_without_changing_presence_check(tmp_path, monkeypatch):
    reference = tmp_path / "PRIVATE_STT"
    reference.mkdir()
    monkeypatch.setattr("app.foundation.s2s.importlib.util.find_spec", lambda name: object())
    provider = FasterWhisperRecognizer(lambda: None, S2SConfig(), str(reference))
    status = provider.readiness()
    assert status["ready"] and status["local_model_present"]
    assert "PRIVATE_STT" not in json.dumps(status)
    assert provider.model_name == str(reference)


def test_sync_handoff_readiness_is_bounded_nonblocking_and_single_flight():
    application, _, _, _ = build_application(settings=Settings(profile="mock"), background_scheduler=False)
    # Isolate the external handoff probe from catalog/resource scans, which
    # have separate coverage and no latency SLA on this machine.
    application.scenarios = None
    application.metrics = SimpleNamespace(snapshot=lambda: {})
    async def runtime_ready():
        return {"ready": False}
    application.runtime_manager.readiness = runtime_ready
    release = threading.Event()
    class Handoff:
        calls = 0
        def readiness(self):
            self.calls += 1
            release.wait(3)
            return {"ready": True}
    handoff = Handoff()
    application.adult_handoff = handoff
    api = create_app(application)
    ready = next(route.endpoint for route in api.routes if route.path == "/ready")
    async def run():
        started = time.monotonic()
        first = asyncio.create_task(ready())
        second = asyncio.create_task(ready())
        try:
            await asyncio.sleep(0.05)
            assert time.monotonic() - started < 0.8, "sync handoff blocked the event loop"
            results = await asyncio.gather(first, second)
            assert all(not result["dependencies"]["adult_handoff"]["ready"] for result in results)
            assert handoff.calls == 1
            assert time.monotonic() - started < 2.5
            # Timeout must not abandon the native owner or spawn another probe.
            third = asyncio.create_task(ready())
            await asyncio.sleep(0.05)
            release.set()
            result = await third
            assert result["dependencies"]["adult_handoff"]["ready"] is True
            assert handoff.calls == 1
        finally:
            release.set()
            await asyncio.gather(first, second, return_exceptions=True)
            await application.runtime_manager.close()
    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["ready", "simulated", "error"])
def test_async_handoff_readiness_keeps_readiness_contract(outcome):
    application, _, _, _ = build_application(settings=Settings(profile="mock"), background_scheduler=False)
    class Handoff:
        async def readiness(self):
            if outcome == "error":
                raise OSError("PRIVATE_TRANSPORT_DETAIL")
            return {"ready": True, "simulated": outcome == "simulated"}
    application.adult_handoff = Handoff()
    api = create_app(application)
    ready = next(route.endpoint for route in api.routes if route.path == "/ready")
    try:
        status = asyncio.run(ready())["dependencies"]["adult_handoff"]
        assert status["ready"] is (outcome == "ready")
        assert "PRIVATE_TRANSPORT_DETAIL" not in json.dumps(status)
    finally:
        asyncio.run(application.runtime_manager.close())
