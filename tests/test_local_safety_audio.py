"""The local safety response retains stable recorded phrase IDs and truthful audio."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import pytest

from app.foundation.voice_catalog import REQUIRED_COUNT, REQUIRED_PHRASE_IDS, load_phrase_catalog

ROOT = Path(__file__).resolve().parents[1]
AUDIO = ROOT / "content/audio"


def test_local_safety_catalog_keeps_stable_ids_and_only_the_truthful_excerpt():
    catalog = load_phrase_catalog(AUDIO / "phrase_catalog_ru.txt")
    document = json.loads((AUDIO / "voice_manifest.json").read_text())
    assets = {item["phrase_id"]: item for item in document["assets"]}
    assert REQUIRED_COUNT == 102
    assert set(catalog) == set(assets) == REQUIRED_PHRASE_IDS
    assert catalog["PHRASE_091"] == "Позови взрослого рядом"
    assert catalog["PHRASE_108"] == "Сколько тебе лет?"
    assert catalog["PHRASE_017"] == "Спасибо. Хочешь рассказать или позвать взрослого?"
    assert not {"PHRASE_072", "PHRASE_086", "PHRASE_088", "PHRASE_089", "PHRASE_090", "PHRASE_092"} & set(catalog)
    assert {item.stem for item in (AUDIO / "voice").glob("*.wav")} == REQUIRED_PHRASE_IDS
    assert document["required_recorded_count"] == document["required_approved_count"] == 102
    assert not any("передаю" in text.lower() or "получил просьбу" in text.lower()
                   or "передать просьбу" in text.lower() or "уже идёт" in text.lower()
                   for text in catalog.values())
    asset = assets["PHRASE_091"]
    assert asset["asset_sha256"] == "845b5b4cf8654d9b57c6dfcb5eed8f99abc6392bf2db36840e65ebf90634e43f"
    assert hashlib.sha256((AUDIO / asset["asset_path"]).read_bytes()).hexdigest() == asset["asset_sha256"]
    assert asset["approved_by"] == "approved_source_excerpt"
    provenance = asset["provenance"]
    assert provenance["kind"] == "lossless_excerpt_of_approved_recording"
    assert provenance["source_start_frame"] == 57330
    assert provenance["source_end_frame"] - provenance["source_start_frame"] == asset["frame_count"]
    with wave.open(str(AUDIO / asset["asset_path"]), "rb") as recording:
        assert recording.getnframes() == 43835
        assert recording.getframerate() == 22050
        # A silent lead-in protects the first spoken phoneme at the edit boundary.
        assert recording.readframes(4410) == bytes(8820)


@pytest.mark.parametrize("row", ["text without ID", "PHRASE_1\ttext", "PHRASE_001\t", "PHRASE_001\ttext\textra", "PHRASE_001\ttext\nPHRASE_001\tother"])
def test_catalog_rejects_ambiguous_or_duplicate_ids(tmp_path, row):
    path = tmp_path / "catalog.txt"
    path.write_text(row, encoding="utf-8")
    with pytest.raises(ValueError):
        load_phrase_catalog(path)


def test_manifest_discovery_preserves_ids_and_does_not_approve_new_audio(tmp_path):
    (tmp_path / "voice").mkdir()
    shutil.copyfile(AUDIO / "voice/PHRASE_091.wav", tmp_path / "voice/PHRASE_091.wav")
    path = tmp_path / "manifest.json"
    subprocess.run([sys.executable, str(ROOT / "scripts/build_voice_manifest.py"),
                    "--catalog", str(AUDIO / "phrase_catalog_ru.txt"),
                    "--manifest", str(path)], check=True, capture_output=True, text=True)
    document = json.loads(path.read_text())
    assets = {item["phrase_id"]: item for item in document["assets"]}
    assert set(assets) == REQUIRED_PHRASE_IDS
    assert assets["PHRASE_091"]["transcript"] == "Позови взрослого рядом"
    assert assets["PHRASE_091"]["status"] == "recorded_pending_approval"
    assert assets["PHRASE_091"]["approved_by"] is None
    assert document["required_recorded_count"] == 102
    assert document["approved_count"] == 0
