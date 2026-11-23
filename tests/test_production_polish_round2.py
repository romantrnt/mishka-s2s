import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.foundation.application import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.game_engine import advance as advance_game, current_challenge, start as start_game
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
    state = start_game("quiz", age_band="5-7", seed=1, now=1.0, immersive=True)
    ch0 = current_challenge(state)
    wrong = next(b for b, _ in ch0.options if b != ch0.answer)
    # 3 misses to narrow to 1 button
    for seq in range(1, 4):
        tr = advance_game(state, edge="ButtonPressed", button=wrong, sequence=seq, now=1.0 + seq)
        state = tr.state
    assert state.narrowed_options == (ch0.answer,)
    # Miss 1 and 2 on single visible button -> "Попробуй ещё раз", hint stays visible
    tr1 = advance_game(state, edge="ButtonPressed", button=wrong, sequence=4, now=5.0)
    assert tr1.status == "retry"
    assert tr1.speech_text == "Попробуй ещё раз"
    assert tr1.state.narrowed_options == (ch0.answer,)
    tr2 = advance_game(tr1.state, edge="ButtonPressed", button=wrong, sequence=5, now=6.0)
    assert tr2.status == "retry"
    assert tr2.speech_text == "Попробуй ещё раз"
    assert tr2.state.narrowed_options == (ch0.answer,)
    # Miss 3 on single visible button -> "Давай попробуем следующий" and advance once
    tr3 = advance_game(tr2.state, edge="ButtonPressed", button=wrong, sequence=6, now=7.0)
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
        eng = start_game("quiz", age_band="5-7", seed=1, now=2.0, immersive=True)
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
        assert res["reason"] == "game_progress"
        assert led.exhibition_effects.effect == "wave"
        assert led.exhibition_effects.wave_phase == "game_success"
        # Advance wave to completion and verify next question buttons land cleanly
        for step in range(1, 10):
            led.exhibition_effects.advance(2.0 + step * 0.1)
        assert led.exhibition_effects.effect == "idle"
        snap = led.output.snapshot()
        assert any(v == 1 for v in snap.values())
        # Now test voice answer on the next question
        st.game_audio_stage = "playing"
        st.active_audio_id = None
        ch2 = current_challenge(st.game_engine_state)
        res_v = await app.receive_async(
            Event("SpeechReceived", sid, 3, 3.0, "audio", {}, "c3", 3.0),
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
        assert r1["reason"] == "input_safety_retry"
        assert any(e.get("phrase_id") == "PHRASE_074" for e in r1["effects"])
        # Second retry does NOT loop PHRASE_074; offers button hint PHRASE_075
        r2 = await app.receive_async(Event("SpeechReceived", sid, 3, 3.0, "audio", {}, "c3", 3.0), "")
        assert r2["reason"] == "exhibition_age_button_hint"
        assert any(e.get("phrase_id") == "PHRASE_075" for e in r2["effects"])
        assert not any(e.get("phrase_id") == "PHRASE_074" for e in r2["effects"])
        # With mic_unavailable=True, even first retry never asks to repeat by voice
        st.voice_retry_count = 0
        app._mic_unavailable_probe = lambda: True
        r3 = await app.receive_async(Event("SpeechReceived", sid, 4, 4.0, "audio", {}, "c4", 4.0), "")
        assert r3["reason"] == "exhibition_age_button_hint"
        assert not any(e.get("phrase_id") == "PHRASE_074" for e in r3["effects"])

    asyncio.run(scenario())
