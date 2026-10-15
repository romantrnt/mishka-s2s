import asyncio

from app.foundation.api import build_application
from app.foundation.canonical_contract import (
    CanonicalEvent,
    CanonicalRuntime,
    normalize_exhibition_age,
    normalize_exhibition_name,
)
from app.foundation.config import Settings
from app.foundation.domain import Event


def ce(kind, seq, version, source="test", payload=None, monotonic=10.0):
    return CanonicalEvent(kind, "gr03", seq, version, source, payload or {}, f"{source}-{seq}", 100.0, monotonic)


def to_name_capture(runtime):
    assert runtime.handle(ce("HandsetLifted", 1, 0, "hid"))["reason"] == "exhibition_handset_lifted"
    assert runtime.handle(ce("WelcomeTimerFired", 2, 1, "timer", monotonic=15.0))["reason"] == "welcome_started"
    assert runtime.handle(ce("AudioCompleted", 3, 2, "audio", {"response_id": "PHRASE_001"}))["reason"] == "exhibition_introduction_started"
    state = runtime.sessions["gr03"]
    assert runtime.handle(ce("AudioCompleted", 4, state.state_version, "audio", {"response_id": "PHRASE_078"}))["reason"] == "exhibition_name_question_started"
    state = runtime.sessions["gr03"]
    result = runtime.handle(ce("AudioCompleted", 5, state.state_version, "audio", {"response_id": "PHRASE_099"}))
    assert result["reason"] == "exhibition_name_capture_started"
    return runtime


def to_age_selection(runtime, name="Меня зовут Аня"):
    to_name_capture(runtime)
    state = runtime.sessions["gr03"]
    accepted = runtime.handle(
        ce("VoiceIntentReady", 6, state.state_version, "speech", {"intent": "free_speech", "safety_severity": "none"}),
        transient_text=name,
    )
    assert accepted["reason"] == "exhibition_age_question_started"
    state = runtime.sessions["gr03"]
    hint = runtime.handle(ce("AudioCompleted", 7, state.state_version, "audio", {"response_id": "PHRASE_108"}))
    assert hint["reason"] == "exhibition_age_hint_started"
    state = runtime.sessions["gr03"]
    result = runtime.handle(ce("AudioCompleted", 8, state.state_version, "audio", {"response_id": "PHRASE_075"}))
    assert result["reason"] == "exhibition_age_selection_started"
    return runtime


def test_current_exhibition_onboarding_and_age_button_contract():
    for button, band in ((1, "1-3"), (4, "4-5"), (6, "6-7")):
        runtime = to_age_selection(CanonicalRuntime(exhibition_mode=True, monotonic_now=lambda: 10.0))
        state = runtime.sessions["gr03"]
        selected = runtime.handle(ce("ButtonPressed", 9, state.state_version, "hid", {"button_id": button}))
        assert selected["reason"] == "exhibition_age_selected"
        assert selected["state"]["mode"] == "exhibition_age_complete"
        assert selected["state"]["age_years"] == button
        assert selected["state"]["age_band"] == band
        assert selected["state"]["age_band_source"] == "exhibition_button"
        assert selected["state"]["age_policy"]["band"] == band
        assert [x["phrase_id"] for x in selected["effects"] if x["type"] == "speak"] == ["PHRASE_107"]
        state = runtime.sessions["gr03"]
        ready = runtime.handle(ce("AudioCompleted", 10, state.state_version, "audio", {"response_id": "PHRASE_107"}))
        assert ready["reason"] == "exhibition_onboarding_ready"
        assert ready["state"]["substate"] == "exhibition_onboarding_ready"


def test_speech_pipeline_privacy_reset_and_stale_audio():
    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock", content_root="content"))
        clock = [10.0]
        app.monotonic_now = lambda: clock[0]
        app.canonical_runtime.monotonic_now = app.monotonic_now
        sid = "gr03-app"
        await app.receive_async(Event("HandsetLifted", sid, 1, 100.0, "hid", {}, "lift", 10.0))
        clock[0] = 15.0
        await app.flush_welcome_timers()
        token = app.sessions.get(sid).cancellation_token
        lease = dict(app._active_audio[sid])
        callback = {
            "response_id": "PHRASE_001",
            "token": token,
            "playback_lease": lease["playback_lease"],
            "generation": lease["generation"],
        }
        result = await app.receive_async(Event("AudioCompleted", sid, 1, 101.0, "audio", callback, "welcome", 15.0))
        assert result["canonical_state"]["mode"] == "exhibition_introduction"
        assert result["reason"] == "exhibition_introduction_started"
        assert app.canonical_runtime.sessions[sid].display_name is None
        bad = await app.receive_async(
            Event("AudioCompleted", sid, 2, 104.0, "audio", {"response_id": "PHRASE_001", "token": "invalid"}, "bad-token", 15.25)
        )
        assert bad["reason"] == "stale_exhibition_audio_completion"
        result = await app.receive_async(Event("HandsetPlaced", sid, 3, 104.1, "hid", {}, "down", 15.3))
        assert result["reason"] == "exhibition_reset"
        assert sid not in app.canonical_runtime.sessions
        stale = await app.receive_async(
            Event("AudioCompleted", sid, 2, 105.0, "audio", {"response_id": "PHRASE_001", "token": token}, "stale", 15.4)
        )
        assert stale["reason"] == "stale_exhibition_audio_completion"

    asyncio.run(run())


def test_bad_name_retries_and_valid_name_advances_to_age():
    for raw, expected_reason, expected_name in (
        ("", "exhibition_name_unrecognized", None),
        ("123", "exhibition_name_unrecognized", None),
        ("  меня зовут Маша-Поля. ", "exhibition_age_question_started", "Маша-Поля"),
    ):
        runtime = CanonicalRuntime(exhibition_mode=True, monotonic_now=lambda: 10.0)
        to_name_capture(runtime)
        state = runtime.sessions["gr03"]
        result = runtime.handle(
            ce("VoiceIntentReady", 6, state.state_version, "speech", {"intent": "free_speech", "safety_severity": "none"}),
            transient_text=raw,
        )
        assert result["reason"] == expected_reason
        assert result["state"]["mode"] == ("exhibition_name_capture" if expected_name is None else "exhibition_age_prompt")
        assert runtime.sessions["gr03"].display_name == expected_name


def test_webpanel_audio_completion_uses_current_canonical_path():
    from app.foundation.stand import create_stand_app

    async def run():
        api = create_stand_app(Settings(profile="mock"))
        stand = api.state.stand
        try:
            app = stand.application
            clock = [10.0]
            app.monotonic_now = lambda: clock[0]
            app.canonical_runtime.monotonic_now = app.monotonic_now
            sid = "gr03-web"
            await app.receive_async(Event("HandsetLifted", sid, 1, 100.0, "web", {}, "up", 10.0))
            clock[0] = 15.0
            await app.flush_welcome_timers()
            token = app.sessions.get(sid).cancellation_token
            lease = dict(app._active_audio[sid])
            route = next(route.endpoint for route in api.routes if route.path == "/stand/audio/completed")
            result = await route({
                "session_id": sid,
                "response_id": "PHRASE_001",
                "token": token,
                "playback_lease": lease["playback_lease"],
                "generation": lease["generation"],
            })
            assert result["reason"] == "exhibition_introduction_started"
            assert result["canonical_state"]["mode"] == "exhibition_introduction"
        finally:
            stand.close()

    asyncio.run(run())


def test_button_before_welcome_cannot_enter_age_flow():
    runtime = CanonicalRuntime(exhibition_mode=True, monotonic_now=lambda: 10.0)
    runtime.handle(ce("HandsetLifted", 1, 0, "hid"))
    early = runtime.handle(ce("ButtonPressed", 2, 1, "hid", {"button_id": 5}))
    assert early["reason"] == "exhibition_age_not_ready"
    assert early["state"]["mode"] == "voice_welcome"
    assert runtime.handle(ce("WelcomeTimerFired", 3, 1, "timer", monotonic=15.0))["reason"] == "welcome_started"


def test_name_and_spoken_age_extract_slots_from_natural_answers():
    assert normalize_exhibition_name("Меня зовут Вася") == "Вася"
    assert normalize_exhibition_name("моё имя Маша-Поля") == "Маша-Поля"
    assert normalize_exhibition_name("меня") is None
    assert normalize_exhibition_age("мне семь лет") == 7
    assert normalize_exhibition_age("мне 6") == 6
    assert normalize_exhibition_age("мне десять") is None


def test_exhibition_accepts_all_seven_exact_age_buttons_and_voice_age():
    for button in range(1, 8):
        runtime = to_age_selection(CanonicalRuntime(exhibition_mode=True, monotonic_now=lambda: 10.0))
        state = runtime.sessions["gr03"]
        selected = runtime.handle(ce("ButtonPressed", 9, state.state_version, "hid", {"button_id": button}))
        assert selected["reason"] == "exhibition_age_selected"
        assert selected["state"]["age_years"] == button

    runtime = to_age_selection(CanonicalRuntime(exhibition_mode=True, monotonic_now=lambda: 10.0))
    state = runtime.sessions["gr03"]
    selected = runtime.handle(
        ce("VoiceIntentReady", 9, state.state_version, "speech", {"intent": "free_speech", "safety_severity": "none"}),
        transient_text="мне семь лет",
    )
    assert selected["reason"] == "exhibition_age_selected"
    assert selected["state"]["age_years"] == 7
    assert selected["state"]["age_band"] == "6-7"


def test_menu_tutorial_can_be_skipped_and_unclear_repeat_is_bounded():
    runtime = to_age_selection(CanonicalRuntime(exhibition_mode=True, monotonic_now=lambda: 10.0))
    state = runtime.sessions["gr03"]
    runtime.handle(ce("ButtonPressed", 9, state.state_version, "hid", {"button_id": 4}))
    state = runtime.sessions["gr03"]
    runtime.handle(ce("AudioCompleted", 10, state.state_version, "audio", {"response_id": "PHRASE_107"}))
    state = runtime.sessions["gr03"]
    runtime.handle(ce("ExhibitionOnboardingComplete", 11, state.state_version, "wave"))
    state = runtime.sessions["gr03"]
    skipped = runtime.handle(ce("ButtonPressed", 12, state.state_version, "hid", {"button_id": 6}))
    assert skipped["reason"] == "exhibition_menu_tutorial_skipped"
    assert skipped["state"]["mode"] == "menu_ready"

    state.mode = "exhibition_menu_repeat_listen"
    state.handset = "up"
    state.menu_repeat_attempts = 0
    first = runtime.handle(ce("VoiceIntentReady", 13, state.state_version, "speech", {"intent": "free_speech", "safety_severity": "none"}), transient_text="не понял")
    assert first["reason"] == "exhibition_menu_repeat_unclear"
    state = runtime.sessions["gr03"]
    second = runtime.handle(ce("VoiceIntentReady", 14, state.state_version, "speech", {"intent": "free_speech", "safety_severity": "none"}), transient_text="опять не понял")
    assert second["reason"] == "exhibition_menu_repeat_defaulted"
    assert second["state"]["mode"] == "menu_ready"


def test_s2s_does_not_publish_partial_open_say_block():
    from app.foundation.s2s import MockLLMProvider, ResponseTagParser, StreamingTokenConsumer

    async def run():
        outputs = []

        async def receive(item):
            outputs.append(item)

        await StreamingTokenConsumer().consume_parsed(
            MockLLMProvider(["<say>Зебра живет. ", "В Африке.</say>"]),
            [], "request", lambda: False, ResponseTagParser(), receive,
        )
        assert [item.value for item in outputs] == ["Зебра живет. В Африке."]

    asyncio.run(run())
