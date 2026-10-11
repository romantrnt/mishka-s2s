from __future__ import annotations

import re, time
import json
import sqlite3
from pathlib import Path
from .domain import Intent

class RuleBasedIntentClassifier:
    def classify(self, text, request_id, cancelled=None):
        started=time.perf_counter(); value="unknown"; lower=text.lower()
        rules=(("want_exit",r"пока|выход|стоп"),("want_game",r"игр"),("want_story",r"сказк"),("mood_positive",r"хорош|рад"),("mood_negative",r"груст|плох"),("mood_neutral",r"нормаль"))
        for candidate, pattern in rules:
            if re.search(pattern, lower): value=candidate; break
        return Intent(value, .95 if value != "unknown" else .2, request_id, "rule_based", (time.perf_counter()-started)*1000)

MockIntentClassifier = RuleBasedIntentClassifier

class ProfileProviderError(RuntimeError):
    """The configured production profile store could not be queried."""


class MockProfileProvider:
    def __init__(self):
        self.data={
            "mock-child-001":{"child_id":"fixture-child-001", "display_name":"Fixture", "available_scenarios":["fixture_core"], "profile_type":"child"},
            "mock-adult-dev-001":{"child_id":None, "display_name":"Developer", "available_scenarios":["dev_core"], "profile_type":"adult_dev", "dev_mode":True},
        }
    def resolve(self, tag_id): return self.data.get(tag_id)

class SQLiteProfileProvider:
    """SQLite profile provider with explicit schema and operational states."""
    REQUIRED_COLUMNS = {"tag_id", "payload"}
    DEFAULT_PROFILES = (
        ("test-child-001", json.dumps({
            "child_id": "test-child-001",
            "display_name": "Тестовый ребёнок",
            "available_scenarios": ["fixture_core"],
            "profile_type": "child",
        }, ensure_ascii=False)),
        ("test-adult-dev-001", json.dumps({
            "child_id": None,
            "display_name": "Developer",
            "available_scenarios": ["dev_core"],
            "profile_type": "adult_dev",
            "dev_mode": True,
        }, ensure_ascii=False)),
    )

    def __init__(self, path: str, *, initialize_schema: bool = True):
        if not path:
            raise ValueError("profile database path is required")
        self.path = path
        self.db = None
        self.last_error = None
        try:
            if path != ":memory:":
                Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path, check_same_thread=False)
            if initialize_schema:
                self.db.execute("CREATE TABLE IF NOT EXISTS profiles (tag_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
                has_profiles = self.db.execute("SELECT 1 FROM profiles LIMIT 1").fetchone() is not None
                if not has_profiles:
                    self.db.executemany("INSERT OR IGNORE INTO profiles(tag_id, payload) VALUES (?, ?)", self.DEFAULT_PROFILES)
                self.db.commit()
        except (OSError, sqlite3.Error) as exc:
            self.last_error = {"reason": "database_unreadable", "error_class": type(exc).__name__}

    def resolve(self, tag_id):
        if not tag_id:
            return None
        if self.db is None:
            raise ProfileProviderError("profile database unavailable")
        try:
            row = self.db.execute("SELECT payload FROM profiles WHERE tag_id = ?", (str(tag_id),)).fetchone()
            if not row:
                return None
            payload = json.loads(row[0])
            if not isinstance(payload, dict):
                raise ValueError("profile payload must be a JSON object")
            self.last_error = None
            return payload
        except (sqlite3.Error, json.JSONDecodeError, ValueError) as exc:
            self.last_error = {"reason": "invalid_response", "error_class": type(exc).__name__}
            raise ProfileProviderError("profile record invalid or unavailable") from exc

    def readiness(self) -> dict:
        base = {"provider": type(self).__name__, "provider_state": "unavailable", "schema_state": "unknown", "profile_availability": "unknown"}
        if self.db is None:
            return {"ready": False, **base, "reason": "database_unreadable", "details": dict(self.last_error or {})}
        try:
            table = self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='profiles'").fetchone()
            if not table:
                return {"ready": False, **base, "schema_state": "invalid", "reason": "schema_invalid", "details": {"missing_tables": ["profiles"]}}
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(profiles)").fetchall()}
            missing = sorted(self.REQUIRED_COLUMNS - columns)
            if missing:
                return {"ready": False, **base, "schema_state": "invalid", "reason": "schema_invalid", "details": {"missing_columns": missing}}
            indexed_columns = set()
            for index in self.db.execute("PRAGMA index_list(profiles)").fetchall():
                indexed_columns.update(row[2] for row in self.db.execute("PRAGMA index_info(%s)" % index[1]).fetchall())
            if "tag_id" not in indexed_columns:
                return {"ready": False, **base, "schema_state": "invalid", "reason": "schema_invalid", "details": {"missing_indexes": ["tag_id"]}}
            if self.last_error:
                return {"ready": False, "provider": type(self).__name__, "provider_state": "unavailable", "schema_state": "valid", "profile_availability": "unknown", "reason": self.last_error.get("reason", "provider_unavailable"), "details": dict(self.last_error)}
            self.db.execute("SELECT tag_id, payload FROM profiles LIMIT 1").fetchone()
            count = int(self.db.execute("SELECT COUNT(*) FROM profiles").fetchone()[0])
            return {"ready": True, "provider": type(self).__name__, "provider_state": "operational", "schema_state": "valid", "profile_availability": "with_profiles" if count else "empty", "reason": None if count else "operational_empty", "details": {"profile_count": count}}
        except sqlite3.Error as exc:
            self.last_error = {"reason": "provider_unavailable", "error_class": type(exc).__name__}
            return {"ready": False, **base, "reason": "provider_unavailable", "details": dict(self.last_error)}


class CachedProfileProvider:
    """Bounded cache whose errors remain distinct from confirmed not-found."""
    def __init__(self, remote, now=time.monotonic, ttl_s=60.0, failure_limit=3, cooldown_s=30.0):
        self.remote, self.now, self.ttl_s, self.failure_limit, self.cooldown_s = remote, now, ttl_s, failure_limit, cooldown_s
        self.cache={}; self.failures=0; self.opened_until=0.0; self.last_error=None
    def resolve(self, tag_id):
        cached=self.cache.get(tag_id); now=self.now()
        if cached and cached[0] > now: return cached[1]
        if now < self.opened_until:
            self.last_error={"provider":type(self.remote).__name__,"reason":"provider_unavailable","error_class":"CircuitOpen"}
            raise ProfileProviderError("remote profile provider circuit is open")
        try:
            value=self.remote.resolve(tag_id); self.failures=0; self.last_error=None
            if value is not None and not isinstance(value, dict):
                raise ValueError("remote profile response must be an object")
            if value is not None: self.cache[tag_id]=(now+self.ttl_s, value)
            return value
        except Exception as exc:
            self.failures += 1
            if self.failures >= self.failure_limit: self.opened_until=now+self.cooldown_s
            reason = "invalid_response" if isinstance(exc, ValueError) else "provider_unavailable"
            self.last_error={"provider":type(self.remote).__name__,"reason":reason,"error_class":type(exc).__name__}
            raise ProfileProviderError(f"{reason}: remote profile provider failed") from exc
    def readiness(self):
        if self.last_error:
            return {"ready":False,"provider":type(self).__name__,"provider_state":"unavailable","schema_state":"unknown","profile_availability":"unknown","reason":self.last_error.get("reason","provider_unavailable"),"details":dict(self.last_error)}
        return {"ready":True,"provider":type(self).__name__,"provider_state":"operational","schema_state":"not_applicable","profile_availability":"unknown","reason":None,"details":{"cache_entries":len(self.cache)}}


class RecordingAudio:
    def __init__(self): self.commands=[]; self.token=0
    def play(self, response_id, asset_id, token):
        if token == self.token: self.commands.append({"response_id":response_id,"asset_id":asset_id,"token":token})
    def play_text(self, response_id, text, token):
        if token == self.token:
            self.commands.append({"response_id":response_id,"resolved_text":text,"content_kind":"action","token":token})
    def cancel(self, token): self.token=token; self.commands.clear()
class RecordingLED:
    def __init__(self): self.commands=[]
    def pattern(self, pattern_id, color, duration_ms, token): self.commands.append({"pattern":pattern_id,"color":color,"duration_ms":duration_ms,"token":token})
    def pattern_map(self, pattern_id, colors, duration_ms, token): self.commands.append({"pattern":pattern_id,"colors":colors,"duration_ms":duration_ms,"token":token})
