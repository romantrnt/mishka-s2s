from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
import sys
import resource
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

# This harness intentionally uses deterministic provider doubles for stress and
# cleanup. It exercises production queue/session ownership, not model quality.
os.environ.setdefault("MISHKA_PROFILE", "mock")
os.environ.setdefault("VALIDATION_PROVIDER", "mock")

from app.foundation.adapters import RecordingAudio, RecordingLED, RuleBasedIntentClassifier
from app.foundation.application import ScenarioApplication
from app.foundation.domain import Lifecycle, SessionRecord
from app.foundation.observability import StructuredEventPublisher
from app.foundation.lifecycle import StartupLifecycle
from app.foundation.runtime import S2SRuntimeManager
from app.foundation.repositories import InMemoryHistoryRepository, InMemorySessionRepository
from app.foundation.s2s import (
    BoundedConversationEngine, ConversationContext, MockLLMProvider, MockTTSProvider,
    PassthroughVoiceProvider, S2SConfig, AudioChunk,
)


class ScenarioRepo:
    def get(self, _name):
        return {}

    def game(self, _name):
        return None

    def story(self, _name):
        return None


class SlowTTS(MockTTSProvider):
    async def synthesize(self, text, token, cancelled):
        await asyncio.sleep(5)
        return await super().synthesize(text, token, cancelled)


class FailingTTS(MockTTSProvider):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def synthesize(self, text, token, cancelled):
        self.calls += 1
        if self.calls >= 2:
            raise RuntimeError("simulated_midstream_tts_failure")
        return await super().synthesize(text, token, cancelled)


class BlockingPlayback:
    def __init__(self):
        self.active_token = 0
        self.started = asyncio.Event()

    async def play(self, _audio):
        self.started.set()
        await asyncio.Future()

    async def stop(self):
        return None


class Clock:
    value = 100.0

    def now(self):
        return self.value


def make_engine(*, llm=None, tts=None, playback=None):
    config = replace(
        S2SConfig(), queue_size=2, max_tts_chunks_per_generation=8,
        max_tts_queue_bytes=32 * 1024, max_history_chars=256,
        max_history_entries=10, max_response_chars=320, max_chunk_chars=32,
        llm_timeout_s=2, tts_timeout_s=2, playback_timeout_s=2,
    )
    return BoundedConversationEngine(
        llm or MockLLMProvider(["<say>Короткий ответ.</say>"]),
        tts or MockTTSProvider(), PassthroughVoiceProvider(),
        playback or _ImmediatePlayback(), config,
    )


class _ImmediatePlayback:
    active_token = 0

    async def play(self, _audio):
        return None

    async def stop(self):
        return None


def context(session_id, *, max_turns=200):
    return ConversationContext(session_id, "dev_core", "dev_dialogue", ("__all__",), (), max_turns, 600, 8)


def snapshot(engine, ctx=None):
    result = dict(engine.resource_snapshot())
    try:
        rss = {"value": int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024}
    except (AttributeError, OSError, ValueError):
        rss = {"value": None, "reason": "resource_unavailable"}
    try:
        import torch
        if torch.cuda.is_available():
            gpu = {"allocated_bytes": {"value": int(torch.cuda.memory_allocated())}, "reserved_bytes": {"value": int(torch.cuda.memory_reserved())}}
        else:
            gpu = {"allocated_bytes": {"value": None, "reason": "cuda_unavailable"}}
    except Exception:
        gpu = {"allocated_bytes": {"value": None, "reason": "cuda_unavailable"}}
    result["memory"] = {"rss_bytes": rss, "gpu": gpu}
    if ctx is not None:
        result["history_chars"] = sum(len(a) + len(b) for a, b in ctx.history)
        result["history_entries"] = len(ctx.history)
    return result


def assert_clean(after, label):
    for key in ("active_sessions", "active_tasks", "queued_chunks", "pcm_bytes", "registry_size"):
        assert after.get(key, 0) == 0, (label, key, after)


async def sequential_turns():
    engine = make_engine()
    ctx = context("turns")
    before = snapshot(engine, ctx)
    for _ in range(50):
        result = await engine.run_text_turn("скажи короткий ответ", ctx)
        assert result.fallback_reason is None, result
    after = snapshot(engine, ctx)
    assert after["history_chars"] <= 256
    assert_clean(after, "50_turns")
    return {"scenario": "50_sequential_turns", "before": before, "after": after,
            "outcome": "ok", "history_bounded": True}


async def barge_ins():
    engine = make_engine(tts=SlowTTS())
    before = snapshot(engine)
    for index in range(10):
        task = asyncio.create_task(engine.run_text_turn(f"barge {index}", context(f"barge-{index}")))
        await asyncio.sleep(0.02)
        await engine.cancel_session(f"barge-{index}")
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert_clean(snapshot(engine), f"barge_{index}")
    after = snapshot(engine)
    return {"scenario": "10_barge_in", "before": before, "after": after,
            "outcome": "ok", "stale_generations": 0}


async def tts_failure():
    llm = MockLLMProvider(["<say>Первая фраза. ", "Вторая фраза. ", "Третья фраза.</say>"])
    engine = make_engine(llm=llm, tts=FailingTTS())
    before = snapshot(engine)
    result = await engine.run_text_turn("длинный ответ", context("tts-failure"))
    after = snapshot(engine)
    assert result.fallback_reason in {"provider_or_validation_failure", "tts_queue_overflow"}
    assert_clean(after, "tts_failure")
    return {"scenario": "midstream_tts_failure", "before": before, "after": after,
            "outcome": result.fallback_reason}


async def disconnect_playback():
    playback = BlockingPlayback()
    engine = make_engine(playback=playback)
    task = asyncio.create_task(engine.run_text_turn("ответ", context("disconnect")))
    await asyncio.wait_for(playback.started.wait(), 2)
    before = snapshot(engine)
    await engine.cancel_session("disconnect")
    with contextlib.suppress(asyncio.CancelledError):
        await task
    after = snapshot(engine)
    assert_clean(after, "disconnect")
    return {"scenario": "disconnect_during_playback", "before": before, "after": after,
            "outcome": "ok"}


async def long_response():
    tokens = ["<say>" + ("длинный ответ. " * 30) + "</say>"]
    engine = make_engine(llm=MockLLMProvider(tokens))
    ctx = context("long-response")
    before = snapshot(engine, ctx)
    result = await engine.run_text_turn("длинный запрос", ctx)
    after = snapshot(engine, ctx)
    assert_clean(after, "long_response")
    assert after["history_chars"] <= 256
    return {"scenario": "bounded_long_response", "before": before, "after": after,
            "outcome": result.fallback_reason or "completed", "history_bounded": True}


async def idle_ttl():
    clock = Clock()
    engine = make_engine()
    sessions = InMemorySessionRepository()
    app = ScenarioApplication(
        sessions, ScenarioRepo(), lambda _tag: None, RuleBasedIntentClassifier(),
        RecordingAudio(), RecordingLED(), StructuredEventPublisher(), now=clock.now,
        bounded_s2s=engine, history=InMemoryHistoryRepository(),
        max_session_seconds=120, idle_timeout_seconds=5,
    )
    app.start_session("idle")
    session = sessions.get("idle")
    session.state = Lifecycle.ACTIVE
    session.current_node = "dev_dialogue"
    session.scenario_id = "dev_core"
    app.conversation_contexts["idle"] = context("idle")
    app.conversation_contexts["idle"].history = [("old", "context")]
    sessions.save(session)
    before = app.resource_snapshot()
    before["memory"] = snapshot(engine)["memory"]
    clock.value = 106
    expired = await app.expire_idle_sessions()
    after = app.resource_snapshot()
    after["memory"] = snapshot(engine)["memory"]
    assert expired and expired[0]["reason"] == "idle_timeout"
    assert sessions.get("idle").state == Lifecycle.ENDED
    assert after["session_contexts"] == 0 and after["generation_registry"] == 0
    again = await app.expire_idle_sessions()
    assert again == []
    return {"scenario": "idle_ttl", "before": before, "after": after,
            "outcome": "ok", "termination_reason": sessions.get("idle").termination_reason,
            "repeat_cleanup": "idempotent"}


async def dependency_failure_cleanup():
    engine = make_engine(tts=SlowTTS())
    task = asyncio.create_task(engine.run_text_turn("dependency failure", context("recovery")))
    await asyncio.sleep(0.02)
    before = snapshot(engine)
    cleanup = await engine.cancel_all_sessions()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    after = snapshot(engine)
    assert_clean(after, "dependency_failure")
    settings = SimpleNamespace(profile="production_candidate")
    manager = S2SRuntimeManager(settings, object(), object(), object(), make_engine())
    async def insufficient_resources():
        return {"ready": False, "reason": "simulated_memory_pressure", "available_vram_gb": 0.1}
    manager.resource_readiness = insufficient_resources
    lifecycle = StartupLifecycle()
    recovery = await manager.recover(lifecycle, "simulated_ollama_restart", {"memory_pressure": True})
    assert recovery["reason"] == "resource_capacity_insufficient"
    assert lifecycle.snapshot()["state"] == "DEGRADED"
    return {"scenario": "simulated_ollama_restart_or_memory_pressure", "before": before,
            "after": after, "cleanup": cleanup, "recovery": recovery,
            "lifecycle": lifecycle.snapshot(), "outcome": "degraded_fail_closed",
            "production_dependency_call": False}


async def run():
    started = time.monotonic()
    reports = []
    for scenario in (sequential_turns, barge_ins, tts_failure, disconnect_playback,
                     long_response, idle_ttl, dependency_failure_cleanup):
        case_started = time.monotonic()
        report = await scenario()
        report["duration_ms"] = round((time.monotonic() - case_started) * 1000, 2)
        reports.append(report)
    return {"harness": "long_session_acceptance", "provider_mode": "simulated_deterministic",
            "acceptance_type": "simulation", "environment": "disposable_test",
            "real_dependencies": False, "hardware_level": "none",
            "readiness_evidence": "not_production_acceptance",
            "ok": True, "duration_ms": round((time.monotonic() - started) * 1000, 2),
            "scenarios": reports,
            "physical_runtime_required": ["real_ollama_restart", "real_memory_pressure", "hardware_playback"]}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=2))
