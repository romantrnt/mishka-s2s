from __future__ import annotations

import asyncio

from app.foundation.canonical_contract import CanonicalEvent, CanonicalRuntime
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.stand import build_application


def _event(kind: str, sid: str, seq: int, payload=None, source="test") -> Event:
    return Event(kind, sid, seq, 1.0, source, payload or {}, f"{source}:{seq}", 0.0)


def test_urgent_local_stop_ignores_later_speech_until_handset_down():
    runtime = CanonicalRuntime()
    sid = "urgent-priority"
    def send(kind, seq, payload=None):
        state = runtime.sessions.get(sid)
        return runtime.handle(CanonicalEvent(kind, sid, seq, state.state_version if state else 0,
            "test", payload or {}, f"corr:{seq}", 1.0))
    send("HandsetLifted", 1)
    send("VoiceIntentReady", 2, {"intent": "free_speech", "safety_severity": "urgent"})
    owner = runtime.sessions[sid].state_version
    for seq, severity in enumerate(("none", "concern", "urgent"), 3):
        result = send("VoiceIntentReady", seq, {"intent": "free_speech", "safety_severity": severity})
        assert result["state"]["mode"] == "safety_stop"
        assert result["state"]["safety_severity"] == "urgent"
        assert result["state"]["state_version"] == owner and result["effects"] == []
    result = send("HandsetPlaced", 6)
    assert result["reset_session"] is True
    assert result["state"]["mode"] == "idle" and result["state"]["handset"] == "down"
    assert any(e["type"] == "lighting_dark" for e in result["effects"])



def test_hangup_during_provider_cancel_cannot_emit_old_audio_or_raise():
    class DelayedCancel:
        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0

        async def cancel_session(self, _sid):
            self.calls += 1
            if self.calls == 1:
                self.entered.set()
                await self.release.wait()

    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        sid = "hung-cancel"
        await app.receive_async(_event("HandsetLifted", sid, 1))
        delayed = DelayedCancel()
        app.bounded_s2s = delayed
        old = asyncio.create_task(app.receive_async(_event(
            "ButtonPressed", sid, 2, {"button_id": 5}, "button",
        )))
        await asyncio.wait_for(delayed.entered.wait(), 1)
        down = await asyncio.wait_for(app.receive_async(_event("HandsetPlaced", sid, 3, source="handset")), 2)
        count_after_down = len(app.audio.commands)
        delayed.release.set()
        old_result = await asyncio.wait_for(old, 2)
        assert down["reason"] == "handset_down"
        assert old_result["reason"] == "stale_session_generation"
        assert len(app.audio.commands) == count_after_down
        assert app.canonical_runtime.sessions[sid].handset == "down"

    asyncio.run(run())


def test_prior_audio_callback_does_not_cancel_new_playback_watchdog():
    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="exhibition"))
        sid = "watchdog-owner"
        await app.receive_async(_event("HandsetLifted", sid, 1))
        app._register_audio_lease(sid, "PHRASE_001", 0, "play-a", "lease-a")
        old = dict(app._active_audio[sid])
        async def transition(_event, _text=None):
            app._register_audio_lease(sid, "PHRASE_001", 0, "play-b", "lease-b")
            return {"reason": "new_playback", "outputs": []}
        app._receive_canonical_event = transition
        await app.receive_async(_event("AudioCompleted", sid, 2, {
            "response_id": old["content_id"], "token": old["token"],
            "generation": old["generation"], "playback_lease": old["playback_lease"],
        }))
        assert app._active_audio[sid]["playback_id"] == "play-b"
        assert not app._audio_timeout_tasks[sid].cancelled()
        assert not app._audio_callback_is_current(_event("AudioCompleted", sid, 3, {
            "response_id": old["content_id"], "token": old["token"],
            "generation": old["generation"], "playback_lease": old["playback_lease"],
        }))
        app._invalidate_audio_lease(sid)

    asyncio.run(run())


def test_repeated_phrase_has_distinct_playback_lease_without_version_change():
    runtime = CanonicalRuntime()
    sid = "repeat-identity"
    first = runtime.handle(CanonicalEvent(
        "AnonymousStart", sid, 1, 0, "test", {}, "start", 1.0,
    ))
    state = runtime.sessions[sid]
    state.mode = "wellbeing_check"
    state.handset = "up"
    version = state.state_version
    a = runtime.handle(CanonicalEvent(
        "ButtonPressed", sid, 2, version, "test", {"button_id": 5}, "press-a", 1.0,
    ))
    b = runtime.handle(CanonicalEvent(
        "ButtonPressed", sid, 3, a["state"]["state_version"], "test", {"button_id": 5}, "press-b", 1.0,
    ))
    a_audio = next(e for e in a["effects"] if e["type"] == "support_question")
    b_audio = next(e for e in b["effects"] if e["type"] == "support_question")
    assert a_audio["playback_lease"] != b_audio["playback_lease"]


def test_local_safety_can_end_without_nonexistent_adult_acknowledgement():
    for ending in ("HandsetPlaced", "SessionCancel", "SessionReset"):
        runtime = CanonicalRuntime(exhibition_mode=True)
        sid = "local-ending"
        runtime.handle(CanonicalEvent("HandsetLifted", sid, 1, 0, "test", {}, "lift", 1.))
        state = runtime.sessions[sid]
        runtime.handle(CanonicalEvent("VoiceIntentReady", sid, 2, state.state_version,
            "test", {"intent": "free_speech", "safety_severity": "urgent"}, "urgent", 1.))
        result = runtime.handle(CanonicalEvent(ending, sid, 3, state.state_version, "test", {}, "end", 1.))
        assert result["reset_session"] is True
        assert result["state"]["mode"] != "safety_stop"
        assert result["state"]["safety_severity"] == "none"
        assert any(e["type"] == "lighting_dark" for e in result["effects"])



def test_late_stt_result_after_hangup_cannot_recreate_session():
    class DelayedIngress:
        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def feed_audio(self, *_args, **_kwargs):
            self.entered.set()
            await self.release.wait()
            return object()

        async def cancel_session(self, _sid):
            return None

    async def run(mode):
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode=mode))
        sid = f"late-stt-{mode}"
        await app.receive_async(_event("HandsetLifted", sid, 1))
        ingress = DelayedIngress()
        app.s2s_audio = ingress
        pending = asyncio.create_task(app.receive_audio_async(sid, (0.0,), 2))
        await asyncio.wait_for(ingress.entered.wait(), 1)
        down = await app.receive_async(_event("HandsetPlaced", sid, 3, source="handset"))
        audio_count = len(app.audio.commands)
        ingress.release.set()
        result = await asyncio.wait_for(pending, 2)
        assert result["reason"] == "stale_session_generation"
        assert len(app.audio.commands) == audio_count
        assert app.sessions.get(sid) is None or app.sessions.get(sid).handset_up is False
        if mode == "exhibition":
            assert down["reason"] == "exhibition_reset"
            assert sid not in app.canonical_runtime.sessions

    asyncio.run(run("exhibition"))
    asyncio.run(run("clinical"))


def test_generic_application_reset_clears_local_safety_and_private_session():
    async def run():
        app, *_ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        sid = "reset-local-stop"
        try:
            await app.receive_async(_event("HandsetLifted", sid, 1))
            await app.receive_async(_event("VoiceIntentReady", sid, 2,
                {"intent": "free_speech", "safety_severity": "urgent"}))
            result = await app.reset_session(sid)
            assert result["reset"] is True
            assert sid not in app.canonical_runtime.sessions
            assert app.sessions.get(sid) is None
            assert sid not in app._active_audio
        finally:
            await app._cleanup_session_resources(sid)
    asyncio.run(run())



def test_urgent_local_guidance_keeps_safety_lighting():
    async def run():
        app, *_ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        sid = "urgent-local"
        try:
            await app.receive_async(_event("HandsetLifted", sid, 1))
            result = await app.receive_async(_event("VoiceIntentReady", sid, 2,
                {"intent": "free_speech", "safety_severity": "urgent"}))
            assert result["reason"] == "urgent_safety"
            assert result["canonical_state"]["safety_severity"] == "urgent"
            assert any(e.get("preset") == "SAFE_STOP" for e in result["outputs"])
            assert any(e.get("phrase_id") == "PHRASE_091" for e in result["outputs"])
        finally:
            await app._cleanup_session_resources(sid)
    asyncio.run(run())



def test_exhibition_help_hangup_fences_waiting_provider():
    class DelayedCancel:
        def __init__(self):
            self.entered = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = 0

        async def cancel_session(self, _sid):
            self.calls += 1
            if self.calls == 1:
                self.entered.set()
                await self.release.wait()

    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="exhibition"))
        sid = "exhibition-help-race"
        app.monotonic_now = lambda: 0.0
        app.canonical_runtime.monotonic_now = app.monotonic_now
        await app.receive_async(_event("HandsetLifted", sid, 1))
        # Reach a post-onboarding activity without exercising the unrelated
        # menu workflow. The help priority is the real canonical transition.
        app.canonical_runtime.sessions[sid].mode = "root_menu"
        delayed = DelayedCancel()
        app.bounded_s2s = delayed
        old = asyncio.create_task(app.receive_async(_event(
            "ButtonPressed", sid, 2, {"button_id": 5}, "button",
        )))
        await asyncio.wait_for(delayed.entered.wait(), 1)
        down = await asyncio.wait_for(app.receive_async(
            _event("HandsetPlaced", sid, 3, source="handset"),
        ), 2)
        after_down = len(app.audio.commands)
        delayed.release.set()
        stale = await asyncio.wait_for(old, 2)
        assert down["reason"] == "exhibition_reset"
        assert stale["reason"] == "stale_session_generation"
        assert len(app.audio.commands) == after_down
        assert sid not in app.canonical_runtime.sessions

    asyncio.run(run())


def test_new_lift_waits_for_old_exhibition_reset_cleanup():
    async def run():
        app, _, _, _ = build_application(
            settings=Settings(profile="mock", runtime_mode="exhibition"),
            monotonic_now=lambda: 0.0,
        )
        sid = "lift-during-reset"
        await app.receive_async(_event("HandsetLifted", sid, 1))
        entered, release = asyncio.Event(), asyncio.Event()
        original_cleanup = app._cleanup_session_resources

        async def gated_cleanup(session_id):
            entered.set()
            await release.wait()
            return await original_cleanup(session_id)

        app._cleanup_session_resources = gated_cleanup
        down_task = asyncio.create_task(app.receive_async(
            _event("HandsetPlaced", sid, 2, source="handset"),
        ))
        await asyncio.wait_for(entered.wait(), 1)
        lift_task = asyncio.create_task(app.receive_async(
            _event("HandsetLifted", sid, 3, source="new-handset"),
        ))
        await asyncio.sleep(0)
        release.set()
        down, lifted = await asyncio.wait_for(asyncio.gather(down_task, lift_task), 2)
        assert down["reason"] == "exhibition_reset"
        assert lifted["reason"] == "exhibition_handset_lifted"
        assert app.canonical_runtime.sessions[sid].handset == "up"
        assert app.sessions.get(sid).handset_up is True
        assert sid in app._welcome_timers and not app._welcome_timers[sid].cancelled()
        app._cancel_welcome_timer(sid)

    asyncio.run(run())
