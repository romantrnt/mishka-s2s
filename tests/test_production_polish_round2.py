import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.game_engine import advance as advance_game, current_challenge, finish_prompt, game_answer_buttons, start as start_game
from app.foundation.lighting import ExhibitionLightingEngine, FakeLightingOutput, LightingCommand


def _build_app():
    settings = Settings.from_env()
    led = SimpleNamespace(
        output=FakeLightingOutput(),
        exhibition_effects=None,
        clear_all=lambda transition_ms=0: led.output.clear_all(transition_ms),
        set_element=lambda eid, st, transition_ms=0: led.output.set_element(eid, st, transition_ms),
        set_elements=lambda cmds: led.output.set_elements(cmds),
    )
    led.exhibition_effects = ExhibitionLightingEngine(led.output, background_scheduler=False)
    app, _, _, _ = build_application(Path("content"), settings, led_controller=led, background_scheduler=False)
    return app, led


def test_quiz_single_button_ignores_unlit_buttons_and_accepts_lit_button():
    state = start_game("quiz", age_band="5-7", seed=1, now=1.0, immersive=True, sequence=0, selection_button=2)
    state = finish_prompt(state, 1.0).state
    ch0 = current_challenge(state)
    # 3 misses to narrow to 1 button
    for seq in range(1, 4):
        wrong = next(b for b in game_answer_buttons(state) if b != ch0.answer)
        tr = advance_game(state, edge="ButtonPressed", button=wrong, sequence=seq * 2 - 1, now=1.0 + seq)
        tr_rel = advance_game(tr.state, edge="ButtonReleased", button=wrong, sequence=seq * 2, now=1.1 + seq)
        state = finish_prompt(tr_rel.state, 1.2 + seq).state
    assert state.narrowed_options == (ch0.answer,)
    wrong = next(b for b, _ in ch0.options if b != ch0.answer)
    # Unlit buttons must remain logically ignored when only 1 hint button is lit
    tr1 = advance_game(state, edge="ButtonPressed", button=wrong, sequence=10, now=5.0)
    assert tr1.status == "ignored"
    assert tr1.state.narrowed_options == (ch0.answer,)
    s1 = advance_game(tr1.state, edge="ButtonReleased", button=wrong, sequence=11, now=5.1).state
    # Pressing the single lit button advances to the next challenge
    tr_ok = advance_game(s1, edge="ButtonPressed", button=ch0.answer, sequence=12, now=6.0)
    assert tr_ok.status == "progress"
    assert tr_ok.state.challenge_index == 1
    assert tr_ok.state.round == 1
    assert tr_ok.state.narrowed_options == ()


def test_correct_quiz_answer_button_and_voice_trigger_success_wave_without_overwrite():
    async def scenario():
        app, led = _build_app()
        sid = "quiz-success"
        await app.receive_async(Event("HandsetLifted", sid, 1, 1.0, "hid", {}, "c1", 1.0), None)
        # Enter quiz directly via canonical runtime
        st = app.canonical_runtime.sessions[sid]
        eng = start_game("quiz", age_band="5-7", seed=1, now=2.0, immersive=True, sequence=0, selection_button=2)
        eng = replace_phase(eng)
        st.mode = "game_active"
        st.active_game_id = "quiz"
        st.game_engine_state = eng
        st.game_audio_stage = "playing"
        st.active_audio_id = None
        ch = current_challenge(eng)
        res = await app.receive_async(
            Event("ButtonPressed", sid, 2, 2.0, "hid", {"button_id": ch.answer}, "c2", 2.0),
            None,
        )
        await app.receive_async(
            Event("ButtonReleased", sid, 3, 2.1, "hid", {"button_id": ch.answer}, "c2r", 2.1),
            None,
        )
        assert res["reason"] == "game_progress"
        assert led.exhibition_effects.effect == "wave"
        assert led.exhibition_effects.wave_phase == "game_success"
        # Advance wave to completion and verify next question buttons land cleanly
        origin = led.exhibition_effects.started_at_monotonic or 0.0
        for step in range(1, 10):
            led.exhibition_effects.advance(origin + step * 0.1)
        assert led.exhibition_effects.effect == "idle"
        snap = led.output.snapshot()
        assert any(v == 1 for v in snap.values())
        # Now test voice answer on the next question
        st.game_audio_stage = "playing"
        st.active_audio_id = None
        st.game_engine_state = replace_phase(st.game_engine_state)
        ch2 = current_challenge(st.game_engine_state)
        res_v = await app.receive_async(
            Event("SpeechReceived", sid, 4, 3.0, "audio", {}, "c3", 3.0),
            str(ch2.answer),
        )
        assert res_v["reason"] == "game_progress"
        assert led.exhibition_effects.wave_phase == "game_success"

    def replace_phase(eng):
        from dataclasses import replace
        return replace(eng, phase="playing")

    asyncio.run(scenario())


def test_input_retry_stops_after_one_repeat_and_skips_voice_retry_when_mic_unavailable():
    async def scenario():
        app, _ = _build_app()
        sid = "retry-guard"
        await app.receive_async(Event("HandsetLifted", sid, 1, 1.0, "hid", {}, "c1", 1.0), None)
        st = app.canonical_runtime.sessions[sid]
        st.mode = "exhibition_age_selection"
        # First retry asks once with PHRASE_074
        r1 = await app.receive_async(Event("SpeechReceived", sid, 2, 2.0, "audio", {}, "c2", 2.0), "")
        assert r1["reason"] in {"exhibition_age_unrecognized", "input_safety_retry"}
        assert any(e.get("phrase_id") == "PHRASE_074" for e in r1.get("outputs", []))
        # Second retry does NOT loop PHRASE_074; offers button hint PHRASE_075
        r2 = await app.receive_async(Event("SpeechReceived", sid, 3, 3.0, "audio", {}, "c3", 3.0), "")
        assert r2["reason"] == "exhibition_age_button_hint"
        assert any(e.get("phrase_id") == "PHRASE_075" for e in r2.get("outputs", []))
        assert not any(e.get("phrase_id") == "PHRASE_074" for e in r2.get("outputs", []))
        # With mic_unavailable=True, even first retry never asks to repeat by voice
        st.voice_retry_count = 0
        app._mic_unavailable_probe = lambda: True
        r3 = await app.receive_async(Event("SpeechReceived", sid, 4, 4.0, "audio", {}, "c4", 4.0), "")
        assert r3["reason"] == "exhibition_age_button_hint"
        assert not any(e.get("phrase_id") == "PHRASE_074" for e in r3.get("outputs", []))

    asyncio.run(scenario())


def test_submenus_never_expose_button5_help_and_silent_hold_has_no_clear_all_flicker():
    async def scenario():
        app, led = _build_app()
        sid = "submenu-and-hold"
        # Button 6 hold step 1 and step 2 must not emit clear_all() batches (all 0s)
        await app.receive_async(Event("ButtonPressed", sid, 1, 1.0, "hid", {"button_id": 6}, "c1", 1.0), None)
        await app.receive_async(Event("SilentHoldTick", sid, 2, 2.0, "timer", {"generation": 1}, "c2", 2.0), None)
        assert not any(all(cmd.state == 0 for cmd in batch) for batch in led.output.batches)

        # Enter fixed_story_menu -> all 5 stories on 1 page, button 5 plays 5th story (not wellbeing_check)
        await app.receive_async(Event("ButtonReleased", sid, 3, 2.5, "hid", {"button_id": 6}, "c2r", 2.5), None)
        await app.receive_async(Event("HandsetLifted", sid, 4, 2.6, "hid", {}, "c2h", 2.6), None)
        st = app.canonical_runtime.sessions[sid]
        st.mode = "menu_ready"
        r_menu = await app.receive_async(Event("ButtonPressed", sid, 5, 3.0, "hid", {"button_id": 2}, "c3", 3.0), None)
        await app.receive_async(Event("ButtonReleased", sid, 6, 3.1, "hid", {"button_id": 2}, "c4", 3.1), None)
        desc = st.scene_menu_descriptor
        assert desc["buttons"] == [1, 2, 3, 4, 5, 7]
        assert "помощь" not in desc["spoken_text"].lower()
        r_story5 = await app.receive_async(Event("ButtonPressed", sid, 7, 4.0, "hid", {"button_id": 5}, "c5", 4.0), None)
        assert r_story5["reason"] == "fixed_story_started"
        assert st.mode == "fixed_story_playing"
        assert st.active_story_id == "fixed_neznayka_v1"

    asyncio.run(scenario())


def test_s2s_transfer_preserves_dialogue_history_and_reopens_capture():
    async def scenario():
        app, _ = _build_app()
        sid = "s2s-transfer"
        await app.receive_async(Event("HandsetLifted", sid, 1, 1.0, "hid", {}, "c1", 1.0), None)
        st = app.canonical_runtime.sessions[sid]
        st.mode = "bear_dialogue"
        ctx = app._bounded_dialogue_context(sid)
        ctx.history.append(("Привет", "Привет! Как дела?"))
        app.conversation_contexts[sid] = ctx

        # Open game_menu from bear_dialogue and press 7 -> returns to bear_dialogue
        r_cat = await app._receive_canonical_event(
            Event("DialogueFallback", sid, 2, 2.0, "internal", {"reason": "catalogue_request", "action": "open_catalog", "target": "games"}, "c2", 2.0),
            dialogue_fallback_authorized=True,
        )
        assert st.mode == "game_menu"
        assert st.activity_return_mode == "bear_dialogue"
        r_back = await app.receive_async(Event("ButtonPressed", sid, 3, 3.0, "hid", {"button_id": 7}, "c3", 3.0), None)
        assert r_back["reason"] == "game_menu_back"
        assert st.mode == "bear_dialogue"

        # Start word_game from bear_dialogue -> preserves suspended dialogue history, restores on exit
        app.canonical_runtime._start_game(st, "word_game", 4.0, from_dialogue=True)
        w_ctx = app._bounded_dialogue_context(sid, game_id="word_game")
        assert w_ctx._game_id == "word_game"
        assert sid in app._suspended_dialogue_contexts
        res_exit = app.canonical_runtime._game_exit(st, "word_game_finished", preserve_output=True)
        assert st.mode == "bear_dialogue"
        assert any(e.get("type") == "capture_open" for e in res_exit["effects"])
        restored = app._bounded_dialogue_context(sid)
        assert restored.history == [("Привет", "Привет! Как дела?")]

        # request_information AudioFailed in bear_dialogue still reopens capture_open
        st.dialogue_information_kind = "weather"
        st.active_audio_id = "TYPED_WEATHER"
        r_fail = await app._receive_canonical_event(
            Event("AudioFailed", sid, 5, 6.0, "audio", {
                "response_id": "TYPED_WEATHER",
                "playback_lease": st.playback_lease,
            }, "c5", 6.0),
        )
        assert r_fail["reason"] == "typed_information_finished"
        assert any(e.get("type") == "capture_open" for e in r_fail.get("outputs", []))

    asyncio.run(scenario())


def test_repeat_pattern_uses_button_7_and_lively_exit_hints():
    for band in ("3-5", "5-7"):
        eng = start_game("repeat_pattern", age_band=band, seed=1, now=1.0, immersive=True, sequence=0, selection_button=3)
        assert 7 in game_answer_buttons(eng)
    app, _ = _build_app()
    st = app.canonical_runtime._new_state("exit-hints", None)
    st.mode = "game_active"
    st.game_engine_state = start_game("quiz", age_band="5-7", seed=1, now=1.0, immersive=True, sequence=0, selection_button=2)
    q_prompt = app.canonical_runtime._game_prompt(st)
    assert "зажми семёрку" in q_prompt["rules_intro"].lower()
    st.game_engine_state = start_game("free_lights", age_band="5-7", seed=1, now=1.0, immersive=True, sequence=0, selection_button=1)
    f_prompt = app.canonical_runtime._game_prompt(st)
    assert "зажми семёрку" in f_prompt["exit_hint"].lower()
