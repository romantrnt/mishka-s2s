import logging
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app.foundation.config import Settings, SettingsError
from app.foundation.stand import create_stand_app
from scripts.run_stand import StandAccessLogRedactionFilter


def make_dev_app(monkeypatch, enabled):
    monkeypatch.setenv("STAND_ACCESS_TOKEN", "dev-secret")
    api = create_stand_app(Settings(profile="mock", stand_access_token="dev-secret", allow_query_token=enabled))
    return api


def test_development_flag_off_rejects_query_token(monkeypatch):
    api = make_dev_app(monkeypatch, False)
    try:
        with TestClient(api) as client:
            assert client.get("/console?token=dev-secret").status_code == 401
            assert client.get("/console", headers={"x-stand-token": "dev-secret"}).status_code == 200
    finally:
        api.state.stand.close()


def test_development_flag_on_allows_only_console_query_fallback(monkeypatch):
    api = make_dev_app(monkeypatch, True)
    try:
        with TestClient(api) as client:
            response = client.get("/console?token=dev-secret")
            assert response.status_code == 200
            assert "stand_token=" in response.headers.get("set-cookie", "")
            assert client.get("/console/console.js").status_code == 200
            assert client.get("/stand/state?token=dev-secret").status_code == 401
    finally:
        api.state.stand.close()


def test_development_query_token_invalid_and_header_priority(monkeypatch):
    api = make_dev_app(monkeypatch, True)
    try:
        with TestClient(api) as client:
            assert client.get("/console?token=wrong").status_code == 401
            assert client.get("/console?token=dev-secret", headers={"x-stand-token": "wrong"}).status_code == 401
            assert client.get("/stand/state", headers={"x-stand-token": "dev-secret"}).status_code == 200
    finally:
        api.state.stand.close()


@pytest.mark.parametrize("profile", ["production", "production_candidate", "production_qwen_rvc"])
def test_production_profiles_reject_query_token_configuration(profile):
    with pytest.raises(SettingsError, match="MISHKA_ALLOW_QUERY_TOKEN"):
        Settings(profile=profile, llm_provider="ollama", allow_query_token=True).validate()


@pytest.mark.parametrize("value", ["", "maybe", "2", "enabled"])
def test_invalid_query_token_flag_is_not_enablement(monkeypatch, value):
    monkeypatch.setenv("MISHKA_PROFILE", "mock")
    monkeypatch.setenv("MISHKA_ALLOW_QUERY_TOKEN", value)
    with pytest.raises(SettingsError, match="MISHKA_ALLOW_QUERY_TOKEN"):
        Settings.from_env()


def test_query_token_access_log_value_is_redacted():
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s %s %s', ("client", "GET", "/console?token=dev-secret"), None)
    assert StandAccessLogRedactionFilter().filter(record) is True
    assert "dev-secret" not in record.args
    assert "<redacted>" in unquote(record.args[2])
