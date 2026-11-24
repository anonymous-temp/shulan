"""Pure display labels for source observations; no application configuration."""

import re
import unicodedata


def _key(value):
    return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', value or '')).casefold()


def clinical_observation_display_name(row):
    """Avoid repeating a literal result in its label; keep source fields intact."""
    name = str(row.get('name') or '')
    value = unicodedata.normalize('NFKC', str(row.get('value') or '')).strip()
    value = re.sub(r'(?<=\d)\s*[xX*×]\s*(?=\d)', '×', value)
    parent, separator, label = name.rpartition(' · ')
    if separator and _key(label) and _key(label) in _key(value):
        return parent
    first = re.split(r'[。；;\n]', value, maxsplit=1)[0].strip()
    if _key(name) == _key(first):
        return ''
    return name
