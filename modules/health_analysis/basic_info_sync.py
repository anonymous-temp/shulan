"""Consumer-owned confirmation port; durable truth and recovery remain in Profile."""

import asyncio
from copy import deepcopy
from typing import Protocol


class BasicInfoUnavailableError(RuntimeError):
    """Expected registration/application dependency failure; contains no field values."""


class BasicInfoDeliveryError(RuntimeError):
    """A necessary durable confirmation registration could not be established."""


class BasicInfoConflictError(RuntimeError):
    """The fixed target or command cannot be applied."""


class BasicInfoConfirmationPort(Protocol):
    async def register(self, user_id, operation_id, *, values, confirmed_at, subject_id=None) -> dict: ...

    async def apply(self, user_id, command_id) -> dict: ...

    async def get(self, user_id, operation_id) -> dict | None: ...

    async def list_pending(self, *, user_id=None, limit=100, cursor=None) -> dict: ...


CONFIRMATION_OPERATION_PREFIX = 'health-analysis:'
_COMPLETE = frozenset({'applied', 'no_change', 'superseded'})
_REJECTED = frozenset({'blocked', 'cancelled'})
_PENDING = frozenset({'registered', 'pending'})
_STATUSES = _PENDING | _COMPLETE | _REJECTED


def checked_receipt(receipt, *, user_id, operation_id, previous=None, allow_rejected=False):
    if not isinstance(receipt, dict):
        raise TypeError('Invalid Profile confirmation receipt')
    if not isinstance(user_id, str) or not user_id or not isinstance(operation_id, str) or not operation_id:
        raise TypeError('Invalid Profile confirmation scope')
    if receipt.get('userId') != user_id or receipt.get('operationId') != operation_id:
        raise BasicInfoConflictError('Profile confirmation scope mismatch')
    if receipt.get('contractVersion') != 'profile-basic-info-command.v1':
        raise TypeError('Unsupported Profile confirmation receipt')
    for key in ('commandId', 'subjectId', 'payloadHash', 'confirmedAt'):
        if not isinstance(receipt.get(key), str) or not receipt[key]:
            raise TypeError('Incomplete Profile confirmation receipt')
    if type(receipt.get('lifecycleEpoch')) is not int or receipt['lifecycleEpoch'] < 1:
        raise TypeError('Invalid Profile target lifecycle')
    epochs = receipt.get('fieldEpochs')
    if not isinstance(epochs, dict) or set(epochs) != {'name', 'age', 'gender'}:
        raise TypeError('Invalid Profile field epochs')
    if any(type(epoch) is not int or epoch < 1 for epoch in epochs.values()):
        raise TypeError('Invalid Profile field epoch')
    if (
        not isinstance(receipt.get('status'), str)
        or receipt['status'] not in _STATUSES
        or not isinstance(receipt.get('fields'), dict)
    ):
        raise TypeError('Invalid Profile application state')
    fields = receipt['fields']
    status = receipt['status']
    allowed = {'pending', 'applied', 'no_change', 'superseded', 'blocked', 'cancelled'}
    if fields and (
        set(fields) != {'name', 'age', 'gender'}
        or any(
            not isinstance(row, dict) or not isinstance(row.get('status'), str) or row['status'] not in allowed
            for row in fields.values()
        )
    ):
        raise TypeError('Invalid Profile field receipt')
    field_states = {row['status'] for row in fields.values()}
    if status in _COMPLETE:
        terminal = {'applied', 'no_change'} | ({'superseded'} if status == 'superseded' else set())
        if set(fields) != {'name', 'age', 'gender'} or not field_states <= terminal:
            raise TypeError('Incomplete Profile terminal receipt')
        if status == 'no_change' and field_states != {'no_change'}:
            raise TypeError('Inconsistent Profile no-change receipt')
        if status == 'superseded' and 'superseded' not in field_states:
            raise TypeError('Inconsistent Profile superseded receipt')
    if status in _REJECTED and (
        set(fields) != {'name', 'age', 'gender'} or field_states.intersection(_PENDING) or status not in field_states
    ):
        raise TypeError('Inconsistent Profile rejected receipt')
    if previous is not None and any(
        receipt.get(key) != previous.get(key)
        for key in ('commandId', 'subjectId', 'lifecycleEpoch', 'payloadHash', 'fieldEpochs', 'confirmedAt')
    ):
        raise BasicInfoConflictError('Profile confirmation identity changed')
    if not allow_rejected and status in _REJECTED:
        raise BasicInfoConflictError('Profile confirmation is not applicable')
    return deepcopy(receipt)


class BasicInfoSynchronizer:
    def __init__(self, port, *, timeout_seconds=5):
        self.port = port
        self.timeout_seconds = timeout_seconds

    async def confirm(self, *, user_id, operation_id, info, confirmed_at, deadline_at):
        if self.port is None:
            raise BasicInfoDeliveryError('Profile confirmation provider is not integrated')
        local_deadline = min(deadline_at, asyncio.get_running_loop().time() + self.timeout_seconds)
        try:
            async with asyncio.timeout_at(local_deadline):
                receipt = await self.port.register(
                    user_id, operation_id, values=info.values(), confirmed_at=confirmed_at
                )
        except (BasicInfoUnavailableError, TimeoutError):
            if asyncio.get_running_loop().time() >= deadline_at:
                raise
            try:
                async with asyncio.timeout_at(
                    min(deadline_at, asyncio.get_running_loop().time() + self.timeout_seconds)
                ):
                    receipt = await self.port.get(user_id, operation_id)
            except (BasicInfoUnavailableError, TimeoutError):
                if asyncio.get_running_loop().time() >= deadline_at:
                    raise
                raise BasicInfoDeliveryError('Profile confirmation registration could not be confirmed') from None
            if receipt is None:
                raise BasicInfoDeliveryError('Profile confirmation registration could not be confirmed') from None
        receipt = checked_receipt(receipt, user_id=user_id, operation_id=operation_id)
        if receipt['status'] in _COMPLETE:
            return {'receipt': receipt, 'applicationAttempt': 'completed'}
        try:
            async with asyncio.timeout_at(min(deadline_at, asyncio.get_running_loop().time() + self.timeout_seconds)):
                applied = await self.port.apply(user_id, receipt['commandId'])
        except (BasicInfoUnavailableError, TimeoutError):
            if asyncio.get_running_loop().time() >= deadline_at:
                raise
            return {'receipt': receipt, 'applicationAttempt': 'deferred'}
        applied = checked_receipt(applied, user_id=user_id, operation_id=operation_id, previous=receipt)
        return {
            'receipt': applied,
            'applicationAttempt': 'completed' if applied['status'] in _COMPLETE else 'deferred',
        }


class BasicInfoCompensationWorker:
    """One bounded pass over Profile's public pending log, without model or trace writes."""

    def __init__(self, port, *, batch_size=100, timeout_seconds=5):
        if type(batch_size) is not int or not 1 <= batch_size <= 100:
            raise ValueError('Invalid compensation batch size')
        self.port, self.batch_size, self.timeout_seconds = port, batch_size, timeout_seconds
        self._lock = asyncio.Lock()
        self._cursor = None

    async def run_once(self):
        async with self._lock:
            async with asyncio.timeout(self.timeout_seconds):
                page = await self.port.list_pending(limit=self.batch_size, cursor=self._cursor)
            if not isinstance(page, dict) or not isinstance(page.get('items'), list):
                raise TypeError('Invalid Profile confirmation page')
            if len(page['items']) > self.batch_size:
                raise TypeError('Profile confirmation page exceeds capacity')
            cursor = page.get('nextCursor')
            if cursor is not None and (not isinstance(cursor, str) or not cursor or cursor == self._cursor):
                raise TypeError('Invalid Profile confirmation cursor')
            attempted = 0
            for row in page['items']:
                if not isinstance(row, dict):
                    raise TypeError('Invalid Profile confirmation receipt')
                user_id, operation_id = row.get('userId'), row.get('operationId')
                checked_receipt(row, user_id=user_id, operation_id=operation_id, allow_rejected=True)
                if not operation_id.startswith(CONFIRMATION_OPERATION_PREFIX) or row['status'] not in _PENDING:
                    continue
                attempted += 1
                try:
                    async with asyncio.timeout(self.timeout_seconds):
                        result = await self.port.apply(user_id, row['commandId'])
                except (BasicInfoUnavailableError, BasicInfoConflictError, TimeoutError):
                    continue
                checked_receipt(result, user_id=user_id, operation_id=operation_id, previous=row, allow_rejected=True)
            self._cursor = cursor
            return attempted
