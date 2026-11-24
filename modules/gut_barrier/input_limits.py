"""Shared, bounded JSON-document contract for inline and downloaded NGS data."""
from __future__ import annotations

import json
import math
from typing import Any

MAX_STRUCTURED_SOURCE_BYTES = 2 * 1024 * 1024
MAX_STRUCTURED_SOURCE_NODES = 30000
MAX_STRUCTURED_SOURCE_DEPTH = 20
MAX_STRUCTURED_SOURCE_STRING = 20000
MAX_STRUCTURED_SOURCE_KEY = 200


def normalize_structured_source(value: Any) -> Any:
    """Validate without mutating the caller; preserve integers outside BSON range as text."""
    remaining = MAX_STRUCTURED_SOURCE_NODES

    def visit(item: Any, depth: int) -> Any:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > MAX_STRUCTURED_SOURCE_DEPTH:
            raise ValueError("structured source exceeds complexity limit")
        if isinstance(item, dict):
            if any(not isinstance(key, str) or len(key) > MAX_STRUCTURED_SOURCE_KEY for key in item):
                raise ValueError("invalid structured source field name")
            return {key: visit(child, depth + 1) for key, child in item.items()}
        if isinstance(item, list):
            return [visit(child, depth + 1) for child in item]
        if isinstance(item, str):
            if len(item) > MAX_STRUCTURED_SOURCE_STRING:
                raise ValueError("structured source string exceeds limit")
            return item
        if item is None or isinstance(item, bool):
            return item
        if isinstance(item, int):
            return item if -(2 ** 63) <= item < 2 ** 63 else str(item)
        if isinstance(item, float) and math.isfinite(item):
            return item
        raise ValueError("structured source must contain finite JSON values")

    normalized = visit(value, 0)
    encoded = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode("utf-8")) > MAX_STRUCTURED_SOURCE_BYTES:
        raise ValueError("structured source exceeds limit")
    return normalized
