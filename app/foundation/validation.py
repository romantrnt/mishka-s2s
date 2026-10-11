"""Asynchronous safety validation for streamed child-facing text.

The node is deliberately advisory about wording and fail-closed about uncertainty:
regex blocks known hazards immediately; a tiny local classifier only returns a
structured decision and never writes text that can be spoken to a child.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Protocol


@dataclass(frozen=True)
class ValidationDecision:
    action: str
    allowed: bool
    reason: str
    confidence: float
    source: str
    latency_ms: float = 0.0
    generation_id: int | None = None

    @property
    def blocked(self) -> bool:
        return not self.allowed


class MiniLLMValidator(Protocol):
    async def validate(self, text: str, context: tuple[str, ...] = ()) -> ValidationDecision:
        ...


class RegexSafetyFilter:
    """Fast deterministic filter; never generates or rewrites a response."""

    _rules = (
        ("medical_advice", re.compile(r"(\u0434\u0438\u0430\u0433\u043d\u043e\u0437|\u043b\u0435\u043a\u0430\u0440\u0441\u0442\u0432|\u0442\u0430\u0431\u043b\u0435\u0442|\u043b\u0435\u0447\u0438\u0442\u044c|\u043f\u0440\u0438\u043d\u0438\u043c\u0430\u0439.*\u043b\u0435\u043a\u0430\u0440\u0441\u0442\u0432|\u043a\u0430\u043a.*\u043b\u0435\u0447\u0438\u0442\u044c|\bdiagnos\w*\b|\bmedicin\w*\b|\bprescrib\w*\b|\btake.*pill\b)", re.I)),
        ("self_harm_or_violence", re.compile(r"(\u0441\u0443\u0438\u0446\u0438\u0434|\u0443\u0431\u0438\u0442\u044c|\u0437\u0430\u0440\u0435\u0437\u0430\u0442\u044c|\u043e\u0440\u0443\u0436\u0438\u0435|\u0432\u0437\u043e\u0440\u0432\u0430\u0442\u044c|\bkill\b|\bsuicide\b|\bweapon\b|\bshoot\b|\bbomb\b)", re.I)),
        ("sexual_content", re.compile(r"(\u043f\u043e\u0440\u043d\u043e|\u0441\u0435\u043a\u0441|\u043d\u0430\u0433\u043e\u0439|\bsex\b|\bporno\b|\bnude\b)", re.I)),
        ("personal_data", re.compile(r"(\u043f\u0430\u0440\u043e\u043b\u044c|\u043a\u043e\u0434.*\u0434\u043e\u0441\u0442\u0443\u043f|\u0430\u0434\u0440\u0435\u0441|\u0442\u0435\u043b\u0435\u0444\u043e\u043d|\u043a\u043e\u043e\u0440\u0434\u0438\u043d\u0430\u0442|\bpassword\b|\baddress\b|\bphone number\b)", re.I)),
    )

    def check(self, text: str) -> tuple[bool, str]:
        normalized = " ".join(str(text or "").split())
        if not normalized:
            return False, "empty_text"
        for reason, pattern in self._rules:
            if pattern.search(normalized):
                return False, reason
        return True, "regex_allow"


class MockMiniLLMValidator:
    """Deterministic offline classifier used by mock and tests."""

    def __init__(self, delay_s: float = 0.0):
        self.delay_s = delay_s

    async def validate(self, text: str, context: tuple[str, ...] = ()) -> ValidationDecision:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        lowered = str(text).lower()
        suspicious = ("\u0441\u0434\u0435\u043b\u0430\u0439 \u043c\u043d\u0435", "\u043d\u0430\u0440\u0443\u0448\u044c", "ignore safety", "jailbreak")
        if any(term in lowered for term in suspicious):
            return ValidationDecision("block", False, "mini_llm_suspicious", 0.98, "mock_mini_llm")
        if len(text) > 320:
            return ValidationDecision("review", False, "mini_llm_uncertain_length", 0.42, "mock_mini_llm")
        return ValidationDecision("allow", True, "mini_llm_allow", 0.94, "mock_mini_llm")


class OllamaMiniLLMValidator:
    """Structured-only Ollama adapter. It cannot emit child-facing text."""

    def __init__(self, base_url: str = "http://127.0.0.1:11434", model: str = "qwen3:0.6b", timeout_s: float = 0.12, fail_closed: bool = True, num_ctx: int = 1024, keep_alive: str = "-1"):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s
        self.fail_closed = fail_closed
        self.num_ctx = int(num_ctx)
        self.keep_alive = keep_alive

    def _request(self, payload: bytes) -> bytes:
        request = urllib.request.Request(self.base_url + "/api/chat", data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
            return response.read()

    async def validate(self, text: str, context: tuple[str, ...] = ()) -> ValidationDecision:
        started = time.perf_counter()
        prompt = ("You are a safety gate. Return exactly one JSON object and nothing else with keys "
                  "decision, confidence, reason. decision must be allow, block, or review. "
                  "Use allow for ordinary harmless child-safe conversation such as greetings, stories, "
                  "games, weather, and questions. Use block only for explicit violence, sexual content, "
                  "self-harm, medical instructions, or requests for personal secrets. Use review only "
                  "when genuinely uncertain. Never rewrite the candidate. Candidate response: " + text[:1200])
        keep_alive = self.keep_alive.strip()
        if keep_alive.lstrip("-").isdigit():
            keep_alive = int(keep_alive)
        payload = json.dumps({"model": self.model, "stream": False, "think": False, "format": "json",
                              "keep_alive": keep_alive,
                              "messages": [{"role": "user", "content": prompt}],
                              "options": {"temperature": 0, "num_predict": 96, "num_ctx": self.num_ctx}}, ensure_ascii=False).encode("utf-8")
        try:
            raw = await asyncio.wait_for(asyncio.to_thread(self._request, payload), timeout=self.timeout_s + 0.03)
            outer = json.loads(raw.decode("utf-8"))
            content = outer.get("message", {}).get("content", "")
            match = re.search(r"\{.*\}", content, re.S)
            parsed = json.loads(match.group(0)) if match else {}
            action = str(parsed.get("decision", "review")).lower()
            confidence = float(parsed.get("confidence", 0.0))
            allowed = action == "allow" and confidence >= 0.75
            if action not in {"allow", "block", "review"}:
                action = "review"
            return ValidationDecision(action, allowed, str(parsed.get("reason", "mini_llm_decision")), max(0.0, min(1.0, confidence)), "ollama_mini_llm", (time.perf_counter() - started) * 1000)
        except Exception as exc:
            return ValidationDecision("block" if self.fail_closed else "review", False, "mini_llm_error:" + type(exc).__name__, 0.0, "ollama_mini_llm", (time.perf_counter() - started) * 1000)


@dataclass
class ValidationMetrics:
    regex_blocks: int = 0
    mini_calls: int = 0
    mini_blocks: int = 0
    mini_errors: int = 0
    allowed: int = 0
    blocked: int = 0
    total_ms: float = 0.0

    def snapshot(self) -> dict[str, Any]:
        return {"regex_blocks": self.regex_blocks, "mini_calls": self.mini_calls, "mini_blocks": self.mini_blocks, "mini_errors": self.mini_errors, "allowed": self.allowed, "blocked": self.blocked, "avg_ms": round(self.total_ms / max(1, self.mini_calls + self.regex_blocks + self.allowed), 3)}


class AsyncResponseValidator:
    """Regex -> optional mini-LLM gate, always on a worker-friendly async path."""

    def __init__(self, mini: MiniLLMValidator | None = None, timeout_ms: int = 120, confidence_threshold: float = 0.75, max_context: int = 6, fail_closed: bool = True):
        self.regex = RegexSafetyFilter()
        self.mini = mini or MockMiniLLMValidator()
        self.timeout_s = max(0.01, timeout_ms / 1000.0)
        self.confidence_threshold = confidence_threshold
        self.max_context = max_context
        self.fail_closed = fail_closed
        self.metrics = ValidationMetrics()

    def _needs_mini(self, text: str) -> bool:
        normalized = " ".join(text.split())
        return len(normalized) > 80 or bool(re.search(r"(\u043f\u043e\u0447\u0435\u043c\u0443|\u0437\u0430\u0447\u0435\u043c|\u043a\u0430\u043a|\u0447\u0442\u043e \u0442\u0430\u043a\u043e\u0435|why|how|what is)", normalized, re.I))

    async def validate_trusted(self, text: str, generation_id: int | None = None) -> ValidationDecision:
        """Validate application-generated text without a second model hop.

        The caller must have already validated the structured source contract
        (for example schedule/weather).  The async boundary is preserved, but
        deterministic text does not incur an avoidable MiniLLM round trip.
        """
        started = time.perf_counter()
        allowed, reason = self.regex.check(text)
        if not allowed:
            self.metrics.regex_blocks += 1
            self.metrics.blocked += 1
            return ValidationDecision("block", False, reason, 1.0, "regex_trusted", (time.perf_counter() - started) * 1000, generation_id)
        self.metrics.allowed += 1
        latency = (time.perf_counter() - started) * 1000
        self.metrics.total_ms += latency
        return ValidationDecision("allow", True, "trusted_application_text", 1.0, "regex_trusted", latency, generation_id)

    async def validate(self, text: str, context: Iterable[str] = (), generation_id: int | None = None) -> ValidationDecision:
        started = time.perf_counter()
        allowed, reason = self.regex.check(text)
        if not allowed:
            self.metrics.regex_blocks += 1
            self.metrics.blocked += 1
            return ValidationDecision("block", False, reason, 1.0, "regex", (time.perf_counter() - started) * 1000, generation_id)
        if not self._needs_mini(text):
            self.metrics.allowed += 1
            self.metrics.total_ms += (time.perf_counter() - started) * 1000
            return ValidationDecision("allow", True, reason, 1.0, "regex_fast_path", (time.perf_counter() - started) * 1000, generation_id)
        self.metrics.mini_calls += 1
        try:
            decision = await asyncio.wait_for(self.mini.validate(text, tuple(context)[-self.max_context:]), timeout=self.timeout_s)
        except asyncio.TimeoutError:
            self.metrics.mini_errors += 1
            decision = ValidationDecision("block" if self.fail_closed else "review", False, "mini_llm_timeout", 0.0, "validator_timeout", latency_ms=(time.perf_counter() - started) * 1000)
        except Exception as exc:
            self.metrics.mini_errors += 1
            decision = ValidationDecision("block" if self.fail_closed else "review", False, "mini_llm_error:" + type(exc).__name__, 0.0, "validator_error")
        confidence_ok = decision.confidence >= self.confidence_threshold
        final_allowed = decision.action == "allow" and decision.allowed and confidence_ok
        result = ValidationDecision("allow" if final_allowed else "block", final_allowed, decision.reason if final_allowed else decision.reason, decision.confidence, decision.source, (time.perf_counter() - started) * 1000, generation_id)
        self.metrics.total_ms += result.latency_ms
        if final_allowed:
            self.metrics.allowed += 1
        else:
            self.metrics.mini_blocks += 1
            self.metrics.blocked += 1
        return result


class GenerationGuard:
    """Monotonic generation token preventing stale audio after barge-in/reset."""

    def __init__(self):
        self._generation = 0
        self._tasks: set[asyncio.Task[Any]] = set()
        self._lock = asyncio.Lock()

    async def start(self) -> int:
        async with self._lock:
            self._generation += 1
            return self._generation

    async def cancel(self) -> int:
        async with self._lock:
            self._generation += 1
            tasks, self._tasks = self._tasks, set()
        for task in tasks:
            if not task.done():
                task.cancel()
        return self._generation

    def current(self, generation_id: int) -> bool:
        return generation_id == self._generation

    def register(self, task: asyncio.Task[Any]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def clear_queue(queue: asyncio.Queue[Any]) -> int:
        count = 0
        while True:
            try:
                queue.get_nowait()
                queue.task_done()
                count += 1
            except asyncio.QueueEmpty:
                return count


async def validate_chunks(chunks: Iterable[str], validator: AsyncResponseValidator, context: Iterable[str] = (), generation_id: int | None = None) -> list[ValidationDecision]:
    """Validate chunks concurrently while preserving input order."""
    values = list(chunks)
    return await asyncio.gather(*(validator.validate(value, context, generation_id) for value in values))
