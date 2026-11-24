"""用户健康画像 API 使用的不透明游标编码。"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Any


def encode_cursor(kind: str, payload: Mapping[str, Any]) -> str:
    document = {
        'v': 1,
        'kind': _required_text('kind', kind),
        **dict(payload),
    }
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return base64.urlsafe_b64encode(encoded).decode('ascii').rstrip('=')


def decode_cursor(kind: str, cursor: str) -> dict[str, Any]:
    expected_kind = _required_text('kind', kind)
    normalized = _required_text('cursor', cursor)
    padding = '=' * (-len(normalized) % 4)
    try:
        decoded = base64.b64decode(
            normalized + padding,
            altchars=b'-_',
            validate=True,
        )
        document = json.loads(decoded.decode('utf-8'))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError('cursor 无效') from error
    if not isinstance(document, dict):
        raise ValueError('cursor 无效')
    if document.get('v') != 1 or document.get('kind') != expected_kind:
        raise ValueError('cursor 版本或类型不匹配')
    return document


def _required_text(name: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f'{name} must be a string')
    normalized = value.strip()
    if not normalized:
        raise ValueError(f'{name} cannot be empty')
    return normalized
