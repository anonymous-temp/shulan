"""Private receipts for text actually accepted and applied by this assembler.

A citation list is evidence metadata, not an approval token. These receipts
bind an exact field and owner after application; input dictionaries cannot
manufacture the opaque receipt type or its process-local seal.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_SEAL = object()
_RECORD_FIELDS = (
    'code', 'name', 'scientific_name', 'kind', 'value', 'unit', 'reference_range',
    'source_status', 'assessment_level', 'detection_status',
)
_ROW_FIELDS = ('code', 'name', 'value', 'unit', 'referenceRange', 'status', 'observedAt')


def _get(value, name, default=None):
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _text_digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def source_identity(record):
    """Exact reference identity, with source dates kept independently."""
    refs = [(
        _get(source, 'source_code', _get(source, 'sourceCode')),
        _get(source, 'record_id', _get(source, 'recordId')),
        _get(source, 'observed_at', _get(source, 'observedAt')),
    ) for source in _get(record, 'sources', ())]
    return tuple(sorted(set(refs), key=lambda ref: json.dumps(ref, ensure_ascii=False)))


def _record_digest(record, native_sources):
    return _digest({
        'type': type(record).__module__ + '.' + type(record).__qualname__,
        'fields': {key: _get(record, key) for key in _RECORD_FIELDS},
        'nativeSources': native_sources,
    })


def _row_digest(row):
    return _digest({'fields': {key: row.get(key) for key in _ROW_FIELDS}, 'sources': source_identity(row)})


@dataclass(frozen=True)
class _FieldReceipt:
    target_id: str
    path: tuple[str | int, ...]
    field: str
    text_sha256: str
    owner_sha256: str
    native_sources: tuple
    primary_fact_ids: tuple[str, ...]
    used_fact_ids: tuple[str, ...]
    completion: str
    seal: Any

    def __deepcopy__(self, memo):
        return self  # Immutable receipt; retain the issuer's opaque seal.


@dataclass(frozen=True)
class _ProjectionReceipt:
    field_receipt: _FieldReceipt
    row_sha256: str
    seal: Any

    def __deepcopy__(self, memo):
        return self


def record_accepted_field(record, *, target_id, path, primary_fact_ids, used_fact_ids,
                          completion, native_sources):
    """Issue only after the caller accepted and actually applied this exact field."""
    path = tuple(path)
    if not path or not isinstance(path[-1], str) or completion not in {'complete', 'source_only'}:
        return
    field = path[-1]
    text = _get(record, field)
    if not isinstance(text, str) or not text.strip() or not target_id:
        return
    receipt = _FieldReceipt(
        target_id=target_id, path=path, field=field, text_sha256=_text_digest(text),
        owner_sha256=_record_digest(record, tuple(native_sources)), native_sources=tuple(native_sources),
        primary_fact_ids=tuple(dict.fromkeys(primary_fact_ids)),
        used_fact_ids=tuple(dict.fromkeys(used_fact_ids)), completion=completion, seal=_SEAL,
    )
    receipts = dict(getattr(record, '_assessment_reviewed_fields', {}) or {})
    receipts[path] = receipt
    object.__setattr__(record, '_assessment_reviewed_fields', receipts)


def _matching_receipt(record, field, *, path=None):
    text = _get(record, field)
    if not isinstance(text, str) or not text.strip():
        return None
    receipts = getattr(record, '_assessment_reviewed_fields', {}) or {}
    if not isinstance(receipts, Mapping):
        return None
    for location, receipt in receipts.items():
        if (not isinstance(receipt, _FieldReceipt) or receipt.seal is not _SEAL
                or tuple(location) != receipt.path or receipt.field != field or receipt.path[-1] != field
                or path is not None and tuple(path) != receipt.path
                or receipt.text_sha256 != _text_digest(text)
                or receipt.owner_sha256 != _record_digest(record, receipt.native_sources)
                or not set(receipt.native_sources).issubset(source_identity(record))):
            continue
        return receipt
    return None


def project_reviewed_field(record, row, *, field='explanation', output_field='interpretation'):
    """Project exact applied text, including a legitimately empty citation list."""
    receipt = _matching_receipt(record, field)
    row[output_field] = _get(record, field) if receipt else None
    if receipt:
        row['_assessmentReviewedInterpretation'] = _ProjectionReceipt(receipt, _row_digest(row), _SEAL)


def has_reviewed_interpretation(row):
    binding = row.get('_assessmentReviewedInterpretation')
    text = row.get('interpretation')
    return bool(
        isinstance(binding, _ProjectionReceipt) and binding.seal is _SEAL
        and isinstance(text, str) and text
        and binding.field_receipt.seal is _SEAL
        and binding.field_receipt.text_sha256 == _text_digest(text)
        and binding.row_sha256 == _row_digest(row)
    )


def transfer_reviewed_interpretation(source, destination, *, occurrence_id):
    """Carry a receipt after the view verifies the same original comparison.

    The caller still checks the original occurrence's value/unit/reference/
    state. Here the exact occurrence locator must belong to both rows; a
    shared PDF, a name or an auxiliary reference cannot replace that locator.
    """
    if not re.fullmatch(r'parsed\.sourceTaxonOccurrences\[\d+\]', occurrence_id or ''):
        return False
    if not has_reviewed_interpretation(source):
        return False
    if not all(any(ref[0] == 'C' and ref[1] == occurrence_id for ref in source_identity(row))
               for row in (source, destination)):
        return False
    destination['interpretation'] = source['interpretation']
    destination['evidenceRefs'] = list(source.get('evidenceRefs') or [])
    receipt = source['_assessmentReviewedInterpretation'].field_receipt
    destination['_assessmentReviewedInterpretation'] = _ProjectionReceipt(
        receipt, _row_digest(destination), _SEAL,
    )
    return True
