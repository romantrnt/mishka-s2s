import importlib.util
from pathlib import Path

import pytest


def load_launcher():
    path = Path(__file__).parents[1] / "scripts" / "run_stand.py"
    spec = importlib.util.spec_from_file_location("mishka_run_stand", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_canonical_launcher_defaults_to_port_8000(monkeypatch):
    launcher = load_launcher()
    monkeypatch.delenv("MISHKA_PORT", raising=False)
    monkeypatch.delenv("MISHKA_ALLOW_NONCANONICAL_PORT", raising=False)
    assert launcher.resolve_port() == 8000


def test_canonical_launcher_rejects_8015_without_explicit_test_escape_hatch(monkeypatch):
    launcher = load_launcher()
    monkeypatch.setenv("MISHKA_PORT", "8015")
    monkeypatch.delenv("MISHKA_ALLOW_NONCANONICAL_PORT", raising=False)
    with pytest.raises(RuntimeError, match="port 8000"):
        launcher.resolve_port()


def test_noncanonical_port_requires_explicit_test_escape_hatch(monkeypatch):
    launcher = load_launcher()
    monkeypatch.setenv("MISHKA_PORT", "8015")
    monkeypatch.setenv("MISHKA_ALLOW_NONCANONICAL_PORT", "1")
    assert launcher.resolve_port() == 8015


def test_runpod_start_forces_canonical_port_and_uses_stand_launcher():
    script = (Path(__file__).parents[1] / "scripts" / "runpod_start.sh").read_text()
    assert "MISHKA_PORT=8000" in script
    assert "scripts/run_stand.py" in script
    assert "MISHKA_ALLOW_NONCANONICAL_PORT" in script
    assert "8015" not in script


def test_legacy_main_fails_fast_instead_of_exporting_none_app():
    source = (Path(__file__).parents[1] / "app" / "main.py").read_text()
    assert "app = None" not in source
    assert "deprecated" in source.lower()


def test_operations_documents_one_canonical_launcher():
    docs = (Path(__file__).parents[1] / "docs" / "OPERATIONS.md").read_text()
    assert "./scripts/runpod_start.sh" in docs
    assert "only canonical launcher" in docs
    assert "runpod_bootstrap.sh` is deprecated" in docs


def test_canonical_and_docker_launchers_do_not_select_legacy_or_mock_app():
    root = Path(__file__).parents[1]
    start = (root / "scripts" / "runpod_start.sh").read_text()
    docker = (root / "docker" / "entrypoint.sh").read_text()
    assert "app.main:app" not in start
    assert "run_mock.py" not in start
    assert "runpod_start.sh" in docker
    assert "app.main:app" not in docker
    assert "MISHKA_STAND" not in docker
