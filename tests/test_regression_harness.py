
from pathlib import Path

import pytest

from scripts.run_regression import MARKER_MODES, SMOKE_TARGETS, parse_pins, pytest_args

pytestmark = pytest.mark.unit


def test_test_lock_has_exact_pytest_pin():
    root = Path(__file__).parents[1]
    assert parse_pins(root / "requirements-test.lock")["pytest"] == "9.1.1"


def test_runner_modes_have_distinct_contracts():
    assert pytest_args("unit", []) == ["-m", MARKER_MODES["unit"]]
    assert pytest_args("integration", []) == ["-m", "integration"]
    assert pytest_args("acceptance", []) == ["-m", "acceptance"]
    assert pytest_args("smoke", []) == list(SMOKE_TARGETS)
    assert pytest_args("full", []) == ["-m", "not acceptance"]
    assert pytest_args("full", [], "production") == []
    with pytest.raises(ValueError):
        pytest_args("targeted", [])
    assert pytest_args("targeted", ["tests/test_s2s.py::test_audio_chunk"]) == [
        "tests/test_s2s.py::test_audio_chunk"
    ]

