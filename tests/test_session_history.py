"""Durable session and history contracts independent of profile resolution."""
from dataclasses import asdict
import json

import pytest

from app.foundation.repositories import HistoryProviderError, SQLiteRepository
from app.foundation.domain import Lifecycle, SessionRecord

pytestmark = pytest.mark.integration


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
    parent = tmp_path / "missing"
    parent.write_text("not a directory", encoding="utf-8")
    path = parent / "history.sqlite3"
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


@pytest.mark.parametrize("loader", ["get", "iter_sessions", "active_for_installation"])
def test_legacy_session_discards_rfid_tag_and_restores_waiting_profile(tmp_path, loader):
    path = tmp_path / "legacy-sessions.sqlite3"
    first = SQLiteRepository(path)
    legacy = asdict(SessionRecord("legacy-session", "installation", "corr"))
    legacy.update(state="WAITING_RFID", rfid_tag="UID_PRIVATE_MARKER")
    first.db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?)", (
        "legacy-session", "installation", "WAITING_RFID", json.dumps(legacy), 0.0,
    ))
    first.db.commit()
    first.db.close()

    restored = SQLiteRepository(path)
    if loader == "get":
        session = restored.get("legacy-session")
    elif loader == "iter_sessions":
        (session,) = restored.iter_sessions()
    else:
        session = restored.active_for_installation("installation")
    assert session is not None
    assert session.state is Lifecycle.WAITING_PROFILE
    assert not hasattr(session, "rfid_tag")
    assert "PRIVATE_MARKER" not in json.dumps(asdict(session))
    restored.save(session)
    stored = restored.db.execute("SELECT payload FROM sessions WHERE session_id=?",
                                 ("legacy-session",)).fetchone()[0]
    assert "rfid_tag" not in json.loads(stored)
    assert "PRIVATE_MARKER" not in stored
    assert SQLiteRepository(path).get("legacy-session").state is Lifecycle.WAITING_PROFILE
