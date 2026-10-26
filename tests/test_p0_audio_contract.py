from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.foundation.canonical_contract import CanonicalEvent, CanonicalRuntime, FixedStoryManifest
from app.foundation.config import Settings
from app.foundation.stand import HardwareIntegrationStand, HeadlessAudioOutput, UARTAudioOutput


def test_failed_playback_requires_actual_stop():
    runtime = CanonicalRuntime()
    sid = "failed-playback"
    runtime.handle(CanonicalEvent("AnonymousStart", sid, 1, 0, "test", {}, "start", 1.0))
    state = runtime.sessions[sid]
    state.handset = "up"
    state.active_audio_id = "PHRASE_084"
    state.playback_lease = "lease-a"
    result = runtime.handle(CanonicalEvent(
        "AudioFailed", sid, 2, state.state_version, "test",
        {"response_id": "PHRASE_084", "playback_lease": "lease-a", "error_code": "device_failure"},
        "failed-a", 1.0,
    ))
    assert result["state"]["audio_status"] == "failed"
    assert any(e["type"] == "audio_stop" for e in result["effects"])


def test_verified_story_duration_owns_watchdog_budget():
    manifest = FixedStoryManifest("content/stories/fixed_manifest.json", ".")
    story = manifest.get_verified("fixed_aibolit_v1")
    assert story is not None and story["duration_ms"] > 60_000
    async def run():
        from app.foundation.stand import build_application
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="exhibition"))
        app._register_audio_lease(
            "story-timeout", "fixed_aibolit_v1", 0, "play-story", "lease-story",
            duration_ms=story["duration_ms"],
        )
        assert app._active_audio["story-timeout"]["timeout_s"] > story["duration_ms"] / 1000
        app._invalidate_audio_lease("story-timeout")
    asyncio.run(run())


def test_browser_story_command_carries_asset_path_and_original_identity():
    stand = HardwareIntegrationStand(Settings(profile="mock", hardware_mode="simulator"))
    try:
        handle = SimpleNamespace(
            session_id="original-session", generation=7, playback_id="play-1",
            playback_lease="lease-1", response_id="fixed_aibolit_v1", token=3,
        )
        stand.audio_output.play_owned(handle, "assets/stories/example.wav")
        stand._drain_device_commands()
        command = next(item for item in reversed(stand.commands) if item["type"] == "audio_play")
        payload = command["payload"]
        assert payload["session_id"] == "original-session"
        assert payload["generation"] == 7
        assert payload["playback_lease"] == "lease-1"
        assert payload["playback_id"] == "play-1"
        assert payload["asset_path"] == "assets/stories/example.wav"
        assert payload["content_kind"] == "story"
    finally:
        stand.close()


def test_headless_adapter_forwards_immutable_handle_to_physical_task():
    output = HeadlessAudioOutput()
    observed = []
    output.on_asset_play = lambda response, asset, token, offset, handle: observed.append(
        (response, asset, token, offset, handle)
    )
    handle = SimpleNamespace(
        session_id="owner", generation=4, playback_id="play-x",
        playback_lease="lease-x", response_id="PHRASE_001", token=2,
    )
    output.play_owned(handle, "voice/PHRASE_001.wav", offset_ms=500)
    assert observed == [("PHRASE_001", "voice/PHRASE_001.wav", 2, 500, handle)]


def test_physical_completion_uses_original_handle_after_new_playback(tmp_path):
    import io
    import struct
    import wave

    from app.foundation.stand import HardwareIntegrationStand

    wav = tmp_path / "short.wav"
    with wave.open(str(wav), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(struct.pack("<h", 100))

    entered, release = asyncio.Event(), asyncio.Event()
    callbacks = []

    class Playback:
        active_token = 0
        last_playback_completed = True

        async def play(self, _audio):
            entered.set()
            await release.wait()

    class Application:
        _active_audio = {"sid": {"playback_lease": "new-lease", "generation": 9}}

        def _mark_audio_effect_lifecycle(self, *_args, **_kwargs):
            return None

        def record_audio_diagnostic(self, *_args, **_kwargs):
            return None

        async def receive_async(self, event):
            callbacks.append(event)

    async def offload(call):
        return call()

    stand = SimpleNamespace(
        playback=Playback(), application=Application(), stories={},
        commands=[], errors=[], _audio_event_sequence=0,
        _offload_audio=offload, _collect_events=lambda: None,
    )
    old = SimpleNamespace(
        session_id="sid", generation=3, playback_id="old-play",
        playback_lease="old-lease", response_id="PHRASE_001", token=2,
    )

    async def run():
        task = asyncio.create_task(HardwareIntegrationStand._play_asset_audio(
            stand, "sid", "PHRASE_001", str(wav), 2, 0, old,
        ))
        await asyncio.wait_for(entered.wait(), 1)
        release.set()
        await asyncio.wait_for(task, 1)
        assert len(callbacks) == 1
        assert callbacks[0].payload["playback_lease"] == "old-lease"
        assert callbacks[0].payload["generation"] == 3
        assert callbacks[0].payload["playback_id"] == "old-play"

    asyncio.run(run())


def test_story_pause_saves_physical_checkpoint_in_canonical_state():
    from app.foundation.domain import Event
    from app.foundation.stand import build_application

    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        sid = "pause-checkpoint"
        await app.receive_async(Event("HandsetLifted", sid, 1, 1.0, "test", {}, "lift", 0.0))
        state = app.canonical_runtime.sessions[sid]
        state.mode = "fixed_story_playing"
        state.substate = "story_playing"
        state.handset = "up"
        state.active_story_id = "fixed_aibolit_v1"
        state.expected_inputs = ["button_6", "button_7", "handset_down"]

        async def pause():
            return {"story_id": "fixed_aibolit_v1", "offset_ms": 34567}

        app.audio.pause = pause
        result = await app.receive_async(Event(
            "ButtonPressed", sid, 2, 1.0, "button", {"button_id": 6}, "pause", 0.0,
        ))
        assert result["reason"] == "story_pause_requested"
        assert app.canonical_runtime.sessions[sid].dialogue_memory.story_checkpoint == {
            "story_id": "fixed_aibolit_v1", "offset_ms": 34567,
        }

    asyncio.run(run())


def test_playback_timeout_stops_actual_output():
    from app.foundation.domain import Event
    from app.foundation.stand import build_application

    async def run():
        app, _, _, _ = build_application(settings=Settings(
            profile="mock", runtime_mode="exhibition", playback_timeout_s=0.02,
        ))
        app.monotonic_now = lambda: 0.0
        app.canonical_runtime.monotonic_now = app.monotonic_now
        sid = "timeout-stops-output"
        await app.receive_async(Event("HandsetLifted", sid, 1, 1.0, "test", {}, "lift", 0.0))
        app.monotonic_now = lambda: 5.0
        app.canonical_runtime.monotonic_now = app.monotonic_now
        await app.flush_welcome_timers()
        assert app.audio.commands
        await asyncio.sleep(0.08)
        assert app.canonical_runtime.sessions[sid].audio_status == "failed"
        assert not app.audio.commands
        assert not app._active_audio.get(sid)

    asyncio.run(run())


def test_story_resume_uses_new_owned_playback_and_remaining_duration():
    from app.foundation.domain import Event
    from app.foundation.stand import build_application

    async def run():
        app, _, _, _ = build_application(settings=Settings(
            profile="mock", runtime_mode="clinical", playback_timeout_s=0.02,
        ))
        sid = "resume-owner"
        await app.receive_async(Event("HandsetLifted", sid, 1, 1.0, "test", {}, "lift", 0.0))
        state = app.canonical_runtime.sessions[sid]
        state.mode = "story_resume_announcement"
        state.handset = "up"
        state.active_story_id = "fixed_aibolit_v1"
        state.active_audio_id = "PHRASE_064"
        state.playback_lease = "announcement"
        state.dialogue_memory.story_checkpoint = {
            "story_id": "fixed_aibolit_v1", "offset_ms": 30000,
        }
        result = await app._receive_canonical_event(Event(
            "AudioCompleted", sid, 2, 1.0, "test",
            {"response_id": "PHRASE_064", "playback_lease": "announcement"},
            "resume", 0.0,
        ))
        assert result["reason"] == "story_resumed_after_prompt"
        resumed = next(e for e in result["outputs"] if e["type"] == "story_audio_resume")
        lease = app._active_audio[sid]
        assert resumed["playback_id"] == lease["playback_id"]
        assert lease["content_id"] == "fixed_aibolit_v1"
        assert lease["timeout_s"] > 60
        assert app.audio.commands[-1]["offset_ms"] == 30000
        app._invalidate_audio_lease(sid)

    asyncio.run(run())


def test_two_real_physical_callbacks_reach_canonical_with_distinct_execution_ids(tmp_path):
    import struct
    import wave
    from app.foundation.domain import Event, PlaybackHandle
    from app.foundation.stand import build_application

    wav = tmp_path / "clip.wav"
    with wave.open(str(wav), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(struct.pack("<h", 100))

    class Playback:
        active_token = 0
        last_playback_completed = True

        async def play(self, _audio):
            return None

    async def offload(call):
        return call()

    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        sid = "real-physical-repeat"
        await app.receive_async(Event("HandsetLifted", sid, 1, 1.0, "test", {}, "lift", 0.0))
        state = app.canonical_runtime.sessions[sid]
        state.mode = "wellbeing_check"
        state.handset = "up"
        observed = []
        original_receive = app.receive_async

        async def receive(event):
            result = await original_receive(event)
            if event.source == "physical_audio":
                observed.append((event, result))
            return result

        app.receive_async = receive
        fake_stand = SimpleNamespace(
            playback=Playback(), application=app, stories={},
            commands=[], errors=[], _audio_event_sequence=0,
            _offload_audio=offload, _collect_events=lambda: None,
        )
        token = app.sessions.get(sid).cancellation_token
        handles = [
            PlaybackHandle(sid, app._session_epochs.get(sid, 0), "play-A", "lease-A", "PHRASE_084", token),
            PlaybackHandle(sid, app._session_epochs.get(sid, 0), "play-B", "lease-B", "PHRASE_084", token),
        ]
        for handle in handles:
            state.active_audio_id = "PHRASE_084"
            state.playback_lease = handle.playback_lease
            app._register_audio_lease(sid, "PHRASE_084", token, handle.playback_id, handle.playback_lease)
            await HardwareIntegrationStand._play_asset_audio(
                fake_stand, sid, "PHRASE_084", str(wav), token, 0, handle,
            )
        assert len(observed) == 2
        assert observed[0][1]["reason"] != "idempotent_duplicate"
        assert observed[1][1]["reason"] != "idempotent_duplicate"
        assert observed[0][0].correlation_id != observed[1][0].correlation_id
        assert observed[0][0].payload["playback_id"] == "play-A"
        assert observed[1][0].payload["playback_id"] == "play-B"
        state.active_audio_id = "PHRASE_084"
        state.playback_lease = "lease-B"
        app._register_audio_lease(sid, "PHRASE_084", token, "play-B", "lease-B")
        late_a = await original_receive(observed[0][0])
        assert late_a["reason"] == "stale_audio_callback"
        app._invalidate_audio_lease(sid)

    asyncio.run(run())


def test_late_lifecycle_for_repeated_phrase_cannot_mark_new_effect():
    from app.foundation.stand import build_application

    app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
    newer = {
        "type": "speak", "phrase_id": "PHRASE_084", "session_id": "sid",
        "generation": 2, "playback_id": "play-B", "playback_lease": "lease-B",
        "audio_status": "queued",
    }
    app.last_canonical_effects = [newer]
    app._mark_audio_effect_lifecycle(
        "PHRASE_084", "finished", session_id="sid", generation=2,
        playback_id="play-A", playback_lease="lease-A",
    )
    assert newer["audio_status"] == "queued"
    app._mark_audio_effect_lifecycle("PHRASE_084", "finished")
    assert newer["audio_status"] == "queued"
    app._mark_audio_effect_lifecycle(
        "PHRASE_084", "finished", session_id="sid", generation=2,
        playback_id="play-B", playback_lease="lease-B",
    )
    assert newer["audio_status"] == "finished"


def test_browser_lifecycle_route_rejects_stale_and_unidentified_execution():
    from fastapi.testclient import TestClient
    from app.foundation.stand import create_stand_app

    api = create_stand_app(Settings(
        profile="mock", hardware_mode="simulator", stand_access_token="test-token",
        mutation_auth_policy="token_required",
    ))
    api.state.lifecycle = None
    stand = api.state.stand
    newer = {
        "type": "speak", "phrase_id": "PHRASE_084", "session_id": "browser-sid",
        "generation": 3, "playback_id": "browser-B", "playback_lease": "browser-lease-B",
        "audio_status": "queued",
    }
    stand.application.last_canonical_effects = [newer]
    try:
        with TestClient(api) as client:
            client.headers.update({"x-stand-token": "test-token"})
            old = client.post("/stand/audio/lifecycle", json={
                "session_id": "browser-sid", "response_id": "PHRASE_084",
                "generation": 3, "playback_id": "browser-A",
                "playback_lease": "browser-lease-A", "status": "finished",
            })
            assert old.status_code == 200, (old.status_code, old.json())
            assert old.json().get("reason") == "stale_audio_lifecycle", old.json()
            assert newer["audio_status"] == "queued"
            missing = client.post("/stand/audio/lifecycle", json={
                "response_id": "PHRASE_084", "status": "finished",
            })
            assert missing.status_code == 422
            assert newer["audio_status"] == "queued"
            valid = client.post("/stand/audio/lifecycle", json={
                "session_id": "browser-sid", "response_id": "PHRASE_084",
                "generation": 3, "playback_id": "browser-B",
                "playback_lease": "browser-lease-B", "status": "playing",
            })
            assert valid.json()["audio_status"] == "playing"
            assert newer["audio_status"] == "playing"
    finally:
        stand.close()


def test_browser_pause_checkpoint_route_requires_exact_paused_playback_and_generation():
    from fastapi.testclient import TestClient
    from app.foundation.stand import create_stand_app

    api = create_stand_app(Settings(
        profile="mock", hardware_mode="simulator", stand_access_token="test-token",
        mutation_auth_policy="token_required",
    ))
    api.state.lifecycle = None
    stand = api.state.stand
    app = stand.application
    sid = "browser-checkpoint"
    app.canonical_runtime.handle(CanonicalEvent(
        "AnonymousStart", sid, 1, 0, "test", {}, "start", 1.0,
    ))
    state = app.canonical_runtime.sessions[sid]
    state.mode = "story_pause_announcement"
    state.handset = "up"
    state.active_story_id = "fixed_aibolit_v1"
    app._session_epochs[sid] = 3
    app._paused_story_playback = {sid: {
        "content_id": "fixed_aibolit_v1", "playback_id": "old-play",
        "playback_lease": "old-lease", "generation": 3,
    }}
    payload = {
        "session_id": sid, "story_id": "fixed_aibolit_v1",
        "playback_id": "old-play", "playback_lease": "old-lease",
        "generation": 3, "offset_ms": 34567,
    }
    try:
        with TestClient(api) as client:
            client.headers.update({"x-stand-token": "test-token"})
            accepted = client.post("/stand/audio/checkpoint", json=payload)
            assert accepted.json()["reason"] == "story_checkpoint_updated"
            assert state.dialogue_memory.story_checkpoint["offset_ms"] == 34567
            duplicate = client.post("/stand/audio/checkpoint", json=payload)
            assert duplicate.json()["reason"] == "stale_story_checkpoint"
            assert state.dialogue_memory.story_checkpoint["offset_ms"] == 34567
            app._paused_story_playback[sid] = {
                "content_id": "fixed_aibolit_v1", "playback_id": "new-play",
                "playback_lease": "new-lease", "generation": 3,
            }
            delayed = client.post("/stand/audio/checkpoint", json={**payload, "offset_ms": 90000})
            assert delayed.json()["reason"] == "stale_story_checkpoint"
            app._session_epochs[sid] = 4
            after_hangup = client.post("/stand/audio/checkpoint", json={
                **payload, "playback_id": "new-play", "playback_lease": "new-lease",
            })
            assert after_hangup.json()["reason"] == "stale_story_checkpoint"
    finally:
        stand.close()


def test_browser_callback_route_accepts_two_executions_of_same_phrase_once_each():
    from fastapi.testclient import TestClient
    from app.foundation.stand import create_stand_app

    api = create_stand_app(Settings(
        profile="mock", runtime_mode="clinical", hardware_mode="simulator",
        stand_access_token="test-token", mutation_auth_policy="token_required",
    ))
    api.state.lifecycle = None
    stand = api.state.stand
    app = stand.application
    sid = "browser-repeated-callback"
    app.canonical_runtime.handle(CanonicalEvent(
        "AnonymousStart", sid, 1, 0, "test", {}, "start", 1.0,
    ))
    state = app.canonical_runtime.sessions[sid]
    state.mode = "wellbeing_check"
    state.handset = "up"
    try:
        with TestClient(api) as client:
            client.headers.update({"x-stand-token": "test-token"})
            for identity in ("A", "B"):
                state.active_audio_id = "PHRASE_084"
                state.playback_lease = "lease-" + identity
                app._active_audio[sid] = {
                    "content_id": "PHRASE_084", "token": 0,
                    "playback_id": "play-" + identity,
                    "playback_lease": "lease-" + identity, "generation": 0,
                }
                payload = {
                    "session_id": sid, "response_id": "PHRASE_084", "token": 0,
                    "playback_id": "play-" + identity,
                    "playback_lease": "lease-" + identity, "generation": 0,
                }
                accepted = client.post("/stand/audio/completed", json=payload)
                assert accepted.json()["reason"] != "idempotent_duplicate"
                assert accepted.json()["reason"] != "stale_audio_callback"
                if identity == "A":
                    first_payload = payload
                else:
                    assert client.post("/stand/audio/completed", json=first_payload).json()["reason"].startswith("stale_")
                assert client.post("/stand/audio/completed", json=payload).json()["reason"].startswith("stale_")
            seen = state.seen_correlations
            assert "browser-audio-complete:play-A" in seen
            assert "browser-audio-complete:play-B" in seen
    finally:
        stand.close()


def test_repeated_watchdog_failures_use_playback_execution_correlation():
    from app.foundation.domain import Event
    from app.foundation.stand import build_application

    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        sid = "watchdog-repeat"
        await app.receive_async(Event("HandsetLifted", sid, 1, 1.0, "test", {}, "lift", 0.0))
        state = app.canonical_runtime.sessions[sid]
        state.mode = "wellbeing_check"
        state.handset = "up"
        observed = []
        original_receive = app.receive_async

        async def capture(event):
            result = await original_receive(event)
            if event.source == "audio_watchdog":
                observed.append((event, result))
            return result

        app.receive_async = capture
        for identity in ("A", "B"):
            state.active_audio_id = "PHRASE_084"
            state.playback_lease = "lease-" + identity
            app._active_audio[sid] = {
                "content_id": "PHRASE_084", "token": 0,
                "playback_id": "play-" + identity,
                "playback_lease": "lease-" + identity, "generation": 0,
            }
            await app._audio_playback_watchdog(
                sid, "PHRASE_084", 0, "play-" + identity,
                "lease-" + identity, 0, 0.001,
            )
        assert len(observed) == 2
        assert observed[0][0].correlation_id != observed[1][0].correlation_id
        assert observed[0][1]["reason"] != "idempotent_duplicate"
        assert observed[1][1]["reason"] != "idempotent_duplicate"

    asyncio.run(run())


def test_two_physical_failures_for_repeated_phrase_have_distinct_canonical_ids(tmp_path):
    import struct
    import wave
    from app.foundation.domain import Event, PlaybackHandle
    from app.foundation.stand import build_application

    wav = tmp_path / "clip.wav"
    with wave.open(str(wav), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16000)
        target.writeframes(struct.pack("<h", 100))

    class FailingPlayback:
        active_token = 0

        async def play(self, _audio):
            raise RuntimeError("fake output failed")

    async def offload(call):
        return call()

    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        sid = "physical-repeat-failed"
        await app.receive_async(Event("HandsetLifted", sid, 1, 1.0, "test", {}, "lift", 0.0))
        state = app.canonical_runtime.sessions[sid]
        state.mode = "wellbeing_check"
        state.handset = "up"
        observed = []
        original_receive = app.receive_async

        async def receive(event):
            result = await original_receive(event)
            if event.source == "physical_audio":
                observed.append((event, result))
            return result

        app.receive_async = receive
        fake_stand = SimpleNamespace(
            playback=FailingPlayback(), application=app, stories={},
            commands=[], errors=[], _audio_event_sequence=0,
            _offload_audio=offload, _collect_events=lambda: None,
        )
        for identity in ("A", "B"):
            lease = "lease-" + identity
            state.active_audio_id = "PHRASE_084"
            state.playback_lease = lease
            app._register_audio_lease(sid, "PHRASE_084", 0, "play-" + identity, lease)
            handle = PlaybackHandle(sid, 0, "play-" + identity, lease, "PHRASE_084", 0)
            await HardwareIntegrationStand._play_asset_audio(
                fake_stand, sid, "PHRASE_084", str(wav), 0, 0, handle,
            )
        assert len(observed) == 2
        assert observed[0][0].correlation_id != observed[1][0].correlation_id
        assert observed[0][1]["reason"] != "idempotent_duplicate"
        assert observed[1][1]["reason"] != "idempotent_duplicate"

    asyncio.run(run())


def test_browser_pause_effect_retains_only_paused_story_execution_until_checkpoint():
    from app.foundation.domain import Event
    from app.foundation.stand import build_application

    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        sid = "pause-command-owner"
        await app.receive_async(Event("HandsetLifted", sid, 1, 1.0, "test", {}, "lift", 0.0))
        state = app.canonical_runtime.sessions[sid]
        state.mode = "fixed_story_playing"
        state.handset = "up"
        state.active_story_id = "fixed_aibolit_v1"
        state.expected_inputs = ["button_6", "button_7", "handset_down"]
        app._active_audio[sid] = {
            "content_id": "fixed_aibolit_v1", "token": 2,
            "playback_id": "story-play", "playback_lease": "story-lease",
            "generation": app._session_epochs.get(sid, 0),
        }
        app.audio.pause = lambda: None
        await app.receive_async(Event(
            "ButtonPressed", sid, 2, 1.0, "button", {"button_id": 6}, "pause", 0.0,
        ))
        assert app._paused_story_playback[sid]["playback_id"] == "story-play"
        assert app._paused_story_playback[sid]["playback_lease"] == "story-lease"
        await app.receive_async(Event("HandsetPlaced", sid, 3, 1.0, "handset", {}, "down", 0.0))
        assert sid not in app._paused_story_playback

    asyncio.run(run())


def test_each_declared_audio_effect_has_lease_and_support_uses_actual_phrase_id():
    runtime = CanonicalRuntime()
    sid = "multi-audio-owner"
    runtime.handle(CanonicalEvent("AnonymousStart", sid, 1, 0, "test", {}, "start", 1.0))
    state = runtime.sessions[sid]
    state.mode = "visual_welcome"
    state.profile_status = "identified"
    state.display_name = "Миша"
    first = runtime.handle(CanonicalEvent(
        "HandsetLifted", sid, 2, state.state_version, "test", {}, "lift", 1.0,
    ))
    spoken = [e for e in first["effects"] if e["type"] in {"speak", "speak_dynamic"}]
    assert len(spoken) == 2
    assert all(e.get("playback_lease") for e in spoken)
    assert spoken[0]["playback_lease"] != spoken[1]["playback_lease"]
    assert state.active_audio_id == "PHRASE_014"
    state.mode = "wellbeing_check"
    second = runtime.handle(CanonicalEvent(
        "ButtonPressed", sid, 3, state.state_version, "test",
        {"button_id": 5}, "help", 1.0,
    ))
    support = next(e for e in second["effects"] if e["type"] == "support_question")
    assert support["phrase_id"] == "PHRASE_084"
    assert state.active_audio_id == "PHRASE_084"
