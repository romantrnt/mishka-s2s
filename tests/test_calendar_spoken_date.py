"""Actual typed-calendar -> current Vox normalizer, without provider facts."""
from datetime import datetime

import pytest

from app.foundation.canonical_services import TypedInfoRenderer
from app.foundation.voice_pipeline import prepare_vox_text


@pytest.mark.asyncio
@pytest.mark.parametrize('instant,expected', [
    ('2026-11-24T12:00:00+00:00', 'Сегодня двадцать четвёртое ноября две тысячи двадцать шестого года.'),
    ('2026-01-01T12:00:00+00:00', 'Сегодня первое января две тысячи двадцать шестого года.'),
    ('2024-02-29T12:00:00+00:00', 'Сегодня двадцать девятое февраля две тысячи двадцать четвёртого года.'),
    ('2026-12-31T22:00:00+00:00', 'Сегодня первое января две тысячи двадцать седьмого года.'),
])
async def test_calendar_speaks_ordinals_in_the_selected_timezone(instant, expected):
    renderer = TypedInfoRenderer(None, {'timezone':'Europe/Moscow'}, None,
                                 now=lambda: datetime.fromisoformat(instant))
    report = await renderer.render('date')
    assert report['kind'] == 'date'
    assert prepare_vox_text(report['text']) == expected
