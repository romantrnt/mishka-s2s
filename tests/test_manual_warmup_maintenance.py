"""Maintenance admission uses local app/ASGI objects, never real providers."""
import asyncio
import copy
import threading
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from app.foundation import api
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.input_safety import InputSafetyDecision
from app.foundation.lifecycle import RuntimeBusyError
from app.foundation.s2s import AudioChunk, FasterWhisperRecognizer, S2SConfig


async def application(monkeypatch):
    monkeypatch.setattr(api, "_prepare_runtime_dirs", lambda _: None)
    app, *_ = api.build_application(settings=Settings(profile="mock"), background_scheduler=False)
    web = api.create_app(app)

    async def ready():
        return {"ok": True, "e2e": {"ok": True}}

    await web.state.lifecycle.run_now(ready)
    return app, web


def event(kind, seq=1, sid="synthetic-maintenance"):
    return Event(kind, sid, seq, 1., "fixture", {}, f"fixture:{seq}")


async def post(web):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web), base_url="http://local.test") as client:
        return await client.post("/internal/s2s/warmup")


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", ["classifier", "paused", "native"])
async def test_real_route_rejects_busy_before_lifecycle_or_session_change(monkeypatch, busy):
    app, web = await application(monkeypatch)
    calls = []
    release, entered = asyncio.Event(), asyncio.Event()
    thread_release, thread_entered = threading.Event(), threading.Event()
    task = None
    recognizer = None

    async def warmup():
        calls.append(True)
        return {"ok": True, "e2e": {"ok": True}}

    monkeypatch.setattr(app.runtime_manager, "warmup", warmup)
    if busy != "native":
        await app.receive_async(event("HandsetLifted"))
    if busy == "classifier":
        class Classifier:
            async def classify(self, *_args, **_kwargs):
                entered.set()
                await release.wait()
                return InputSafetyDecision("none", "synthetic")
        app.safety_classifier = Classifier()
        task = asyncio.create_task(app.receive_async(event("SpeechReceived", 2), "Нейтральная реплика"))
        await asyncio.wait_for(entered.wait(), 2)
    elif busy == "paused":
        app.canonical_runtime.sessions["synthetic-maintenance"].mode = "story_paused"
        app._active_audio.clear()
    else:
        class NativeModel:
            def transcribe(self, *_args, **_kwargs):
                thread_entered.set()
                assert thread_release.wait(5)
                return ([SimpleNamespace(text="Тест", no_speech_prob=0., avg_logprob=0.)], None)
        recognizer = FasterWhisperRecognizer(lambda: None, S2SConfig())
        recognizer._model = NativeModel()
        app.runtime_manager.stt = recognizer
        task = asyncio.create_task(recognizer.transcribe(AudioChunk((.1,) * 1600, 16000, 1), lambda: False))
        assert await asyncio.to_thread(thread_entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not thread_release.is_set()
    before_lifecycle = web.state.lifecycle.snapshot()
    before_sessions = copy.deepcopy(app.canonical_runtime.sessions)
    try:
        response = await post(web)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "runtime_busy"
        assert not calls
        assert web.state.lifecycle.snapshot() == before_lifecycle
        assert app.canonical_runtime.sessions == before_sessions
    finally:
        release.set()
        thread_release.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        if recognizer is not None:
            await asyncio.gather(*recognizer._native_tasks, return_exceptions=True)
        await app._cleanup_session_resources("synthetic-maintenance")
    if recognizer is not None:
        assert recognizer.native_worker_snapshot()["active_jobs"] == 0
        assert (await post(web)).status_code == 200 and calls == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["event", "audio", "canonical"])
async def test_registration_precedes_the_first_application_await(monkeypatch, entry):
    app, web = await application(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    async def seam(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return [] if entry == "event" else {"reason": "synthetic"}

    if entry == "event":
        monkeypatch.setattr(app, "expire_engagement_sessions", seam)
        work = app.receive_async(event("HandsetLifted"))
    elif entry == "audio":
        monkeypatch.setattr(app, "_receive_canonical_audio", seam)
        work = app.receive_audio_async("synthetic-maintenance", (.1,), 1)
    else:
        monkeypatch.setattr(app, "_receive_canonical_event_inner", seam)
        work = app._receive_canonical_event(event("HandsetLifted"))
    task = asyncio.create_task(work)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert len(app._runtime_ingress) == 1 and not app._engagement_ingress
        before = web.state.lifecycle.snapshot()
        assert (await post(web)).status_code == 409
        assert web.state.lifecycle.snapshot() == before
    finally:
        release.set()
        await task
        assert not app._runtime_ingress
        await app._cleanup_session_resources("synthetic-maintenance")


def tracked_lease(app, lifecycle):
    calls = []
    original = app.acquire_runtime_maintenance

    def acquire():
        release = original()
        calls.append("acquire")

        def finish():
            calls.append("release")
            release()
        return finish

    lifecycle._maintenance_acquire = acquire
    return calls


@pytest.mark.asyncio
async def test_http_waiter_cancellation_keeps_warmup_lease_and_coalesces(monkeypatch):
    app, web = await application(monkeypatch)
    lifecycle = web.state.lifecycle
    calls = tracked_lease(app, lifecycle)
    entered, release = asyncio.Event(), asyncio.Event()
    warmups = []

    async def warmup():
        warmups.append(True)
        entered.set()
        await release.wait()
        return {"ok": True, "e2e": {"ok": True}}

    monkeypatch.setattr(app.runtime_manager, "warmup", warmup)
    first = asyncio.create_task(post(web))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        generation = lifecycle.snapshot()["generation"]
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert app._runtime_maintenance is not None and calls == ["acquire"]
        assert not lifecycle._warmup.done()
        assert (await app.receive_async(event("HandsetLifted")))["reason"] == "runtime_unavailable"
        assert (await app.receive_audio_async("synthetic-maintenance", (.1,), 1))["reason"] == "runtime_unavailable"
        assert (await app._receive_canonical_event(event=event("HandsetLifted")))["reason"] == "runtime_unavailable"
        assert not app.canonical_runtime.sessions and not app._runtime_ingress
        second = asyncio.create_task(post(web))
        await asyncio.sleep(0)
        assert lifecycle.snapshot()["generation"] == generation
        release.set()
        assert (await second).status_code == 200
        assert warmups == [True] and calls == ["acquire", "release"]
        assert app._runtime_maintenance is None and lifecycle.admits_users()
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_child_cannot_reuse_a_finished_parents_ingress_token(monkeypatch):
    app, web = await application(monkeypatch)
    release = asyncio.Event()

    async def child():
        await release.wait()
        return await app.receive_async(event("HandsetLifted"))

    with app._runtime_work() as admitted:
        assert admitted
        token = app._runtime_ingress[asyncio.current_task()]
        with app._runtime_work():
            assert app._runtime_ingress == {asyncio.current_task(): token}
        task = asyncio.create_task(child())
    assert not app._runtime_ingress
    lease = app.acquire_runtime_maintenance()
    try:
        release.set()
        assert (await task)["reason"] == "runtime_unavailable"
        assert not app.canonical_runtime.sessions and not app._runtime_ingress
    finally:
        lease()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["HandsetPlaced", "SessionReset", "HardwareDisconnected",
                                   "ClientProfileCleared", "IdentityAuthorizationExpired", "InteractionIdleExpired"])
async def test_maintenance_does_not_remove_cleanup_or_expiry_authority(monkeypatch, kind):
    app, _web = await application(monkeypatch)
    reached = []

    async def inner(event, *_args, **_kwargs):
        reached.append(event.event_type)
        return {"reason": "synthetic_cleanup"}

    monkeypatch.setattr(app, "_receive_canonical_event_inner", inner)
    lease = app.acquire_runtime_maintenance()
    try:
        result = await app._receive_canonical_event(event(kind), interaction_idle_authorized=True)
        assert result["reason"] == "synthetic_cleanup" and reached == [kind]
        assert not app._runtime_ingress
    finally:
        lease()


@pytest.mark.asyncio
async def test_busy_urgent_classifier_still_reaches_safety_stop(monkeypatch):
    app, web = await application(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()

    class Classifier:
        async def classify(self, *_args, **_kwargs):
            entered.set()
            await release.wait()
            return InputSafetyDecision("urgent", "synthetic")

    app.safety_classifier = Classifier()
    await app.receive_async(event("HandsetLifted"))
    task = asyncio.create_task(app.receive_async(event("SpeechReceived", 2), "Синтетическая реплика"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert (await post(web)).status_code == 409
        release.set()
        assert (await task)["reason"] == "urgent_safety"
        assert app.canonical_runtime.sessions["synthetic-maintenance"].mode == "safety_stop"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await app._cleanup_session_resources("synthetic-maintenance")


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["failure", "exception", "timeout", "before_start", "dependency", "shutdown"])
async def test_lease_releases_once_on_every_terminal_path(monkeypatch, ending):
    app, web = await application(monkeypatch)
    gate = web.state.lifecycle
    calls = tracked_lease(app, gate)
    entered, cleanup_entered, cleanup_release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def warmup():
        if ending == "failure":
            return {"ok": False, "reason": "synthetic"}
        if ending == "exception":
            raise RuntimeError("synthetic")
        if ending == "timeout":
            return await asyncio.wait_for(asyncio.Event().wait(), .01)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_entered.set()
            await cleanup_release.wait()

    await gate.start(warmup)
    stop = None
    try:
        if ending in {"failure", "exception", "timeout"}:
            assert not (await asyncio.shield(gate._warmup))["ok"]
        elif ending == "before_start":
            gate._warmup.cancel()
            await asyncio.gather(gate._warmup, return_exceptions=True)
        else:
            await asyncio.wait_for(entered.wait(), 2)
            if ending == "dependency":
                gate.dependency_failed("synthetic", "unavailable")
            else:
                stop = asyncio.create_task(gate.stop())
            await asyncio.wait_for(cleanup_entered.wait(), 2)
            assert app._runtime_maintenance is not None and calls == ["acquire"]
            if ending == "dependency":
                assert (await post(web)).status_code == 409
            cleanup_release.set()
            await asyncio.gather(gate._warmup, *([stop] if stop else []), return_exceptions=True)
        assert app._runtime_maintenance is None
        assert calls == ["acquire", "release"]
        assert not gate.admits_users()
        assert gate.snapshot()["state"] == ("STOPPING" if ending == "shutdown" else "DEGRADED")
    finally:
        cleanup_release.set()
        await asyncio.gather(gate._warmup, *([stop] if stop else []), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["warmup", "recovery"])
async def test_recovery_and_manual_warmup_share_one_exclusive_owner(monkeypatch, first):
    app, web = await application(monkeypatch)
    gate, manager = web.state.lifecycle, app.runtime_manager
    calls = tracked_lease(app, gate)
    entered, release = asyncio.Event(), asyncio.Event()

    async def warmup():
        entered.set()
        await release.wait()
        return {"ok": True, "e2e": {"ok": True}}

    async def resources():
        return {"ready": True}

    manager.settings = replace(manager.settings, profile="local_cpu")
    for name in ("vad", "stt", "llm", "tts", "playback"):
        monkeypatch.setattr(manager, name, SimpleNamespace(readiness=lambda: {"ready": True}))
    monkeypatch.setattr(manager, "resource_readiness", resources)
    monkeypatch.setattr(manager, "warmup", warmup)
    task = asyncio.create_task(post(web) if first == "warmup" else manager.recover(gate, "synthetic"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        before = gate.snapshot()
        stops = app.bounded_s2s.playback.stops
        if first == "warmup":
            assert (await manager.recover(gate, "synthetic"))["reason"] == "runtime_busy"
            assert not manager._recovery_running
        else:
            assert (await post(web)).status_code == 409
            assert (await manager.recover(gate, "synthetic"))["reason"] == "recovery_already_running"
        assert gate.snapshot() == before and calls == ["acquire"]
        assert app.bounded_s2s.playback.stops == stops
        release.set()
        result = await task
        assert result.status_code == 200 if first == "warmup" else result["ok"]
        assert app._runtime_maintenance is None and not manager._recovery_running
        assert calls == ["acquire", "release"] and gate.admits_users()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["cancel_waiting_lock", "exception", "stopping"])
async def test_recovery_failed_begin_always_clears_manager_ownership(monkeypatch, failure):
    app, web = await application(monkeypatch)
    gate, manager = web.state.lifecycle, app.runtime_manager
    before = gate.snapshot()["generation"]
    if failure == "cancel_waiting_lock":
        await gate._lock.acquire()
        task = asyncio.create_task(manager.recover(gate, "synthetic"))
        try:
            await asyncio.sleep(0)
            assert manager._recovery_running
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            gate._lock.release()
    elif failure == "exception":
        async def fail(*_args):
            raise RuntimeError("synthetic begin failure")
        monkeypatch.setattr(gate, "begin_recovery", fail)
        assert (await manager.recover(gate, "synthetic"))["reason"] == "recovery_exception"
    else:
        gate.begin_shutdown()
        assert (await manager.recover(gate, "synthetic"))["reason"] == "lifecycle_stopping"
        before += 1
    assert gate.snapshot()["generation"] == before
    assert not manager._recovery_running and app._runtime_maintenance is None
    assert app.bounded_s2s.playback.stops == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["shutdown", "busy", "exception"])
async def test_recovery_terminal_cleanup_keeps_lease_until_actual_exit(monkeypatch, ending):
    app, web = await application(monkeypatch)
    gate, manager = web.state.lifecycle, app.runtime_manager
    calls = tracked_lease(app, gate)
    entered, release = asyncio.Event(), asyncio.Event()
    cleanup_entered, cleanup_release = asyncio.Event(), asyncio.Event()

    async def warmup():
        entered.set()
        try:
            await release.wait()
            raise RuntimeBusyError("synthetic") if ending == "busy" else RuntimeError("synthetic")
        finally:
            cleanup_entered.set()
            await cleanup_release.wait()

    async def resources():
        return {"ready": True}

    manager.settings = replace(manager.settings, profile="local_cpu")
    for name in ("vad", "stt", "llm", "tts", "playback"):
        monkeypatch.setattr(manager, name, SimpleNamespace(readiness=lambda: {"ready": True}))
    monkeypatch.setattr(manager, "resource_readiness", resources)
    monkeypatch.setattr(manager, "warmup", warmup)
    task = asyncio.create_task(manager.recover(gate, "synthetic"))
    stop = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if ending == "shutdown":
            stop = asyncio.create_task(gate.stop())
        else:
            release.set()
        await asyncio.wait_for(cleanup_entered.wait(), 2)
        assert app._runtime_maintenance is not None and manager._recovery_running
        assert calls == ["acquire"]
        cleanup_release.set()
        if ending == "shutdown":
            with pytest.raises(asyncio.CancelledError):
                await task
            await stop
        else:
            result = await task
            assert result["reason"] == ("runtime_busy" if ending == "busy" else "recovery_exception")
        assert app._runtime_maintenance is None and not manager._recovery_running
        assert calls == ["acquire", "release"]
        assert not gate.admits_users()
    finally:
        release.set()
        cleanup_release.set()
        await asyncio.gather(task, *([stop] if stop else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_stale_recovery_finish_cannot_release_next_generation(monkeypatch):
    app, web = await application(monkeypatch)
    gate = web.state.lifecycle
    calls = tracked_lease(app, gate)
    generation = await gate.begin_recovery("synthetic", "technical-correlation")
    assert gate.finish_recovery(generation, {"ok": True, "e2e": {"ok": True}})

    async def warmup():
        await asyncio.Event().wait()

    await gate.start(warmup)
    try:
        owner = app._runtime_maintenance
        assert not gate.finish_recovery(generation, {"ok": False})
        assert app._runtime_maintenance is owner
        assert calls == ["acquire", "release", "acquire"]
    finally:
        await gate.stop()
    assert calls == ["acquire", "release", "acquire", "release"]


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["engagement", "vad", "pending", "queue", "detached", "cleanup", "native"])
async def test_busy_predicate_keeps_existing_owners_including_cancel_resistant_jobs(monkeypatch, owner):
    app, web = await application(monkeypatch)
    release, cancelled = asyncio.Event(), asyncio.Event()
    task = None

    async def job():
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    if owner == "engagement":
        app._engagement_ingress[("fixture", None)] = 1
    elif owner == "vad":
        app.s2s_audio.ingress["fixture"] = SimpleNamespace(active=True)
    elif owner in {"pending", "detached"}:
        task = asyncio.create_task(job())
        await asyncio.sleep(0)
        if owner == "pending":
            app.s2s_audio.pending_tasks["fixture"] = task
        else:
            app._owned_text_jobs.add(task)
            task.cancel()
            await asyncio.wait_for(cancelled.wait(), 2)
    elif owner == "queue":
        app.bounded_s2s.active_queues["fixture"] = object()
    elif owner == "cleanup":
        app._session_reset_waiters["fixture"] = (asyncio.current_task(), asyncio.get_running_loop().create_future())
    else:
        app.runtime_manager.tts = SimpleNamespace(native_worker_snapshot=lambda: {"active_jobs": 0, "queued_jobs": 1})
    before = web.state.lifecycle.snapshot()
    try:
        assert (await post(web)).status_code == 409
        assert web.state.lifecycle.snapshot() == before
    finally:
        release.set()
        if task:
            await task


@pytest.mark.asyncio
async def test_idle_ingress_metadata_and_generations_do_not_block_maintenance(monkeypatch):
    app, web = await application(monkeypatch)
    app.s2s_audio.ingress["old"] = SimpleNamespace(active=False)
    app.s2s_audio.generations["old"] = 17

    async def warmup():
        return {"ok": True, "e2e": {"ok": True}}

    monkeypatch.setattr(app.runtime_manager, "warmup", warmup)
    assert (await post(web)).status_code == 200
    assert app._runtime_maintenance is None and web.state.lifecycle.admits_users()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["cancel", "timeout", "load"])
async def test_native_thread_remains_owned_until_actual_completion(ending):
    entered, release = threading.Event(), threading.Event()

    class Model:
        def transcribe(self, *_args, **_kwargs):
            entered.set()
            assert release.wait(5)
            return ([SimpleNamespace(text="Тест", no_speech_prob=0., avg_logprob=0.)], None)

    def loader():
        entered.set()
        assert release.wait(5)
        return Model()

    recognizer = FasterWhisperRecognizer(loader, S2SConfig(stt_timeout_s=.03))
    if ending != "load":
        recognizer._model = Model()
    task = asyncio.create_task(recognizer.warmup() if ending == "load" else
                               recognizer.transcribe(AudioChunk((.1,) * 1600, 16000, 1), lambda: False))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if ending == "timeout":
            with pytest.raises(RuntimeError, match="transcription failed"):
                await task
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert recognizer.native_worker_snapshot()["active_jobs"] == 1
        release.set()
        await asyncio.gather(*recognizer._native_tasks)
        assert recognizer.native_worker_snapshot()["active_jobs"] == 0
    finally:
        release.set()
        await asyncio.gather(task, *recognizer._native_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_native_tracking_does_not_serialize_ordinary_recognition():
    entered, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    started = []

    class Model:
        def transcribe(self, *_args, **_kwargs):
            with lock:
                started.append(True)
                if len(started) == 2:
                    entered.set()
            assert release.wait(5)
            return ([SimpleNamespace(text="Тест", no_speech_prob=0., avg_logprob=0.)], None)

    recognizer = FasterWhisperRecognizer(lambda: None, S2SConfig())
    recognizer._model = Model()
    tasks = [asyncio.create_task(recognizer.transcribe(AudioChunk((.1,) * 1600, 16000, index), lambda: False))
             for index in (1, 2)]
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert recognizer.native_worker_snapshot()["active_jobs"] == 2
        release.set()
        results = await asyncio.gather(*tasks)
        assert [item.text for item in results] == ["Тест", "Тест"]
        assert recognizer.native_worker_snapshot()["active_jobs"] == 0
    finally:
        release.set()
        await asyncio.gather(*tasks, *recognizer._native_tasks, return_exceptions=True)
