"""Stable, bounded trace content; the writer owns actual persistence."""

import hashlib
import json


def encode_record(record: dict) -> bytes:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode(
        'utf-8'
    )


def trace_hash(record: dict) -> str:
    return hashlib.sha256(encode_record(record)).hexdigest()
