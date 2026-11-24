"""Bounded event snapshots for logs; never read sessions or retain event history."""

from __future__ import annotations

from dataclasses import replace
from itertools import islice

from utils.log.preview import PreviewFragment
from utils.log.preview import PreviewLimits
from utils.log.preview import PreviewText
from utils.log.preview import is_secret_key
from utils.log.preview import preview_fragment


class _Fields:
    def __init__(self, limits: PreviewLimits, capacity: int, depth: int):
        self.limits = limits
        self.capacity = capacity
        self.depth = depth
        self.values: dict[str, PreviewFragment | PreviewText] = {}
        self.costs: dict[str, int] = {}
        self.used = 0
        self.omitted = False

    def remove(self, key: str) -> None:
        self.values.pop(key, None)
        self.used -= self.costs.pop(key, 0)

    def put(self, key: str, value, *, delta: bool = False) -> None:
        if not isinstance(key, str) or len(key) > self.limits.string_chars:
            self.omitted = True
            return
        if key not in self.values and len(self.values) >= self.limits.dict_items:
            self.omitted = True
            return
        if is_secret_key(key):
            value, delta = '[REDACTED]', False
        current = self.values.get(key)
        if delta and isinstance(value, str) and isinstance(current, PreviewText):
            current.append(value)
            return
        available = self.capacity - self.used + self.costs.get(key, 0)
        key_cost = len(key.encode('utf-8', errors='backslashreplace')) + 4
        budget = available - key_cost
        if budget < 32:
            self.omitted = True
            return
        if delta and isinstance(value, str):
            # UTF-8 text and Python Unicode both need at most four bytes per
            # retained code point. Final JSON escaping has its own output budget.
            limit = min(self.limits.string_chars, (budget - 8) // 4)
            result = PreviewText(limit)
            result.append(value)
            cost = key_cost + limit * 4 + 8
        else:
            result = preview_fragment(value, replace(self.limits, total_bytes=budget), depth=self.depth)
            cost = key_cost + len(result.encoded.encode('utf-8'))
        self.used += cost - self.costs.get(key, 0)
        self.values[key] = result
        self.costs[key] = cost

    def snapshot(self) -> dict:
        values = dict(self.values)
        if self.omitted:
            key = '***'
            while key in values:
                key += '*'
            values[key] = PreviewFragment('"***"')
        return values


class StreamLogSummary:
    """One envelope plus bounded data fields, independent of event count."""

    def __init__(self, limits: PreviewLimits):
        self.limits = limits
        self.metadata = _Fields(limits, limits.total_bytes // 4, depth=1)
        self.data = _Fields(limits, limits.total_bytes * 3 // 4 - 256, depth=2)
        self.failed = False

    def observe(self, payload: dict, *, event_type: str, failed: bool) -> None:
        if failed or not self.failed:
            for key in ('code', 'message', 'uuid', 'timestamp', 'elapsed_ms'):
                if key in payload:
                    self.metadata.put(key, payload[key])
            if failed and not isinstance(payload.get('code'), int):
                # Do not attach a preceding success code/message to a bare error.
                self.metadata.remove('code')
                self.metadata.remove('message')
        data = payload.get('data', payload)
        if isinstance(data, dict):
            for key, value in islice(data.items(), self.limits.dict_items):
                if self.failed and not failed and key in {'type', 'error'}:
                    continue
                if key == 'content' and event_type in {'content', 'reasoning'}:
                    self.data.put(event_type, value, delta=True)
                else:
                    self.data.put(key, value)
            if len(data) > self.limits.dict_items:
                self.data.omitted = True
        self.failed |= failed

    def snapshot(self) -> dict:
        return {**self.metadata.snapshot(), 'data': self.data.snapshot()}
