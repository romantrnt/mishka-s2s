"""Speech copies preserve numeric meaning; no model, network or audio hardware."""
import pytest
from app.foundation.spoken_numbers import normalize_spoken_numbers as spoken

@pytest.mark.parametrize('raw,expected',[
 ('−2°C','минус два градуса по Цельсию'),('-2 °С','минус два градуса по Цельсию'),
 ('+1℃','плюс один градус по Цельсию'),('0°C','ноль градусов по Цельсию'),
 ('21%','двадцать один процент'),('22 %','двадцать два процента'),('11%','одиннадцать процентов'),
 ('−2,50 °C','минус две целых пятьдесят сотых градуса по Цельсию'),
 ('2-3 минуты','от двух до трёх минут'),('−2–−1°C','от минус двух до минус одного градуса по Цельсию'),
 ('1/2 литра','одна вторая литра'),('2/3 л','две третьих литра'),
 ('5 мл','пять миллилитров'),('3 м/с','три метра в секунду'),
 ('К 3 часам','К трём часам'),('Около 2 кг','Около двух килограммов'),
 ('При 2 °C','При двух градусах по Цельсию'),
 ('24.11.2026','двадцать четвёртое ноября две тысячи двадцать шестого года'),
 ('2026-11-24','двадцать четвёртое ноября две тысячи двадцать шестого года'),
 ('До 24 ноября 2026 года','До двадцать четвёртого ноября две тысячи двадцать шестого года'),
 ('Приём в 14:30, кабинет 12','Приём в четырнадцать тридцать, кабинет двенадцать'),
 ('В 09:05','В девять ноль пять'),('5-й этаж','пятый этаж'),('До 5-го этажа','До пятого этажа'),
 ('Код 007','Код ноль ноль семь'),('1 234,50 м','одна тысяча двести тридцать четыре целых пятьдесят сотых метра'),
])
def test_numeric_speech(raw,expected):
    assert spoken(raw)==expected
    assert spoken(expected)==expected


def test_punctuation_words_and_structure_are_retained():
    data={'temperature_c':-2.5,'summary':'Ясно, −2,5°C!','room':12}
    original=dict(data)
    assert spoken(data['summary'])=='Ясно, минус две целых пять десятых градуса по Цельсию!'
    assert data==original
    assert spoken('Все кнопки работают лучше, чем вчера…')=='Все кнопки работают лучше, чем вчера…'


@pytest.mark.parametrize('raw,expected',[
 ('С 2 до 3 минут','С двух до трёх минут'),
 ('С 24.11.2026','С двадцать четвёртого ноября две тысячи двадцать шестого года'),
 ('−2e-2°C','минус ноль целых две сотых градуса по Цельсию'),
 ('2e3 м','две тысячи метров'),
 ('–2°C','минус два градуса по Цельсию'),
])
def test_explicit_numeric_contexts(raw,expected):
    assert spoken(raw)==expected


def test_numeric_sentence_end_waits_for_complete_token():
    from app.foundation.spoken_numbers import sentence_matches
    for full in ['Температура −2.5°C.', 'Сегодня 24.11.2026.']:
        for end in range(1,len(full)):
            assert sentence_matches(full[:end])==[]
        assert [m[0] for m in sentence_matches(full,final=True)]==[full]
    assert sentence_matches('Нажми 2.')==[]
    assert [m[0] for m in sentence_matches('Нажми 2. Потом продолжим.')]==['Нажми 2.',' Потом продолжим.']


@pytest.mark.parametrize('raw,expected',[
 ('±2°C','плюс-минус два градуса по Цельсию'),
 ('+1e-05 °C','плюс ноль целых одна стотысячная градуса по Цельсию'),
 ('−0,00°C','минус ноль целых ноль сотых градуса по Цельсию'),
 ('12°C','двенадцать градусов по Цельсию'),('14°C','четырнадцать градусов по Цельсию'),
 ('21°C','двадцать один градус по Цельсию'),('22°C','двадцать два градуса по Цельсию'),
 ('25°C','двадцать пять градусов по Цельсию'),
 ('Сегодня 24.11.2026','Сегодня двадцать четвёртое ноября две тысячи двадцать шестого года'),
 ('Приём 24.11.2026 в 09:05, кабинет 12','Приём двадцать четвёртого ноября две тысячи двадцать шестого года в девять ноль пять, кабинет двенадцать'),
])
def test_actual_weather_and_calendar_families(raw,expected):
    assert spoken(raw)==expected
    assert spoken(expected)==expected


def test_numeric_stage_does_not_touch_internal_phonetic_marks():
    assert spoken('Ид+ёт приём, +2°C.')=='Ид+ёт приём, плюс два градуса по Цельсию.'


from test_experimental_voice import configured, wait_command
from app.foundation.experimental_voice import ExperimentalVoiceUnavailable

@pytest.mark.parametrize('route',['qwen765','voxcpm765','vox555'])
@pytest.mark.parametrize('method',['whole','semantic','native'])
def test_real_worker_request_has_normalized_speech(configured,route,method):
    import asyncio
    provider,_,worker=configured(route)
    async def run():
        try:
            if method=='native':
                chunks=[part async for part in provider.synthesize_audio_stream('−2,50°C',17,lambda:False,first_chunk=True)]
                assert chunks and all(part.token==17 for part in chunks)
            elif method=='semantic':
                await provider.synthesize_stream_chunk('−2,50°C',17,lambda:False,first_chunk=True)
            else:
                await provider.synthesize('−2,50°C',17,lambda:False)
            outgoing=(await wait_command(worker)).split(':',1)[1]
            assert outgoing=='минус две целых пятьдесят сотых градуса по Цельсию'
        finally:
            await provider.close()
    asyncio.run(run())

@pytest.mark.parametrize('method',['whole','native'])
def test_expanded_request_cannot_bypass_worker_text_cap(configured,method):
    import asyncio
    provider,_,worker=configured('vox555',max_text_chars=8)
    async def run():
        try:
            with pytest.raises(ExperimentalVoiceUnavailable,match='text_limit'):
                if method=='native':
                    async for _ in provider.synthesize_audio_stream('−2°C',1,lambda:False,first_chunk=True):pass
                else:
                    await provider.synthesize('−2°C',1,lambda:False)
            assert provider.jobs_submitted==0 and provider._process is None
            assert not worker.with_suffix('.active').exists()
        finally:
            await provider.close()
    asyncio.run(run())


@pytest.mark.parametrize('raw,expected',[
 ('2+3=5','два плюс три равно пять'),
 ('2-3=-1','два минус три равно минус один'),
 ('2×3=6','два умножить на три равно шесть'),
 ('6/2=3','шесть делить на два равно три'),
 ('−2+3=1°C','минус два плюс три равно один градус по Цельсию'),
 ('2-3 минуты','от двух до трёх минут'),
 ('2e31°C','два умножить на десять в степени тридцать один градусов по Цельсию'),
 ('2e-31°C','два умножить на десять в степени минус тридцать один градуса по Цельсию'),
])
def test_formula_operators_do_not_solve_or_erase_values(raw,expected):
    assert spoken(raw)==expected


def test_large_scientific_unit_does_not_materialize_unbounded_integer(monkeypatch):
    import app.foundation.spoken_numbers as module
    def forbidden(*args):raise AssertionError('large exponent must not construct Decimal')
    monkeypatch.setattr(module,'Decimal',forbidden)
    assert module.normalize_spoken_numbers('2e31°C')=='два умножить на десять в степени тридцать один градусов по Цельсию'


@pytest.mark.parametrize('raw,expected',[
 ('До 21°C','До двадцати одного градуса по Цельсию'),
 ('До 11°C','До одиннадцати градусов по Цельсию'),
 ('К 21 метру','К двадцати одному метру'),
 ('К 101 метру','К ста одному метру'),
 ('К 111 метрам','К ста одиннадцати метрам'),
 ('При 21°C','При двадцати одном градусе по Цельсию'),
 ('При 11°C','При одиннадцати градусах по Цельсию'),
 ('На 3-ю кнопку','На третью кнопку'),
 ('На 5-м этаже','На пятом этаже'),
])
def test_compound_one_oblique_units_and_explicit_ordinal_suffix(raw,expected):
    assert spoken(raw)==expected


@pytest.mark.parametrize('raw,expected',[
 ('2+3','два плюс три'),('2×3','два умножить на три'),('2÷3','два делить на три'),
])
def test_unambiguous_arithmetic_without_equals(raw,expected):
    assert spoken(raw)==expected
