import json
import sqlite3

import pytest

from app.foundation.adapters import CachedProfileProvider, ProfileProviderError, SQLiteProfileProvider
from app.foundation.api import build_application
from app.foundation.repositories import HistoryProviderError, SQLiteRepository
from app.foundation.config import Settings
from app.foundation.domain import Lifecycle, SessionRecord

pytestmark = pytest.mark.integration

def test_sqlite_profile_provider_resolves_known_and_unknown_rfid(tmp_path):
    path = tmp_path / "profiles.sqlite3"
    provider = SQLiteProfileProvider(str(path))
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO profiles(tag_id, payload) VALUES (?, ?)", (
            "rfid-real-001", json.dumps({"child_id": "child-42", "display_name": "Ada", "age": 7}),
        ))
    assert provider.resolve("rfid-real-001")["child_id"] == "child-42"
    assert provider.resolve("unknown-rfid") is None
    assert provider.readiness()["ready"] is True


def test_sqlite_profile_provider_fails_closed_on_invalid_payload(tmp_path):
    path = tmp_path / "profiles.sqlite3"
    provider = SQLiteProfileProvider(str(path))
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO profiles(tag_id, payload) VALUES (?, ?)", ("bad", "[]"))
    with pytest.raises(ProfileProviderError, match="invalid or unavailable"):
        provider.resolve("bad")
    status = provider.readiness()
    assert status["ready"] is False
    assert status["reason"] == "invalid_response"


def test_production_build_requires_configured_profile_db(monkeypatch):
    monkeypatch.delenv("MISHKA_PROFILE_DB", raising=False)
    with pytest.raises(RuntimeError, match="MISHKA_PROFILE_DB"):
        build_application(settings=Settings(profile="production_candidate", llm_provider="ollama"))


def test_production_build_injects_sqlite_provider_without_mock_fallback(monkeypatch, tmp_path):
    path = tmp_path / "profiles.sqlite3"
    SQLiteProfileProvider(str(path))
    monkeypatch.setenv("MISHKA_PROFILE_DB", str(path))
    application, _, _, _ = build_application(settings=Settings(profile="production_candidate", llm_provider="ollama"))
    assert type(application.profiles).__name__ == "SQLiteProfileProvider"
    assert type(application.profiles).__name__ != "MockProfileProvider"
    assert type(application.history_repository).__name__ == "SQLiteRepository"
    assert application.history_repository.readiness()["history_availability"] == "empty"


def test_sqlite_readiness_distinguishes_empty_and_invalid_schema(tmp_path):
    empty = SQLiteProfileProvider(str(tmp_path / "empty.sqlite3"))
    status = empty.readiness()
    assert status["ready"] is True
    assert status["provider_state"] == "operational"
    assert status["schema_state"] == "valid"
    assert status["profile_availability"] == "empty"
    invalid_path = tmp_path / "invalid.sqlite3"
    with sqlite3.connect(invalid_path) as db:
        db.execute("CREATE TABLE profiles (wrong TEXT)")
    invalid = SQLiteProfileProvider(str(invalid_path), initialize_schema=False)
    status = invalid.readiness()
    assert status["ready"] is False
    assert status["schema_state"] == "invalid"
    assert status["reason"] == "schema_invalid"


def test_cached_provider_preserves_not_found_timeout_and_invalid_response():
    class Remote:
        def __init__(self, value): self.value = value
        def resolve(self, _tag):
            if isinstance(self.value, BaseException): raise self.value
            return self.value
    assert CachedProfileProvider(Remote(None)).resolve("unknown") is None
    with pytest.raises(ProfileProviderError): CachedProfileProvider(Remote(TimeoutError())).resolve("x")
    provider = CachedProfileProvider(Remote([]))
    with pytest.raises(ProfileProviderError): provider.resolve("x")
    assert provider.last_error["reason"] == "invalid_response"


def test_sqlite_history_empty_and_persists_across_repository_instances(tmp_path):
    path = tmp_path / "history.sqlite3"
    first = SQLiteRepository(path)
    assert first.readiness()["ready"] is True
    assert first.readiness()["history_availability"] == "empty"
    first.append("story", "child-42", "story-1", {"replay": ["a", "b"]})
    second = SQLiteRepository(path)
    assert second.readiness()["history_availability"] == "with_records"
    assert second.list_history(child_id="child-42") == [{
        "entity": "story", "child_id": "child-42", "item_id": "story-1",
        "payload": {"replay": ["a", "b"]},
    }]


def test_sqlite_history_provider_unavailable_is_not_empty(tmp_path):
    path = tmp_path / "missing" / "history.sqlite3"
    provider = SQLiteRepository(path)
    status = provider.readiness()
    assert status["ready"] is False
    assert status["history_availability"] == "unknown"
    assert status["reason"] == "database_unavailable"
    with pytest.raises(HistoryProviderError, match="history provider unavailable"):
        provider.list_history()


def test_sqlite_history_read_and_write_failures_are_diagnostic(tmp_path):
    provider = SQLiteRepository(tmp_path / "history.sqlite3")
    provider.db.close()
    with pytest.raises(HistoryProviderError, match="history write failed"):
        provider.append("game", "child-42", "game-1", {"ok": True})
    assert provider.last_error["reason"] == "history_write_failure"
    with pytest.raises(HistoryProviderError, match="history read failed"):
        provider.list_history()
    assert provider.last_error["reason"] == "history_read_failure"


def test_sqlite_session_delete_is_durable(tmp_path):
    path = tmp_path / "sessions.sqlite3"
    first = SQLiteRepository(path)
    first.save(SessionRecord("session-1", "installation", "corr", state=Lifecycle.ACTIVE))
    first.delete("session-1")
    second = SQLiteRepository(path)
    assert second.get("session-1") is None
    assert second.active_for_installation("installation") is None
