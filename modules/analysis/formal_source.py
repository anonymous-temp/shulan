"""Durable formal-source orchestration. HTTP queries never call this worker."""

from __future__ import annotations

import asyncio
import time
import uuid
from copy import deepcopy
from dataclasses import asdict

from modules.analysis.formal_source_contracts import FORMAL_TERMINAL
from modules.analysis.formal_source_contracts import DocumentFormalSourceCommand
from modules.analysis.formal_source_contracts import DocumentFormalSourceConflict
from modules.analysis.formal_source_contracts import DocumentFormalSourceLeaseLost
from modules.analysis.formal_source_contracts import DocumentFormalSourcePolicy
from modules.analysis.snapshots import digest


class DocumentFormalSourceWait(Exception):
    """An independently durable prerequisite has not yet reached the required state."""


class DocumentFormalSourceService:
    def __init__(self, *, store, steps, policy=DocumentFormalSourcePolicy(), clock=time.time):
        self.store, self.steps, self.policy, self.clock = store, steps, policy, clock

    async def submit(self, command):
        return await self._submit_command(command, DocumentFormalSourceCommand)

    async def _submit_command(self, command, command_type):
        if not isinstance(command, command_type):
            raise TypeError('an exact formal Document command is required')
        raw = command.as_dict()
        now = self.clock()
        row = await self.store.insert(
            {
                '_id': command.command_id,
                'revision': 1,
                'command': raw,
                'commandDigest': digest(raw),
                'status': 'pending',
                'phase': 'prepare',
                'progress': {},
                'createdAt': now,
                'deadlineAt': now + self.policy.deadline_seconds,
                'nextAttemptAt': now,
                'policy': asdict(self.policy),
                'transportFailures': 0,
                'leaseGeneration': 0,
            }
        )
        self._command(row)
        if row['command'] != raw:
            raise DocumentFormalSourceConflict('formal request ID already binds different inputs or selection')
        return self._receipt(row)

    async def execute(self, command):
        """Explicit bounded command work; never sleep for Profile application."""
        await self.submit(command)
        row = await self.store.get(command.command_id)
        self._command(row)
        try:
            # The accepted policy controls this call, including after deployment
            # defaults change. At most two claims / sixteen persisted stages.
            async with asyncio.timeout(row['policy']['io_timeout_seconds'] * 2):
                for _ in range(2):
                    result = await self.advance(command.command_id)
                    if result is None or result['status'] in FORMAL_TERMINAL:
                        break
        except TimeoutError:
            # Cancellation of an in-flight step preserves its durable unknown
            # execution stage. A later worker replays the same idempotent inputs.
            pass
        return await self.read_receipt(scope=command.scope, command_id=command.command_id)

    async def prepare_report(self, command):
        if not isinstance(command, DocumentFormalSourceCommand) or command.operation != 'report':
            raise TypeError('an exact formal Report preparation command is required')
        return await self.steps.prepare_report(scope=command.scope, inputs=command.inputs)

    async def read_report_projection(self, *, scope, projection_ref):
        return await self.steps.read_report_projection(scope=scope, projection_ref=projection_ref)

    @staticmethod
    def _command(row):
        if row is None:
            raise LookupError('formal Document command is unavailable')
        command = DocumentFormalSourceCommand.from_dict(row['command'])
        if command.command_id != row['_id'] or digest(command.as_dict()) != row['commandDigest']:
            raise DocumentFormalSourceConflict('the persisted formal command binding changed')
        DocumentFormalSourcePolicy(**row['policy'])
        return command

    @staticmethod
    def _receipt(row):
        progress = row['progress']
        sources = progress.get('sources')
        completed = [item['result'] for item in sources or [] if item.get('phase') == 'done']
        result = row.get('result') or {
            'basePublications': completed,
            'projectionRef': progress.get('projectionRef'),
            'processingStatus': progress.get('processingStatus'),
            'reportPublication': progress.get('reportPublication'),
        }
        return deepcopy(
            {
                'commandId': row['_id'],
                'commandRevision': row['command']['commandRevision'],
                'status': row['status'],
                'phase': row['phase'],
                'completedBaseCount': len(completed),
                'totalUniqueSources': None if sources is None else len(sources),
                'result': result,
                'errorCode': row.get('errorCode'),
            }
        )

    async def read_receipt(self, *, scope, command_id):
        row = await self.store.get(command_id)
        command = self._command(row)
        if command.scope != scope:
            raise PermissionError('formal Document command belongs to another scope')
        return self._receipt(row)

    async def _save(self, row, changes):
        next_row = {**deepcopy(row), **deepcopy(changes), 'revision': row['revision'] + 1}
        if not await self.store.replace(row, next_row, now=self.clock()):
            raise DocumentFormalSourceLeaseLost('formal Document claim expired or advanced')
        return next_row

    async def advance(self, command_id):
        row = await self.store.claim(command_id, owner=uuid.uuid4().hex, now=self.clock())
        if row is None:
            return None
        try:
            command = self._command(row)
            # Each step must return a durable binding before performing its next
            # external side effect. A timeout replays that same recorded step.
            for _ in range(8):
                remaining = min(row['deadlineAt'], row['leaseExpiresAt'] - 1) - self.clock()
                if row['deadlineAt'] <= self.clock():
                    row = await self._save(row, {'status': 'failed', 'errorCode': 'formal_deadline_exhausted'})
                    break
                if remaining <= 0 or row['status'] in FORMAL_TERMINAL:
                    break
                try:
                    async with asyncio.timeout(min(remaining, row['policy']['io_timeout_seconds'])):
                        changes = await self.steps.step(command, deepcopy(row))
                    if not isinstance(changes, dict) or set(changes) - {'phase', 'progress', 'result', 'status'}:
                        raise DocumentFormalSourceConflict('formal step returned unsupported progress')
                    if changes.get('status', 'pending') not in FORMAL_TERMINAL | {'pending'}:
                        raise DocumentFormalSourceConflict('formal step returned an unknown state')
                    row = await self._save(row, {**changes, 'errorCode': None})
                except DocumentFormalSourceWait:
                    row = await self._save(row, {'nextAttemptAt': self.clock() + row['policy']['retry_seconds']})
                    break
                except DocumentFormalSourceLeaseLost:
                    raise
                except (ValueError, PermissionError) as error:
                    row = await self._save(row, {'status': 'failed', 'errorCode': type(error).__name__})
                    break
                except Exception as error:
                    failures = row['transportFailures'] + 1
                    row = await self._save(
                        row,
                        {
                            'transportFailures': failures,
                            'status': 'failed' if failures >= row['policy']['max_transport_failures'] else 'pending',
                            'errorCode': type(error).__name__,
                            'nextAttemptAt': self.clock() + row['policy']['retry_seconds'],
                        },
                    )
                    break
            return self._receipt(row)
        except (ValueError, PermissionError) as error:
            row = await self._save(row, {'status': 'failed', 'errorCode': type(error).__name__})
            return self._receipt(row)
        finally:
            # Never release a successor's lease. Cancellation does not discard the
            # durable command or turn an unknown external execution into success.
            latest = await self.store.get(command_id)
            if latest and latest.get('leaseOwner') == row.get('leaseOwner'):
                try:
                    await self._save(latest, {'leaseOwner': None, 'leaseExpiresAt': 0})
                except DocumentFormalSourceLeaseLost:
                    pass

    async def recover_pending(self, *, limit=100):
        count = 0
        for command_id in await self.store.pending(now=self.clock(), limit=limit):
            try:
                if await self.advance(command_id) is not None:
                    count += 1
            except DocumentFormalSourceLeaseLost:
                pass
        return count
