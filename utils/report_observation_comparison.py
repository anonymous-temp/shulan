"""Compare report evidence formatting while preserving each original value."""

import re
import unicodedata

_SUPERSCRIPT_DIGITS = '⁰¹²³⁴⁵⁶⁷⁸⁹'
_SUPERSCRIPT_TRANSLATION = str.maketrans(_SUPERSCRIPT_DIGITS, '0123456789')
_SUPERSCRIPT_SCALE = re.compile(r'(?<=10)([' + _SUPERSCRIPT_DIGITS + r']+)(?=\s*/)')


def observation_literal(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value)).split()) if value is not None else ''


def observation_label_key(value):
    return ''.join(observation_literal(value).split()).casefold()


def normalize_count_unit_exponents(text: str) -> str:
    """Convert superscript powers of ten in units to caret notation."""
    return _SUPERSCRIPT_SCALE.sub(lambda match: '^' + match[1].translate(_SUPERSCRIPT_TRANSLATION), text)


def observation_unit_key(value):
    # Superscripts and multiplication signs are typography in scale units.
    raw = normalize_count_unit_exponents(''.join(str(value or '').split()))
    unit = observation_label_key(raw).replace('μ', 'u').replace('µ', 'u')
    unit = re.sub(r'^[x×*](?=10\^\d+/)', '', unit)
    return re.sub(r'/([a-z]+)\^([23])$', r'/\1\2', unit)
