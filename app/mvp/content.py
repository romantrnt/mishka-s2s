from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REQUIRED_NODE_FIELDS = {"id", "type", "prompt", "audio_asset", "allowed_intents", "transitions", "timeout", "fallback", "actions", "metadata"}
NODE_TYPES = {"say", "listen", "intent", "button_choice", "led_pattern", "game", "story", "wait", "end", "fallback"}


@dataclass
class ContentLoadResult:
    scenarios: dict[str, dict]
    responses: dict[str, dict]
    stories: dict[str, dict]
    games: dict[str, dict]
    errors: list[str]


class ContentRepository:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root else Path(__file__).resolve().parents[2] / "content"
        self.data = self._load()

    def _read(self, path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._errors.append(f"invalid or missing content: {path.name}")
            return default

    def _load(self) -> ContentLoadResult:
        self._errors: list[str] = []
        responses = self._read(self.root / "responses" / "phrases.json", {"responses": {}}).get("responses", {})
        stories = self._read(self.root / "stories" / "stories.json", {"stories": {}}).get("stories", {})
        games = self._read(self.root / "games" / "games.json", {"games": {}}).get("games", {})
        scenarios: dict[str, dict] = {}
        for path in sorted((self.root / "scenarios").glob("*.json")) if (self.root / "scenarios").exists() else []:
            document = self._read(path, {})
            scenario = self._validate_scenario(document, path.name)
            if scenario:
                scenarios[scenario["id"]] = scenario
        if not scenarios:
            scenarios["safe_fallback"] = {"id": "safe_fallback", "start": "end", "nodes": {"end": self._node("end")}}
        return ContentLoadResult(scenarios, responses, stories, games, self._errors)

    @staticmethod
    def _node(node_type: str) -> dict:
        return {"id": "end", "type": node_type, "prompt": "ending", "audio_asset": None, "allowed_intents": [], "transitions": {}, "timeout": 0, "fallback": None, "actions": [], "metadata": {}}

    def _validate_scenario(self, document: dict, label: str) -> dict | None:
        if not isinstance(document, dict) or not isinstance(document.get("id"), str) or not isinstance(document.get("nodes"), list):
            self._errors.append(f"invalid scenario structure: {label}")
            return None
        nodes: dict[str, dict] = {}
        for raw in document["nodes"]:
            if not isinstance(raw, dict) or REQUIRED_NODE_FIELDS - set(raw) or raw.get("type") not in NODE_TYPES or not isinstance(raw.get("id"), str):
                self._errors.append(f"invalid node in {label}")
                continue
            nodes[raw["id"]] = raw
        start = document.get("start")
        if not nodes or start not in nodes:
            self._errors.append(f"scenario has no valid start: {label}")
            return None
        return {"id": document["id"], "start": start, "nodes": nodes}

    def scenario(self, scenario_id: str) -> dict:
        scenario = self.data.scenarios.get(scenario_id)
        if scenario is not None:
            return scenario
        # Valid content normally has main_demo only; a safe fallback is needed
        # only when no scenario survived validation.
        return self.data.scenarios.get("safe_fallback") or next(iter(self.data.scenarios.values()))

    def response(self, response_id: str | None) -> dict:
        fallback = {"id": "content_error", "text": "Давай попробуем ещё раз.", "audio_asset": None}
        if not response_id:
            return fallback
        value = self.data.responses.get(response_id)
        return value if isinstance(value, dict) and isinstance(value.get("text"), str) else fallback

    def story(self, story_id: str) -> dict | None:
        value = self.data.stories.get(story_id)
        return value if isinstance(value, dict) else None

    def game(self, game_id: str) -> dict | None:
        value = self.data.games.get(game_id)
        return value if isinstance(value, dict) else None
