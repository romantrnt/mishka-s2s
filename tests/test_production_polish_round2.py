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


def test_quiz_single_button_gives_two_retries_then_advances_once():
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
    # Miss 1 and 2 on single visible button -> "Попробуй ещё раз", hint stays visible
    tr1 = advance_game(state, edge="ButtonPressed", button=wrong, sequence=10, now=5.0)
    assert tr1.status == "retry"
    assert tr1.speech_text == "Попробуй ещё раз"
    assert tr1.state.narrowed_options == (ch0.answer,)
    s1 = finish_prompt(advance_game(tr1.state, edge="ButtonReleased", button=wrong, sequence=11, now=5.1).state, 5.2).state
    tr2 = advance_game(s1, edge="ButtonPressed", button=wrong, sequence=12, now=6.0)
    assert tr2.status == "retry"
    assert tr2.speech_text == "Попробуй ещё раз"
    assert tr2.state.narrowed_options == (ch0.answer,)
    s2 = finish_prompt(advance_game(tr2.state, edge="ButtonReleased", button=wrong, sequence=13, now=6.1).state, 6.2).state
    # Miss 3 on single visible button -> "Давай попробуем следующий" and advance once
    tr3 = advance_game(s2, edge="ButtonPressed", button=wrong, sequence=14, now=7.0)
    assert tr3.status == "skipped_next"
    assert tr3.speech_text.startswith("Давай попробуем следующий.")
    assert tr3.state.challenge_index == 1
    assert tr3.state.round == 1
    assert tr3.state.narrowed_options == ()


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
