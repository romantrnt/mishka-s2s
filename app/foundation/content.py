from __future__ import annotations

import json
import re
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from .domain import SCHEMA_VERSION, ValidationIssue
from .led_contract import BUTTON_LED_COLORS

NODE_TYPES = {"say", "listen", "intent", "button_choice", "led_pattern", "game", "story", "wait", "end", "fallback", "subscenario", "bounded_free_conversation"}
BOUNDED_FIELDS = {"max_duration_seconds", "max_turns", "allowed_topics", "forbidden_topics", "context_window", "fallback_node", "timeout_node", "on_handset_down", "on_barge_in", "on_safety_signal", "return_node"}
BOUNDED_TARGET_FIELDS = {"fallback_node", "timeout_node", "on_handset_down", "on_barge_in", "on_safety_signal", "return_node"}
REQUIRED_META = {"id", "version", "language", "status", "source", "author", "updated_at", "approval_status", "compatibility", "content_type", "fallback"}
REQUIRED_NODE = {"id", "type", "prompt", "audio_asset", "allowed_intents", "transitions", "timeout", "fallback", "actions", "metadata"}
BUTTON_KEYS = {f"button_{number}" for number in range(1, 8)}
ACTION_TYPES = {"led_pattern", "led_clear", "display"}


class ContentValidator:
    def validate(self, document: dict[str, Any], responses: set[str] | None = None, assets: set[str] | None = None, games: dict[str, dict] | None = None, stories: dict[str, dict] | None = None) -> list[ValidationIssue]:
        responses = set() if responses is None else responses
        assets = set() if assets is None else assets
        issues: list[ValidationIssue] = []
        sid = str(document.get("id", "<missing>"))
        if not isinstance(document.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", document.get("id", "")):
            issues.append(ValidationIssue(sid, None, "id", "must be a valid scenario ID", "use 1-64 letters, digits, dot, underscore, or hyphen"))
        meta = document.get("metadata")
        if not isinstance(meta, dict):
            return [ValidationIssue(sid, None, "metadata", "missing metadata", "add the versioned metadata object")]
        for key in REQUIRED_META:
            if key not in meta:
                issues.append(ValidationIssue(sid, None, f"metadata.{key}", "required field is missing", f"add metadata.{key}"))
        if meta.get("compatibility") != SCHEMA_VERSION:
            issues.append(ValidationIssue(sid, None, "metadata.compatibility", "incompatible schema version", f"set to {SCHEMA_VERSION} or migrate content"))
        nodes = document.get("nodes")
        if not isinstance(nodes, list):
            return issues + [ValidationIssue(sid, None, "nodes", "must be a list", "provide a node list")]
        ids: set[str] = set(); graph: dict[str, set[str]] = {}
        for node in nodes:
            nid = str(node.get("id", "<missing>")) if isinstance(node, dict) else "<invalid>"
            if not isinstance(node, dict):
                issues.append(ValidationIssue(sid, nid, "node", "must be an object", "replace with an object")); continue
            if nid in ids: issues.append(ValidationIssue(sid, nid, "id", "duplicate ID", "use a unique node ID"))
            ids.add(nid)
            for key in REQUIRED_NODE - set(node):
                issues.append(ValidationIssue(sid, nid, key, "required field is missing", f"add {key}"))
            if node.get("type") not in NODE_TYPES:
                issues.append(ValidationIssue(sid, nid, "type", "unsupported node type", "use a documented node type"))
            if not isinstance(node.get("prompt"), str):
                issues.append(ValidationIssue(sid, nid, "prompt", "must be a string", "use a response ID or an empty string"))
            if node.get("audio_asset") is not None and not isinstance(node.get("audio_asset"), str):
                issues.append(ValidationIssue(sid, nid, "audio_asset", "must be a string or null", "use a manifest ID or null"))
            if not isinstance(node.get("allowed_intents"), list) or not all(isinstance(item, str) for item in node.get("allowed_intents", [])):
                issues.append(ValidationIssue(sid, nid, "allowed_intents", "must be a string list", "use an array of intent IDs"))
            transitions = node.get("transitions")
            if not isinstance(transitions, dict):
                issues.append(ValidationIssue(sid, nid, "transitions", "must be an object", "map transition names to node IDs")); transitions = {}
            for transition, target in transitions.items():
                if not isinstance(transition, str):
                    issues.append(ValidationIssue(sid, nid, "transitions", "transition names must be strings", "use named transitions"))
                elif transition.startswith("button_") and transition not in BUTTON_KEYS:
                    issues.append(ValidationIssue(sid, nid, f"transitions.{transition}", "button must be in range 1..7", "use button_1 through button_7"))
                if not isinstance(target, str) or not target:
                    issues.append(ValidationIssue(sid, nid, f"transitions.{transition}", "target must be a non-empty string", "reference a node ID"))
            actions = node.get("actions")
            if not isinstance(actions, list):
                issues.append(ValidationIssue(sid, nid, "actions", "must be a list", "use an array of action objects"))
            else:
                for index, action in enumerate(actions):
                    field = f"actions[{index}]"
                    if not isinstance(action, dict) or action.get("type") not in ACTION_TYPES:
                        issues.append(ValidationIssue(sid, nid, field, "unsupported action", "use led_pattern, led_clear, or display")); continue
                    if action["type"] == "led_pattern" and not isinstance(action.get("pattern"), str):
                        issues.append(ValidationIssue(sid, nid, f"{field}.pattern", "must be a string", "set the LED pattern ID"))
                    if action.get("duration_ms") is not None and (not isinstance(action["duration_ms"], int) or not 0 <= action["duration_ms"] <= 120000):
                        issues.append(ValidationIssue(sid, nid, f"{field}.duration_ms", "must be an integer from 0 to 120000", "set a bounded duration"))
                    if "colors" in action and (not isinstance(action["colors"], dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in action["colors"].items())):
                        issues.append(ValidationIssue(sid, nid, f"{field}.colors", "must be a string map", "map button numbers to color names"))
                    if action["type"] == "led_pattern" and isinstance(action.get("colors"), dict):
                        for button_id, color in action["colors"].items():
                            expected = BUTTON_LED_COLORS.get(int(button_id)) if str(button_id).isdigit() else None
                            if expected is None or str(color).lower() != expected:
                                issues.append(ValidationIssue(
                                    sid, nid, f"{field}.colors.{button_id}",
                                    "color does not match canonical button mapping",
                                    "use the immutable button/color contract",
                                ))
            if node.get("type") == "bounded_free_conversation":
                for key in BOUNDED_FIELDS - set(node):
                    issues.append(ValidationIssue(sid, nid, key, "bounded conversation field is missing", f"add {key}"))
                for key in ("max_duration_seconds", "max_turns", "context_window"):
                    if not isinstance(node.get(key), int) or int(node.get(key, 0)) <= 0:
                        issues.append(ValidationIssue(sid, nid, key, "must be a positive integer", f"set a positive {key}"))
                for key in ("allowed_topics", "forbidden_topics"):
                    value = node.get(key)
                    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
                        issues.append(ValidationIssue(sid, nid, key, "must be a non-empty string list", f"add explicit {key}"))
            if node.get("type") != "end" and not node.get("fallback"):
                issues.append(ValidationIssue(sid, nid, "fallback", "missing fallback", "reference a safe fallback or end node"))
            prompt = node.get("prompt")
            if prompt and prompt not in responses:
                issues.append(ValidationIssue(sid, nid, "prompt", "unknown response ID", "add the response or change prompt"))
            asset = node.get("audio_asset")
            if asset and asset not in assets:
                issues.append(ValidationIssue(sid, nid, "audio_asset", "missing asset", "add asset manifest entry or remove reference"))
            targets: set[str] = {str(t) for t in transitions.values() if t}
            if node.get("fallback"):
                targets.add(str(node["fallback"]))
            if node.get("type") == "bounded_free_conversation":
                targets.update(str(node[key]) for key in BOUNDED_TARGET_FIELDS if node.get(key))
            metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
            if node.get("type") == "game" and games is not None:
                game_id = metadata.get("game_id"); game = games.get(str(game_id)) if game_id else None
                if game is None:
                    issues.append(ValidationIssue(sid, nid, "metadata.game_id", "unknown game ID", "reference an entry in games.json"))
                else:
                    expected = game.get("expected")
                    if not isinstance(expected, list) or not expected or not all(isinstance(value, int) and 1 <= value <= 7 for value in expected):
                        issues.append(ValidationIssue(sid, nid, "metadata.game_id.expected", "must be a non-empty list of buttons 1..7", "fix games.json"))
                    if not isinstance(game.get("max_attempts"), int) or game["max_attempts"] <= 0:
                        issues.append(ValidationIssue(sid, nid, "metadata.game_id.max_attempts", "must be positive", "fix games.json"))
                    led_sequence = metadata.get("led_sequence")
                    if led_sequence is not None:
                        if not isinstance(led_sequence, list) or not led_sequence or not all(isinstance(value, int) and 1 <= value <= 7 for value in led_sequence):
                            issues.append(ValidationIssue(sid, nid, "metadata.led_sequence", "must be a non-empty list of buttons 1..7", "fix the game LED sequence"))
                        elif led_sequence != expected:
                            issues.append(ValidationIssue(sid, nid, "metadata.led_sequence", "must match games.json expected order", "keep one canonical game sequence"))
                    for response_key in ("success_response", "failure_response"):
                        if game.get(response_key) not in responses:
                            issues.append(ValidationIssue(sid, nid, f"metadata.game_id.{response_key}", "unknown response ID", "add the response or fix games.json"))
            if node.get("type") == "story" and stories is not None:
                story_id = metadata.get("story_id"); story = stories.get(str(story_id)) if story_id else None
                if story is None or not isinstance(story.get("text"), str) or not story["text"].strip():
                    issues.append(ValidationIssue(sid, nid, "metadata.story_id", "unknown or empty story", "reference a story with non-empty text"))
            graph[nid] = {t for t in targets if t != "retry"}
        start = document.get("start")
        if start not in ids: issues.append(ValidationIssue(sid, None, "start", "does not reference a node", "point start at a valid node"))
        for nid, targets in graph.items():
            for target in targets:
                if target not in ids:
                    issues.append(ValidationIssue(sid, nid, "transitions", f"unknown target {target}", "reference an existing node"))
        if start in ids:
            reachable = self._reachable(graph, str(start))
            for nid in ids - reachable: issues.append(ValidationIssue(sid, nid, "id", "unreachable node", "add a transition or remove node"))
            if not any(node.get("id") in reachable and node.get("type") == "end" for node in nodes if isinstance(node, dict)):
                issues.append(ValidationIssue(sid, None, "graph", "no reachable end node", "add a path to an end node"))
        return issues

    @staticmethod
    def _reachable(graph: dict[str, set[str]], start: str) -> set[str]:
        todo, seen = [start], set()
        while todo:
            current = todo.pop()
            if current in seen: continue
            seen.add(current); todo.extend(graph.get(current, set()) - seen)
        return seen


class FileScenarioRepository:
    """Filesystem adapter with fail-closed, bounded content diagnostics."""

    DEFAULT_ISSUE_LIMIT = 200

    def __init__(self, root: str | Path, *, strict: bool = False, max_issues: int | None = None):
        self.root = Path(root)
        self.strict = bool(strict)
        self.issue_limit = max(1, int(max_issues or self.DEFAULT_ISSUE_LIMIT))
        self.scenarios: dict[str, dict] = {}
        self.issues: list[ValidationIssue] = []
        self.notices: list[dict[str, Any]] = []
        self._issue_count = 0
        self._error_count = 0
        self._issues_truncated = 0
        self._partial_load = False
        self.games = self._mapping(self.root / "games", "games")
        self.stories = self._mapping(self.root / "stories", "stories")
        responses = self._ids(self.root / "responses")
        assets = self._ids(self.root / "audio")
        validator = ContentValidator()
        scenario_dir = self.root / "scenarios"
        if not scenario_dir.exists():
            self._record(ValidationIssue(
                "<content>", None, "scenarios", "required resource directory is missing",
                "provide content/scenarios/*.json", category="missing_resource",
                resource="scenarios", severity="error",
            ))
        for path in sorted(scenario_dir.glob("*.json")):
            document = self._load_json(path, category="malformed_json")
            if document is None:
                continue
            if not isinstance(document, dict):
                self._record(ValidationIssue(
                    path.stem, None, "document", "must be a JSON object",
                    "replace the document with an object", category="invalid_schema",
                    resource=str(path.relative_to(self.root)), expected="object",
                ))
                continue
            if path.stem == "main_demo" and not isinstance(document.get("metadata"), dict):
                self.notices.append({
                    "category": "legacy_excluded",
                    "resource": str(path.relative_to(self.root)),
                    "scenario_id": str(document.get("id", path.stem)),
                    "severity": "info",
                    "detail": "legacy scenario excluded from canonical content contract",
                })
                continue
            problems = validator.validate(document, responses, assets, self.games, self.stories)
            for issue in problems:
                self._record(replace(
                    issue, category="schema", resource=str(path.relative_to(self.root)),
                    expected=issue.suggested_fix,
                ))
            if not problems:
                scenario_id = document.get("id")
                if scenario_id in self.scenarios:
                    self._record(ValidationIssue(
                        str(scenario_id), None, "id", "duplicate scenario ID",
                        "use a unique scenario ID", category="duplicate_id",
                        resource=str(path.relative_to(self.root)), expected="unique string",
                    ))
                elif isinstance(scenario_id, str):
                    self.scenarios[scenario_id] = document
        self._partial_load = bool(self._error_count and self.scenarios)
        if self._error_count:
            self.scenarios = {}
        if self.strict and self._error_count:
            detail = json.dumps(self.report(), ensure_ascii=False, sort_keys=True)
            raise ValueError("content validation failed: " + detail)
        if not self.scenarios and not self._errors:
            self._record(ValidationIssue(
                "<content>", None, "scenarios", "no valid scenarios loaded",
                "provide at least one valid scenario", category="empty_content",
                resource="scenarios",
            ))
            if self.strict:
                raise ValueError("content validation failed: " + json.dumps(self.report(), ensure_ascii=False))

    def _record(self, issue: ValidationIssue) -> None:
        self._issue_count += 1
        if issue.severity == "error":
            self._error_count += 1
        if len(self.issues) < self.issue_limit:
            self.issues.append(issue)
        else:
            self._issues_truncated += 1

    @property
    def _errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def has_errors(self) -> bool:
        return self._error_count > 0

    def _load_json(self, path: Path, *, category: str) -> Any | None:
        duplicates: list[str] = []

        def object_pairs_hook(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    duplicates.append(str(key))
                value[key] = item
            return value

        try:
            data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=object_pairs_hook)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            self._record(ValidationIssue(
                path.stem, None, "file", f"invalid JSON: {type(exc).__name__}",
                "fix JSON syntax and encoding", category=category,
                resource=str(path.relative_to(self.root)), expected="valid UTF-8 JSON",
            ))
            return None
        for duplicate in duplicates:
            self._record(ValidationIssue(
                path.stem, None, "id", f"duplicate ID {duplicate}",
                "keep one definition for each ID", category="duplicate_id",
                resource=str(path.relative_to(self.root)), expected="unique object keys",
            ))
        return data

    def _ids(self, folder: Path) -> set[str]:
        found: set[str] = set()
        if not folder.exists():
            self._record(ValidationIssue(
                folder.name, None, "directory", "required resource directory is missing",
                f"provide {folder.name}/*.json", category="missing_resource",
                resource=str(folder.relative_to(self.root)),
            ))
            return found
        for path in sorted(folder.glob("*.json")):
            data = self._load_json(path, category="malformed_json")
            if data is None:
                continue
            values = data.get("responses") if folder.name == "responses" else data.get("assets")
            if values is None:
                self._record(ValidationIssue(
                    path.stem, None, folder.name, "required mapping is missing",
                    f"add a {folder.name} object", category="missing_required_field",
                    resource=str(path.relative_to(self.root)), expected="object",
                ))
                continue
            if not isinstance(values, dict):
                self._record(ValidationIssue(
                    path.stem, None, folder.name, "mapping must be an object",
                    f"make {folder.name} an object keyed by ID", category="invalid_schema",
                    resource=str(path.relative_to(self.root)), expected="object",
                ))
                continue
            for key, value in values.items():
                if not isinstance(key, str) or not key:
                    self._record(ValidationIssue(
                        path.stem, None, folder.name, "resource ID must be a non-empty string",
                        "use a unique string ID", category="invalid_field_type",
                        resource=str(path.relative_to(self.root)), expected="string",
                    ))
                    continue
                if key in found:
                    self._record(ValidationIssue(
                        path.stem, None, f"{folder.name}.{key}", "duplicate ID",
                        "keep one definition for each ID", category="duplicate_id",
                        resource=str(path.relative_to(self.root)), expected="unique ID",
                    ))
                found.add(key)
        return found

    def _mapping(self, folder: Path, key: str) -> dict[str, dict]:
        found: dict[str, dict] = {}
        if not folder.exists():
            self._record(ValidationIssue(
                folder.name, None, key, "required resource directory is missing",
                f"provide {folder.name}/*.json", category="missing_resource",
                resource=str(folder.relative_to(self.root)),
            ))
            return found
        for path in sorted(folder.glob("*.json")):
            data = self._load_json(path, category="malformed_json")
            if data is None:
                continue
            values = data.get(key)
            if values is None:
                self._record(ValidationIssue(
                    path.stem, None, key, "required mapping is missing",
                    f"add a {key} object", category="missing_required_field",
                    resource=str(path.relative_to(self.root)), expected="object",
                ))
                continue
            if not isinstance(values, dict):
                self._record(ValidationIssue(
                    path.stem, None, key, "mapping must be an object",
                    f"make {key} an object keyed by ID", category="invalid_schema",
                    resource=str(path.relative_to(self.root)), expected="object",
                ))
                continue
            for name, value in values.items():
                if not isinstance(value, dict):
                    self._record(ValidationIssue(
                        path.stem, None, f"{key}.{name}", "resource must be an object",
                        "replace the resource with an object", category="invalid_schema",
                        resource=str(path.relative_to(self.root)), expected="object",
                    ))
                    continue
                if str(name) in found:
                    self._record(ValidationIssue(
                        path.stem, None, f"{key}.{name}", "duplicate ID",
                        "keep one definition for each ID", category="duplicate_id",
                        resource=str(path.relative_to(self.root)), expected="unique ID",
                    ))
                found[str(name)] = value
        return found

    def get(self, scenario_id: str) -> dict:
        return self.scenarios[scenario_id]

    def game(self, game_id: str) -> dict | None:
        return self.games.get(game_id)

    def story(self, story_id: str) -> dict | None:
        return self.stories.get(story_id)

    def report(self) -> dict:
        return {
            "valid_scenarios": sorted(self.scenarios),
            "issues": [asdict(x) for x in self.issues],
            "notices": list(self.notices),
            "issue_count": self._issue_count,
            "error_count": self._error_count,
            "issues_truncated": self._issues_truncated,
            "issue_limit": self.issue_limit,
            "partial_load": self._partial_load,
            "strict": self.strict,
        }
