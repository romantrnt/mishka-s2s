from __future__ import annotations

import asyncio
import hashlib
import json
import wave
from pathlib import Path
from types import SimpleNamespace

from app.foundation.api import build_application
from app.foundation.canonical_contract import VoiceManifest
from app.foundation.config import Settings
from app.foundation.domain import Event
from app.foundation.input_adapters import KeyboardHIDInputAdapter


def test_content_readiness_report_is_machine_readable_and_separates_approval():
    manifest = VoiceManifest("content/audio/voice_manifest.json", allow_development_assets=True)
    report = manifest.readiness()
    assert report["catalog_version"] == "mishka.voice-manifest/v1"
    assert report["validator_version"] == "gr07/v1"
    assert report["catalog_count"] == 108
    assert report["valid_count"] == 108
    assert report["errors"] == []
    assert report["approved_recorded_count"] == 0
    assert report["simulator_usable"] is True
    assert report["physical_production_ready"] is False
    assert len(report["pending_approval"]) == 108
    assert {item["phrase_id"] for item in report["required_exhibition_content"]} == {
        "PHRASE_001", "PHRASE_007", "PHRASE_008", "PHRASE_016",
        "PHRASE_068", "PHRASE_074", "PHRASE_075", "PHRASE_078",
        "PHRASE_099", "PHRASE_102", "PHRASE_107", "PHRASE_108",
    }


def test_manifest_detects_hash_mismatch_and_unsafe_path(tmp_path: Path):
    audio = tmp_path / "audio"
    audio.mkdir()
    source = audio / "phrase_catalog.txt"
    source.write_text("Привет", encoding="utf-8")
    wav_path = audio / "voice.wav"
    with wave.open(str(wav_path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(22050)
        out.writeframes(b"\x01\x00" * 2205)
    entry = {
        "phrase_id": "PHRASE_001", "transcript": "Привет", "language": "ru-RU",
        "asset_path": "voice.wav", "asset_sha256": "0" * 64,
        "format": "wav_pcm_s16le", "status": "approved",
    }
    manifest_path = audio / "voice_manifest.json"
    manifest_path.write_text(json.dumps({"contract": "mishka.voice-manifest/v1", "source_catalog": "phrase_catalog.txt", "assets": [entry]}, ensure_ascii=False), encoding="utf-8")
    report = VoiceManifest(manifest_path).readiness()
    assert any(item["code"] == "hash_mismatch" for item in report["errors"])
    assert VoiceManifest(manifest_path).diagnose("PHRASE_404")["code"] == "content_id_missing"
    entry["asset_path"] = "../outside.wav"
    manifest_path.write_text(json.dumps({"contract": "mishka.voice-manifest/v1", "source_catalog": "phrase_catalog.txt", "assets": [entry]}, ensure_ascii=False), encoding="utf-8")
    unsafe = VoiceManifest(manifest_path).readiness()
    assert any(item["code"] == "unsafe_asset_path" for item in unsafe["errors"])


def test_runtime_phrase_resolution_exposes_content_and_asset_diagnostics():
    async def scenario():
        app, *_ = build_application(settings=Settings(profile="mock", content_root="content"))
        clock = [0.0]
        app.monotonic_now = lambda: clock[0]
        app.canonical_runtime.monotonic_now = app.monotonic_now
        await app.receive_async(Event("HandsetLifted", "audio", 1, 0.0, "hid", {}, "lift", 0.0))
        clock[0] = 4.0
        result = await app.flush_welcome_timers()
        effect = next(item for item in result[0]["outputs"] if item.get("phrase_id") == "PHRASE_001")
        assert effect["content_id"] == "PHRASE_001"
        assert effect["asset_ref"] == "voice/PHRASE_001.wav"
        assert effect["audio_status"] == "queued"
        await app.reset_session("audio")
        assert app.resource_snapshot()["audio_completion_registry"] == 0

    asyncio.run(scenario())


def test_hid_dispatch_error_is_diagnostic_and_repeat_is_not_an_edge():
    diagnostics = []
    received = []

    def callback(event):
        received.append(event)
        raise RuntimeError("canonical ingress unavailable")

    adapter = KeyboardHIDInputAdapter("/dev/null", callback, debounce_ms=0, on_error=diagnostics.append)
    adapter.feed_key(adapter.KEY_SPACE, 1, timestamp=1.0)
    adapter.feed_key(adapter.KEY_SPACE, 2, timestamp=2.0)
    adapter.feed_key(adapter.KEY_SPACE, 1, timestamp=3.0)
    assert len(received) == 1
    assert adapter.diagnostics()["status"] == "dispatch_failure"
    assert diagnostics[-1]["kind"] == "dispatch_failure"


def test_stale_reader_generation_is_ignored_without_handset_transition():
    from app.foundation import stand as stand_module

    stand = stand_module.HardwareIntegrationStand.__new__(stand_module.HardwareIntegrationStand)
    stand._hid_generation = 2
    stand._physical_loop = None
    stand._physical_sequence = 0
    event = SimpleNamespace(type="SpaceReleased", payload={})
    stand._on_physical_input(event, generation=1)
    assert stand._physical_sequence == 0
