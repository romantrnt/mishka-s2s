from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.foundation.canonical_contract import (
    CanonicalEvent,
    CanonicalRuntime,
    DialogueMemory,
    FixedStoryManifest,
    age_band_from_profile,
    validate_generated_story,
)
from app.foundation.canonical_services import CanonicalContentCatalog


def event(kind: str, seq: int, version: int, *, payload=None, source="acceptance", timestamp=1.0, correlation=None):
    return CanonicalEvent(kind, "acceptance", seq, version, source, payload or {}, correlation or f"corr-{seq}", timestamp)


def test_catalog_is_single_canonical_boundary_and_reports_ready_content():
    catalog = CanonicalContentCatalog("content", allow_development_assets=False)
    document = catalog.get("canonical_product")
    report = catalog.report()
    assert document["contract"] == "canonical_product/v1"
    assert document["langgraph_authorization"] is True
    assert len(document["states"]) == 50
    assert catalog.game("catch_light")["input"] == "button"
    assert catalog.story("fixed_aibolit_v1") is not None
    assert report["ready"] is True
    assert report["voice"]["approved_recorded_count"] == 108
    dev_catalog = CanonicalContentCatalog("content", allow_development_assets=True)
    assert dev_catalog.story("fixed_aibolit_v1")["rights_status"] == "operator_approved"
    with pytest.raises(KeyError):
        catalog.get("fixture_core")


def test_profile_dob_and_accessibility_policy_are_deterministic():
    now = datetime(2026, 10, 13, tzinfo=timezone.utc)
    band, source = age_band_from_profile({"date_of_birth": "2020-10-12", "timezone": "UTC"}, now=now)
    assert (band, source) == ("5-7", "profile_dob")
    runtime = CanonicalRuntime()
    result = runtime.handle(event("RfidPresented", 1, 0, payload={}), {"profile_ref": "opaque", "date_of_birth": "2020-10-12", "trigger_topics": [{"topic_id": "loss", "status": "active", "handling": "adult_neutral"}]})
    assert result["state"]["profile_status"] == "identified"
    assert result["state"]["age_band_source"] == "profile_dob"
    assert result["state"]["accessibility_policy"]["content_constraints"] == ("adult_neutral",)
    assert "display_name" not in result["state"] and "schedule_ref" not in result["state"]


def test_button_mapping_handset_gate_and_safety_handoff_survive_tag_removal():
    runtime = CanonicalRuntime()
    assert runtime.handle(event("AnonymousStart", 1, 0))["state"]["mode"] == "anonymous_visual"
    gated = runtime.handle(event("ButtonPressed", 2, 1, payload={"button_id": 6}))
    assert gated["reason"] == "handset_required" and gated["state"]["pending_mode"] == "bear_dialogue"
    runtime.handle(event("HandsetLifted", 3, 2))
    safety = runtime.handle(event("VoiceIntentReady", 4, 3, payload={"intent": "request_adult", "safety_severity": "none"}))
    assert safety["state"]["mode"] == "safety_handoff"
    preserved = runtime.handle(event("RfidRemoved", 5, 4))
    assert preserved["state"]["mode"] == "safety_handoff"
    resolved = runtime.handle(event("AdultResolved", 6, 5))
    assert resolved["state"]["mode"] == "root_menu"


def test_state_version_correlation_and_ram_ttl_are_fail_closed():
    clock = [100.0]
    runtime = CanonicalRuntime(now=lambda: clock[0])
    first = runtime.handle(event("AnonymousStart", 1, 0, timestamp=100.0))
    duplicate = runtime.handle(event("AnonymousStart", 2, 1, timestamp=100.0, correlation="corr-1"))
    assert duplicate["reason"] == "idempotent_duplicate"
    stale = runtime.handle(event("ButtonPressed", 3, 0, payload={"button_id": 1}, timestamp=100.0))
    assert stale["reason"] == "stale_state_version"
    runtime.handle(event("ButtonPressed", 4, 1, payload={"button_id": 6}, timestamp=100.0))
    runtime.handle(event("HandsetLifted", 5, 2, timestamp=100.0))
    down = runtime.handle(event("HandsetPlaced", 6, 3, timestamp=100.0))
    assert down["state"]["mode"] == "dialogue_suspended_ram"
    assert down["state"]["dialogue_memory"]["ttl_seconds"] == 300
    clock[0] = 401.0
    expired = runtime.handle(event("ButtonPressed", 7, 4, payload={"button_id": 1}, timestamp=401.0))
    assert expired["state"]["mode"] == "game_menu"
    assert expired["state"]["dialogue_memory"]["status"] == "expired"


def test_content_rights_and_generated_story_validation_block_unsafe_paths():
    manifest = FixedStoryManifest("content/stories/fixed_manifest.json", ".")
    assert manifest.get_verified("fixed_aibolit_v1") is not None
    runtime = CanonicalRuntime(stories=manifest, allow_development_placeholders=False)
    runtime.handle(event("AnonymousStart", 1, 0))
    runtime.handle(event("ButtonPressed", 2, 1, payload={"button_id": 2}))
    started = runtime.handle(event("ButtonPressed", 3, 2, payload={"button_id": 1}))
    assert started["reason"] == "fixed_story_started"
    valid, reason = validate_generated_story({"title": "Доброе путешествие", "text": "Герой нашёл спокойную дорогу и вернулся домой.", "age_band": "3-5", "estimated_duration_sec": 70, "safety_tags": ["safe"], "ending_state": "safe_resolved"}, {"age_band": "3-5", "theme": "dinosaurs"})
    assert (valid, reason) == (True, None)
    invalid, reason = validate_generated_story({"title": "Айболит", "text": "Секрет от взрослых.", "age_band": "3-5", "estimated_duration_sec": 70, "safety_tags": [], "ending_state": "safe_resolved"}, {"age_band": "3-5", "theme": "dinosaurs"})
    assert invalid is False and reason == "forbidden_content"
