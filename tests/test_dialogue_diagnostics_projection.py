"""Public fallback projections must not promote raw provider/child data."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.foundation.api import build_application
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.stand import HardwareIntegrationStand
from app.foundation.validation import AsyncResponseValidator, MockMiniLLMValidator


@pytest.mark.parametrize("reason,expected", [
    ("mini_llm_timeout", "mini_llm_timeout"),
    ("mini_llm_block", "mini_llm_block"),
    ("mini_llm_error:JSONDecodeError", "mini_llm_error:JSONDecodeError"),
    ("unclosed <say> tag", "unclosed_say_tag"),
    ("PRIVATE_CHILD_NAME", "validation_detail_redacted"),
    ("mini_llm_error:PRIVATE_CHILD_NAME", "validation_detail_redacted"),
    ({"raw": "PRIVATE_CHILD_NAME"}, "validation_detail_redacted"),
])
def test_validation_reason_is_enum_not_adapter_prose(reason, expected):
    raw = {"events": [{"event": "validation_block", "reason": reason,
        "source": "ollama_mini_llm", "latency_ms": 123.5, "timeout_ms": 1000,
        "text": "PRIVATE_CHILD_NAME", "response": "PRIVATE_MODEL_RESPONSE", "utterance_hash": "PRIVATE_HASH"}]}
    result = HardwareIntegrationStand._panel_metrics(raw)
    event = result["events"][0]
    assert event["reason"] == expected
    assert event["source"] == "ollama_mini_llm"
    assert event["latency_ms"] == 123.5 and event["timeout_ms"] == 1000
    assert "PRIVATE_" not in json.dumps(result)


@pytest.mark.parametrize("bad", ["PRIVATE_NUMBER", True, -1, float("nan"), float("inf"), 10**1000, {"raw": "PRIVATE_NESTED"}])
def test_numeric_and_source_fields_cannot_smuggle_private_data(bad):
    raw = {"PRIVATE_STAGE": 1, "utterance_hash": "PRIVATE_HASH",
           "stt": {"count": bad, "last_ms": bad, "raw": "PRIVATE_TEXT"},
           "events": [{"event": "validation_failure", "reason": "mini_llm_timeout",
                       "source": "PRIVATE_SOURCE", "latency_ms": bad, "timeout_ms": bad,
                       "at": bad, "token_index": bad, "session_id": "PRIVATE_SESSION"},
                      {"event": "PRIVATE_EVENT", "reason": "PRIVATE_TEXT"}]}
    result = HardwareIntegrationStand._panel_metrics(raw)
    assert result["stt"] == {}
    assert len(result["events"]) == 1
    assert result["events"][0] == {"event": "validation_failure", "reason": "mini_llm_timeout", "source": "unknown"}
    assert "PRIVATE_" not in json.dumps(result, allow_nan=False)


def test_nonvalidation_failure_detail_is_not_promoted():
    result = HardwareIntegrationStand._panel_metrics({"events": [
        {"event": "rvc_v2_fallback", "reason": "PRIVATE_PATH exception", "source": "PRIVATE_SOURCE", "at": 2.5},
        {"event": "token_received", "token_index": 4, "token": "PRIVATE_TOKEN"},
    ]})
    assert result["events"] == [{"event": "rvc_v2_fallback", "at": 2.5}, {"event": "token_received", "token_index": 4}]


def test_result_envelope_unwraps_only_safe_fallback_fields():
    raw = {"input": "text", "at": 12.5, "correlation_id": "PRIVATE_CORRELATION", "result": {
        "reason": "dialogue_turn", "state": "ACTIVE", "session_id": "PRIVATE_SESSION",
        "outputs": [{"type": "bounded_dialogue_turn", "resolution": "bounded_dialogue_fallback",
                     "fallback_reason": "provider_or_validation_failure", "chunks": ["PRIVATE_MODEL_RESPONSE"],
                     "reason": "PRIVATE_REASON", "source": "PRIVATE_SOURCE", "asset_path": "PRIVATE_PATH"},
                    {"type": "speak", "phrase_id": "PHRASE_104", "text": "PRIVATE_TEXT"}],
        "error": {"payload": "PRIVATE_ERROR"}, "name": "PRIVATE_NAME", "utterance_hash": "PRIVATE_HASH",
    }}
    result = HardwareIntegrationStand._panel_result_item(raw)
    assert result == {"input": "text", "at": 12.5, "reason": "dialogue_turn", "state": "ACTIVE", "outputs": [
        {"type": "bounded_dialogue_turn", "resolution": "bounded_dialogue_fallback", "fallback_reason": "provider_or_validation_failure"},
        {"type": "speak", "phrase_id": "PHRASE_104"}]}
    assert "PRIVATE_" not in json.dumps(result)
    raw["result"]["reason"] = "PRIVATE_REASON"
    raw["result"]["state"] = "PRIVATE_STATE"
    raw["result"]["outputs"][0]["fallback_reason"] = "PRIVATE_FALLBACK"
    assert "PRIVATE_" not in json.dumps(HardwareIntegrationStand._panel_result_item(raw))


def test_new_payload_field_is_allowlisted_at_storage_boundary():
    assert HardwareIntegrationStand._panel_payload({"fallback_reason": "provider_or_validation_failure"}) == {
        "fallback_reason": "provider_or_validation_failure"}
    assert HardwareIntegrationStand._panel_payload({"fallback_reason": "PRIVATE_RAW_EXCEPTION", "text": "PRIVATE_TEXT"}) == {}


def test_actual_validator_timeout_reaches_panel_with_fallback_and_no_text():
    async def run():
        app, _, _, _ = build_application(settings=Settings(profile="mock"), background_scheduler=False)
        sid = "PRIVATE_SYNTHETIC_SESSION"
        try:
            await app.receive_async(Event("HandsetLifted", sid, 1, 1., "test", {}, "PRIVATE_CORRELATION"))
            app.canonical_runtime.sessions[sid].mode = "bear_dialogue"
            app.sessions.get(sid).current_node = "bear_dialogue"
            app.bounded_s2s.response_validator = AsyncResponseValidator(MockMiniLLMValidator(delay_s=.1), timeout_ms=10)
            result = await app.receive_async(Event("SpeechReceived", sid, 2, 2., "test", {}, "PRIVATE_CORRELATION2"), "Привет")
            stored = {"at": 2., "input": "text", "result": HardwareIntegrationStand._public_result(result)}
            # This is exactly the in-memory envelope used by inject and the
            # asynchronous audio-completion callback, without a physical PTY.
            stand = HardwareIntegrationStand.__new__(HardwareIntegrationStand)
            stand.snapshot = lambda: {"results": [stored], "pipeline_metrics": app.bounded_s2s.metrics.snapshot()}
            panel = stand.panel_snapshot()
            reasons = [out.get("fallback_reason") for item in panel["results"] for out in item.get("outputs", [])]
            assert "provider_or_validation_failure" in reasons
            events = [item for item in panel["pipeline_metrics"]["events"] if item["event"] == "validation_block"]
            assert events and events[0]["reason"] == "mini_llm_timeout"
            assert events[0]["source"] == "validator_timeout"
            assert events[0]["timeout_ms"] == 10
            serialized = json.dumps(panel, ensure_ascii=False, allow_nan=False)
            assert "PRIVATE_" not in serialized and "Привет" not in serialized
        finally:
            await app._cleanup_session_resources(sid)
            await app.runtime_manager.close()
    asyncio.run(run())



def test_secrecy_defense_retains_fixed_reason_code():
    from app.foundation.stand import HardwareIntegrationStand
    projected = HardwareIntegrationStand._panel_metrics({"events": [{
        "event": "validation_block", "reason": "secrecy_or_isolation", "source": "regex",
    }]})
    assert projected["events"][0]["reason"] == "secrecy_or_isolation"



def test_external_contact_block_and_story_unavailability_keep_fixed_codes():
    from app.foundation.stand import HardwareIntegrationStand
    projected = HardwareIntegrationStand._panel_metrics({"events": [{
        "event": "validation_block", "reason": "unverified_external_contact", "source": "regex",
    }]})
    assert projected["events"][0]["reason"] == "unverified_external_contact"
    assert HardwareIntegrationStand._panel_payload({"fallback_reason": "personal_story_unavailable"})["fallback_reason"] == "personal_story_unavailable"
