from __future__ import annotations
from app.foundation.game_engine import GameState

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
            (4, "personal_story_theme", "PERSONAL_STORY"),
            (7, "schedule_weather", "SCHEDULE_WEATHER"),
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
            if target in {"game_menu", "fixed_story_menu", "personal_story_theme", "schedule_weather"}:
                page = result["canonical_state"]["scene_menu_descriptor"]
                assert any(effect.get("type") == "scene_menu_page"
                           and effect.get("descriptor") == page for effect in result["outputs"])
            else:
                assert any(effect.get("type") == "lighting_preset"
                           and effect.get("preset") == preset for effect in result["outputs"])
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


@pytest.mark.parametrize("level", [1, 5])
def test_game_unreachable_target_is_not_presented_as_playable(level):
    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="clinical"))
        sid = "game-target-control"
        app.canonical_runtime.sessions[sid] = CanonicalState(
            sid, mode="game_active", handset="up", game_target=1, game_level=level, game_engine_state=GameState("catch_light"),
            game_success_streak=1,
        )
        result = await app.receive_async(_event("ButtonPressed", sid, 1, {"button_id": 1}))
        assert not any(effect.get("type") == "lighting_preset"
                       and effect.get("preset") == "TARGET"
                       and effect.get("target_button") in {5, 7}
                       for effect in result["outputs"])
        assert result["canonical_state"]["game_target"] not in {5, 7}
    asyncio.run(run())


@pytest.mark.parametrize("trigger", ["success", "mistake", "timeout"])
def test_game_targets_remain_reachable_after_progress_and_downgrade(trigger):
    from app.foundation.canonical_contract import CanonicalEvent, CanonicalRuntime
    for level in range(1, 10):
        runtime = CanonicalRuntime()
        state = CanonicalState("game", mode="game_active", handset="up", game_level=level,
                               game_target=1, game_success_streak=1, game_attempts=2, game_engine_state=GameState("catch_light"))
        runtime.sessions["game"] = state
        request = CanonicalEvent("TimeoutFired" if trigger == "timeout" else "ButtonPressed",
            "game", 1, state.state_version, "test", {"button_id": 1 if trigger == "success" else 2})
        result = runtime.handle(request)
        if state.mode != "game_active":
            assert result["reason"] == "game_completed"
            continue
        assert state.game_target in {1, 2, 3, 4, 6}
        scored = runtime.handle(CanonicalEvent("ButtonPressed", "game", 2, state.state_version,
                                               "test", {"button_id": state.game_target}))
        assert scored["reason"] in {"game_progress", "game_completed"}


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
            sid, mode="game_active", handset="up", game_target=1, game_level=0, game_engine_state=GameState("catch_light"),
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
            normal, mode="game_active", handset="up", game_target=1, game_level=0, game_engine_state=GameState("catch_light"),
        )
        progressed = await app.receive_async(_event("ButtonPressed", normal, 1, {"button_id": 1}))
        assert progressed["reason"] == "game_progress"
        assert progressed["canonical_state"]["game_engine_state"]["round"] == 1
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


def test_submenu_exit_seven_is_digit_only_and_game_menu_turns_off_six():
    from app.foundation.canonical_services import LED_PRESETS
    from app.foundation.lighting import BUTTON_DIGIT_ELEMENTS, BUTTON_FILL_ELEMENTS, PICTOGRAM_ELEMENTS
    game_menu = dict(LED_PRESETS["GAME_MENU"])
    for b in range(1, 6):
        assert game_menu.get(BUTTON_FILL_ELEMENTS[b]) == 1
        assert game_menu.get(BUTTON_DIGIT_ELEMENTS[b][1]) == 1
    assert BUTTON_FILL_ELEMENTS[6] not in game_menu
    assert BUTTON_DIGIT_ELEMENTS[6][1] not in game_menu
    assert PICTOGRAM_ELEMENTS[6] not in game_menu
    for preset_name in ("GAME_MENU", "STORY_MENU", "SUPPORT", "PERSONAL_STORY", "SCHEDULE_WEATHER", "GAME_OFFER"):
        preset = dict(LED_PRESETS[preset_name])
        assert preset.get(BUTTON_DIGIT_ELEMENTS[7][1]) == 1, preset_name
        assert BUTTON_FILL_ELEMENTS[7] not in preset, preset_name
        assert PICTOGRAM_ELEMENTS[7] not in preset, preset_name


def test_scene_menu_page_lights_exit_seven_as_digit_only():
    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", runtime_mode="exhibition"))
        sid = "submenu-digit-7"
        from app.foundation.canonical_contract import CanonicalState
        from app.foundation.lighting import BUTTON_DIGIT_ELEMENTS, BUTTON_FILL_ELEMENTS, PICTOGRAM_ELEMENTS
        app.canonical_runtime.sessions[sid] = CanonicalState(sid, mode="menu_ready", handset="up")
        await app.receive_async(_event("ButtonPressed", sid, 1, {"button_id": 1}))
        snapshot = app.led.output.snapshot()
        assert snapshot[str(BUTTON_DIGIT_ELEMENTS[7][1])] == 1
        assert snapshot[str(BUTTON_FILL_ELEMENTS[7])] == 0
        assert snapshot[str(PICTOGRAM_ELEMENTS[7])] == 0
        assert snapshot[str(BUTTON_FILL_ELEMENTS[6])] == 0
        for b in range(1, 6):
            assert snapshot[str(BUTTON_FILL_ELEMENTS[b])] == 1
            assert snapshot[str(BUTTON_DIGIT_ELEMENTS[b][1])] == 1
    asyncio.run(run())
