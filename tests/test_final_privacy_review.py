"""Final review regressions. Paths, transports and child data are synthetic."""
import asyncio
import json
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


def test_safe_health_describes_only_local_policy():
    app, *_ = build_application(settings=Settings(profile="mock"), background_scheduler=False)
    api = create_app(app)
    endpoint = next(route.endpoint for route in api.routes if getattr(route, "path", None) == "/health/safe")
    result = endpoint()
    assert result == {"safety_policy": "canonical_local_stop", "fail_closed": True}
    assert "safe" not in result
    assert "adult_handoff" not in result
