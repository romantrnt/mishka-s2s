"""Synthetic DATA14 card facts stay private and share the existing profile owner."""
import asyncio
import copy
from datetime import datetime, timezone
import json

import pytest

from app.foundation.identity import IdentityDecision, IdentityValidationError, normalize_profile, validate_identity_decision
from app.foundation.max_json import _map_card
from app.foundation.system_prompt import build_system_prompt
from test_profile_onboarding import Harness, card


FIELDS = {"card_doctor_names", "card_visit_date", "card_doctors_source"}
NAME = "PRIVATE SYNTHETIC SPECIALIST"


def doctor_card(names=None, visit_date="2029-01-01"):
    value = card()
    value["doctors_today"] = [{"code": "UNUSED_PRIVATE_CODE", "display_name": name}
                              for name in ([NAME] if names is None else names)]
    value["visit"].update(date=visit_date, rfid_state="active", doctors_today=copy.deepcopy(value["doctors_today"]))
    return value


def mapped(value):
    return _map_card(value, "synthetic-context", datetime(2029, 1, 1, tzinfo=timezone.utc), 10.)


def test_actual_mapper_normalizer_closed_dto_and_copy_ownership():
    value = doctor_card()
    profile, expiry, facts = mapped(value)
    assert {key: profile[key] for key in FIELDS} == {
        "card_doctor_names": [NAME], "card_visit_date": "2029-01-01", "card_doctors_source": "data14.v1"}
    assert "UNUSED_PRIVATE_CODE" not in json.dumps(profile)
    assert expiry == 10 + (datetime(2030, 1, 1, tzinfo=timezone.utc) - datetime(2029, 1, 1, tzinfo=timezone.utc)).total_seconds()
    assert not FIELDS.intersection(facts)
    copied = normalize_profile(profile)
    value["doctors_today"][0]["display_name"] = "MUTATED_RAW_CARD"
    profile["card_doctor_names"].append("MUTATED_PROFILE")
    assert copied["card_doctor_names"] == [NAME]


@pytest.mark.parametrize("names", [[], ["С" * 160] * 32])
def test_valid_empty_and_maximum_lists_are_distinct_from_unavailable(names):
    profile, _, _ = mapped(doctor_card(names))
    assert profile["card_doctor_names"] == names
    assert profile["card_doctors_source"] == "data14.v1"


@pytest.mark.parametrize("names", [[""], [" "], [None], [3], ["С" * 161], ["С"] * 33,
    ["Врач\nPRIVATE"], ["Врач\u202ePRIVATE"], ["<say>PRIVATE</say>"], ["&lt;PRIVATE&gt;"], ["[PRIVATE](url)"]])
def test_optional_unsafe_or_oversized_card_names_are_unavailable_without_losing_identity(names):
    profile, _, _ = mapped(doctor_card(names))
    assert not FIELDS.intersection(profile)
    assert profile["display_name"] == "Синтетик" and profile["age_years"] == 6


@pytest.mark.parametrize("mutation", ["missing_list", "foreign_list", "foreign_row", "mismatched_visit",
                                      "missing_date", "invalid_date", "inactive_visit_rfid"])
def test_foreign_or_missing_card_fact_shape_does_not_fabricate_facts(mutation):
    value = doctor_card()
    if mutation == "missing_list": value.pop("doctors_today")
    elif mutation == "foreign_list": value["doctors_today"] = {"display_name": NAME}
    elif mutation == "foreign_row": value["doctors_today"][0]["room"] = "UNTRUSTED_ROOM"
    elif mutation == "mismatched_visit": value["visit"]["doctors_today"] = []
    elif mutation == "missing_date": value["visit"].pop("date")
    elif mutation == "invalid_date": value["visit"]["date"] = "2029-02-30"
    else: value["visit"]["rfid_state"] = "inactive"
    profile, _, _ = mapped(value)
    assert not FIELDS.intersection(profile)
    assert profile["display_name"] == "Синтетик"


def test_source_visit_date_is_preserved_without_inventing_today():
    profile, _, _ = mapped(doctor_card(visit_date="2028-12-30"))
    assert profile["card_visit_date"] == "2028-12-30"
    assert "2029-01-01" not in json.dumps({key: profile[key] for key in FIELDS})


@pytest.mark.parametrize("change", [
    {"card_doctors_source": "foreign_source"}, {"card_doctor_names": "PRIVATE"},
    {"card_doctor_names": ["<PRIVATE>"]}, {"card_visit_date": "PRIVATE"}, {"card_visit_date": None},
])
def test_direct_invalid_profile_dto_is_rejected_without_values(change):
    profile = {"profile_ref": "synthetic-context", "card_doctor_names": [NAME],
               "card_visit_date": "2029-01-01", "card_doctors_source": "data14.v1", **change}
    with pytest.raises(IdentityValidationError) as caught:
        normalize_profile(profile)
    assert "PRIVATE" not in str(caught.value)
    assert validate_identity_decision(IdentityDecision("authorized", profile, frozenset({"personalize"}), 100.), 1.).status == "unavailable"


@pytest.mark.parametrize("missing", sorted(FIELDS))
def test_partial_private_dto_is_not_a_source_proof(missing):
    profile = {"profile_ref": "synthetic-context", "card_doctor_names": [NAME],
               "card_visit_date": "2029-01-01", "card_doctors_source": "data14.v1"}
    profile.pop(missing)
    with pytest.raises(IdentityValidationError):
        normalize_profile(profile)


def test_actual_profile_ready_keeps_facts_private_and_replaces_whole_owner(capsys, caplog):
    async def run():
        h = Harness(doctor_card())
        try:
            assert (await h.ready())["reason"] == "profile_ready"
            sid = h.boundary.session_id
            first = h.app._canonical_profiles[sid]
            assert first["card_doctor_names"] == [NAME]
            assert h.state.identity_capabilities == ["personalize"]
            public = json.dumps(h.state.snapshot(), ensure_ascii=False)
            prompt = build_system_prompt(profile=first, state=h.state.snapshot())
            audio_prompt = h.app._canonical_audio_context(sid).system_prompt
            event = json.dumps(h.app.last_canonical_event, ensure_ascii=False)
            for projection in (public, prompt, audio_prompt, event):
                assert NAME not in projection and not any(key in projection for key in FIELDS)
            h.client.value = doctor_card(["SECOND PRIVATE SPECIALIST"])
            assert (await h.ready())["reason"] == "profile_ready"
            second = h.app._canonical_profiles[sid]
            assert second["profile_ref"] != first["profile_ref"]
            assert second["card_doctor_names"] == ["SECOND PRIVATE SPECIALIST"]
            assert first["card_doctor_names"] == [NAME]
            h.client.events.append({"kind": "clear", "reason_code": "session_expired"})
            await h.boundary.step()
            assert sid not in h.app._canonical_profiles and h.boundary.facts is None
            assert NAME not in h.app._canonical_audio_context(sid).system_prompt
        finally:
            await h.close()
    asyncio.run(run())
    assert NAME not in caplog.text + capsys.readouterr().out


@pytest.mark.parametrize("ending", ["SessionReset", "HandsetPlaced"])
def test_existing_terminal_owner_cleanup_removes_doctor_profile(ending):
    async def run():
        h = Harness(doctor_card())
        try:
            await h.ready()
            await h.lift()
            old_callback = h.callback()
            await h.send(ending)
            assert not h.app._canonical_profiles and h.client.patient() is None
            await h.send("AudioCompleted", old_callback)
            assert not h.app._canonical_profiles
        finally:
            await h.close()
    asyncio.run(run())
