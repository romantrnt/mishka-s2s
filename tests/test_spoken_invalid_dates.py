"""Invalid named dates stay literal; they must never crash speech preparation."""
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "invalid_date_spoken_numbers", Path(__file__).parents[1] / "app/foundation/spoken_numbers.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize("raw, expected", [
    ("31 февраля", "тридцать один февраля"),
    ("31 февраля 2026 года", "тридцать один февраля две тысячи двадцать шесть года"),
    ("29 февраля 2025 года", "двадцать девять февраля две тысячи двадцать пять года"),
    ("00 января", "ноль ноль января"),
    ("32 ноября!", "тридцать два ноября!"),
    ("29 февраля 0000 года", "двадцать девять февраля ноль ноль ноль ноль года"),
    ("2026-02-31", "две тысячи двадцать шесть точка ноль два точка тридцать один"),
    ("31.02.2026", "тридцать один точка ноль два точка две тысячи двадцать шесть"),
    ("29 февраля 2024 года", "двадцать девятое февраля две тысячи двадцать четвёртого года"),
    ("24 ноября 2026 года", "двадцать четвёртое ноября две тысячи двадцать шестого года"),
])
def test_invalid_dates_do_not_invent_a_valid_calendar_date(raw, expected):
    assert module.normalize_spoken_numbers(raw) == expected
    assert module.normalize_spoken_numbers(expected) == expected


def test_actual_vox_preparation_preserves_invalid_literal_and_other_quantities():
    from app.foundation.voice_pipeline import prepare_vox_text

    assert prepare_vox_text("Не 31 февраля, а 24 ноября 2026 года. На улице −2°C.") == (
        "Не тридцать один февраля, а двадцать четвёртое ноября две тысячи двадцать шестого года. "
        "На улице минус два градуса по Цельсию.")
