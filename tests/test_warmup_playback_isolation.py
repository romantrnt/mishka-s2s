"""Playback ownership regressions; local objects only, no model/device calls."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.foundation import api
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.e2e_probe import ProductionE2EWarmupProbe, _SilentWarmupPlayback
from app.foundation.input_safety import InputSafetyDecision
from app.foundation.lifecycle import StartupLifecycle
from app.foundation.s2s import (
    AudioSynthesisQueue, BoundedConversationEngine, ConversationContext,
    EnergyVADAdapter, LocalS2SPipeline, MockLLMProvider, MockPlaybackController,
    MockSpeechRecognizer, MockTTSProvider, RVCProvider,
)


class StopBeforeProviders(RuntimeError):
    pass


def engine():
    return BoundedConversationEngine(
        MockLLMProvider(), MockTTSProvider(), RVCProvider(), MockPlaybackController(),
        settings=Settings(profile="mock"),
    )


def probe_for(value, monkeypatch):
    pipeline = LocalS2SPipeline(EnergyVADAdapter(), MockSpeechRecognizer(), value)
    manager = SimpleNamespace(
        engine=value,
        settings=SimpleNamespace(profile="local_cpu", e2e_stage_timeout_s=1,
                                 e2e_warmup_timeout_s=2),
    )
    probe = ProductionE2EWarmupProbe(manager, pipeline)
    monkeypatch.setattr(probe, "_read_pcm", lambda *_: (.1,) * 1024)
    return probe, pipeline


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", [False, True], ids=["run_now", "recover"])
@pytest.mark.parametrize("cancel_resistant", [False, True], ids=["ordinary", "cancel_resistant"])
async def test_preadmitted_classifier_cannot_capture_probe_sink(monkeypatch, recovery, cancel_resistant):
    monkeypatch.setattr(api, "_prepare_runtime_dirs", lambda _: None)
    app, *_ = api.build_application(settings=Settings(profile="mock"), background_scheduler=False)
    value, manager = app.bounded_s2s, app.runtime_manager
    playback = value.playback
    lifecycle = StartupLifecycle()

    async def ready():
        return {"ok": True, "e2e": {"ok": True}}

    await lifecycle.run_now(ready)
    sid = "synthetic-warmup-overlap"
    event = lambda kind, seq: Event(kind, sid, seq, 1., "phone", {}, f"fixture:{seq}")
    await app.receive_async(event("HandsetLifted", 1))
    app.canonical_runtime.sessions[sid].mode = "bear_dialogue"
    epoch = app._session_epochs.get(sid, 0)
    entered, release, cancellation_seen = asyncio.Event(), asyncio.Event(), asyncio.Event()
    probe_entered, probe_release = asyncio.Event(), asyncio.Event()
    queues = []

    class Classifier:
        async def classify(self, *_args, **_kwargs):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await release.wait()
            return InputSafetyDecision("none", "synthetic")

    class Ready:
        def readiness(self):
            return {"ready": True}

    async def feed(*_args, **_kwargs):
        probe_entered.set()
        await probe_release.wait()
        return SimpleNamespace(pending=None)

    async def resources():
        return {"ready": True}

    async def capture_queue(queue, *_args):
        queues.append(queue)
        raise StopBeforeProviders()

    app.safety_classifier = Classifier()
    manager.settings = replace(manager.settings, profile="local_cpu")
    for name in ("vad", "stt", "llm", "tts", "playback"):
        monkeypatch.setattr(manager, name, Ready())
    monkeypatch.setattr(manager, "resource_readiness", resources)
    monkeypatch.setattr(app.s2s_audio, "feed_audio", feed)
    probe = ProductionE2EWarmupProbe(manager, app.s2s_audio)
    monkeypatch.setattr(probe, "_read_pcm", lambda *_: (.1,) * 1024)

    async def warmup():
        report = await probe.run()
        return {"ok": report["ok"], "e2e": report}

    monkeypatch.setattr(manager, "warmup", warmup)
    monkeypatch.setattr(AudioSynthesisQueue, "start", capture_queue)
    user = asyncio.create_task(app.receive_async(event("SpeechReceived", 2), "Расскажи о дружбе"))
    maintenance = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert not value.active_tasks and not value.active_queues and not app.s2s_audio.pending_tasks
        assert not app._session_has_active_work(sid)
        assert sum(app._engagement_ingress.values()) == 1
        if cancel_resistant:
            user.cancel()
            await asyncio.wait_for(cancellation_seen.wait(), 2)
            assert not user.done()
        maintenance = asyncio.create_task(
            manager.recover(lifecycle, trigger="synthetic") if recovery
            else lifecycle.run_now(manager.warmup))
        await asyncio.wait_for(probe_entered.wait(), 2)
        assert not lifecycle.admits_users()
        release.set()
        await asyncio.wait_for(user, 2)
        assert len(queues) == 1
        assert queues[0].playback is playback
        assert value.playback is playback
        assert app._session_epochs.get(sid, 0) == epoch
        stops_before_probe_cleanup = playback.stops
        probe_release.set()
        await asyncio.wait_for(maintenance, 2)
        assert playback.stops == stops_before_probe_cleanup
        assert value.playback is playback
    finally:
        release.set()
        probe_release.set()
        await asyncio.gather(user, *([maintenance] if maintenance else []), return_exceptions=True)
        await app._cleanup_session_resources(sid)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["mock", "pcm", "empty", "pipeline", "timeout", "cleanup"])
async def test_probe_failure_cleanup_never_stops_default_playback(monkeypatch, failure):
    value = engine()
    playback = value.playback
    probe, pipeline = probe_for(value, monkeypatch)
    cleanup_sinks = []
    original_cancel = pipeline.cancel_session

    async def cleanup(sid):
        cleanup_sinks.append(value.playback)
        await original_cancel(sid)
        if failure == "cleanup":
            raise RuntimeError("synthetic cleanup error")

    async def feed(*_args, **_kwargs):
        if failure == "pipeline":
            raise RuntimeError("synthetic pipeline error")
        if failure == "timeout":
            await asyncio.Event().wait()
        return SimpleNamespace(pending=None)

    def missing_pcm(*_args):
        raise FileNotFoundError("synthetic missing PCM")

    monkeypatch.setattr(pipeline, "cancel_session", cleanup)
    monkeypatch.setattr(pipeline, "feed_audio", feed)
    if failure == "mock":
        probe.manager.settings.profile = "mock"
    elif failure == "pcm":
        monkeypatch.setattr(probe, "_read_pcm", missing_pcm)
    elif failure == "timeout":
        probe.manager.settings.e2e_warmup_timeout_s = .01
    if failure == "cleanup":
        with pytest.raises(RuntimeError, match="synthetic cleanup error"):
            await probe.run()
    else:
        report = await probe.run()
        expected = {"mock": "mock_profile_not_allowed", "pcm": "pcm_input_failed",
                    "empty": "vad_no_speech_end", "pipeline": "e2e_pipeline_failure",
                    "timeout": "e2e_overall_timeout"}
        assert report["reason"] == expected[failure]
    assert len(cleanup_sinks) == 1 and isinstance(cleanup_sinks[0], _SilentWarmupPlayback)
    assert playback.stops == 0
    assert value.playback is playback


@pytest.mark.asyncio
async def test_real_pipeline_probe_queue_is_silent_and_next_turn_uses_default(monkeypatch):
    value = engine()
    playback = value.playback
    probe, _ = probe_for(value, monkeypatch)
    monkeypatch.setattr(probe, "_read_pcm", lambda *_: (.2,) * 16000)
    queues = []

    async def capture_queue(queue, *_args):
        queues.append(queue)
        raise StopBeforeProviders()

    monkeypatch.setattr(AudioSynthesisQueue, "start", capture_queue)
    report = await probe.run()
    assert report["reason"] == "e2e_pipeline_failure"
    assert len(queues) == 1 and isinstance(queues[0].playback, _SilentWarmupPlayback)
    assert playback.stops == 0
    assert value.playback is playback
    context = ConversationContext("next-user", "canonical", "bear_dialogue", ("__all__",), (), 2, 120, 2)
    with pytest.raises(StopBeforeProviders):
        await value.run_text_turn("Расскажи о дружбе", context)
    assert len(queues) == 2 and queues[1].playback is playback


def test_nested_scopes_preserve_each_engine_and_default_assignments():
    first, second = engine(), engine()
    first_default, second_default = first.playback, second.playback
    outer, inner, other = object(), object(), object()
    replacement, other_replacement = object(), object()
    with first.scoped_playback(outer):
        assert first.playback is outer and second.playback is second_default
        with second.scoped_playback(other):
            assert first.playback is outer and second.playback is other
            with first.scoped_playback(inner):
                first.playback = replacement
                second.playback = other_replacement
                assert first.playback is inner and second.playback is other
            assert first.playback is outer and second.playback is other
        assert first.playback is outer and second.playback is other_replacement
        with pytest.raises(RuntimeError, match="synthetic scope error"):
            with first.scoped_playback(inner):
                raise RuntimeError("synthetic scope error")
        assert first.playback is outer
    assert first.playback is replacement and first.playback is not first_default
    assert second.playback is other_replacement


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["timeout", "cancel"])
async def test_late_cancel_resistant_probe_child_keeps_its_sink(monkeypatch, ending):
    value, other = engine(), engine()
    playback, other_playback = value.playback, other.playback
    probe, pipeline = probe_for(value, monkeypatch)
    probe.manager.settings.e2e_warmup_timeout_s = .05
    entered, release, child_cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    children, queues, sinks = [], [], []

    async def capture_queue(queue, *_args):
        queues.append(queue)
        raise StopBeforeProviders()

    async def child():
        try:
            await release.wait()
        except asyncio.CancelledError:
            child_cancelled.set()
            await release.wait()
        context = ConversationContext("late-probe-child", "canonical", "warmup_speech", ("__all__",), (), 2, 120, 2)
        with pytest.raises(StopBeforeProviders):
            await value.run_text_turn("Расскажи о дружбе", context)
        await value.cancel_session(context.session_id)
        assert other.playback is other_playback
        return value.playback

    async def feed(*_args, **_kwargs):
        sinks.append(value.playback)
        children.append(asyncio.create_task(child()))
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(pipeline, "feed_audio", feed)
    monkeypatch.setattr(AudioSynthesisQueue, "start", capture_queue)
    parent = asyncio.create_task(probe.run())
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if ending == "cancel":
            parent.cancel()
            with pytest.raises(asyncio.CancelledError):
                await parent
        else:
            assert (await parent)["reason"] == "e2e_overall_timeout"
        assert value.playback is playback and other.playback is other_playback
        assert not children[0].done()
        children[0].cancel()
        await asyncio.wait_for(child_cancelled.wait(), 2)
        release.set()
        child_sink = await asyncio.wait_for(children[0], 2)
        assert isinstance(child_sink, _SilentWarmupPlayback)
        assert child_sink is sinks[0] is queues[0].playback
        assert playback.stops == 0 and other_playback.stops == 0
        assert value.playback is playback
    finally:
        release.set()
        if not parent.done():
            parent.cancel()
        await asyncio.gather(parent, *children, return_exceptions=True)


@pytest.mark.asyncio
async def test_successful_probe_uses_same_pipeline_queue_then_normal_turn_is_audible(monkeypatch):
    value = engine()
    playback = value.playback
    probe, pipeline = probe_for(value, monkeypatch)
    monkeypatch.setattr(probe, "_read_pcm", lambda *_: (.2,) * 16000)
    report = await probe.run()
    assert report["ok"], report
    assert pipeline.conversation is value
    assert report["trace"]["stage_durations_ms"]["playback"] >= 0
    assert playback.played == [] and playback.stops == 0
    assert not value.active_tasks and not value.active_queues and not pipeline.pending_tasks
    assert value.playback is playback
    context = ConversationContext("next-user", "canonical", "bear_dialogue", ("__all__",), (), 2, 120, 2)
    turn = await value.run_text_turn("Расскажи о дружбе", context)
    assert turn.fallback_reason is None
    assert len(playback.played) == 1
