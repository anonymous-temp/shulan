"""Fixed, domain-owned rule tools. No model may choose or replace clinical rules."""

from __future__ import annotations

import asyncio
import inspect
from copy import deepcopy
from dataclasses import dataclass

from .content_catalog import CONTENT_VERSION
from .display_versions import LATEST_DISPLAY_TEMPLATE
from .review_release import REVIEW_VERSION
from .risk_index_rules import VERSION as RISK_RULE_VERSION

RULE_TOOL_VERSIONS = {
    'assess': 'gut-assessment.v1',
    'registered_risks': 'registered-risk-adapter.v1',
    'evidence_queries': 'gut-evidence-queries.v1',
    'dynamic_assessment': 'gut-dynamic-assessment.v1',
    'risk_index': RISK_RULE_VERSION,
    'review': REVIEW_VERSION,
    'interventions': CONTENT_VERSION,
    'presentation': LATEST_DISPLAY_TEMPLATE,
}


@dataclass(frozen=True)
class RuleBinding:
    version: str
    handler: object


class GutBarrierRuleTools:
    """One invocation's fixed eight-tool allowance; not a resumable model loop.

    Call ids, permissions and versions are server-owned. Only receipt metadata is
    archived. Clinical inputs/results stay in the existing workflow audit.
    """

    def __init__(self, bindings, *, run_id, allowed=None, max_calls=8):
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError('rule run identity is required')
        if type(max_calls) is not int or not 1 <= max_calls <= 8:
            raise ValueError('rule calls must be bounded by the fixed workflow')
        self.bindings = dict(bindings)
        self.allowed = frozenset(RULE_TOOL_VERSIONS if allowed is None else allowed)
        if not self.allowed <= RULE_TOOL_VERSIONS.keys():
            raise PermissionError('unknown rule permission')
        for name, binding in self.bindings.items():
            if (
                name not in RULE_TOOL_VERSIONS
                or not isinstance(binding, RuleBinding)
                or binding.version != RULE_TOOL_VERSIONS[name]
                or not callable(binding.handler)
            ):
                raise ValueError('unsupported rule binding')
        self.run_id, self.max_calls = run_id, max_calls
        self._seen, self.receipts = set(), []

    async def invoke(self, name, *args, call_id, **kwargs):
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError
        if name not in self.allowed or name not in self.bindings:
            raise PermissionError('rule tool is not authorized')
        if not isinstance(call_id, str) or not call_id.strip() or call_id in self._seen:
            raise ValueError('rule call identity must be unique')
        if len(self._seen) >= self.max_calls:
            raise RuntimeError('rule tool call budget exhausted')
        binding = self.bindings[name]
        inspect.signature(binding.handler).bind(*args, **kwargs)
        self._seen.add(call_id)
        receipt = {
            'runId': self.run_id,
            'callId': call_id,
            'name': name,
            'version': binding.version,
            'status': 'started',
        }
        self.receipts.append(receipt)
        try:
            # Preserve the legacy functions' ownership/mutation semantics. This
            # executor owns no database or publisher and cannot cross domains.
            result = binding.handler(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
            expected = tuple if name == 'registered_risks' else list if name == 'evidence_queries' else dict
            if not isinstance(result, expected):
                raise TypeError('invalid rule tool result')
            receipt['status'] = 'completed'
            return result
        except BaseException as error:
            receipt['status'] = 'cancelled' if isinstance(error, asyncio.CancelledError) else 'failed'
            raise

    def audit(self):
        return deepcopy(self.receipts)
