"""Synthetic ingress and stale lifecycle-owner regressions."""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app.foundation.config import Settings
from app.foundation.events import EventValidationError, parse_event
from app.foundation.lifecycle import StartupLifecycle
from app.foundation.stand import create_stand_app


INVALID_HANDSET_STATES = (None, "invalid", "UP", "", False, True, 0, 1, {}, [])


@pytest.mark.parametrize("state", INVALID_HANDSET_STATES)
def test_handset_ingress_rejects_invalid_state(state):
    with pytest.raises(EventValidationError, match="handset state"):
        parse_event({"type": "handset", "state": state}, "audit", 1)


def test_handset_ingress_requires_state_and_preserves_explicit_aliases():
    with pytest.raises(EventValidationError, match="handset state"):
        parse_event({"type": "handset"}, "audit", 1)
    for raw, expected in (("up", "HandsetLifted"), ("down", "HandsetPlaced")):
        assert parse_event({"type": "handset", "state": raw}, "audit", 1).event_type == expected
    for raw, expected in (("handset_up", "HandsetLifted"), ("handset_down", "HandsetPlaced"),
                          ("SpaceReleased", "HandsetLifted"), ("SpacePressed", "HandsetPlaced")):
        assert parse_event({"type": raw}, "audit", 1).event_type == expected


@pytest.mark.parametrize("replacement", ["dependency", "recovering", "ready", "failed", "shutdown"])
def test_cancelled_warmup_cannot_rewrite_newer_lifecycle(replacement):
    async def scenario():
        gate = StartupLifecycle()
        entered = asyncio.Event()

        async def warmup():
            entered.set()
            await asyncio.Event().wait()

        await gate.start(warmup)
        await entered.wait()
        task = gate._warmup
        gate.dependency_failed("synthetic", "lost")
        if replacement in {"recovering", "ready", "failed"}:
            generation = await gate.begin_recovery("synthetic", "audit-recovery")
            if replacement != "recovering":
                gate.finish_recovery(generation, {"ok": replacement == "ready", "e2e": {"ok": True},
                                                  "reason": "synthetic_recovery_failed"})
        if replacement == "shutdown":
            gate.begin_shutdown()
        before = gate.snapshot()
        with pytest.raises(asyncio.CancelledError):
            await task
        after = gate.snapshot()
        for field in ("state", "reason", "changed_monotonic", "last_degraded", "generation", "recovery"):
            assert after[field] == before[field], field
        assert gate.admits_users() is (replacement == "ready")

    asyncio.run(scenario())


def test_current_warmup_cancellation_still_degrades():
    async def scenario():
        gate = StartupLifecycle()
        entered = asyncio.Event()

        async def warmup():
            entered.set()
            await asyncio.Event().wait()

        await gate.start(warmup)
        await entered.wait()
        gate._warmup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await gate._warmup
        assert gate.snapshot()["state"] == "DEGRADED"
        assert gate.snapshot()["reason"] == "warmup_cancelled"
        assert not gate.admits_users()

    asyncio.run(scenario())


def test_malformed_handset_http_and_pty_leave_active_session_intact(monkeypatch):
    token = "audit-synthetic-token"
    monkeypatch.setenv("STAND_ACCESS_TOKEN", token)
    api = create_stand_app(Settings(profile="mock", runtime_mode="exhibition", hardware_mode="simulator",
                                    stand_access_token=token))
    # Synthetic control admission only: no real model warmup or hardware.
    monkeypatch.setattr(api.state.lifecycle, "admits_users", lambda: True)
    sid = "audit-handset"
    stand = api.state.stand
    try:
        with TestClient(api) as client:
            headers = {"x-stand-token": token}
            lifted = client.post("/stand/handset", json={"session_id": sid, "state": "up"}, headers=headers)
            assert lifted.status_code == 200
            assert lifted.json()["reason"] == "exhibition_handset_lifted"
            owner = stand.application.canonical_runtime.sessions[sid]
            before = (owner.mode, owner.handset, owner.state_version)
            payloads = [{"session_id": sid}] + [
                {"session_id": sid, "state": state} for state in INVALID_HANDSET_STATES
            ]
            for payload in payloads:
                response = client.post("/stand/handset", json=payload, headers=headers)
                assert response.status_code == 422, response.json()
                assert response.json()["error"]["code"] == "validation_error"
                assert stand.application.canonical_runtime.sessions[sid] is owner
                assert (owner.mode, owner.handset, owner.state_version) == before
            placed = client.post("/stand/handset", json={"session_id": sid, "state": "down"}, headers=headers)
            assert placed.status_code == 200 and placed.json()["reason"] == "exhibition_reset"
            assert sid not in stand.application.canonical_runtime.sessions
            assert not any(stand.lighting_output.snapshot().values())
            lifted_again = client.post("/stand/handset", json={"session_id": sid, "state": "up"}, headers=headers)
            assert lifted_again.status_code == 200
            assert lifted_again.json()["reason"] == "exhibition_handset_lifted"
            assert stand.application.canonical_runtime.sessions[sid] is not owner
    finally:
        stand.close()


@pytest.mark.parametrize("event_type", ["text", "SpeechReceived"])
def test_stale_typed_speech_keeps_version_fence_without_persisting_text(event_type):
    from types import SimpleNamespace
    from app.foundation.api import build_application

    async def scenario():
        app, *_ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"),
                                     background_scheduler=False)
        calls = []

        async def classify(text):
            calls.append(text)
            return SimpleNamespace(reason="synthetic", severity="none", verified=True)

        app.safety_classifier = SimpleNamespace(classify=classify)
        sid = "audit-speech-version"
        try:
            await app.receive_async(parse_event({"type": "handset", "state": "up"}, sid, 1))
            owner = app.canonical_runtime.sessions[sid]
            current_version = owner.state_version
            event = parse_event({"type": event_type, "text": "привет", "state_version": current_version - 1,
                                 "private_extra": "must not survive"}, sid, 2)
            assert event.payload == {"length": 6, "state_version": current_version - 1}
            result = await app.receive_async(event, "привет")
            assert result["reason"] == "stale_state_version"
            assert not calls and owner.state_version == current_version
            compatible = parse_event({"type": event_type, "text": "привет"}, sid, 3)
            assert compatible.payload == {"length": 6}
            await app.receive_async(compatible, "привет")
            assert calls == ["привет"]
        finally:
            await app._cleanup_session_resources(sid)

    asyncio.run(scenario())


@pytest.mark.parametrize("limit", [3, 200, 256])
def test_watchdog_history_uses_its_configured_bound_after_many_events(limit):
    from app.foundation.observability import Metrics
    metrics = Metrics(watchdog_limit=limit)
    for index in range(300):
        metrics.record_watchdog_event({"index": index})
    assert len(metrics.watchdog_events) == limit
    assert [event["index"] for event in metrics.watchdog_events] == list(range(300 - limit, 300))


def test_real_watchdog_loop_survives_more_than_two_hundred_observations():
    from app.foundation.api import build_application

    async def scenario():
        settings = Settings(profile="mock", runtime_mode="exhibition", observability_watchdog_limit=256).validate()
        app, *_ = build_application(settings=settings, background_scheduler=False)
        app.watchdog_interval_seconds = 0
        task = asyncio.create_task(app._watchdog_loop())
        try:
            for _ in range(300):
                await asyncio.sleep(0)
            assert not task.done(), task.exception() if task.done() else None
            assert len(app.metrics.watchdog_events) == 256
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await app.shutdown_identity()

    asyncio.run(scenario())



@pytest.mark.parametrize("game", ["catch_light", "hold_light"])
@pytest.mark.parametrize("terminal", ["AudioCompleted", "AudioFailed"])
@pytest.mark.parametrize("extra_releases", [0, 3])
def test_game_completion_after_releases_closes_only_its_owned_audio(monkeypatch, game, terminal, extra_releases):
    import app.foundation.canonical_contract as contract
    from app.foundation.api import build_application
    from app.foundation.domain import Event
    from app.foundation.game_engine import AVAILABLE_GAME_SPECS

    # Keep menu paging out of this regression, as existing game mechanic tests do.
    monkeypatch.setattr(contract, "AVAILABLE_GAME_SPECS", tuple(
        spec for spec in AVAILABLE_GAME_SPECS if spec.game_id == game))

    async def scenario():
        clock = [100.]
        app, *_ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"),
                                     monotonic_now=lambda: clock[0], background_scheduler=False)
        sequence = 0
        sid = "audit-game-completion"

        async def send(kind, button=None, payload=None):
            nonlocal sequence
            sequence += 1
            if payload is None:
                payload = {} if button is None else {"button_id": button}
            return await app.receive_async(Event(kind, sid, sequence, clock[0], "hid",
                                                 payload, str(sequence), clock[0]))

        try:
            await send("HandsetLifted")
            for _ in range(3):
                await send("ButtonPressed", 1)
                await send("ButtonReleased", 1)
            assert app.canonical_runtime.sessions[sid].mode == "game_active"
            if game == "hold_light":
                await send("ButtonPressed", 1)
                clock[0] += .1
                assert (await send("ButtonReleased", 1))["reason"] == "game_early_release"
                assert app.canonical_runtime.sessions[sid].game_engine_state.round == 0
            for round_index in range(8):
                target = app.canonical_runtime.sessions[sid].game_engine_state.target
                pressed = await send("ButtonPressed", target)
                clock[0] += 1.1
                released = await send("ButtonReleased", target)
                result = released if game == "hold_light" else pressed
                assert result["reason"] == ("game_completed" if round_index == 7 else "game_progress")
            state = app.canonical_runtime.sessions[sid]
            assert state.mode == "root_menu" and state.game_engine_state is None
            frame = app.led.output.snapshot()
            completed = app._active_audio[sid]
            assert completed["content_id"] == "PHRASE_028"
            for _ in range(extra_releases):
                await send("ButtonReleased", target)
                assert state.mode == "root_menu" and state.game_engine_state is None
                assert app.led.output.snapshot() == frame
                assert app._active_audio[sid] is completed
            payload = {"response_id": completed["content_id"], **{
                key: completed[key] for key in ("token", "generation", "playback_id", "playback_lease")}}
            await send(terminal, payload={**payload, "playback_lease": "stale-audit-lease"})
            assert app._active_audio[sid] is completed and state.active_audio_id == "PHRASE_028"
            result = await send(terminal, payload=payload)
            assert result["reason"] == ("game_instruction_audio_finished" if terminal == "AudioCompleted"
                                        else "game_instruction_audio_failed")
            assert state.mode == "root_menu" and state.active_audio_id is None
            assert sid not in app._active_audio
            assert "button" in state.expected_inputs
            assert app.led.output.snapshot() == frame
            await send("ButtonPressed", 6)
            replacement = app._active_audio[sid]
            await send(terminal, payload=payload)
            assert app._active_audio[sid] is replacement
            assert state.mode == "bear_dialogue" and state.active_audio_id == replacement["content_id"]
        finally:
            await app._cleanup_session_resources(sid)

    asyncio.run(scenario())


def test_game_release_authorizer_allows_only_the_terminal_menu_edge():
    from app.scenarios.canonical_graph import CanonicalLangGraphAuthorizer
    graph = CanonicalLangGraphAuthorizer()
    assert graph.authorize("game_active", "root_menu", "ButtonReleased")["ok"]
    for event in ("ButtonHeld", "ButtonLongHeld"):
        assert not graph.authorize("game_active", "root_menu", event)["ok"]
    assert not graph.authorize("game_menu", "game_active", "ButtonReleased")["ok"]
    assert not graph.authorize("bear_dialogue", "root_menu", "ButtonReleased")["ok"]
    assert not graph.authorize("game_active", "fixed_story_menu", "ButtonReleased")["ok"]
