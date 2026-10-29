"""Synthetic-only identity trust boundary and public ingress regressions."""
import asyncio
from dataclasses import replace
import json
import os

import pytest

from app.foundation.config import Settings, SettingsError
from app.foundation.domain import Event, IDENTITY_INTERNAL_EVENT_TYPES
from app.foundation.events import EventValidationError, parse_event, redacted_event
from app.foundation.identity import (
    DisabledIdentityResolver, IdentityDecision, IdentityValidationError,
    SyntheticIdentityResolver, normalize_profile, validate_identity_decision, validate_uid,
)


def authorized(profile=None, *, expiry=100.0, capabilities=frozenset({"personalize"})):
    return IdentityDecision("authorized", {"profile_ref": "synthetic-subject"} if profile is None else profile, capabilities, expiry)


def test_identity_defaults_are_disabled_independently_of_operator_token():
    settings = Settings(stand_access_token="synthetic-operator-token").validate()
    assert settings.identity_mode == "guest_only"
    assert settings.identity_provider == "disabled"
    assert (settings.rfid_lookup_timeout_s, settings.rfid_ready_ttl_s, settings.rfid_debounce_s) == (2.0, 60.0, 0.5)
    assert settings.operator_token_present
    assert asyncio.run(DisabledIdentityResolver().resolve("synthetic-card", "stand", "request")).status == "unavailable"
    assert DisabledIdentityResolver().readiness()["ready"] is False


def test_identity_environment_is_explicit_and_typed(monkeypatch):
    monkeypatch.setattr(os, "environ", {
        "MISHKA_PROFILE": "mock", "MISHKA_HARDWARE_MODE": "simulator",
        "MISHKA_IDENTITY_MODE": "optional_rfid", "MISHKA_IDENTITY_PROVIDER": "synthetic",
        "MISHKA_RFID_LOOKUP_TIMEOUT_S": "1.5", "MISHKA_RFID_READY_TTL_S": "30",
        "MISHKA_RFID_DEBOUNCE_S": "0.25",
    })
    settings = Settings.from_env()
    assert settings.identity_provider == "synthetic"
    assert (settings.rfid_lookup_timeout_s, settings.rfid_ready_ttl_s, settings.rfid_debounce_s) == (1.5, 30.0, 0.25)
    assert settings.operational_snapshot()["identity_mode"] == "optional_rfid"


@pytest.mark.parametrize("profile,hardware", [("production", "fake"), ("production_candidate", "fake"), ("local_cpu", "fake"), ("mock", "physical")])
def test_synthetic_identity_is_not_a_production_backend(profile, hardware):
    with pytest.raises(SettingsError, match="synthetic identity requires"):
        Settings(profile=profile, hardware_mode=hardware, identity_mode="optional_rfid", identity_provider="synthetic").validate()


@pytest.mark.parametrize("changes", [
    {"identity_mode": "allow_all"}, {"identity_provider": "sqlite"},
    {"identity_provider": "synthetic"}, {"rfid_lookup_timeout_s": 0},
    {"rfid_lookup_timeout_s": 31}, {"rfid_ready_ttl_s": float("inf")},
    {"rfid_ready_ttl_s": float("nan")}, {"rfid_debounce_s": -1},
    {"rfid_debounce_s": True}, {"rfid_lookup_timeout_s": "2"},
])
def test_unsupported_identity_settings_fail_closed(changes):
    with pytest.raises(SettingsError):
        replace(Settings(), **changes).validate()


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


def test_legacy_alias_is_normalized_and_invalid_injected_provider_results_fail_closed():
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


def test_synthetic_fixture_only_grants_and_finite_expiry():
    clock = [10.0]
    resolver = SyntheticIdentityResolver({
        "001a": authorized(), "001A": IdentityDecision("denied"),
        "invalid": authorized({"profile_ref": "bad", "timezone": "Invalid/zone"}),
        "same-as-subject": authorized({"profile_ref": "same-as-subject"}),
    }, clock=lambda: clock[0], lease_s=5)
    async def scenario():
        granted = await resolver.resolve("001a", "synthetic-stand", "request-1")
        assert granted.status == "authorized"
        assert granted.expires_monotonic == 15.0
        assert (await resolver.resolve("001A", "synthetic-stand", "request-2")).status == "denied"
        assert (await resolver.resolve("other", "synthetic-stand", "request-3")).status == "unknown"
        assert (await resolver.resolve("invalid", "synthetic-stand", "request-4")).status == "unavailable"
        assert (await resolver.resolve("same-as-subject", "synthetic-stand", "request-5")).status == "unavailable"
        clock[0] = 100
        assert (await resolver.resolve("001a", "synthetic-stand", "request-6")).status == "unavailable"
    asyncio.run(scenario())
    readiness = resolver.readiness()
    assert readiness["synthetic"] and not readiness["production_ready"]
    assert "001a" not in json.dumps(readiness)


def test_synthetic_fixture_is_copied_at_construction():
    profile = {"profile_ref": "synthetic-subject", "display_name": "Original"}
    fixtures = {"synthetic-card": authorized(profile)}
    resolver = SyntheticIdentityResolver(fixtures, clock=lambda: 1.0)
    profile["display_name"] = "Changed"
    fixtures["other-card"] = authorized()
    assert asyncio.run(resolver.resolve("synthetic-card", "stand", "request")).profile["display_name"] == "Original"
    assert asyncio.run(resolver.resolve("other-card", "stand", "request")).status == "unknown"


@pytest.mark.parametrize("uid", [None, "", " \t", "x" * 129, "SECRET_PRIVATE_MARKER\n"])
def test_uid_validation_is_nonempty_bounded_and_does_not_echo_values(uid):
    with pytest.raises(IdentityValidationError) as error:
        validate_uid(uid)
    assert "PRIVATE_MARKER" not in str(error.value)
    with pytest.raises(EventValidationError):
        parse_event({"type": "rfid", "tag_id": uid}, "test", 1)


def test_uid_is_opaque_and_transient_until_lookup():
    uid = "000aBc-01"
    event = parse_event({"type": "rfid", "tag_id": uid, "profile": {"display_name": "PRIVATE_MARKER"}}, "test", 1)
    assert event.payload == {"tag_id": uid}
    assert validate_uid(uid) == uid
    assert uid not in json.dumps(redacted_event(event))


@pytest.mark.parametrize("name", sorted(IDENTITY_INTERNAL_EVENT_TYPES | {"AdultResolved", "DialogueFallback"}))
@pytest.mark.parametrize("source", ["api", "uart_pty", "internal_identity", "trusted_provider"])
def test_generic_ingress_cannot_forge_internal_outcomes(name, source):
    with pytest.raises(EventValidationError, match="internal event"):
        parse_event({"type": name, "status": "authorized", "authorized": True,
                     "profile": {"profile_ref": "forged"}}, "test", 1, source)


@pytest.mark.parametrize("name", ["RfidRemoved", "AnonymousStart", "HandsetPlaced", "SessionCancel", "HardwareReconnected"])
def test_generic_removal_and_other_events_discard_private_payloads(name):
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
