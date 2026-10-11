from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field


@dataclass
class ChildProfile:
    child_id: str
    rfid_tag: str
    name: str
    age: int
    birth_month_year: str
    motor_profile: str
    sensory_profile: str
    cognitive_profile: str
    available_scenarios: list[str]
    game_history: list[dict] = field(default_factory=list)
    story_history: list[dict] = field(default_factory=list)

    def public(self) -> dict:
        return asdict(self)


class MockProfileProvider:
    """In-memory mock data only. It never stores voice or speech audio."""
    def __init__(self):
        self.by_tag = {
            "mock-child-001": ChildProfile(
                child_id="child-001", rfid_tag="mock-child-001", name="Саша", age=7,
                birth_month_year="05.2019", motor_profile="mock: prefers large buttons",
                sensory_profile="mock: calm light", cognitive_profile="mock: short instructions",
                available_scenarios=["main_demo"],
            ),
        }

    def get_by_tag(self, tag_id: str) -> ChildProfile | None:
        return self.by_tag.get(tag_id)

    def record_game(self, child_id: str, game_id: str, success: bool) -> None:
        profile = next((p for p in self.by_tag.values() if p.child_id == child_id), None)
        if profile:
            profile.game_history.append({"game_id": game_id, "success": bool(success)})

    def record_story(self, child_id: str, story_id: str) -> None:
        profile = next((p for p in self.by_tag.values() if p.child_id == child_id), None)
        if profile:
            profile.story_history.append({"story_id": story_id})


class SQLiteProfileProvider:
    """Small local provider for exhibition prototypes; stores outcomes, never audio."""
    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS profiles (tag TEXT PRIMARY KEY, child_id TEXT, name TEXT, age INTEGER)")
        self.db.execute("CREATE TABLE IF NOT EXISTS outcomes (child_id TEXT, kind TEXT, item_id TEXT, success INTEGER)")
        self.db.commit()

    def get_by_tag(self, tag_id: str) -> ChildProfile | None:
        row = self.db.execute("SELECT child_id, name, age FROM profiles WHERE tag=?", (tag_id,)).fetchone()
        if not row:
            return None
        return ChildProfile(row[0], tag_id, row[1], row[2], "mock", "mock", "mock", "mock", ["main_demo"])

    def record_game(self, child_id: str, game_id: str, success: bool) -> None:
        self.db.execute("INSERT INTO outcomes VALUES (?, 'game', ?, ?)", (child_id, game_id, int(success)))
        self.db.commit()

    def record_story(self, child_id: str, story_id: str) -> None:
        self.db.execute("INSERT INTO outcomes VALUES (?, 'story', ?, 1)", (child_id, story_id))
        self.db.commit()
