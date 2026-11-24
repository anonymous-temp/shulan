"""Shared reasoning effort contract for profiles, requests and API adapters."""

from collections.abc import Mapping
from typing import Any

DEFAULT_REASONING_EFFORT = 'low'
REASONING_EFFORTS = ('low', 'high', 'max')


def validate_reasoning_effort(value: Any) -> str:
    if not isinstance(value, str) or value not in REASONING_EFFORTS:
        raise ValueError('reasoning_effort must be one of: low, high, max')
    return value


def validate_reasoning_kwargs(options: Mapping[str, Any]) -> None:
    # Retain the existing top-level model_kwargs setting for code callers.
    if 'reasoning_effort' in options:
        validate_reasoning_effort(options['reasoning_effort'])
    extra_body = options.get('extra_body')
    if isinstance(extra_body, Mapping) and 'reasoning_effort' in extra_body:
        raise ValueError('Configure reasoning_effort outside extra_body so request overrides take effect')
