"""Ready-profile trust boundary and public ingress regressions."""
from dataclasses import replace
import json
import os

import pytest

from app.foundation.config import Settings, SettingsError
from app.foundation.domain import Event, IDENTITY_INTERNAL_EVENT_TYPES
from app.foundation.events import EventValidationError, parse_event, redacted_event
from app.foundation.identity import (
    IdentityDecision, IdentityValidationError, normalize_profile, validate_identity_decision,
)


def authorized(profile=None, *, expiry=100.0, capabilities=frozenset({"personalize"})):
    return IdentityDecision("authorized", {"profile_ref": "synthetic-subject"} if profile is None else profile, capabilities, expiry)


def test_profile_defaults_are_guest_only_independently_of_operator_token():
    settings = Settings(stand_access_token="synthetic-operator-token").validate()
    assert settings.identity_mode == "guest_only"
    assert settings.profile_ready_ttl_s == 60.0
    assert settings.operator_token_present


def test_profile_environment_is_explicit_and_typed(monkeypatch):
    monkeypatch.setattr(os, "environ", {
        "MISHKA_PROFILE": "mock", "MISHKA_HARDWARE_MODE": "simulator",
        "MISHKA_IDENTITY_MODE": "max_json", "MISHKA_PROFILE_READY_TTL_S": "30",
    })
    settings = Settings.from_env()
    assert settings.identity_mode == "max_json"
    assert settings.hardware_mode == "simulator"
    assert settings.profile_ready_ttl_s == 30.0
    snapshot = settings.operational_snapshot()
    assert snapshot["identity_mode"] == "max_json"
    assert snapshot["profile_ready_ttl_s"] == 30.0


@pytest.mark.parametrize("hardware", ["fake", "simulator"])
def test_max_json_accepts_explicit_mock_exhibition_profiles(hardware):
    settings = Settings(profile="mock", runtime_mode="exhibition", hardware_mode=hardware,
                        identity_mode="max_json").validate()
    assert settings.identity_mode == "max_json"


@pytest.mark.parametrize("profile,hardware", [
    ("production", "fake"), ("production_candidate", "fake"),
    ("exhibition_candidate", "fake"), ("production_qwen_rvc", "fake"),
    ("local_cpu", "fake"), ("local_gpu", "fake"), ("mock", "physical"),
])
def test_max_json_is_not_a_production_or_physical_backend(profile, hardware):
    with pytest.raises(SettingsError, match="max_json requires"):
        Settings(profile=profile, hardware_mode=hardware, identity_mode="max_json").validate()


def test_max_json_requires_exhibition_session_policy():
    with pytest.raises(SettingsError, match="exhibition session policy"):
        Settings(identity_mode="max_json", runtime_mode="clinical").validate()


@pytest.mark.parametrize("changes", [
    {"identity_mode": "allow_all"}, {"identity_mode": "optional_rfid"},
    {"identity_mode": "synthetic"}, {"profile_ready_ttl_s": 0},
    {"profile_ready_ttl_s": 0.09}, {"profile_ready_ttl_s": 3600.1},
    {"profile_ready_ttl_s": float("inf")}, {"profile_ready_ttl_s": float("nan")},
    {"profile_ready_ttl_s": -1}, {"profile_ready_ttl_s": True},
    {"profile_ready_ttl_s": "2"},
])
def test_unsupported_profile_settings_fail_closed(changes):
    with pytest.raises(SettingsError):
        replace(Settings(), **changes).validate()


@pytest.mark.parametrize("ttl", [0.1, 3600.0])
def test_profile_ready_ttl_accepts_supported_boundaries(ttl):
    assert Settings(profile_ready_ttl_s=ttl).validate().profile_ready_ttl_s == ttl


def test_profile_normalization_has_one_reference_and_a_bounded_allowlist():
    profile = normalize_profile({
        "child_id": "synthetic-subject", "display_name": "Синтетический пример",
        "date_of_birth": "2021-01-02", "timezone": "UTC", "language": "ru-RU",
        "motor_flags": ["limited_fine_motor"], "schedule_ref": "synthetic-schedule",
        "tag_id": "UID_PRIVATE_MARKER", "schedule": [{"private": "SCHEDULE_PRIVATE_MARKER"}],
        "notes": "DB_PRIVATE_MARKER",
    })
    assert profile["profile_ref"] == "synthetic-subject"
    assert "child_id" not in profile
    assert profile["date_of_birth"] == "2021-01-02"
    assert not {"tag_id", "schedule", "notes"} & profile.keys()
    assert "PRIVATE_MARKER" not in json.dumps(profile)


@pytest.mark.parametrize("profile", [
    {}, {"profile_ref": 1}, {"profile_ref": " "},
    {"profile_ref": "one", "child_id": "two"},
    {"profile_ref": "one", "timezone": "Invalid/SECRET_PRIVATE_MARKER"},
    {"profile_ref": "one", "date_of_birth": "2021-02-30"},
    {"profile_ref": "one", "date_of_birth": "2999-01-01"},
    {"profile_ref": "one", "date_of_birth": "20210101"},
    {"profile_ref": "one", "motor_flags": "private-free-form-note"},
    {"profile_ref": "one", "sensory_flags": ["x"] * 9},
    {"profile_ref": "one", "communication_flags": [1]},
    {"profile_ref": "one", "display_name": "x" * 81},
    {"profile_ref": "one", "language": "ru\nprivate"},
    {"profile_ref": "one", "grammatical_gender": "unsupported"},
])
def test_invalid_profile_data_is_rejected_without_values(profile):
    with pytest.raises(IdentityValidationError) as error:
        normalize_profile(profile)
    assert "PRIVATE_MARKER" not in str(error.value)
    decision = validate_identity_decision(authorized(profile), 1.0)
    assert decision.status == "unavailable"
    assert decision.profile is None
    assert not decision.capabilities


@pytest.mark.parametrize("expiry", [0.0, 10.0, float("inf"), float("nan"), True, "100"])
def test_expired_or_malformed_grants_do_not_expose_profiles(expiry):
    assert validate_identity_decision(authorized(expiry=expiry), 10.0) == IdentityDecision("unavailable")


def test_legacy_alias_is_normalized_and_invalid_profile_grants_fail_closed():
    original = {"child_id": "synthetic-subject", "display_name": "Test"}
    decision = validate_identity_decision(authorized(original), 10.0)
    assert decision.profile == {"profile_ref": "synthetic-subject", "display_name": "Test"}
    original["display_name"] = "Mutated"
    assert decision.profile["display_name"] == "Test"
    assert "Test" not in repr(decision)
    assert validate_identity_decision({"status": "authorized", "profile": original}, 10.0).status == "unavailable"
    assert validate_identity_decision(authorized(capabilities=frozenset({"allow_everything"})), 10.0).status == "unavailable"
    assert validate_identity_decision(authorized(capabilities={"personalize"}), 10.0).status == "unavailable"
    for status in ("unknown", "denied", "unavailable"):
        assert validate_identity_decision(IdentityDecision(status, original, frozenset({"personalize"}), 100), 10) == IdentityDecision(status)


@pytest.mark.parametrize("name", ["rfid", "rfid_presented", "rfid_removed", "RfidPresented", "RfidRemoved"])
@pytest.mark.parametrize("source", ["api", "uart_pty", "internal_identity", "trusted_provider"])
def test_generic_ingress_rejects_raw_uid_events_without_echoing_values(name, source):
    with pytest.raises(EventValidationError, match="unknown event type") as error:
        parse_event({"type": name, "tag_id": "UID_PRIVATE_MARKER",
                     "profile": {"display_name": "PROFILE_PRIVATE_MARKER"}}, "test", 1, source)
    assert "PRIVATE_MARKER" not in str(error.value)
    direct = Event(name, "test", 1, 0, source, {
        "tag_id": "UID_PRIVATE_MARKER", "profile": {"display_name": "PROFILE_PRIVATE_MARKER"},
    })
    assert redacted_event(direct)["payload"] == {}
    assert "PRIVATE_MARKER" not in json.dumps(redacted_event(direct))


@pytest.mark.parametrize("name", sorted(IDENTITY_INTERNAL_EVENT_TYPES | {"DialogueFallback"}))
@pytest.mark.parametrize("source", ["api", "uart_pty", "internal_identity", "trusted_provider"])
def test_generic_ingress_cannot_forge_internal_outcomes(name, source):
    with pytest.raises(EventValidationError, match="internal event"):
        parse_event({"type": name, "status": "authorized", "authorized": True,
                     "profile": {"profile_ref": "forged"}}, "test", 1, source)


@pytest.mark.parametrize("name", ["AdultResolved", "AdultHandoffAcknowledged", "AdultHandoffFailed"])
@pytest.mark.parametrize("source", ["api", "uart_pty", "internal_identity", "trusted_provider"])
def test_removed_delivery_events_are_unknown_and_do_not_echo_private_data(name, source):
    with pytest.raises(EventValidationError, match="unknown event type") as error:
        parse_event({"type": name, "incident_id": "INCIDENT_PRIVATE_MARKER",
                     "profile": {"profile_ref": "PROFILE_PRIVATE_MARKER"}}, "test", 1, source)
    assert "PRIVATE_MARKER" not in str(error.value)
    direct = Event(name, "test", 1, 0, source, {"incident_id": "INCIDENT_PRIVATE_MARKER"})
    assert redacted_event(direct)["payload"] == {}


@pytest.mark.parametrize("name", ["SessionStarted", "SessionReset", "AnonymousStart", "HandsetPlaced", "SessionCancel", "HardwareReconnected"])
def test_generic_session_events_discard_private_payloads(name):
    private = {"tag_id": "UID_PRIVATE_MARKER", "uid": "UID_PRIVATE_MARKER", "profile_ref": "REF_PRIVATE_MARKER",
               "display_name": "NAME_PRIVATE_MARKER", "schedule": [{"private": "SCHEDULE_PRIVATE_MARKER"}],
               "profile": {"private": "PROFILE_PRIVATE_MARKER"}, "_identity_authorized": True}
    parsed = parse_event({"type": name, **private}, "test", 1)
    assert parsed.payload == {}
    direct = Event(name, "test", 1, 0, "internal", private)
    assert redacted_event(direct)["payload"] == {}
    assert "PRIVATE_MARKER" not in json.dumps(redacted_event(direct))


def test_speech_is_transient_and_existing_button_and_audio_fields_survive():
    event = parse_event({"type": "text", "text": "SPEECH_PRIVATE_MARKER", "tag_id": "UID_PRIVATE_MARKER"}, "test", 1)
    assert event.payload == {"length": 21}
    assert "PRIVATE_MARKER" not in json.dumps(redacted_event(event))
    assert parse_event({"type": "button_hold", "button_id": 2, "duration_ms": 500, "profile_ref": "PRIVATE_MARKER"}, "test", 2).payload == {"button_id": 2, "duration_ms": 500}
    audio = parse_event({"type": "AudioCompleted", "response_id": "PHRASE_001", "generation": 2,
                         "playback_lease": "synthetic-lease", "tag_id": "PRIVATE_MARKER"}, "test", 3)
    assert audio.payload == {"response_id": "PHRASE_001", "generation": 2, "playback_lease": "synthetic-lease"}
    assert redacted_event(audio)["payload"] == audio.payload
    with pytest.raises(EventValidationError):
        parse_event({"type": "button", "button_id": True}, "test", 4)


def test_existing_trace_labels_and_optimistic_state_version_are_preserved():
    event = parse_event({"type": "handset_up", "_input_type": "SpaceReleased", "_normalized_event": "handset_up", "state_version": 3}, "test", 1)
    assert event.event_type == "HandsetLifted"
    assert event.payload == {"_input_type": "SpaceReleased", "_normalized_event": "handset_up", "state_version": 3}
    with pytest.raises(EventValidationError, match="state_version"):
        parse_event({"type": "handset_up", "state_version": "3"}, "test", 2)
    assert parse_event({"type": "text", "text": "x" * 512}, "test", 3).payload == {"length": 512}
