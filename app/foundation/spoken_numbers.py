"""Deterministic Russian speech copy; never changes structured source values.

Explicit dates, clocks, quantities and ordinal suffixes carry grammar. Bare
numbers use cardinal speech; unknown units/prose are retained, never guessed.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import re
from num2words import num2words

_DIGITS = ('ноль', 'один', 'два', 'три', 'четыре', 'пять', 'шесть', 'семь', 'восемь', 'девять')
_MONTHS = ('января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря')
# nominative one/few/many, gender, genitive/dative/instrumental/prepositional
# singular/plural; these explicit units avoid a morphology model at synthesis.
_UNITS = {
 'degree': (('градус','градуса','градусов'),'masculine',('градуса','градусов'),('градусу','градусам'),('градусом','градусами'),('градусе','градусах')),
 'percent': (('процент','процента','процентов'),'masculine',('процента','процентов'),('проценту','процентам'),('процентом','процентами'),('проценте','процентах')),
 'meter': (('метр','метра','метров'),'masculine',('метра','метров'),('метру','метрам'),('метром','метрами'),('метре','метрах')),
 'kilometer': (('километр','километра','километров'),'masculine',('километра','километров'),('километру','километрам'),('километром','километрами'),('километре','километрах')),
 'centimeter': (('сантиметр','сантиметра','сантиметров'),'masculine',('сантиметра','сантиметров'),('сантиметру','сантиметрам'),('сантиметром','сантиметрами'),('сантиметре','сантиметрах')),
 'millimeter': (('миллиметр','миллиметра','миллиметров'),'masculine',('миллиметра','миллиметров'),('миллиметру','миллиметрам'),('миллиметром','миллиметрами'),('миллиметре','миллиметрах')),
 'gram': (('грамм','грамма','граммов'),'masculine',('грамма','граммов'),('грамму','граммам'),('граммом','граммами'),('грамме','граммах')),
 'kilogram': (('килограмм','килограмма','килограммов'),'masculine',('килограмма','килограммов'),('килограмму','килограммам'),('килограммом','килограммами'),('килограмме','килограммах')),
 'milligram': (('миллиграмм','миллиграмма','миллиграммов'),'masculine',('миллиграмма','миллиграммов'),('миллиграмму','миллиграммам'),('миллиграммом','миллиграммами'),('миллиграмме','миллиграммах')),
 'liter': (('литр','литра','литров'),'masculine',('литра','литров'),('литру','литрам'),('литром','литрами'),('литре','литрах')),
 'milliliter': (('миллилитр','миллилитра','миллилитров'),'masculine',('миллилитра','миллилитров'),('миллилитру','миллилитрам'),('миллилитром','миллилитрами'),('миллилитре','миллилитрах')),
 'hour': (('час','часа','часов'),'masculine',('часа','часов'),('часу','часам'),('часом','часами'),('часе','часах')),
 'minute': (('минута','минуты','минут'),'feminine',('минуты','минут'),('минуте','минутам'),('минутой','минутами'),('минуте','минутах')),
 'second': (('секунда','секунды','секунд'),'feminine',('секунды','секунд'),('секунде','секундам'),('секундой','секундами'),('секунде','секундах')),
}
_ABBREVIATIONS = {'%':'percent','м':'meter','км':'kilometer','см':'centimeter','мм':'millimeter','г':'gram','кг':'kilogram','мг':'milligram','л':'liter','мл':'milliliter','ч':'hour','мин':'minute','с':'second','сек':'second'}
_ALIASES = dict(_ABBREVIATIONS)
for _name, _unit in _UNITS.items():
    for _forms in (_unit[0], *_unit[2:]):
        for _form in _forms:
            _ALIASES[_form] = _name
_UNIT = r'(?:°\s*[CС]|℃|°|м\s*/\s*с|км\s*/\s*ч|' + '|'.join(sorted(map(re.escape, _ALIASES), key=len, reverse=True)) + ')'
_INT = r'[+\-−–±]?(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)'
_NUM = _INT + r'(?:[.,]\d+)?(?:[eE][+\-−]?\d+)?'
_RANGE = rf'{_NUM}\s*[-–—…]\s*{_NUM}'
_FRACTION = rf'{_INT}\s*/\s*\d+'
_MONTH = '(?:' + '|'.join(_MONTHS) + ')'
_TOKEN = re.compile(
 rf'(?<!\w)(?P<date>\d{{4}}-\d{{2}}-\d{{2}}|\d{{1,2}}\.\d{{1,2}}\.\d{{4}})(?!\d)'
 rf'|(?<!\w)(?P<datewords>\d{{1,2}}\s+{_MONTH}(?:\s+\d{{4}}\s+года)?)\b'
 r'|(?<!\w)(?P<clock>(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?)(?!\d)'
 rf'|(?<!\w)(?P<quantity>(?:{_RANGE}|{_FRACTION}|{_NUM})\s*{_UNIT})(?!\w)'
 rf'|(?<!\w)(?P<range>{_RANGE})(?!\w)'
 rf'|(?<!\w)(?P<fraction>{_FRACTION})(?!\w)'
 r'|(?<!\w)(?P<ordinal>\d+-(?:го|му|ым|ом|ая|ое|ый|ий|ую|ых|й|я|е|ю|м))(?!\w)'
 rf'|(?<!\w)(?P<number>{_NUM})(?!\w)', re.I)
_QUANTITY = re.compile(rf'(?P<number>{_RANGE}|{_FRACTION}|{_NUM})\s*(?P<unit>{_UNIT})', re.I)
_RANGE_PARTS = re.compile(rf'(?P<a>{_NUM})\s*[-–—…]\s*(?P<b>{_NUM})')


def _case(prefix: str) -> str:
    match = re.search(r'\b(до|от|около|после|без|более|менее|к|по|при|о|об|между|с|со)\s*$', prefix, re.I)
    if not match: return 'nominative'
    word = match[1].lower()
    return ('genitive' if word in {'до','от','около','после','без','более','менее'} else
            'dative' if word in {'к','по'} else 'prepositional' if word in {'при','о','об'} else 'instrumental')


def _words(raw: str, case='nominative', gender='masculine') -> str:
    value = re.sub(r'[ \u00a0\u202f]', '', raw).replace(',', '.')
    sign = 'плюс-минус ' if value.startswith('±') else 'минус ' if value.startswith(('-', '−', '–')) else 'плюс ' if value.startswith('+') else ''
    value = value.lstrip('+-−–±').replace('−', '-')
    if 'e' in value.lower():
        mantissa, exponent = re.split('[eE]', value)
        if abs(int(exponent)) > 30:
            return sign + _words(mantissa,case,gender) + ' умножить на десять в степени ' + _words(exponent)
        value = format(Decimal(value), 'f')
    try:
        if '/' in value:
            left,right = value.split('/')
            numerator,denominator = int(left),int(right)
            if denominator == 0: return sign + _words(left) + ' делить на ноль'
            denominator_case = 'genitive' if case == 'nominative' and numerator != 1 else case
            return sign + num2words(numerator,lang='ru',gender='feminine',case=case) + ' ' + num2words(denominator,lang='ru',to='ordinal',gender='feminine',case=denominator_case,plural=numerator != 1)
        if '.' in value:
            left,right = value.split('.')
            if case == 'nominative': return sign + num2words(value,lang='ru')
            whole_one = int(left) % 10 == 1 and int(left) % 100 != 11
            whole = {'genitive':('целой','целых'),'dative':('целой','целым'),'instrumental':('целой','целыми'),'prepositional':('целой','целых')}[case][0 if whole_one else 1]
            numerator = int(right)
            return sign + num2words(int(left),lang='ru',gender='feminine',case=case) + ' ' + whole + ' ' + num2words(numerator,lang='ru',gender='feminine',case=case) + ' ' + num2words(10**len(right),lang='ru',to='ordinal',gender='feminine',case=case,plural=numerator != 1)
        if len(value)>1 and value[0]=='0': return sign + ' '.join(_DIGITS[int(c)] for c in value)
        return sign + num2words(int(value),lang='ru',case=case,gender=gender)
    except (ValueError, OverflowError, NotImplementedError, KeyError):
        # Preserve exact precision outside the library's bounded scale vocabulary.
        return sign + ' '.join('запятая' if c=='.' else _DIGITS[int(c)] for c in value)


def _noun(raw: str, unit: str, case: str) -> str:
    forms,gender,*cases = _UNITS[unit]
    clean = re.sub(r'[ \u00a0\u202f]', '', raw).lstrip('+-−–±').replace('−','-')
    if 'e' in clean.lower():
        exponent = int(re.split('[eE]',clean)[1])
        if exponent > 30: return forms[2]
        if exponent < -30: return cases[0][0]
        value = Decimal(clean.replace(',', '.'))
        if value != value.to_integral_value(): return cases[0][0]
        clean = str(int(value))
    if '.' in clean or ',' in clean or '/' in clean: return cases[0][0]
    n = int(clean)
    if case != 'nominative':
        return cases[{'genitive':0,'dative':1,'instrumental':2,'prepositional':3}[case]][0 if n%10==1 and n%100!=11 else 1]
    ending = n % 100
    return forms[0 if ending%10==1 and ending!=11 else 1 if ending%10 in (2,3,4) and ending not in (12,13,14) else 2]


def _quantity(raw: str, case: str) -> str:
    match = _QUANTITY.fullmatch(raw)
    number,unit = match['number'],match['unit'].lower()
    celsius = bool(re.fullmatch(r'°\s*[cс]|℃',unit))
    suffix = ' по Цельсию' if celsius else ''
    rate = re.fullmatch(r'(м|км)\s*/\s*(с|ч)',unit)
    if celsius or unit=='°': key='degree'
    elif rate:
        key = 'meter' if rate[1]=='м' else 'kilometer'
        suffix = ' в секунду' if rate[2]=='с' else ' в час'
    else: key=_ALIASES[unit]
    pair = _RANGE_PARTS.fullmatch(number)
    if pair:
        return f'от {_words(pair["a"], "genitive", _UNITS[key][1])} до {_words(pair["b"], "genitive", _UNITS[key][1])} {_noun(pair["b"],key,"genitive")}{suffix}'
    return f'{_words(number,case,_UNITS[key][1])} {_noun(number,key,case)}{suffix}'


_ARITHMETIC = re.compile(rf'(?<!\w){_NUM}(?:\s*[+−\-×*·÷/:=±]\s*{_NUM})+(?:\s*{_UNIT})?(?!\w)', re.I)
_OPERATORS = {'+':'плюс','-':'минус','−':'минус','×':'умножить на','*':'умножить на','·':'умножить на','÷':'делить на','/':'делить на',':':'делить на','=':'равно','±':'плюс-минус'}

def _equations(text: str) -> str:
    def replace(match):
        raw=match[0]
        if not any(operator in raw.lstrip('+-−–±') for operator in ('=','+','×','*','·','÷')): return raw
        parts=[]; position=0
        while position<len(raw):
            number=re.match(_NUM,raw[position:])
            if number is None: return raw
            rest=raw[position:]
            quantity=_QUANTITY.fullmatch(rest)
            if quantity:
                parts.append(_quantity(rest,'nominative'))
                break
            parts.append(_words(number[0])); position+=number.end()
            if position==len(raw): break
            operator=re.match(r'\s*([+−\-×*·÷/:=±])\s*',raw[position:])
            if operator is None: return raw
            parts.append(_OPERATORS[operator[1]]); position+=operator.end()
        return ' '.join(parts)
    return _ARITHMETIC.sub(replace,text)


def normalize_spoken_numbers(text: str) -> str:
    """Return a speakable copy, retaining punctuation and nonnumeric words."""
    text=_equations(text)
    def replace(match):
        raw=match[0]; prefix=text[max(0,match.start()-40):match.start()]; case=_case(prefix)
        if re.search(r'\bс(?:о)?\s*$',prefix,re.I) and re.match(r'\s+до\b',text[match.end():],re.I): case='genitive'
        if match['date'] or match['datewords']:
            if match['datewords']:
                parts=raw.split(); day=int(parts[0]); month=_MONTHS.index(parts[1].lower())+1
                year=int(parts[2]) if len(parts)>2 else None
            else:
                parts=raw.split('-' if '-' in raw else '.')
                year,month,day=map(int,parts) if '-' in raw else map(int,reversed(parts))
            try: date(2000 if year is None else year,month,day)
            except ValueError:
                # An invalid date remains a literal sequence, not a fabricated date.
                if match['datewords']:
                    return re.sub(r'\d+', lambda token: _words(token[0]), raw)
                return ' точка '.join(_words(part) for part in re.split(r'[.-]',raw))
            if re.search(r'\b(?:с|со|приём|прием|встреча|занятие|запись)\s*$',prefix,re.I): case='genitive'
            day_case=case if case in {'genitive','dative','instrumental','prepositional'} else 'nominative'
            result=num2words(day,lang='ru',to='ordinal',gender='neuter',case=day_case)+' '+_MONTHS[month-1]
            return result+(f' {num2words(year,lang="ru",to="ordinal",case="genitive")} года' if year else '')
        if match['clock']:
            parts=raw.split(':')
            return ' '.join(_words(part,case) if int(part)>=10 else ('ноль ноль' if int(part)==0 else 'ноль '+_words(str(int(part)),case)) if i else _words(str(int(part)),case) for i,part in enumerate(parts))
        if match['quantity']: return _quantity(raw,case)
        if match['range']:
            pair=_RANGE_PARTS.fullmatch(raw)
            return f'от {_words(pair["a"],"genitive")} до {_words(pair["b"],"genitive")}'
        if match['ordinal']:
            number,suffix=raw.split('-')
            gender='feminine' if suffix in {'я','ая','ую','ю'} else 'neuter' if suffix in {'е','ое'} else 'masculine'
            if suffix=='м':
                if re.search(r'\b(?:в|на|о|об|при)\s*$',prefix,re.I): case='prepositional'
                elif case!='instrumental': return _words(number)+' эм'
            case={'ю':'accusative','ую':'accusative','го':'genitive','му':'dative','ым':'instrumental','ом':'prepositional','ых':'genitive'}.get(suffix,case)
            return num2words(int(number),lang='ru',to='ordinal',gender=gender,case=case)
        return _words(raw,case)
    return _TOKEN.sub(replace,text)


_SENTENCE = re.compile(r'.+?[.!?…]+(?=\s|$)', re.S)

def sentence_matches(text: str, *, final: bool=False) -> list[re.Match[str]]:
    """Existing sentence matcher with a fence for unfinished numeric dots.

    A stream ending in ``2.`` may continue as ``2.5`` or a numeric date. Wait
    for the next token or explicit EOF; punctuation/prosody stays unchanged.
    """
    result=[]
    for match in _SENTENCE.finditer(text):
        value=match[0]
        if value.endswith('.') and len(value)>1 and value[-2].isdigit():
            tail=text[match.end():].lstrip()
            if tail and tail[0].isdigit():
                return []
            if not tail and not final:
                continue
        result.append(match)
    return result
