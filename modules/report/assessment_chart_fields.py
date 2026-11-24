"""Deterministic chart fields of the six-chapter assessment (content spec section 2.7).

The callback keeps every original string. These helpers only read a row's own
``value``, ``unit``, ``referenceRange`` and status text and derive numbers,
reference bounds, grade bands, a direction and a unified ``level``. Anything
that cannot be parsed stays ``None``; nothing is guessed from other rows.

The same helpers serve the projection (to append fields) and the sample
renderer (to fall back on string parsing when a field is absent).
"""

from __future__ import annotations

import math
import re
import unicodedata

FOCUS = 'focus'
BORDERLINE = 'borderline'
NORMAL = 'normal'
NOT_TESTED = 'notTested'
LEVELS = (FOCUS, BORDERLINE, NORMAL, NOT_TESTED)
LEVEL_LABELS = {FOCUS: '重点关注', BORDERLINE: '亚临床边界', NORMAL: '稳健达标', NOT_TESTED: '未检测'}
LEVEL_RANK = {NOT_TESTED: 0, NORMAL: 1, BORDERLINE: 2, FOCUS: 3}
ANCHOR_LIMIT = 6

_NUMBER = r'[+-]?(?:\d+(?:\.\d+)?|\.\d+)'
_PLAIN_NUMBER = re.compile(rf'^\s*({_NUMBER})\s*[↑↓]*\s*$')
_RANGE = re.compile(rf'({_NUMBER})\s*(?:-|~|～|至|–|—)\s*({_NUMBER})')
_LIMIT = re.compile(rf'(<=|>=|[<>≤≥])\s*({_NUMBER})')
_GRADE_BAND = re.compile(
    rf'(?:^|[;；\n])\s*(?P<label>[^;；\n:：]{{1,30}}?)\s*[:：]\s*[\[(（]?\s*'
    rf'(?P<range>(?:<=|>=|[<>≤≥])\s*{_NUMBER}|{_NUMBER}\s*(?:-|~|～|至|–|—)\s*{_NUMBER})'
)
_BAND_LABEL_NOISE = re.compile(r'^(?:原报告|原件|报告)')
_NORMAL_WORDS = frozenset(
    {
        '正常',
        '健康',
        '健康方向',
        '丰富',
        '均衡',
        '达标',
        '阴性',
        '未检出',
        '未检出异常',
        '未见异常',
        '未见明显异常',
        '正常范围内',
        '参考范围内',
        '原文正常所见',
        '平衡',
        '稳健达标',
        '低于实足年龄',
    }
)
_UP = re.compile(r'偏高|升高|增高|超标|过量|肥胖|超重|↑|偏多|高于')
_DOWN = re.compile(r'偏低|降低|减低|极低|不足|↓|低于(?!实足)|较低|偏少|缺乏')
_POSITIVE = re.compile(r'(?<![阴未])阳性')
_SEVERE_WORDS = re.compile(r'重点关注|肥胖|过量|危急')
_ABNORMAL_WORDS = re.compile(
    r'偏高|偏低|升高|降低|异常|阳性|超标|过量|肥胖|超重|有异常所见|失调|不健康|结节|息肉|囊肿|增生|'
    r'RADS|检出序列信号|极低|偏多|偏少|[↑↓]'
)
_FOCUS_DEVIATION = 0.2


def _clean(value) -> str:
    return unicodedata.normalize('NFKC', '' if value is None else str(value)).strip()


def parse_number(value, unit=None) -> float | None:
    """A plain measured number; censored, qualitative or composite values give ``None``."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = _clean(value)
    unit_text = _clean(unit)
    if unit_text and text.casefold().endswith(unit_text.casefold()):
        text = text[: -len(unit_text)].strip()
    match = _PLAIN_NUMBER.match(text)
    if not match:
        return None
    number = float(match[1])
    return number if math.isfinite(number) else None


def _band_bounds(text):
    text = _clean(text)
    bounds = _RANGE.fullmatch(text)
    if bounds:
        low, high = float(bounds[1]), float(bounds[2])
        return (low, high) if high >= low else None
    limit = re.fullmatch(rf'(<=|>=|[<>≤≥])\s*({_NUMBER})', text)
    if limit:
        bound = float(limit[2])
        return (None, bound) if limit[1] in {'<', '<=', '≤'} else (bound, None)
    return None


def parse_grade_bands(text) -> list[dict]:
    """Labelled grade bands such as ``偏低:(0-25)…正常:(25-75)…偏高:(75-100)``; fewer than two gives ``[]``."""
    bands = []
    for match in _GRADE_BAND.finditer(_clean(text)):
        bounds = _band_bounds(match['range'])
        if bounds is None:
            continue
        label = _BAND_LABEL_NOISE.sub('', match['label'].strip()).strip() or match['label'].strip()
        bands.append({'label': label, 'low': bounds[0], 'high': bounds[1]})
    return bands if len(bands) >= 2 else []


def parse_reference(text) -> dict | None:
    """``{low, high, kind}`` of a printed clinical reference; ``kind`` is range/upper/lower/qualitative."""
    raw = _clean(text)
    if not raw or parse_grade_bands(raw):
        return None
    match = _RANGE.search(raw)
    if match:
        low, high = float(match[1]), float(match[2])
        if high > low:
            return {'low': low, 'high': high, 'kind': 'range'}
        return None
    match = _LIMIT.search(raw)
    if match:
        bound = float(match[2])
        if match[1] in {'<', '<=', '≤'}:
            return {'low': None, 'high': bound, 'kind': 'upper'}
        return {'low': bound, 'high': None, 'kind': 'lower'}
    if not re.search(r'\d', raw):
        return {'low': None, 'high': None, 'kind': 'qualitative'}
    return None


def in_band(number, band) -> bool:
    low, high = band.get('low'), band.get('high')
    return (low is None or number >= low) and (high is None or number <= high)


def is_normal_label(label) -> bool:
    return _clean(label) in _NORMAL_WORDS


def status_text(row) -> str:
    return _clean(row.get('templateStatus') or row.get('status'))


def direction_of(status, number=None, low=None, high=None) -> str | None:
    """``up``/``down`` from the reported status, else from the number against its bounds."""
    text = _clean(status)
    if text and not is_normal_label(text):
        up, down = bool(_UP.search(text)), bool(_DOWN.search(text))
        if up != down:
            return 'up' if up else 'down'
    if number is not None:
        if high is not None and number > high:
            return 'up'
        if low is not None and number < low:
            return 'down'
    return None


def worst_level(levels) -> str | None:
    known = [level for level in levels if level in LEVEL_RANK]
    return max(known, key=LEVEL_RANK.__getitem__) if known else None


def _deviation(number, low, high):
    if high is not None and number > high:
        return (number - high) / abs(high) if high else math.inf
    if low is not None and number < low:
        return (low - number) / abs(low) if low else math.inf
    return 0.0


def clinical_chart_fields(row) -> dict:
    """Spec 2.7 fields of a clinical row; existing fields win over parsing."""
    reference = parse_reference(row.get('referenceRange'))
    number = parse_number(row.get('value'), row.get('unit'))
    low = reference['low'] if reference else None
    high = reference['high'] if reference else None
    fields = {
        'valueNumber': number,
        'referenceLow': low,
        'referenceHigh': high,
        'referenceKind': reference['kind'] if reference else None,
        'direction': direction_of(status_text(row), number, low, high),
        'level': clinical_level(row, number=number, reference=reference),
        'target': None,
    }
    for key in fields:
        if row.get(key) is not None:
            fields[key] = row[key]
    return fields


def clinical_level(row, *, number=None, reference=None) -> str | None:
    """Fallback level of one clinical row when the projection has not supplied ``level``."""
    if row.get('level') in LEVEL_RANK:
        return row['level']
    status = status_text(row)
    value = _clean(row.get('value'))
    if not status and not value:
        return None
    if number is None:
        number = parse_number(row.get('value'), row.get('unit'))
    if reference is None:
        reference = parse_reference(row.get('referenceRange'))
    if status.startswith('重点关注') or _POSITIVE.search(status) or (_POSITIVE.search(value) and not status):
        return FOCUS
    if number is not None and reference and reference['kind'] != 'qualitative':
        deviation = _deviation(number, reference['low'], reference['high'])
        if deviation >= _FOCUS_DEVIATION or (deviation > 0 and _SEVERE_WORDS.search(status)):
            return FOCUS
        if deviation > 0:
            return BORDERLINE
        if status and _ABNORMAL_WORDS.search(status) and not is_normal_label(status):
            return BORDERLINE
        return NORMAL
    if status and is_normal_label(status):
        return NORMAL
    if status and _SEVERE_WORDS.search(status):
        return FOCUS
    if status and _ABNORMAL_WORDS.search(status):
        return BORDERLINE
    if reference and reference['kind'] == 'qualitative' and value and value == _clean(row.get('referenceRange')):
        return NORMAL
    return None


def microbiome_kind(row) -> str:
    """abundance / score / structure / panel: how a microbiome row is drawn."""
    unit = _clean(row.get('unit'))
    if unit in {'%', '％'}:
        return 'abundance'
    number = parse_number(row.get('value'), row.get('unit'))
    if number is None:
        return 'panel'
    if unit in {'分', '评分', '得分'} or str(row.get('code') or '').startswith('template_function:'):
        return 'score'
    return 'structure'


def microbiome_level(row, *, number=None, bands=None, reference=None) -> str | None:
    """Unified level of a microbiome row.

    A single-sample deviation stays ``borderline``; only a dysbiosis grade is ``focus``.
    """
    if row.get('level') in LEVEL_RANK:
        return row['level']
    status = status_text(row)
    value = _clean(row.get('value'))
    text = status or value
    if not text:
        return None
    if '失调' in text:
        return FOCUS
    if bands is None:
        bands = parse_grade_bands(row.get('referenceRange'))
    if number is None:
        number = parse_number(row.get('value'), row.get('unit'))
    if bands and number is not None:
        reported = [band for band in bands if _clean(band['label']) == status]
        matched = reported or [band for band in bands if in_band(number, band)]
        if status and not reported and (is_normal_label(status) or _ABNORMAL_WORDS.search(status)):
            return NORMAL if is_normal_label(status) else BORDERLINE
        if matched:
            return NORMAL if any(is_normal_label(band['label']) for band in matched) else BORDERLINE
    if status and is_normal_label(status):
        return NORMAL
    if reference is None:
        reference = parse_reference(row.get('referenceRange'))
    if number is not None and reference and reference['kind'] != 'qualitative':
        return BORDERLINE if _deviation(number, reference['low'], reference['high']) > 0 else NORMAL
    if _ABNORMAL_WORDS.search(text) or '不健康' in text:
        return BORDERLINE
    return None


def microbiome_chart_fields(row) -> dict:
    """Spec 2.7 fields of a microbiome row by kind; existing fields win over parsing."""
    kind = microbiome_kind(row)
    number = parse_number(row.get('value'), row.get('unit'))
    bands = parse_grade_bands(row.get('referenceRange'))
    reference = parse_reference(row.get('referenceRange'))
    low = reference['low'] if reference else None
    high = reference['high'] if reference else None
    fields = {}
    if kind == 'abundance':
        fields = {'abundance': number, 'referenceLow': low, 'referenceHigh': high}
    elif kind == 'score':
        fields = {'score': number, 'bands': bands or None}
    elif kind == 'structure':
        fields = {
            'valueNumber': number,
            'referenceLow': low,
            'referenceHigh': high,
            'referenceKind': reference['kind'] if reference else None,
            'bands': bands or None,
        }
    fields['direction'] = direction_of(status_text(row), number, low, high)
    fields['level'] = microbiome_level(row, number=number, bands=bands, reference=reference)
    for key in fields:
        if row.get(key) is not None:
            fields[key] = row[key]
    return fields


def display_name(name) -> str:
    """Short reader name of an atom: the finding after a section label, without calculation notes."""
    original = '' if name is None else str(name).strip()
    text = original.rsplit(' · ', 1)[-1] if ' · ' in original else original
    text = re.sub(r'[（(]计算值[）)]|\s*及中国成人分级', '', text)
    text = re.sub(r'[（(][^（()）]*靶标面板[）)]$', '', text)
    return text.strip() or original


def anchor_of(row, level) -> dict:
    name = display_name(row.get('name'))
    value = _clean(row.get('value'))
    number = parse_number(row.get('value'), row.get('unit'))
    if number is None and (not value or value == name or len(value) > 12):
        value = None
    unit = (_clean(row.get('unit')) or None) if number is not None else None
    reference = parse_reference(row.get('referenceRange'))
    return {
        'name': name,
        'value': value,
        'unit': unit,
        'direction': row.get('direction')
        or direction_of(
            status_text(row), number, reference['low'] if reference else None, reference['high'] if reference else None
        ),
        'level': level,
    }


def _record_ids(row):
    return {source.get('recordId') for source in row.get('sources') or () if source.get('recordId')}


def system_atoms(code, group_codes, clinical, microbiome) -> list[dict]:
    """Atomic member rows of one overview system, read from chapters three and four."""
    if code == 'microbiome':
        rows = []
        for group in microbiome.get('structureGroups') or ():
            members = list(group.get('rows') or ())
            score = next((row for row in members if row.get('code') == 'microbiomeHealthScore'), None)
            grade = next((row for row in members if row.get('code') == 'balanceStatus'), None)
            if score is not None and grade is not None:
                # The total score and its dysbiosis grade are one finding.
                merged = {**score, 'status': grade.get('value'), 'level': row_level(grade, microbiome=True)}
                members = [merged, *(row for row in members if row is not score and row is not grade)]
            rows.extend(members)
        rows.extend(row for group in microbiome.get('organismGroups') or () for row in group.get('rows') or ())
        return rows
    rows = []
    for group in clinical.get('clinicalGroups') or ():
        if group.get('code') not in group_codes:
            continue
        panels = group.get('templateRows') or ()
        members = [row for panel in panels for row in panel.get('rows') or ()]
        rows.extend(members or group.get('rows') or ())
    if code == 'cardiometabolic':
        body = [row for group in clinical.get('bodyGroups') or () for row in group.get('rows') or ()]
        rows.extend(body or clinical.get('bodyMetrics') or ())
    return rows


def row_level(row, *, microbiome=False) -> str | None:
    if row.get('level') in LEVEL_RANK:
        return row['level']
    return microbiome_level(row) if microbiome else clinical_level(row)


def system_chart_fields(atoms, used_rows=(), *, microbiome=False) -> dict:
    """``level``, at most six abnormal ``anchors`` (used ones first) and ``moreCount`` of one system."""
    if not atoms:
        return {'level': NOT_TESTED, 'anchors': [], 'moreCount': 0}
    used = set().union(*(_record_ids(row) for row in used_rows)) if used_rows else set()
    levels, abnormal, seen = [], [], set()
    for index, row in enumerate(atoms):
        level = row_level(row, microbiome=microbiome)
        levels.append(level)
        if level not in {FOCUS, BORDERLINE}:
            continue
        anchor = anchor_of(row, level)
        identity = (anchor['name'], anchor['value'])
        if identity in seen:
            continue
        seen.add(identity)
        # A panel verdict such as "有益菌 异常" yields to its named members.
        generic = microbiome and microbiome_kind(row) == 'panel' and anchor['value'] in {'异常', '检出序列信号'}
        abnormal.append((-LEVEL_RANK[level], not (_record_ids(row) & used), generic, index, anchor))
    abnormal.sort(key=lambda item: item[:4])
    anchors = [item[4] for item in abnormal[:ANCHOR_LIMIT]]
    return {
        'level': worst_level(levels),
        'anchors': anchors,
        'moreCount': max(0, len(abnormal) - len(anchors)),
    }
