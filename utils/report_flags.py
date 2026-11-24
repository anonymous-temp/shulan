"""Literal report-flag normalization shared by extraction and profile ingestion.

These are source labels, not a medical interpretation of a value/reference range.
In particular, a phrase containing ``高`` need not mean a high measurement.
"""

from __future__ import annotations

_HIGH = frozenset({'偏高', '升高', '超标', '高', '↑', 'h', 'high'})
_LOW = frozenset({'偏低', '降低', '缺乏', '低', '↓', 'l', 'low'})
_NORMAL = frozenset({'正常', '未见异常', 'n', 'normal'})
_UNDIRECTED = frozenset({'异常', '失衡', '需求高', '风险升高', '高风险', '异常升高风险'})
_CRITICAL = frozenset({'危急', '危急值', 'critical'})


def _label(value):
    return value.strip().casefold() if isinstance(value, str) else ''


def normalize_report_flag(value: object) -> str | None:
    """Return the public directional label only for an explicit known source label."""
    label = _label(value)
    if label in _HIGH:
        return '偏高'
    if label in _LOW:
        return '偏低'
    if label in _NORMAL:
        return '正常'
    return None


def report_flag_interpretation(value: object) -> str | None:
    if _label(value) in _CRITICAL:
        return 'critical'
    return {'偏高': 'high', '偏低': 'low', '正常': 'normal'}.get(normalize_report_flag(value))


def report_flag_is_abnormal(value: object) -> bool:
    label = _label(value)
    return label in _HIGH | _LOW | _UNDIRECTED | _CRITICAL
