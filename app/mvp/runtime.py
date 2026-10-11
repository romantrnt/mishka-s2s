from __future__ import annotations

import time
from typing import Callable

from .content import ContentRepository
from .hardware import MockAudioOutput, MockLEDController
from .interfaces import IntentClassifier, ProfileProvider
from .types import GameProgress, IntentResult, Session, SessionState, TransitionRecord


class ScenarioRuntime:
    """The product's primary deterministic state machine.

    Node content is data. Python only interprets the documented node types.
    """
    def __init__(self, content: ContentRepository, classifier: IntentClassifier, profiles: ProfileProvider, led: MockLEDController, audio_for: Callable[[str], MockAudioOutput], clock=time.monotonic, confidence_threshold: float = 0.6):
        self.content, self.classifier, self.profiles, self.led, self.audio_for = content, classifier, profiles, led, audio_for
        self.clock, self.confidence_threshold = clock, confidence_threshold
        self.incidents: list[dict] = []  # deliberately excludes raw child text/audio

    def start(self, session: Session) -> list[dict]:
        scenario = self.content.scenario(session.scenario_id)
        return self.goto(session, scenario["start"], "scenario_start")

    def node(self, session: Session) -> dict:
        nodes = self.content.scenario(session.scenario_id)["nodes"]
        node = nodes.get(session.current_node or "")
        if node is not None:
            return node
        # Legacy mock scenarios predate the foundation fixture and may not
        # expose a global safe_fallback. Keep their fallback local.
        return nodes.get("end") or next(iter(nodes.values()))

    def goto(self, session: Session, node_id: str, reason: str) -> list[dict]:
        scenario = self.content.scenario(session.scenario_id)
        if node_id not in scenario["nodes"]:
            self.incidents.append({"session_id": session.session_id, "kind": "content_transition_error", "node": session.current_node})
            node_id = "fallback" if "fallback" in scenario["nodes"] else "end"
        old = session.current_node
        session.current_node = node_id
        now = self.clock()
        session.node_entered_at = now
        session.last_activity_at = now
        session.transitions.append(TransitionRecord(now, old, node_id, reason))
        return self._enter(session)

    def _enter(self, session: Session) -> list[dict]:
        outputs: list[dict] = []
        # Automatic nodes may chain; cap protects a malformed content loop.
        for _ in range(25):
            node = self.node(session)
            state = node.get("metadata", {}).get("state")
            if state:
                session.state = SessionState(state)
            outputs.extend(self._actions(session, node.get("actions", [])))
            kind = node["type"]
            if kind == "say":
                outputs.extend(self._response(session, node["prompt"], node.get("audio_asset")))
                next_node = node["transitions"].get("next")
                if next_node:
                    outputs.extend(self.goto(session, next_node, "say_complete"))
                return outputs
            if kind == "led_pattern":
                next_node = node["transitions"].get("next")
                if next_node:
                    outputs.extend(self.goto(session, next_node, "led_complete"))
                return outputs
            if kind == "story":
                story_id = node["metadata"].get("story_id", "")
                story = self.content.story(story_id)
                if not story:
                    return outputs + self._fallback(session, "missing_story")
                outputs.extend(self._raw_audio(session, story_id, story["text"], story.get("audio_asset")))
                if session.child_id and not session.anonymous:
                    self.profiles.record_story(session.child_id, story_id)
                next_node = node["transitions"].get("next")
                if next_node:
                    outputs.extend(self.goto(session, next_node, "story_complete"))
                return outputs
            if kind == "game":
                game_id = node["metadata"].get("game_id", "")
                game = self.content.game(game_id)
                if not game:
                    return outputs + self._fallback(session, "missing_game")
                session.state = SessionState.GAME
                session.game = GameProgress(game_id, list(game["expected"]), max_attempts=int(game.get("max_attempts", 3)), started_at=self.clock())
                outputs.extend(self._response(session, node["prompt"], node.get("audio_asset")))
                outputs.append(self.led.show(game["pattern"], "yellow", 800))
                return outputs
            if kind == "fallback":
                session.state = SessionState.FALLBACK
                outputs.extend(self._response(session, node["prompt"], node.get("audio_asset")))
                return outputs
            if kind == "end":
                session.state = SessionState.ENDING
                session.game = None
                outputs.extend(self._response(session, node["prompt"], node.get("audio_asset")))
                outputs.append(self.led.clear())
                return outputs
            # listen, intent, button_choice and wait wait for a future event.
            return outputs
        return self._fallback(session, "automatic_loop_guard")

    def _actions(self, session: Session, actions: list[dict]) -> list[dict]:
        result = []
        for action in actions:
            if action.get("action") == "led_pattern":
                result.append(self.led.show(action.get("pattern", "safe"), action.get("color", "yellow"), int(action.get("duration_ms", 800))))
            elif action.get("action") == "led_clear":
                result.append(self.led.clear())
        return result

    def _response(self, session: Session, response_id: str, asset: str | None = None) -> list[dict]:
        if response_id == "welcome_named" and session.anonymous:
            response_id = "welcome_anonymous"
        response = self.content.response(response_id)
        text = response["text"]
        if "{name}" in text:
            profile = next((p for p in getattr(self.profiles, "by_tag", {}).values() if p.child_id == session.child_id), None)
            text = text.format(name=profile.name if profile else "друг")
        return self._raw_audio(session, response_id, text, asset or response.get("audio_asset"))

    def _raw_audio(self, session: Session, response_id: str, text: str, asset: str | None) -> list[dict]:
        output = self.audio_for(session.session_id).speak(response_id, text, asset, session.response_token)
        if output is None:
            return []
        output["session_id"] = session.session_id
        session.outputs.append(output)
        return [output]

    def handle_text(self, session: Session, text: str) -> list[dict]:
        node = self.node(session)
        if node["type"] not in {"listen", "intent"}:
            return self._fallback(session, "unexpected_text")
        result = self.classifier.classify(text)
        if not result.valid(self.confidence_threshold):
            self.incidents.append({"session_id": session.session_id, "kind": "classifier_fallback", "intent": result.intent, "confidence": result.confidence, "reason": result.reason})
            return self._fallback(session, "low_confidence")
        target = node["transitions"].get(result.intent)
        if result.intent not in node.get("allowed_intents", []) or not target:
            self.incidents.append({"session_id": session.session_id, "kind": "intent_not_allowed", "intent": result.intent})
            return self._fallback(session, "unknown_or_disallowed_intent")
        return self.goto(session, target, f"intent:{result.intent}")

    def handle_button(self, session: Session, button_id: int) -> list[dict]:
        if session.game:
            return self._game_button(session, button_id)
        node = self.node(session)
        target = node.get("transitions", {}).get(f"button_{button_id}")
        if target == "retry":
            target = session.return_node or "choose_mode"
        if target:
            return self.goto(session, target, f"button:{button_id}")
        return self._fallback(session, f"unknown_button:{button_id}")

    def _game_button(self, session: Session, button_id: int) -> list[dict]:
        game = session.game
        assert game is not None
        config = self.content.game(game.game_id) or {}
        if button_id == game.expected[game.index]:
            game.index += 1
            if game.index < len(game.expected):
                return [self.led.show(f"next_button_{game.expected[game.index]}", "green", 500)]
            if session.child_id and not session.anonymous:
                self.profiles.record_game(session.child_id, game.game_id, True)
            session.game = None
            outputs = self._response(session, config.get("success_response", "correct"))
            return outputs + self.goto(session, self.node(session)["transitions"].get("success", "game_result"), "game_success")
        game.attempts += 1
        outputs = self._response(session, "incorrect") + [self.led.show(config.get("pattern", "safe"), "red", 800)]
        if game.attempts >= game.max_attempts:
            if session.child_id and not session.anonymous:
                self.profiles.record_game(session.child_id, game.game_id, False)
            session.game = None
            return outputs + self.goto(session, self.node(session)["transitions"].get("failure", "game_result"), "game_attempts_exhausted")
        game.index = 0
        return outputs

    def timeout(self, session: Session) -> list[dict]:
        node = self.node(session)
        timeout_s = float(node.get("timeout") or 0)
        if not timeout_s or self.clock() - session.node_entered_at < timeout_s:
            return []
        if session.game:
            session.game = None
        target = node.get("transitions", {}).get("timeout") or node.get("fallback") or "end"
        return self.goto(session, target, "timeout")

    def _fallback(self, session: Session, reason: str) -> list[dict]:
        session.errors += 1
        self.incidents.append({"session_id": session.session_id, "kind": "fallback", "reason": reason, "errors": session.errors})
        if session.errors >= session.max_errors:
            return self.goto(session, "end", "fallback_limit")
        session.return_node = session.current_node if session.current_node != "fallback" else "choose_mode"
        return self.goto(session, "fallback", reason)
