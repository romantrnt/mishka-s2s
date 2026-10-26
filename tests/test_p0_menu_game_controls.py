from __future__ import annotations

import asyncio

import pytest

from app.foundation.api import build_application
from app.foundation.canonical_contract import CanonicalState
from app.foundation.config import Settings
from app.foundation.domain import Event


def _event(kind, sid, sequence, payload=None):
    return Event(kind, sid, sequence, float(sequence), "test", payload or {},
                 f"{sid}-{sequence}", float(sequence))


def test_exhibition_menu_existing_use_cases_pass_application_and_graph():
    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="exhibition"))
        for button, target, preset in (
            (1, "game_menu", "GAME_MENU"),
            (3, "support_intake", "SUPPORT"),
            (5, "wellbeing_check", "HELP"),
            (2, "fixed_story_menu", "STORY_MENU"),
            (6, "bear_dialogue", "BEAR_DIALOGUE"),
        ):
            sid = f"menu-{button}"
            app.canonical_runtime.sessions[sid] = CanonicalState(
                sid, mode="menu_ready", handset="up",
            )
            result = await app.receive_async(_event("ButtonPressed", sid, 1, {"button_id": button}))
            assert result["canonical_state"]["mode"] == target
            assert any(effect.get("type") == "exhibition_menu_selected"
                       and effect.get("button_number") == button
                       and effect.get("target_state") == target
                       for effect in result["outputs"])
            assert any(effect.get("type") == "lighting_preset"
                       and effect.get("preset") == preset for effect in result["outputs"])
        sid = "menu-four"
        app.canonical_runtime.sessions[sid] = CanonicalState(sid, mode="menu_ready", handset="up")
        fourth = await app.receive_async(_event("ButtonPressed", sid, 1, {"button_id": 4}))
        assert fourth["canonical_state"]["mode"] == "menu_ready"
        assert not any(effect.get("type") == "generate_personal_story" for effect in fourth["outputs"])
    asyncio.run(run())


def test_exhibition_menu_support_requires_handset_like_root_menu():
    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="exhibition"))
        sid = "menu-support-down"
        app.canonical_runtime.sessions[sid] = CanonicalState(sid, mode="menu_ready", handset="down")
        # Application rejects all physical button input for a handset-down
        # exhibition session before routing; exercise the canonical guard
        # directly for this otherwise unreachable seeded state.
        from app.foundation.canonical_contract import CanonicalEvent
        result = app.canonical_runtime.handle(CanonicalEvent(
            "ButtonPressed", sid, 1, 0, "test", {"button_id": 3},
            "menu-support-down-1", 1.0, 1.0,
        ))
        assert result["state"]["mode"] == "awaiting_handset"
        assert result["state"]["pending_mode"] == "support_intake"
        assert any(effect.get("type") == "exhibition_menu_selected" for effect in result["effects"])
    asyncio.run(run())


def test_game_exit_confirmation_survives_release_hold_timeout_and_confirm():
    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        sid = "game-confirm"
        app.canonical_runtime.sessions[sid] = CanonicalState(
            sid, mode="game_active", handset="up", game_target=1,
        )
        first = await app.receive_async(_event("ButtonPressed", sid, 1, {"button_id": 7}))
        assert first["canonical_state"]["substate"] == "game_exit_confirmation"
        for sequence, kind, payload in (
            (2, "ButtonReleased", {"button_id": 7}),
            (3, "ButtonHeld", {"button_id": 7, "duration_ms": 100}),
            (4, "TimeoutFired", {}),
        ):
            result = await app.receive_async(_event(kind, sid, sequence, payload))
            assert result["canonical_state"]["substate"] == "game_exit_confirmation"
            assert result["canonical_state"]["game_attempts"] == 0
            assert result["canonical_state"]["game_level"] == 0
        confirmed = await app.receive_async(_event("ButtonPressed", sid, 5, {"button_id": 7}))
        assert confirmed["canonical_state"]["mode"] == "root_menu"
        assert confirmed["reason"] == "game_exit_confirmed"
    asyncio.run(run())


def test_game_exit_confirmation_can_be_cancelled_after_release():
    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        sid = "game-cancel"
        app.canonical_runtime.sessions[sid] = CanonicalState(sid, mode="game_active", handset="up", game_target=1)
        await app.receive_async(_event("ButtonPressed", sid, 1, {"button_id": 7}))
        await app.receive_async(_event("ButtonReleased", sid, 2, {"button_id": 7}))
        result = await app.receive_async(_event("ButtonPressed", sid, 3, {"button_id": 6}))
        assert result["canonical_state"]["mode"] == "game_active"
        assert result["reason"] == "game_exit_cancelled"
        assert result["canonical_state"]["game_attempts"] == 0
    asyncio.run(run())


@pytest.mark.xfail(strict=True, reason="unsupported target 5/7 requires approved unavailable-step cue")
@pytest.mark.parametrize("level", [1, 5])
def test_game_unreachable_target_is_not_presented_as_playable(level):
    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        sid = "game-target-control"
        app.canonical_runtime.sessions[sid] = CanonicalState(
            sid, mode="game_active", handset="up", game_target=1, game_level=level,
            game_success_streak=1,
        )
        result = await app.receive_async(_event("ButtonPressed", sid, 1, {"button_id": 1}))
        assert not any(effect.get("type") == "lighting_preset"
                       and effect.get("preset") == "TARGET"
                       and effect.get("target_button") in {5, 7}
                       for effect in result["outputs"])
        assert result["canonical_state"]["game_target"] not in {5, 7}
    asyncio.run(run())


def test_game_ack_fix_does_not_change_story_or_onboarding_release():
    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="exhibition"))
        for sid, mode in (("story-release", "fixed_story_playing"),
                          ("onboarding-release", "exhibition_age_selection")):
            app.canonical_runtime.sessions[sid] = CanonicalState(sid, mode=mode, handset="up")
            result = await app.receive_async(_event("ButtonReleased", sid, 1, {"button_id": 6}))
            assert result["canonical_state"]["mode"] == mode
            assert result["reason"] == "button_edge_ack"
            assert result["canonical_state"]["substate"] == "button_edge_ack"
    asyncio.run(run())


def test_game_target_is_ignored_while_exit_confirmation_waits_for_7_or_6():
    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        sid = "game-pending-target"
        app.canonical_runtime.sessions[sid] = CanonicalState(
            sid, mode="game_active", handset="up", game_target=1, game_level=0,
        )
        await app.receive_async(_event("ButtonPressed", sid, 1, {"button_id": 7}))
        blocked = await app.receive_async(_event("ButtonPressed", sid, 2, {"button_id": 1}))
        assert blocked["canonical_state"]["mode"] == "game_active"
        assert blocked["canonical_state"]["substate"] == "game_exit_confirmation"
        assert blocked["canonical_state"]["game_success_streak"] == 0
        assert blocked["canonical_state"]["game_attempts"] == 0
        assert not any(effect.get("type") in {"game_level", "game_retry"} for effect in blocked["outputs"])
        confirmed = await app.receive_async(_event("ButtonPressed", sid, 3, {"button_id": 7}))
        assert confirmed["reason"] == "game_exit_confirmed"

        normal = "game-normal-target"
        app.canonical_runtime.sessions[normal] = CanonicalState(
            normal, mode="game_active", handset="up", game_target=1, game_level=0,
        )
        progressed = await app.receive_async(_event("ButtonPressed", normal, 1, {"button_id": 1}))
        assert progressed["reason"] == "game_progress"
        assert progressed["canonical_state"]["game_success_streak"] == 1
    asyncio.run(run())


def test_game_pending_exit_keeps_help_priority_and_hangup_cleanup():
    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        help_sid = "game-pending-help"
        app.canonical_runtime.sessions[help_sid] = CanonicalState(
            help_sid, mode="game_active", handset="up", game_target=1,
        )
        await app.receive_async(_event("ButtonPressed", help_sid, 1, {"button_id": 7}))
        helped = await app.receive_async(_event("ButtonPressed", help_sid, 2, {"button_id": 5}))
        assert helped["reason"] == "help_priority"
        assert helped["canonical_state"]["mode"] == "wellbeing_check"
        assert helped["canonical_state"]["game_success_streak"] == 0

        down_sid = "game-pending-hangup"
        app.canonical_runtime.sessions[down_sid] = CanonicalState(
            down_sid, mode="game_active", handset="up", game_target=1,
        )
        await app.receive_async(_event("ButtonPressed", down_sid, 1, {"button_id": 7}))
        down = await app.receive_async(_event("HandsetPlaced", down_sid, 2))
        # Clinical handset-down preserves game position for a visual continue,
        # while stopping capture/audio; this patch must not turn it into score.
        assert down["canonical_state"]["handset"] == "down"
        assert down["canonical_state"]["game_success_streak"] == 0
        assert any(effect.get("type") == "game_continue_visual" for effect in down["outputs"])
    asyncio.run(run())
