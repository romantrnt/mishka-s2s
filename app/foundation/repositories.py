from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict

from .domain import Lifecycle, SessionRecord

MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, installation_id TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_sessions_installation_state ON sessions(installation_id, state);
CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY, entity TEXT NOT NULL, child_id TEXT NOT NULL, item_id TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_history_child ON history(child_id, created_at);
"""

class InMemorySessionRepository:
    def __init__(self): self.items: dict[str, SessionRecord] = {}
    def get(self, session_id): return self.items.get(session_id)
    def save(self, session): self.items[session.session_id] = session
    def delete(self, session_id): self.items.pop(session_id, None)
    def iter_sessions(self): return tuple(self.items.values())
    def active_for_installation(self, installation_id):
        return next((x for x in self.items.values() if x.installation_id == installation_id and x.state not in {Lifecycle.ENDED, Lifecycle.IDLE} and x.error_state != "installation_busy"), None)

class HistoryProviderError(RuntimeError):
    """A history/session provider could not complete an operation."""

class InMemoryHistoryRepository:
    def __init__(self): self.items=[]; self.last_error=None
    def append(self, entity, child_id, item_id, payload): self.items.append({"entity":entity,"child_id":child_id,"item_id":item_id,"payload":payload})
    def has(self,entity,child_id,item_id): return any(x["entity"]==entity and x["child_id"]==child_id and x["item_id"]==item_id for x in self.items)
    def list_history(self, *, entity=None, child_id=None):
        return [dict(item) for item in self.items
                if (entity is None or item["entity"] == entity)
                and (child_id is None or item["child_id"] == child_id)]
    def readiness(self):
        return {"ready": True, "provider": type(self).__name__,
                "provider_state": "operational", "schema_state": "not_applicable",
                "history_availability": "with_records" if self.items else "empty",
                "reason": None if self.items else "operational_empty"}

class SQLiteRepository(InMemorySessionRepository):
    """Durable SQLite session/history adapter used by canonical production."""
    def __init__(self, path=":memory:"):
        super().__init__(); self.path = str(path); self.db = None; self.last_error = None
        try:
            self.db = sqlite3.connect(self.path, check_same_thread=False)
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript(MIGRATION_SQL); self.db.commit()
        except (OSError, sqlite3.Error) as exc:
            self.last_error = {"reason": "database_unavailable", "error_class": type(exc).__name__}
            if self.db is not None:
                self.db.close()
            self.db = None

    def _require_db(self):
        if self.db is None:
            raise HistoryProviderError("history provider unavailable")
        return self.db

    def save(self, session):
        db = self._require_db(); payload = asdict(session); payload["state"] = session.state.value
        try:
            db.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?, ?)", (session.session_id, session.installation_id, session.state.value, json.dumps(payload), session.last_activity_at)); db.commit()
            super().save(session); self.last_error = None
        except (sqlite3.Error, TypeError, ValueError) as exc:
            self.last_error = {"reason": "history_write_failure", "error_class": type(exc).__name__}
            raise HistoryProviderError("history/session write failed") from exc

    def delete(self, session_id):
        db = self._require_db()
        try:
            db.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))
            db.commit()
            super().delete(session_id)
            self.last_error = None
        except sqlite3.Error as exc:
            self.last_error = {"reason": "history_write_failure", "error_class": type(exc).__name__}
            raise HistoryProviderError("history/session delete failed") from exc

    def get(self, session_id):
        cached = super().get(session_id)
        if cached: return cached
        db = self._require_db()
        try:
            row = db.execute("SELECT payload FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if not row: return None
            data = json.loads(row[0]); data["state"] = Lifecycle(data["state"])
            restored = SessionRecord(**data); self.items[session_id] = restored
            self.last_error = None; return restored
        except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError) as exc:
            self.last_error = {"reason": "history_read_failure", "error_class": type(exc).__name__}
            raise HistoryProviderError("history/session read failed") from exc

    def iter_sessions(self):
        db = self._require_db()
        try:
            rows = db.execute("SELECT payload FROM sessions ORDER BY updated_at").fetchall()
            sessions = []
            for (raw,) in rows:
                data = json.loads(raw); data["state"] = Lifecycle(data["state"])
                session = SessionRecord(**data); self.items[session.session_id] = session; sessions.append(session)
            self.last_error = None; return tuple(sessions)
        except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError) as exc:
            self.last_error = {"reason": "history_read_failure", "error_class": type(exc).__name__}
            raise HistoryProviderError("history/session read failed") from exc

    def active_for_installation(self, installation_id):
        return next((x for x in self.iter_sessions()
                     if x.installation_id == installation_id
                     and x.state not in {Lifecycle.ENDED, Lifecycle.IDLE}
                     and x.error_state != "installation_busy"), None)

    def append(self, entity, child_id, item_id, payload):
        db = self._require_db()
        try:
            db.execute("INSERT INTO history(entity, child_id, item_id, payload, created_at) VALUES (?, ?, ?, ?, strftime('%s','now'))", (entity, child_id, item_id, json.dumps(payload))); db.commit(); self.last_error = None
        except (sqlite3.Error, TypeError, ValueError) as exc:
            self.last_error = {"reason": "history_write_failure", "error_class": type(exc).__name__}
            raise HistoryProviderError("history write failed") from exc

    def has(self, entity, child_id, item_id):
        db = self._require_db()
        try:
            result = db.execute("SELECT 1 FROM history WHERE entity=? AND child_id=? AND item_id=? LIMIT 1", (entity, child_id, item_id)).fetchone() is not None
            self.last_error = None; return result
        except sqlite3.Error as exc:
            self.last_error = {"reason": "history_read_failure", "error_class": type(exc).__name__}
            raise HistoryProviderError("history read failed") from exc

    def list_history(self, *, entity=None, child_id=None):
        db = self._require_db()
        try:
            clauses, params = [], []
            if entity is not None: clauses.append("entity=?"); params.append(entity)
            if child_id is not None: clauses.append("child_id=?"); params.append(child_id)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = db.execute("SELECT entity, child_id, item_id, payload FROM history" + where + " ORDER BY id", params).fetchall()
            self.last_error = None
            return [{"entity": row[0], "child_id": row[1], "item_id": row[2], "payload": json.loads(row[3])} for row in rows]
        except (sqlite3.Error, json.JSONDecodeError, TypeError, ValueError) as exc:
            self.last_error = {"reason": "history_read_failure", "error_class": type(exc).__name__}
            raise HistoryProviderError("history read failed") from exc

    def readiness(self):
        base = {"provider": type(self).__name__, "path": self.path,
                "provider_state": "unavailable", "schema_state": "unknown",
                "history_availability": "unknown"}
        if self.db is None:
            reason = self.last_error.get("reason", "database_unavailable") if self.last_error else "database_unavailable"
            return {"ready": False, **base, "reason": reason, "details": dict(self.last_error or {})}
        try:
            tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = sorted({"sessions", "history"} - tables)
            if missing:
                return {"ready": False, **base, "schema_state": "invalid", "reason": "schema_invalid", "details": {"missing_tables": missing}}
            self.db.execute("SELECT 1").fetchone()
            count = int(self.db.execute("SELECT COUNT(*) FROM history").fetchone()[0])
            return {"ready": True, **base, "provider_state": "operational", "schema_state": "valid",
                    "history_availability": "with_records" if count else "empty",
                    "reason": None if count else "operational_empty", "details": {"history_count": count}}
        except sqlite3.Error as exc:
            self.last_error = {"reason": "provider_unavailable", "error_class": type(exc).__name__}
            return {"ready": False, **base, "reason": "provider_unavailable", "details": dict(self.last_error)}
