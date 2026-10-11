from __future__ import annotations

import json
import re
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from .types import ALLOWED_INTENTS, IntentResult


def validate_intent_payload(payload: object) -> IntentResult:
    if not isinstance(payload, dict) or set(payload) - {"intent", "confidence"}:
        raise ValueError("intent payload must only contain intent and confidence")
    intent, confidence = payload.get("intent"), payload.get("confidence")
    if not isinstance(intent, str) or intent not in ALLOWED_INTENTS:
        raise ValueError("intent is outside the allowlist")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= float(confidence) <= 1:
        raise ValueError("confidence must be between 0 and 1")
    return IntentResult(intent=intent, confidence=float(confidence), source="ollama")


class MockIntentClassifier:
    """Deterministic local classifier; it never produces a reply."""
    RULES = [
        ("pain_or_discomfort", r"болит|больно|страшно|плохо себя чувств"),
        ("want_exit", r"пока|до свидания|законч|выйти|стоп"),
        ("want_game", r"игр"),
        ("want_story", r"сказк|истори"),
        ("repeat", r"повтори|ещ[её] раз"),
        ("mood_positive", r"хорош|отличн|весел|рад"),
        ("mood_negative", r"груст|плох|не очень|устал"),
        ("mood_neutral", r"нормальн|обычн|так себе"),
        ("greeting", r"привет|здравств"),
        ("question", r"\?"),
    ]

    def classify(self, text: str) -> IntentResult:
        normalized = text.lower().strip()
        for intent, pattern in self.RULES:
            if re.search(pattern, normalized):
                return IntentResult(intent, 0.95, "mock_rule")
        return IntentResult("unknown", 0.2, "mock_rule", "no rule matched")


class OllamaIntentClassifier:
    """Optional constrained classifier with explicit fail-closed provider errors."""
    def __init__(self, url: str = "http://127.0.0.1:11434/api/chat", model: str = "qwen3:14b", timeout_s: float = 2.0, retries: int = 1, fallback=None):
        self.url, self.model, self.timeout_s, self.retries = url, model, timeout_s, retries
        self.fallback = fallback

    def classify(self, text: str) -> IntentResult:
        prompt = ("Classify Russian child input. Return JSON only with intent and confidence. "
                  f"Allowed intents: {', '.join(sorted(ALLOWED_INTENTS))}. Input: {text!r}")
        payload = {"model": self.model, "stream": False, "format": "json", "messages": [{"role": "user", "content": prompt}]}
        for attempt in range(self.retries + 1):
            try:
                request = Request(self.url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
                with urlopen(request, timeout=self.timeout_s) as response:
                    raw = json.loads(response.read().decode())
                content = raw["message"]["content"]
                return validate_intent_payload(json.loads(content))
            except (URLError, TimeoutError, ValueError, KeyError, json.JSONDecodeError) as exc:
                if attempt == self.retries:
                    if self.fallback is not None:
                        result = self.fallback.classify(text)
                        return IntentResult(result.intent, result.confidence, result.source, f"explicit fallback: {type(exc).__name__}")
                    return IntentResult("unknown", 0.0, "ollama_error", type(exc).__name__)
                # Retry immediately; this synchronous legacy adapter is not an
                # async production path and must not add an arbitrary sleep.
        return IntentResult("unknown", 0.0, "ollama_error", "unreachable")
