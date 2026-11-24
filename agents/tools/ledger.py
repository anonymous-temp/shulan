"""Atomic parent/child dispatch budgets, cancellation and immutable call receipts."""

import hashlib
import json
import time
from copy import deepcopy
from dataclasses import asdict
from uuid import uuid4

from .contracts import ToolRunConflict
from .contracts import ToolRunStopped


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def run_key(context):
    return digest([context.user_id, context.subject_id, context.root_run_id])


def node_key(run_id):
    return digest(run_id)


def usage_sum(*values):
    totals = {}
    for name in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
        known = [value[name] for value in values if value and value.get(name) is not None]
        totals[name] = sum(known) if known else None
    return totals


class ToolRunLedger:
    """A native tool loop is one non-resumable unit with durable accounting.

    Child limits are reserved atomically from their parent before execution.
    Interrupted/unknown child allocations remain reserved for reconciliation.
    Token/cost reservations are explicit accounting allocations, not hard charges.
    """

    def __init__(self, repository, *, clock=time.time, lease_seconds=30, max_runs=64, max_receipts=256):
        if lease_seconds <= 0 or max_runs < 1 or max_receipts < 1:
            raise ValueError('Ledger bounds must be positive')
        self.repository = repository
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.max_runs = max_runs
        self.max_receipts = max_receipts

    async def _update(self, context, change):
        key = run_key(context)
        for _ in range(32):
            document = await self.repository.load(key)
            if document is None:
                raise ToolRunConflict('Root run has not been admitted')
            revision = document['revision']
            result = change(document)
            document['revision'] = revision + 1
            document['updated_at'] = self.clock()
            if len(json.dumps(document, ensure_ascii=False).encode()) > 8 * 1024 * 1024:
                raise ToolRunStopped('Root run ledger size limit exceeded')
            if await self.repository.compare_save(key, revision, document):
                return deepcopy(result)
        raise ToolRunConflict('Concurrent run update could not be committed')

    @staticmethod
    def _node(document, context):
        node = document['runs'].get(node_key(context.run_id))
        if node is None or node['context'] != asdict(context):
            raise ToolRunConflict('Run identity or authorization scope does not match')
        return node

    def _active(self, document, context, token):
        node = self._node(document, context)
        if node['status'] != 'running' or node.get('owner') != token or node['lease_until'] <= self.clock():
            raise ToolRunStopped('Run execution lease is no longer valid')
        ancestor = node
        while ancestor is not None:
            if ancestor['cancel_requested']:
                raise ToolRunStopped('Run cancelled', code='cancelled')
            if (
                ancestor['status'] != 'running'
                or ancestor['lease_until'] <= self.clock()
                or ancestor['budget']['deadline_at'] <= self.clock()
            ):
                raise ToolRunStopped('Run cancelled or deadline exhausted')
            parent = ancestor['context']['parent_run_id']
            ancestor = document['runs'].get(node_key(parent)) if parent else None
        return node

    @staticmethod
    def _limits(node, model=0, tool=0):
        if node['used_model'] + node['reserved_model'] + model > node['budget']['max_model_calls']:
            raise ToolRunStopped('Shared model-call budget exhausted')
        if node['used_tool'] + node['reserved_tool'] + tool > node['budget']['max_tool_calls']:
            raise ToolRunStopped('Shared tool-call budget exhausted')

    async def admit(self, context, budget, fingerprint):
        immutable = {'context': asdict(context), 'budget': asdict(budget), 'fingerprint': fingerprint}
        node = {
            **immutable,
            'status': 'accepted',
            'cancel_requested': False,
            'owner': None,
            'lease_until': 0,
            'used_model': 0,
            'used_tool': 0,
            'reserved_model': 0,
            'reserved_tool': 0,
            'reserved_tokens': 0,
            'reserved_cost': 0,
            'usage': {},
            'child_usage': {},
            'usage_complete': True,
            'calls': {},
            'result': None,
            'reason': None,
            'settled_to_parent': False,
        }
        if context.parent_run_id is None:
            await self.repository.create(
                {
                    '_id': run_key(context),
                    'revision': 0,
                    'runs': {node_key(context.run_id): node},
                    'created_at': self.clock(),
                    'updated_at': self.clock(),
                }
            )

        def change(document):
            existing = document['runs'].get(node_key(context.run_id))
            if existing is not None:
                if any(existing[key] != value for key, value in immutable.items()):
                    raise ToolRunConflict('Run identity is already bound to different input or budget')
                return existing
            if len(document['runs']) >= self.max_runs:
                raise ToolRunStopped('Root run child limit exhausted')
            parent = document['runs'].get(node_key(context.parent_run_id))
            if (
                parent is None
                or parent['status'] != 'running'
                or parent['cancel_requested']
                or parent['lease_until'] <= self.clock()
            ):
                raise ToolRunStopped('Parent is not available for delegation')
            if budget.deadline_at > parent['budget']['deadline_at'] or budget.deadline_at <= self.clock():
                raise ToolRunStopped('Child deadline exceeds the parent deadline')
            self._limits(parent, budget.max_model_calls, budget.max_tool_calls)
            if parent['reserved_tokens'] + budget.token_reservation > parent['budget']['token_reservation']:
                raise ToolRunStopped('Parent token reservation cannot cover this child')
            if parent['reserved_cost'] + budget.cost_reservation_units > parent['budget']['cost_reservation_units']:
                raise ToolRunStopped('Parent cost reservation cannot cover this child')
            parent['reserved_model'] += budget.max_model_calls
            parent['reserved_tool'] += budget.max_tool_calls
            parent['reserved_tokens'] += budget.token_reservation
            parent['reserved_cost'] += budget.cost_reservation_units
            document['runs'][node_key(context.run_id)] = deepcopy(node)
            return node

        return await self._update(context, change)

    async def claim(self, context):
        token = uuid4().hex

        def change(document):
            node = self._node(document, context)
            if node['status'] == 'running' and node['lease_until'] <= self.clock():
                node['status'], node['reason'] = 'interrupted', 'lease_expired_no_step_resume'
                for receipt in node['calls'].values():
                    if receipt['status'] == 'running':
                        receipt['status'] = 'unknown'
                return None
            if node['status'] != 'accepted':
                return None
            if node['cancel_requested'] or node['budget']['deadline_at'] <= self.clock():
                node['status'], node['reason'] = 'cancelled', 'cancelled_or_expired_before_execution'
                return None
            node.update(status='running', owner=token, lease_until=self.clock() + self.lease_seconds)
            return token

        return await self._update(context, change)

    async def check(self, context, token):
        def change(document):
            node = self._active(document, context, token)
            node['lease_until'] = self.clock() + self.lease_seconds
            return node

        return await self._update(context, change)

    async def reserve(self, context, token, call_id, *, kind, identity):
        if kind not in {'model', 'tool'}:
            raise ValueError('Unknown dispatch kind')

        def change(document):
            node = self._active(document, context, token)
            key = node_key(call_id)
            if key in node['calls']:
                raise ToolRunConflict('Call already dispatched; native loop cannot replay it')
            if len(node['calls']) >= self.max_receipts:
                raise ToolRunStopped('Run receipt limit exhausted')
            self._limits(node, model=int(kind == 'model'), tool=int(kind == 'tool'))
            node['used_model'] += int(kind == 'model')
            node['used_tool'] += int(kind == 'tool')
            node['calls'][key] = {
                'call_id': call_id,
                'kind': kind,
                'identity': deepcopy(identity),
                'status': 'running',
                'receipt_ref': None,
                'usage': None,
            }
            return node['calls'][key]

        return await self._update(context, change)

    async def settle(self, context, token, call_id, *, status='completed', receipt_ref=None, usage=None):
        if status not in {'completed', 'failed', 'unknown'}:
            raise ValueError('Invalid receipt status')

        def change(document):
            node = self._node(document, context)
            if node.get('owner') != token or node['lease_until'] <= self.clock():
                raise ToolRunStopped('Stale execution cannot settle receipts')
            receipt = node['calls'].get(node_key(call_id))
            if receipt is None or receipt['status'] != 'running':
                raise ToolRunConflict('Call receipt is missing or already settled')
            receipt.update(status=status, receipt_ref=receipt_ref, usage=deepcopy(usage))
            if receipt['kind'] == 'model' and (
                status != 'completed'
                or not usage
                or any(usage.get(key) is None for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'))
            ):
                node['usage_complete'] = False
            if usage:
                node['usage'] = usage_sum(node['usage'], usage)
            return receipt

        return await self._update(context, change)

    async def cancel(self, context):
        def change(document):
            node = self._node(document, context)
            node['cancel_requested'] = True
            return node

        return await self._update(context, change)

    async def finish(self, context, token, *, status, result=None, reason=None):
        if status not in {'completed', 'failed', 'cancelled'}:
            raise ValueError('Invalid terminal run status')

        def change(document):
            node = self._node(document, context)
            if node.get('owner') != token or node['lease_until'] <= self.clock():
                raise ToolRunStopped('Stale execution cannot publish a result')
            if status == 'completed':
                self._active(document, context, token)
            pending = any(receipt['status'] in {'running', 'unknown'} for receipt in node['calls'].values())
            if pending or node['reserved_model'] or node['reserved_tool']:
                status_value = 'interrupted'
                for receipt in node['calls'].values():
                    if receipt['status'] == 'running':
                        receipt['status'] = 'unknown'
            else:
                status_value = status
            node.update(
                status=status_value,
                result=deepcopy(result) if status_value == 'completed' else None,
                reason=reason,
                owner=None,
                lease_until=0,
            )
            parent_id = context.parent_run_id
            if parent_id and status_value == 'completed' and not node['settled_to_parent']:
                parent = document['runs'][node_key(parent_id)]
                parent['reserved_model'] -= node['budget']['max_model_calls']
                parent['reserved_tool'] -= node['budget']['max_tool_calls']
                parent['reserved_tokens'] -= node['budget']['token_reservation']
                parent['reserved_cost'] -= node['budget']['cost_reservation_units']
                parent['used_model'] += node['used_model']
                parent['used_tool'] += node['used_tool']
                parent['child_usage'] = usage_sum(parent['child_usage'], node['usage'], node['child_usage'])
                parent['usage_complete'] = parent['usage_complete'] and node['usage_complete']
                node['settled_to_parent'] = True
            return node

        return await self._update(context, change)

    async def snapshot(self, context):
        document = await self.repository.load(run_key(context))
        if document is None:
            raise ToolRunConflict('Unknown run')
        node = deepcopy(self._node(document, context))
        included = {context.run_id}
        descendants = []
        remaining = list(document['runs'].values())
        while remaining:
            matched = [
                item
                for item in remaining
                if item['context']['run_id'] in included or item['context']['parent_run_id'] in included
            ]
            if not matched:
                break
            for item in matched:
                included.add(item['context']['run_id'])
                descendants.append(item)
                remaining.remove(item)
        # Include observed work in failed/unknown children, without counting the
        # completed-child rollup a second time. Charges may still be incomplete.
        node['observed_usage'] = usage_sum(*(item['usage'] for item in descendants))
        node['usage_complete'] = all(item['usage_complete'] and item['status'] == 'completed' for item in descendants)
        return node
