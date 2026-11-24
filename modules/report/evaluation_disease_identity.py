"""Exact source-title equivalence, without promoting a parent condition.

Source field identifiers identify a supplier panel; only its declared title
can establish the disease concept of a relation. In particular mood disorder
does not identify depression, and IBD/IBS do not identify their subtypes.
"""
from __future__ import annotations

import re
import unicodedata

from . import evaluation_catalog as catalog


def _key(value):
    return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', str(value or ''))).casefold()


_GENERIC_GROUPS = (
    ('肠易激综合征', '肠易激综合症', 'IBS', 'irritable bowel syndrome'),
    ('炎症性肠病', '炎症性肠病（总体）', '炎症性肠病IBD', 'IBD', 'inflammatory bowel disease'),
)


def same_source_disease(left, right):
    """Accept the same literal title or a confirmed, equally specific alias."""
    a, b = _key(left), _key(right)
    if not a or not b:
        return False
    if a == b:
        return True
    first, second = catalog.fixed_disease_code(left), catalog.fixed_disease_code(right)
    if first and second:
        return first == second
    for group in _GENERIC_GROUPS:
        names = {_key(name) for name in group}
        if a in names and b in names:
            return True
    return False
