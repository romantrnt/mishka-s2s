from app.foundation.api import build_application
from app.foundation.canonical_contract import CanonicalEvent, CanonicalRuntime
from app.foundation.config import Settings
from app.scenarios.canonical_graph import CanonicalLangGraphAuthorizer


def ce(kind, seq, version, source="test", payload=None, monotonic=10.0):
    return CanonicalEvent(kind, "gr04", seq, version, source, payload or {}, f"{source}-{seq}", 100.0, monotonic)


def complete_current_onboarding(runtime):
    runtime.handle(ce("HandsetLifted", 1, 0, "hid"))
    runtime.handle(ce("WelcomeTimerFired", 2, 1, "timer", monotonic=15.0))
    runtime.handle(ce("AudioCompleted", 3, 2, "audio", {"response_id": "PHRASE_001"}))
    state = runtime.sessions["gr04"]
    runtime.handle(ce("AudioCompleted", 4, state.state_version, "audio", {"response_id": "PHRASE_078"}))
    state = runtime.sessions["gr04"]
    runtime.handle(ce("AudioCompleted", 5, state.state_version, "audio", {"response_id": "PHRASE_099"}))
    state = runtime.sessions["gr04"]
    result = runtime.handle(
        ce("VoiceIntentReady", 6, state.state_version, "speech", {"intent": "free_speech", "safety_severity": "none"}),
        transient_text="Меня зовут Аня",
    )
    assert result["reason"] == "exhibition_age_question_started"
    state = runtime.sessions["gr04"]
    runtime.handle(ce("AudioCompleted", 7, state.state_version, "audio", {"response_id": "PHRASE_108"}))
    state = runtime.sessions["gr04"]
    runtime.handle(ce("AudioCompleted", 8, state.state_version, "audio", {"response_id": "PHRASE_075"}))
    state = runtime.sessions["gr04"]
    selected = runtime.handle(ce("ButtonPressed", 9, state.state_version, "hid", {"button_id": 2}))
    assert selected["reason"] == "exhibition_age_selected"
    state = runtime.sessions["gr04"]
    return runtime.handle(ce("AudioCompleted", 10, state.state_version, "audio", {"response_id": "PHRASE_107"}))


def test_current_onboarding_closes_cleanly_and_does_not_implicitly_enter_menu():
    runtime = CanonicalRuntime(exhibition_mode=True, monotonic_now=lambda: 10.0)
    ready = complete_current_onboarding(runtime)
    assert ready["reason"] == "exhibition_onboarding_ready"
    assert ready["state"]["mode"] == "exhibition_onboarding_complete"
    assert ready["state"]["substate"] == "exhibition_onboarding_ready"
    assert ready["state"]["age_years"] == 2
    assert ready["state"]["age_band"] == "1-3"
    assert not any(item.get("phrase_id") == "PHRASE_008" for item in ready["effects"])
    completed = runtime.handle(ce("ExhibitionOnboardingComplete", 11, ready["state"]["state_version"], "handoff"))
    assert completed["reason"] == "exhibition_menu_tutorial_started"
    assert completed["state"]["mode"] == "exhibition_menu_tutorial_buttons"
    assert any(item.get("phrase_id") == "PHRASE_008" for item in completed["effects"])


def test_current_onboarding_reset_is_idempotent_and_clears_private_state():
    runtime = CanonicalRuntime(exhibition_mode=True, monotonic_now=lambda: 10.0)
    complete_current_onboarding(runtime)
    state = runtime.sessions["gr04"]
    reset = runtime.handle(ce("HandsetPlaced", 11, state.state_version, "reset"))
    assert reset["reason"] == "exhibition_reset"
    assert reset["state"]["mode"] == "idle"
    assert reset["state"]["menu_lit_buttons"] == []
    assert reset["state"]["selected_menu_action"] is None
    duplicate = runtime.handle(ce("HandsetPlaced", 12, reset["state"]["state_version"], "reset"))
    assert duplicate["reason"] == "handset_already_placed"


def test_gr05_exhibition_is_explicit_guest_runtime_without_local_patient_provider():
    settings = Settings(profile="mock", runtime_mode="exhibition", history_db="/definitely/not/used.sqlite3")
    assert settings.validate().runtime_mode == "exhibition"
    app, *_ = build_application(settings=settings)
    assert not hasattr(app, "profiles")
    assert not hasattr(app, "identity_resolver")
    assert app.max_json is False
    assert app.canonical_runtime.exhibition_mode is True
    assert type(app.sessions).__name__ == "InMemorySessionRepository"
    assert app.canonical_runtime.sessions == {}
    assert CanonicalLangGraphAuthorizer().status()["ready"] is True


def test_gr05_clinical_mode_requires_explicit_configuration():
    settings = Settings(profile="mock", runtime_mode="clinical")
    assert settings.validate().runtime_mode == "clinical"
    app, *_ = build_application(settings=settings)
    assert app.canonical_runtime.exhibition_mode is False
    assert not hasattr(app, "profiles")
    assert not hasattr(app, "identity_resolver")
    assert app.max_json is False
