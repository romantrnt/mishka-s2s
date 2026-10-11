import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def load_launcher():
    path = ROOT / "scripts" / "run_stand.py"
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


def test_noncanonical_port_requires_explicit_escape_hatch(monkeypatch):
    launcher = load_launcher()
    monkeypatch.setenv("MISHKA_PORT", "8015")
    monkeypatch.delenv("MISHKA_ALLOW_NONCANONICAL_PORT", raising=False)
    with pytest.raises(RuntimeError, match="port 8000"):
        launcher.resolve_port()
    monkeypatch.setenv("MISHKA_ALLOW_NONCANONICAL_PORT", "1")
    assert launcher.resolve_port() == 8015


def test_root_launcher_uses_stand_process():
    source = (ROOT / "start.sh").read_text()
    assert "scripts/run_stand.py" in source


def test_operations_document_root_launcher():
    docs = (ROOT / "docs" / "OPERATIONS.md").read_text()
    assert "./start.sh" in docs


def test_docker_launcher_uses_root_launcher():
    source = (ROOT / "docker" / "entrypoint.sh").read_text()
    assert "CANONICAL=\"$ROOT/start.sh\"" in source
