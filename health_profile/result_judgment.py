"""Read-only interpretation of indicator results for the profile projection."""

from __future__ import annotations

import re
from decimal import Decimal
from decimal import InvalidOperation

from utils.report_observation_comparison import normalize_count_unit_exponents

from .models import FactValueType
from .models import HealthFact

_NUMBER = r'[+-]?(?:(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:[.][0-9]*)?|[.][0-9]+)(?:[eE][+-]?[0-9]+)?'
_UNIT_LETTER = r'A-Za-z\u00b5\u03bc%\uff05\u2030\u4e00-\u9fff'
_UNIT = rf'(?:[xX\u00d7]?\s*10\^[+-]?[0-9]+\s*/\s*|/)?[{_UNIT_LETTER}][{_UNIT_LETTER}/.*^0-9\u00b2\u00b3 -]*'
_BOUND = re.compile(rf'\s*(<=|>=|<|>|\u2264|\u2265)\s*({_NUMBER})\s*({_UNIT})?\s*')
_VALUE = re.compile(rf'\s*(<=|>=|<|>|\u2264|\u2265)?\s*({_NUMBER})\s*({_UNIT})?\s*')
_INTERVAL = re.compile(rf'\s*({_NUMBER})\s*[-\u2013\u2014~\uff5e\u81f3]\s*({_NUMBER})\s*({_UNIT})?\s*')
_COMPARATORS = {'<': '<', '<=': '<=', '\u2264': '<=', '>': '>', '>=': '>=', '\u2265': '>='}
_SOURCE_FLAGS = {'low', 'high', 'normal', 'critical', 'positive', 'negative', 'abnormal'}
_QUALITATIVE_RESULTS = {
    '\u9633\u6027': 'positive',
    'positive': 'positive',
    '+': 'positive',
    '\u9634\u6027': 'negative',
    'negative': 'negative',
    '\u68c0\u51fa': 'detected',
    '\u5df2\u68c0\u51fa': 'detected',
    'detected': 'detected',
    '\u672a\u68c0\u51fa': 'not_detected',
    '\u672a\u67e5\u89c1': 'not_detected',
    'not detected': 'not_detected',
}
type Endpoint = tuple[Decimal, bool]
type Interval = tuple[Endpoint | None, Endpoint | None]


def indicator_result_judgment(fact: HealthFact) -> dict[str, str | None]:
    """Describe the latest result without updating fact or clinical interpretation."""
    flag = fact.interpretation
    if flag in _SOURCE_FLAGS:
        # Only the public Analysis path guarantees that it didn't calculate a flag.
        report_flag = bool(fact.sources) and all(
            source.confirmation_basis == 'analysis_public' for source in fact.sources
        )
        basis = 'report_flag' if report_flag else 'source_flag'
        judgment = _judgment(flag, basis=basis)
        if flag in {'low', 'high', 'normal'}:
            reference_status, _ = _compare_with_reference(fact)
            if reference_status and reference_status != flag:
                judgment['referenceStatus'] = reference_status
        return judgment

    value = fact.value
    if isinstance(value, dict):
        value = value.get('text') or value.get('raw')
    if fact.value_type is FactValueType.TEXT and isinstance(value, str):
        if (status := _QUALITATIVE_RESULTS.get(value.strip().casefold())) is not None:
            return _judgment(status, basis='source_result')

    status, reason = _compare_with_reference(fact)
    if status:
        return _judgment(status, basis='reference_range')
    return _judgment('unknown', reason=reason)


def _judgment(status: str, *, basis: str | None = None, reason: str | None = None) -> dict[str, str | None]:
    return {'status': status, 'basis': basis, 'reason': reason}


def _compare_with_reference(fact: HealthFact) -> tuple[str | None, str | None]:
    value = fact.value
    if value is None or isinstance(value, str) and not value.strip():
        return None, 'missing_value'
    quantity = value if isinstance(value, dict) else {'number': value}
    raw = quantity.get('raw')
    number = _decimal(quantity.get('number'))
    if fact.value_type is not FactValueType.QUANTITY or number is None:
        return None, 'non_numeric_value'
    comparator = None
    if (supplied := quantity.get('comparator')) is not None:
        if not isinstance(supplied, str) or supplied not in _COMPARATORS:
            return None, 'inconclusive_comparator'
        comparator = _COMPARATORS[supplied]
    unit = _unit(quantity.get('unit'))
    if raw is not None:
        match = _VALUE.fullmatch(normalize_count_unit_exponents(str(raw)))
        if not match or _decimal(match[2]) != number:
            return None, 'non_numeric_value'
        raw_comparator = _COMPARATORS.get(match[1])
        if comparator and raw_comparator and comparator != raw_comparator:
            return None, 'inconclusive_comparator'
        comparator = raw_comparator or comparator
        raw_unit = _unit(match[3])
        if unit and raw_unit and unit != raw_unit:
            return None, 'unit_mismatch'
        unit = unit or raw_unit

    reference = fact.reference_range or {}
    text = normalize_count_unit_exponents(str(reference.get('text') or '').strip())
    ref_unit = _unit(reference.get('unit'))
    if text:
        # Ingestion may have guessed numeric bounds. Explicit text has the original
        # signs, comparator and any contextual qualifiers, so it takes precedence.
        if match := _INTERVAL.fullmatch(text):
            bounds = (_decimal(match[1]), _decimal(match[2]))
            text_unit = _unit(match[3])
            if None in bounds or bounds[0] > bounds[1]:
                return None, 'unsupported_reference_range'
            reference_interval = ((bounds[0], True), (bounds[1], True))
        elif match := _BOUND.fullmatch(text):
            bound = _decimal(match[2])
            if bound is None:
                return None, 'unsupported_reference_range'
            reference_interval = _interval(bound, _COMPARATORS[match[1]])
            text_unit = _unit(match[3])
        else:
            return None, 'unsupported_reference_range'
        if ref_unit and text_unit and ref_unit != text_unit:
            return None, 'unit_mismatch'
        ref_unit = ref_unit or text_unit
    else:
        low, high = _decimal(reference.get('low')), _decimal(reference.get('high'))
        if any(reference.get(key) is not None and bound is None for key, bound in (('low', low), ('high', high))):
            return None, 'unsupported_reference_range'
        if low is None and high is None:
            return None, 'missing_reference_range'
        if low is not None and high is not None and low > high:
            return None, 'unsupported_reference_range'
        reference_interval = (None if low is None else (low, True), None if high is None else (high, True))
    if ref_unit and unit != ref_unit:
        return None, 'unit_mismatch'

    result_low, result_high = _interval(number, comparator)
    ref_low, ref_high = reference_interval
    if _below(result_high, ref_low):
        return 'low', None
    if _below(ref_high, result_low):
        return 'high', None
    if _within(result_low, ref_low) and _within(result_high, ref_high, upper=True):
        return 'normal', None
    return None, 'inconclusive_comparator'


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip()
    if re.fullmatch(_NUMBER, text) is None:
        return None
    try:
        number = Decimal(text.replace(',', ''))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _unit(value: object) -> str:
    text = normalize_count_unit_exponents(str(value or '').strip())
    text = re.sub(r'\s+', ' ', text).translate(str.maketrans('\u00b5\uff05', '\u03bc%'))
    text = re.sub(r'^[xX\u00d7]\s*(?=10\^)', '', text)
    return re.sub(r'^(10\^[+-]?[0-9]+)\s*/\s*', r'\1/', text)


def _interval(number: Decimal, comparator: str | None) -> Interval:
    if comparator is None:
        return (number, True), (number, True)
    endpoint = (number, len(comparator) == 2)
    return (None, endpoint) if comparator.startswith('<') else (endpoint, None)


def _below(upper: Endpoint | None, lower: Endpoint | None) -> bool:
    if upper is None or lower is None:
        return False
    return upper[0] < lower[0] or (upper[0] == lower[0] and not (upper[1] and lower[1]))


def _within(result: Endpoint | None, reference: Endpoint | None, *, upper: bool = False) -> bool:
    if reference is None:
        return True
    if result is None:
        return False
    if result[0] == reference[0]:
        return reference[1] or not result[1]
    return result[0] < reference[0] if upper else result[0] > reference[0]
