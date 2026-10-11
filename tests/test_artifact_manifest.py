from __future__ import annotations

import importlib.util
import json
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "artifact_manifest.py"
SPEC = importlib.util.spec_from_file_location("artifact_manifest", SCRIPT)
assert SPEC and SPEC.loader
artifact_manifest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(artifact_manifest)


def test_environment_manifest_excludes_secret_values(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text("YANDEX_WEATHER_API_KEY=secret-value\nPUBLIC_MODE=1\n")
    (tmp_path / ".bootstrap-contract").write_text("IMAGE=runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404\nPYTHON=3.12\nTORCH=2.8.0\nCUDA=12.8\n")
    (tmp_path / "requirements-production.lock").write_text("numpy==2.3.3\n")
    data = artifact_manifest.build_environment(tmp_path)
    assert data["base_image"] == "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"
    assert "YANDEX_WEATHER_API_KEY" in data["environment_names"]
    assert "secret-value" not in json.dumps(data)
    assert data["secret_values_included"] is False


def test_model_inventory_records_hash_and_size(tmp_path: Path) -> None:
    model = tmp_path / "models" / "rvc" / "voice.pth"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"model")
    rvc = next(item for item in artifact_manifest.build_model_inventory(tmp_path)["models"] if item["model_name"] == "rvc")
    assert rvc["provider"] == "rvc"
    assert rvc["bytes"] == 5
    assert rvc["files"][0]["sha256"] == artifact_manifest.sha256(model)


def test_ollama_manifest_is_metadata_only_and_excludes_tmp(tmp_path: Path) -> None:
    cache = tmp_path / "ollama"
    (cache / "manifests").mkdir(parents=True)
    (cache / "manifests" / "index.json").write_text('{"models": []}\n')
    (cache / "tmp").mkdir()
    (cache / "tmp" / "partial.blob").write_bytes(b"temporary")
    data = artifact_manifest.build_ollama(tmp_path, cache)
    assert [item["path"] for item in data["files"]] == ["manifests/index.json"]
    assert "sha256" not in data["files"][0]


def test_source_backup_rejects_cache_members(tmp_path: Path) -> None:
    source = tmp_path / "project.py"
    source.write_text("print('ok')\n")
    safe, unsafe = tmp_path / "safe.tar.gz", tmp_path / "unsafe.tar.gz"
    with tarfile.open(safe, "w:gz") as archive:
        archive.add(source, arcname="project.py")
    with tarfile.open(unsafe, "w:gz") as archive:
        archive.add(source, arcname=".venv/bin/project.py")
    assert artifact_manifest.build_source_backup(safe)["source_only"] is True
    forbidden = artifact_manifest.build_source_backup(unsafe)
    assert forbidden["source_only"] is False
    assert forbidden["forbidden_members"] == [".venv/bin/project.py"]


def test_write_artifact_is_no_overwrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(artifact_manifest, "now", lambda: "2026-10-04T00:00:00Z")
    data = {"schema": artifact_manifest.SCHEMA, "artifact_type": "ollama_cache", "created_at": artifact_manifest.now(), "cache_path": "/tmp/ollama", "secret_values_included": False}
    target = artifact_manifest.write_artifact(data, tmp_path / "artifacts")
    artifact_manifest.validate_manifest(target)
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        artifact_manifest.write_artifact(data, tmp_path / "artifacts")


def test_static_contract_has_four_types_and_no_destructive_copying() -> None:
    text = SCRIPT.read_text()
    for artifact_type in ("source_backup", "environment", "model_inventory", "ollama_cache"):
        assert artifact_type in text
    assert "FORBIDDEN_SOURCE" in text
    assert "shutil.copytree" not in text
    assert "rmtree(" not in text
    assert "refusing to overwrite" in text
