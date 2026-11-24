"""Shared evaluate-only rules for usable results and explicit absence.

Source values and labels are never rewritten here. A quantitative zero and a
qualitative not-detected result have different meanings.
"""
from __future__ import annotations

from collections.abc import Mapping
import math
import re
import unicodedata


def _get(row, snake, camel=None):
    if isinstance(row, Mapping):
        return row.get(camel or snake, row.get(snake))
    return getattr(row, snake, None)


def has_risk_index(row) -> bool:
    """Accept a real source index, including zero or an explicit numeric bound.

    Boolean detection flags and association headings do not supply an index.
    This predicate does not select a scale or calculate a risk grade.
    """
    value = _get(row, 'value')
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, str):
        value = unicodedata.normalize('NFKC', value).strip()
        value = re.sub(r'^(?:<=?|>=?|≤|≥)\s*', '', value)
        if not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', value):
            return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def is_confirmed_negative_health(row) -> bool:
    """Hide only an explicit, non-abnormal qualitative negative result.

    In particular a missing beneficial organism (zero/low) is an abnormal
    finding even when the source parser also gives it a not_detected flag.
    """
    value = _get(row, 'value')
    if not isinstance(value, str):
        return False
    grades = ' '.join(str(_get(row, snake, camel) or '') for snake, camel in (
        ('source_status', 'sourceStatus'), ('assessment_level', 'assessmentLevel')))
    if re.search(r'异常|偏低|偏高|缺乏|不足|超标|阳性|不健康|失衡|中(?:低|高)?风险|高风险|注意|较弱|极低|↑|↓|\babnormal\b|\bpositive\b', grades, re.I):
        return False
    literal = unicodedata.normalize('NFKC', value).strip().casefold()
    return bool(re.fullmatch(r'未检出(?:超标)?|未检测到|未查见|阴性|negative|not\s+detected', literal))


def is_undetected_result(row) -> bool:
    """An explicit negative detection is omitted, never inferred from zero.

    An abnormal low/missing beneficial organism remains a real finding.
    Clinical negative assays retain their normal-result role unless the
    actual result explicitly says that its target was not detected.
    """
    grades = ' '.join(str(_get(row, snake, camel) or '') for snake, camel in (
        ('source_status', 'sourceStatus'), ('assessment_level', 'assessmentLevel')))
    if re.search(r'异常|偏低|偏高|缺乏|不足|超标|阳性|不健康|失衡|中(?:低|高)?风险|高风险|注意|较弱|极低|↑|↓|\babnormal\b|\bpositive\b', grades, re.I):
        return False
    labels = [_get(row, 'value'), _get(row, 'source_status', 'sourceStatus')]
    for label in labels:
        if not isinstance(label, str):
            continue
        value = unicodedata.normalize('NFKC', label).strip().casefold()
        if re.fullmatch(r'未检出(?:超标)?|未检测到|未检测出|未查见|未见相关菌群异常|not[_\s]+detected|undetected', value):
            return True
    sources = _get(row, 'sources') or []
    if sources and all(_get(source, 'source_code', 'sourceCode') == 'C' for source in sources):
        return is_confirmed_negative_health(row)
    return False
