"""Bounded, non-mutating JSON previews for diagnostic logs only."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from itertools import islice


@dataclass(frozen=True)
class PreviewLimits:
    string_chars: int = 2000
    list_items: int = 20
    dict_items: int = 50
    depth: int = 8
    total_bytes: int = 32768


_SECRET_KEYS = frozenset(
    {
        'password',
        'passwd',
        'pwd',
        'secret',
        'clientsecret',
        'accesskeysecret',
        'token',
        'accesstoken',
        'refreshtoken',
        'idtoken',
        'apikey',
        'authorization',
        'cookie',
        'setcookie',
        'credential',
        'credentials',
        'privatekey',
    }
)


def is_secret_key(key: str) -> bool:
    # Do not spend unbounded time normalizing an unusually large field name.
    if len(key) > 128:
        return False
    normalized = ''.join(char for char in key.lower() if char.isalnum())
    return normalized in _SECRET_KEYS or normalized.endswith(('password', 'apikey', 'accesskeysecret'))


def _json(value) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    # Keep even Unicode line separators and unpaired surrogates on one UTF-8 line.
    for char in ('\x85', '\u2028', '\u2029'):
        text = text.replace(char, f'\\u{ord(char):04x}')
    return text.encode('utf-8', errors='backslashreplace').decode('utf-8')


def _size(text: str) -> int:
    return len(text.encode('utf-8'))


def _text(value: str, limit: int, budget: int, *, omitted: bool = False) -> str:
    def candidate(keep: int) -> str:
        if not omitted and keep >= len(value):
            return _json(value)
        head = (keep + 1) // 2
        tail = keep // 2
        return _json(f'{value[:head]}***{value[-tail:] if tail else ""}')

    upper = min(len(value), limit, budget)
    result = candidate(upper)
    if _size(result) <= budget:
        return result
    result = '"***"'
    lower = 0
    while lower <= upper:
        middle = (lower + upper) // 2
        current = candidate(middle)
        if _size(current) <= budget:
            result = current
            lower = middle + 1
        else:
            upper = middle - 1
    return result


@dataclass(frozen=True)
class PreviewFragment:
    """An already bounded JSON value, not a second input to string shortening."""

    encoded: str


class PreviewText:
    """Keep only the head and rolling tail of a sequence of text deltas."""

    def __init__(self, limit: int):
        self.limit = limit
        self.head = ''
        self.tail = ''
        self.length = 0

    def append(self, text: str) -> None:
        head_size = (self.limit + 1) // 2
        tail_size = self.limit // 2
        missing = head_size - len(self.head)
        if missing > 0:
            self.head += text[:missing]
        if tail_size:
            self.tail = (self.tail + text[-tail_size:])[-tail_size:]
        self.length = min(self.limit + 1, self.length + len(text))

    def render(self, budget: int) -> str:
        if self.length <= self.limit:
            remaining = self.length - len(self.head)
            value = self.head + (self.tail[-remaining:] if remaining > 0 else '')
            return _text(value, self.limit, budget)
        return _text(self.head + self.tail, self.limit, budget, omitted=True)


def preview_fragment(value, limits: PreviewLimits, *, depth: int) -> PreviewFragment:
    """Snapshot one field at its eventual depth without retaining the input."""
    try:
        encoded = _render(value, limits, depth, limits.total_bytes, set(), False)
    except Exception:
        encoded = '"***预览失败***"'
    return PreviewFragment(encoded)


def preview(value, limits: PreviewLimits = PreviewLimits(), *, decode_bytes: bool = False) -> str:
    """Render complete JSON tokens within a UTF-8 budget; never slice encoded JSON.

    Only built-in containers are traversed. Iterators and arbitrary repr methods
    are deliberately not evaluated. Each child receives the remaining budget.
    """
    try:
        return _render(value, limits, 0, limits.total_bytes, set(), decode_bytes)
    except Exception:
        return '"***预览失败***"'


def _render(value, limits, depth, budget, ancestors, decode_bytes):
    if isinstance(value, PreviewFragment):
        return value.encoded if _size(value.encoded) <= budget else '"***"'
    if isinstance(value, PreviewText):
        return value.render(budget)
    if isinstance(value, str):
        return _text(value, limits.string_chars, budget)
    if value is None or isinstance(value, bool):
        return _json(value)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return _json(str(value))
        if isinstance(value, int) and value.bit_length() > budget * 3:
            return '"***"'
        text = _json(value)
        return text if _size(text) <= budget else '"***"'
    if isinstance(value, bytes):
        if not decode_bytes:
            return _json(f'***bytes: {len(value)} 字节***')
        sample_bytes = limits.string_chars * 4
        if len(value) > sample_bytes:
            half = sample_bytes // 2
            text = (
                value[:half].decode('utf-8', errors='replace') + '***' + value[-half:].decode('utf-8', errors='replace')
            )
        else:
            text = value.decode('utf-8', errors='replace')
        return _text(text, limits.string_chars, budget)
    if not isinstance(value, (dict, list, tuple)):
        return _text(f'***{type(value).__name__}***', limits.string_chars, budget)
    kind = 'dict' if isinstance(value, dict) else 'list'
    if id(value) in ancestors:
        return '"***"'
    if depth >= limits.depth:
        return '"***"'
    if budget < 320:
        return '"***"'
    ancestors.add(id(value))
    try:
        return _container(value, kind, limits, depth, budget, ancestors, decode_bytes)
    finally:
        ancestors.remove(id(value))


def _container(value, kind, limits, depth, budget, ancestors, decode_bytes):
    parts = []
    used = 2  # Brackets/braces.
    rendered = 0
    rendered_names = set()
    if kind == 'dict':
        entries = islice(value.items(), limits.dict_items)
    else:
        entries = enumerate(islice(value, limits.list_items))
    for key, item in entries:
        # A collision-free all-star marker needs at most rendered + 4 stars.
        # Reserve its space before rendering the next value, even for wide dicts.
        reserve = max(160, rendered + 16)
        available = budget - used - reserve
        if available < 160:
            break
        prefix = ''
        if kind == 'dict':
            name = (
                key if isinstance(key, str) else str(key) if isinstance(key, (int, float, bool)) else type(key).__name__
            )
            encoded_name = _text(name, limits.string_chars, available // 2)
            if encoded_name in rendered_names:
                # Shortening a key must not overwrite an earlier visible field.
                break
            rendered_names.add(encoded_name)
            prefix = encoded_name + ':'
            if is_secret_key(name):
                item = '[REDACTED]'
        child = _render(item, limits, depth + 1, available - _size(prefix), ancestors, decode_bytes)
        parts.append(prefix + child)
        used += _size(parts[-1]) + 1
        rendered += 1
    if rendered < len(value):
        marker = '"***"'
        if kind == 'dict':
            name = '***'
            while _json(name) in rendered_names:
                name += '*'
            marker = _json(name) + ':' + marker
        parts.append(marker)
    opening, closing = ('{', '}') if kind == 'dict' else ('[', ']')
    return opening + ','.join(parts) + closing
