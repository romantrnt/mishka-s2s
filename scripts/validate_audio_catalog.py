#!/usr/bin/env python3
"""Deterministic acceptance validator for the canonical voice manifest."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.foundation.canonical_contract import VoiceManifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path("content/audio/voice_manifest.json"))
    parser.add_argument("--development", action="store_true")
    args = parser.parse_args()
    manifest = VoiceManifest(args.manifest, allow_development_assets=args.development)
    report = manifest.readiness()
    expected_count = 108
    expected = {f"PHRASE_{number:03d}" for number in range(1, expected_count + 1)}
    ids = {
        str(item.get("phrase_id"))
        for item in manifest.document.get("assets", [])
        if isinstance(item, dict)
    }
    errors = []
    if ids != expected:
        errors.append("catalog_ids_must_be_exactly_PHRASE_001_to_PHRASE_108")
    if report["duplicate_phrase_ids"]:
        errors.append("duplicate_phrase_ids")
    if report["valid_count"] != expected_count:
        errors.append("invalid_or_missing_wav")
    if report["status_counts"].get("placeholder") != 0:
        errors.append("placeholder_count_must_be_zero")
    if any(
        item.get("status") == "placeholder"
        for item in manifest.document.get("assets", [])
        if item.get("phrase_id") in {"PHRASE_078", "PHRASE_099", "PHRASE_107"}
    ):
        errors.append("new_onboarding_assets_cannot_be_placeholders")
    print(json.dumps({"ok": not errors, "errors": errors, "readiness": report},
                     ensure_ascii=False, sort_keys=True))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
