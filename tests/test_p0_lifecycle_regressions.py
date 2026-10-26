from __future__ import annotations

import asyncio

from app.foundation.canonical_contract import CanonicalEvent, CanonicalRuntime
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.stand import build_application


def _event(kind: str, sid: str, seq: int, payload=None, source="test") -> Event:
    return Event(kind, sid, seq, 1.0, source, payload or {}, f"{source}:{seq}", 0.0)


def test_urgent_incident_survives_later_child_speech_until_adult_resolution():
    runtime = CanonicalRuntime()
    sid = "urgent-priority"

    def send(kind, seq, payload=None):
        current = runtime.sessions.get(sid)
        return runtime.handle(CanonicalEvent(
            kind, sid, seq, current.state_version if current else 0,
            "test", payload or {}, f"corr:{seq}", 1.0,
        ))

    send("AnonymousStart", 1)
    send("HandsetLifted", 2)
    urgent = send("VoiceIntentReady", 3, {"intent": "free_speech", "safety_severity": "urgent"})
    assert urgent["state"]["mode"] == "safety_handoff"
    for seq, payload in enumerate((
        {"intent": "free_speech", "safety_severity": "none"},
        {"intent": "request_adult", "safety_severity": "concern"},
        {"intent": "free_speech", "safety_severity": "concern"},
    ), start=4):
        result = send("VoiceIntentReady", seq, payload)
        assert result["state"]["mode"] == "safety_handoff"
        assert result["state"]["safety_severity"] == "urgent"
        assert not any(e.get("type") == "adult_incident_closed" for e in result["effects"])
    resolved = runtime.handle(CanonicalEvent(
        "AdultResolved", sid, 7, runtime.sessions[sid].state_version,
        "internal_test", {"incident_id": urgent["state"]["incident_id"]},
        "trusted-resolution", 1.0,
    ), adult_resolution_authorized=True)
    assert resolved["state"]["safety_severity"] == "none"
    assert resolved["state"]["mode"] == "root_menu"


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


def test_urgent_survives_exhibition_handset_down_and_session_cancel():
    runtime = CanonicalRuntime(exhibition_mode=True)
    sid = "urgent-exhibition"
    def send(kind, seq, payload=None):
        current = runtime.sessions.get(sid)
        return runtime.handle(CanonicalEvent(
            kind, sid, seq, current.state_version if current else 0,
            "test", payload or {}, f"corr:{seq}", 1.0,
        ))
    send("HandsetLifted", 1)
    send("VoiceIntentReady", 2, {"intent": "free_speech", "safety_severity": "urgent"})
    for kind, seq in (("HandsetPlaced", 3), ("SessionCancel", 4), ("SessionReset", 5), ("AnonymousStart", 6)):
        result = send(kind, seq)
        assert result["state"]["safety_severity"] == "urgent"
        assert result["state"]["mode"] == "safety_handoff"
        assert not result.get("reset_session")
    incident_id = runtime.sessions[sid].incident_id
    resolved = runtime.handle(CanonicalEvent(
        "AdultResolved", sid, 7, runtime.sessions[sid].state_version,
        "internal_test", {"incident_id": incident_id},
        "trusted-resolution", 1.0,
    ), adult_resolution_authorized=True)
    assert resolved["state"]["safety_severity"] == "none"


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


def test_generic_application_reset_does_not_close_urgent_incident():
    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        sid = "reset-urgent"
        await app.receive_async(_event("HandsetLifted", sid, 1))
        await app.receive_async(_event(
            "VoiceIntentReady", sid, 2,
            {"intent": "free_speech", "safety_severity": "urgent"},
        ))
        result = await app.reset_session(sid)
        assert result["reset"] is False
        assert result["reason"] == "safety_handoff_active"
        assert app.canonical_runtime.sessions[sid].safety_severity == "urgent"
        assert app.sessions.get(sid) is not None

    asyncio.run(run())


def test_urgent_delivery_ack_keeps_parent_safety_effects():
    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        sid = "urgent-ack"
        app.monotonic_now = lambda: 0.0
        app.canonical_runtime.monotonic_now = app.monotonic_now
        await app.receive_async(_event("HandsetLifted", sid, 1))
        result = await app.receive_async(_event(
            "VoiceIntentReady", sid, 2,
            {"intent": "free_speech", "safety_severity": "urgent"},
        ))
        assert result["reason"] == "urgent_safety"
        assert result["canonical_state"]["safety_severity"] == "urgent"
        assert any(e.get("preset") == "SAFE_STOP" for e in result["outputs"])

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


def test_urgent_rejected_rfid_cannot_replace_private_audio_profile():
    class Profiles:
        data = {
            "tag-a": {"profile_ref": "child-a", "display_name": "Ada", "age": 7},
            "tag-b": {"profile_ref": "child-b", "display_name": "Bob", "age": 4},
        }

        def resolve(self, tag_id):
            return self.data.get(tag_id)

    async def run():
        app, _, _, _ = build_application(
            settings=Settings(profile="mock", runtime_mode="clinical"),
            monotonic_now=lambda: 0.0,
        )
        app.profiles = Profiles()
        sid = "rfid-urgent-profile"
        await app.receive_async(_event("RfidPresented", sid, 1, {"tag_id": "tag-a"}))
        await app.receive_async(_event("HandsetLifted", sid, 2))
        await app.receive_async(_event(
            "VoiceIntentReady", sid, 3,
            {"intent": "free_speech", "safety_severity": "urgent"},
        ))
        blocked = await app.receive_async(_event(
            "RfidPresented", sid, 4, {"tag_id": "tag-b"},
        ))
        assert blocked["reason"] == "safety_handoff_active"
        assert app.canonical_runtime.sessions[sid].profile_ref == "child-a"
        assert app._canonical_profiles[sid]["profile_ref"] == "child-a"
        context = app._canonical_audio_context(sid)
        assert context.profile.display_name == "Ada"
        assert "Bob" not in context.system_prompt

    asyncio.run(run())
