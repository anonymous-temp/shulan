"""Lossless request-local fact IDs and bounded health-review transport.

This module changes wire representation only. It cannot grant scientific or
numerical approval; decoded results still enter the original strict guards.
Quoted values, prose, evidence keys, risk IDs and claim IDs stay untouched.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass


_FACT_LISTS = frozenset({'factIds', 'supportFactIds', 'opposingFactIds',
    'backgroundFactIds', 'sourceModelFactIds', 'consideredFactIds', 'consideredOpposingFactIds',
    'sourceFunctionalFactIds', 'sourceFactIds'})


class UnknownWireFactId(ValueError):
    pass


@dataclass(frozen=True)
class FactIdCodec:
    aliases: dict[str, str]

    @classmethod
    def from_items(cls, items):
        identifiers = sorted({fact['factId'] for item in items for fact in item.get('facts', [])
            if isinstance(fact, dict) and isinstance(fact.get('factId'), str)})
        return cls({identifier: f'F{index}' for index, identifier in enumerate(identifiers, 1)})

    def _walk(self, value, *, decode):
        mapping = {alias: identifier for identifier, alias in self.aliases.items()} if decode else self.aliases

        def identity(identifier):
            if not isinstance(identifier, str):
                return identifier  # The original guard rejects wrong types.
            if identifier in mapping:
                return mapping[identifier]
            if decode:
                raise UnknownWireFactId('unrecognized typed wire fact ID')
            return identifier  # Schema placeholders are not patient IDs.

        def walk(node):
            if isinstance(node, dict):
                result = {}
                for key, item in node.items():
                    if key == 'factQuotes' and isinstance(item, dict):
                        result[key] = {identity(fid): deepcopy(quote) for fid, quote in item.items()}
                    elif key == 'factId':
                        result[key] = identity(item)
                    elif key in _FACT_LISTS and isinstance(item, list):
                        result[key] = [identity(identifier) for identifier in item]
                    else:
                        result[key] = walk(item)
                return result
            if isinstance(node, list):
                return [walk(item) for item in node]
            return deepcopy(node)

        return walk(value)

    def encode(self, value):
        return self._walk(value, decode=False)

    def decode(self, value):
        return self._walk(value, decode=True)


def decode_health_rows(codec, response, field, expected_codes):
    """Reject one malformed row without throwing away other reviewed rows."""
    values = response.get(field) if isinstance(response, dict) else None
    if not isinstance(values, list):
        return [], {code: ['health_wire_rows_missing'] for code in expected_codes}
    result, errors, counts = [], {}, {}
    for row in values:
        code = row.get('code') if isinstance(row, dict) else None
        if isinstance(code, str) and code in expected_codes:
            counts[code] = counts.get(code, 0) + 1
    for row in values:
        code = row.get('code') if isinstance(row, dict) else None
        if not isinstance(code, str) or code not in expected_codes:
            continue  # No foreign row can be adopted by another batch.
        if counts[code] != 1:
            errors[code] = ['health_wire_duplicate_code']
            continue
        try:
            result.append(codec.decode(row))
        except UnknownWireFactId:
            errors.setdefault(code, []).append('health_wire_unknown_fact_id')
    return result, errors


def _health_batches(payload, field, *, batch_size=3):
    """Keep each item, its schema and its feedback in the same small packet."""
    if batch_size not in {3, 4}:
        raise ValueError('health review batch must contain three or four items')
    items = payload.get('items', [])
    schema = payload.get('outputSchema', {}).get(field, [])
    codes = [item.get('code') for item in items]
    schema_codes = [item.get('code') for item in schema]
    if (any(not isinstance(code, str) for code in codes) or len(set(codes)) != len(codes)
            or len(schema_codes) != len(codes) or set(schema_codes) != set(codes)):
        raise ValueError('health item and schema scopes differ')
    result = []
    for start in range(0, len(items), batch_size):
        packet = deepcopy(payload)
        packet['items'] = deepcopy(items[start:start + batch_size])
        own = {item['code'] for item in packet['items']}
        packet['outputSchema'][field] = [deepcopy(item) for item in schema if item['code'] in own]
        if field != 'rejectionReasons' and 'rejectionReasons' in packet['outputSchema']:
            packet['outputSchema']['rejectionReasons'] = [item for item in
                packet['outputSchema']['rejectionReasons'] if item.get('code') in own]
        if isinstance(packet.get('previousRejectedDecisions'), dict):
            packet['previousRejectedDecisions'] = {code: feedback for code, feedback in
                packet['previousRejectedDecisions'].items() if code in own}
        result.append(packet)
    return result


def health_review_batches(payload, *, batch_size=3):
    return _health_batches(payload, 'reviews', batch_size=batch_size)


def health_author_batches(payload, *, batch_size=3):
    return _health_batches(payload, 'decisions', batch_size=batch_size)


async def _health_wave(client, payload, codec, field, *, batch_size=3):
    packets = _health_batches(payload, field, batch_size=batch_size)
    responses = await asyncio.gather(*(client.generate(codec.encode(packet)) for packet in packets),
        return_exceptions=True)
    rows, errors, reasons = [], {}, {}
    for packet, response in zip(packets, responses, strict=True):
        codes = {item['code'] for item in packet['items']}
        if isinstance(response, BaseException):
            errors.update({code: ['health_wire_transport_failed'] for code in codes})
            continue
        decoded, rejected = decode_health_rows(codec, response, field, codes)
        rows.extend(decoded)
        errors.update(rejected)
        if field == 'reviews' and isinstance(response, dict) and 'rejectionReasons' in response:
            sidecars, sidecar_errors = decode_health_rows(codec, response, 'rejectionReasons', codes)
            reasons.update({entry['code']: entry for entry in sidecars})
            for code in sidecar_errors:
                errors.setdefault(code, []).append('health_rejection_reason_wire_invalid')
    return rows, errors, reasons


async def author_health_wave(client, payload, codec, *, batch_size=3):
    rows, errors, _ = await _health_wave(client, payload, codec, 'decisions', batch_size=batch_size)
    return rows, errors


async def clarify_health_rejections(client, payload, codec, *, batch_size=3):
    rows, errors, _ = await _health_wave(client, payload, codec, 'rejectionReasons', batch_size=batch_size)
    return {row['code']: row for row in rows}, errors


async def review_health_wave(client, payload, codec, *, batch_size=3, include_reasons=False):
    """A failed small packet cannot discard independently checked siblings."""
    rows, errors, reasons = await _health_wave(client, payload, codec, 'reviews', batch_size=batch_size)
    return (rows, errors, reasons) if include_reasons else (rows, errors)
