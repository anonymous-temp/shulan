"""Durable Record source delivery, independent of QA completion and HTTP queries.

All external work goes through explicit ports. Fresh authorization is supplied
by an independently verified admission provider; this module never manufactures
quiescence or infers freshness from an empty pending queue.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC
from datetime import datetime
from typing import Protocol

from modules.record.profile_source import RECORD_PROFILE_MATERIAL_CONTRACT
from modules.record.profile_source import RECORD_PROFILE_SOURCE_REVISION
from modules.record.profile_source import RecordPublicationPolicy
from modules.record.profile_source import RecordSourceDraftV1
from modules.record.profile_source import content_digest

DELIVERY_MODE = 'profile-source-event.v1'
_TERMINAL = {'applied', 'partial', 'failed'}


class RecordSourceStore(Protocol):
    async def insert(self, row: dict) -> dict: ...
    async def get(self, intent_id: str) -> dict | None: ...
    async def claim(self, intent_id: str, *, now: float, owner: str) -> dict | None: ...
    async def replace(self, expected: dict, replacement: dict, *, now: float) -> bool: ...
    async def pending(self, *, now: float, limit: int) -> list[str]: ...
    async def find_snapshot(self, snapshot_id: str) -> dict | None: ...


class RecordSourceAdmissionPort(Protocol):
    """Return verified durable fresh states, never a caller-asserted permission."""

    async def prepare_self_scope(self, user_id: str, *, display_name: str | None) -> dict: ...
    async def reserve(self, *, material: dict, snapshot_ref: dict, completion_id: str, draft_id: str) -> dict: ...
    async def authorize(self, reservation: dict) -> dict: ...


class RecordSourceReceiverPort(Protocol):
    async def receive(self, event: dict) -> dict: ...
    async def get_receipt(self, event: dict) -> dict: ...


class RecordSourceCompletionEvidencePort(Protocol):
    async def is_committed(self, *, user_id: str, session_id: str, completion_id: str, draft_id: str) -> bool: ...


class RecordSourceConflict(RuntimeError):
    pass


class RecordSourceMaterialReader:
    """Read fixed Record material using only the owner's find_snapshot port.

    Construction and reads do not require Profile admission, receivers, completion
    evidence or a publication service. The store retains its uniqueness contract.
    """

    def __init__(self, store):
        self.store = store

    async def read_source_snapshot(self, snapshot_ref, *, scope):
        # Fixed resolver reads neither mutate state nor ensure a subject.
        ref = deepcopy(snapshot_ref)
        owned = deepcopy(scope)
        if ref.get('provider') != 'record' or ref.get('contractVersion') != RECORD_PROFILE_MATERIAL_CONTRACT:
            raise RecordSourceConflict('unsupported Record material reference')
        row = await self.store.find_snapshot(ref['snapshotId'])
        if row is None or row.get('snapshotRef') != ref or row.get('scope') != owned:
            raise LookupError('the authorized fixed Record material is unavailable')
        material = json.loads(row['materialJson'])
        if 'record_material_' + content_digest(material) != ref['snapshotId']:
            raise RecordSourceConflict('fixed Record material digest changed')
        return material


class RecordSourceLeaseLost(RuntimeError):
    pass


def source_intent_id(user_id, session_id):
    return 'record_source_' + content_digest({'userId': user_id, 'sourceId': session_id})


def frozen_json(value):
    # Preserve nested source object order; hashing may use canonical key ordering.
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _scope(value, user_id):
    if not isinstance(value, dict) or value.get('userId') != user_id or not isinstance(value.get('subjectId'), str):
        raise RecordSourceConflict('source scope is not owned by this Record user')
    if not value['subjectId'] or value['subjectId'].strip() != value['subjectId']:
        raise RecordSourceConflict('source scope has no fixed subject')
    return {'userId': user_id, 'subjectId': value['subjectId']}


def _check_admission(value, row, required_status):
    if not isinstance(value, dict):
        raise RecordSourceConflict('fresh authorization receipt is missing')
    authorization = value.get('authorization', {})
    material = json.loads(row['materialJson'])
    expected = {
        'admissionKind': 'fresh',
        'scope': row['scope'],
        'sourceType': 'record',
        'sourceId': row['sessionId'],
        'materialContractVersion': RECORD_PROFILE_MATERIAL_CONTRACT,
        'materialSnapshotRef': row['snapshotRef'],
        'materialContentDigest': content_digest(material),
    }
    statuses = {'blocked', 'ready'} if required_status == 'blocked' else {required_status}
    if value.get('status') not in statuses or any(authorization.get(key) != item for key, item in expected.items()):
        raise RecordSourceConflict('fresh authorization does not bind the fixed Record material')
    if not authorization.get('authorizationId') or not authorization.get('ownerFenceRef'):
        raise RecordSourceConflict('fresh authorization has no durable owner proof')
    if row.get('reservation') and authorization != row['reservation']['authorization']:
        raise RecordSourceConflict('fresh authorization changed after reservation')
    return deepcopy(value)


class RecordSourcePublicationService:
    def __init__(
        self, *, store, admission, receiver, completion_evidence, policy=RecordPublicationPolicy(), clock=time.time
    ):
        self.store, self.admission, self.receiver = store, admission, receiver
        self.material_reader = RecordSourceMaterialReader(store)
        self.completion_evidence, self.policy, self.clock = completion_evidence, policy, clock

    def prepare_completed_snapshot(self, session):
        if session.get('recordProfileConsumerRevision') != RECORD_PROFILE_SOURCE_REVISION:
            raise RecordSourceConflict('Record source publication requires its accepted fixed revision')
        draft = RecordSourceDraftV1.capture(session)
        session['qaSourceRevision'] = draft.payload['sourceRevision']
        marker = {
            'deliveryMode': DELIVERY_MODE,
            'status': 'source_managed',
            'recordSourceIntent': {
                'consumerRevision': RECORD_PROFILE_SOURCE_REVISION,
                'intentId': source_intent_id(draft.user_id, draft.source_id),
                'draftId': draft.draft_id,
                'rawDigest': draft.digest,
            },
        }
        previous = session.get('qaProfileIngestion')
        if previous is not None and previous != marker:
            raise RecordSourceConflict('cannot replace an existing Record source delivery intent')
        session['qaProfileIngestion'] = marker

    async def stage_completion(self, session):
        # Freeze before the first await; mutable caller state cannot change the intent.
        snapshot = deepcopy(session)
        draft = RecordSourceDraftV1.capture(snapshot)
        marker = snapshot.get('qaProfileIngestion', {})
        binding = marker.get('recordSourceIntent', {})
        completion_id = snapshot.get('recordCompletionId')
        intent_id = source_intent_id(draft.user_id, draft.source_id)
        if (
            snapshot.get('recordProfileConsumerRevision') != RECORD_PROFILE_SOURCE_REVISION
            or marker.get('deliveryMode') != DELIVERY_MODE
            or binding.get('intentId') != intent_id
            or binding.get('draftId') != draft.draft_id
            or binding.get('rawDigest') != draft.digest
            or not isinstance(completion_id, str)
            or not completion_id
        ):
            raise RecordSourceConflict('completion does not bind the accepted Record draft')
        now = self.clock()
        row = await self.store.insert(
            {
                '_id': intent_id,
                'revision': 1,
                'consumerRevision': RECORD_PROFILE_SOURCE_REVISION,
                'userId': draft.user_id,
                'sessionId': draft.source_id,
                'draftId': draft.draft_id,
                'draftJson': frozen_json(draft.payload),
                'completionId': completion_id,
                'status': 'staged',
                'createdAt': now,
                'policy': asdict(self.policy),
                'deadlineAt': now + self.policy.deadline_seconds,
                'nextAttemptAt': now,
                'dispatchAttempts': 0,
                'transportFailures': 0,
                'leaseGeneration': 0,
            }
        )
        if (
            row.get('consumerRevision') != RECORD_PROFILE_SOURCE_REVISION
            or row['draftId'] != draft.draft_id
            or row['completionId'] != completion_id
        ):
            raise RecordSourceConflict('another completion already owns the Record source identity')
        return row

    async def _save(self, row, changes):
        replacement = {**deepcopy(row), **deepcopy(changes), 'revision': row['revision'] + 1}
        if not await self.store.replace(row, replacement, now=self.clock()):
            raise RecordSourceLeaseLost('Record source lease or revision no longer belongs to this worker')
        return replacement

    async def _release(self, row):
        # Never unlock a newer lease or overwrite a worker which already advanced.
        latest = await self.store.get(row['_id'])
        if latest and latest.get('leaseOwner') == row.get('leaseOwner'):
            try:
                await self._save(latest, {'leaseOwner': None, 'leaseExpiresAt': 0})
            except RecordSourceLeaseLost:
                pass

    async def _call(self, row, operation):
        remaining = row['deadlineAt'] - self.clock()
        if remaining <= 0:
            # Ports are async methods; close the unstarted coroutine explicitly.
            operation.close()
            raise TimeoutError('Record source deadline exceeded before external dispatch')
        async with asyncio.timeout(min(row['policy']['io_timeout_seconds'], remaining)):
            return await operation

    async def _prepare(self, row):
        draft = RecordSourceDraftV1(row['draftJson'])
        if not row.get('scope'):
            display_name = next(
                (item['value'] for item in draft.payload['userInputs'] if item['field'] == 'name'), None
            )
            display_name = display_name.strip()[:128] or None if isinstance(display_name, str) else None
            scope = await self._call(row, self.admission.prepare_self_scope(row['userId'], display_name=display_name))
            row = await self._save(row, {'scope': _scope(scope, row['userId'])})
        if not row.get('materialJson'):
            material = draft.profile_material(row['scope'])
            ref = {
                'provider': 'record',
                'contractVersion': RECORD_PROFILE_MATERIAL_CONTRACT,
                'snapshotId': 'record_material_' + content_digest(material),
            }
            row = await self._save(row, {'materialJson': frozen_json(material), 'snapshotRef': ref})
        if not row.get('reservation'):
            state = await self._call(
                row,
                self.admission.reserve(
                    material=json.loads(row['materialJson']),
                    snapshot_ref=deepcopy(row['snapshotRef']),
                    completion_id=row['completionId'],
                    draft_id=row['draftId'],
                ),
            )
            row = await self._save(row, {'reservation': _check_admission(state, row, 'blocked')})
        return row

    async def recover_pending(self, *, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Record source scan limit must be in 1..1000')
        ids = await self.store.pending(now=self.clock(), limit=limit)
        for intent_id in ids:
            await self.recover_one(intent_id)
        return len(ids)

    async def ensure_queued(self, *, user_id, session_id):
        # Completion requests acknowledge only local durable registration.
        # The independent worker alone may call Profile or resume delivery.
        row = await self.store.get(source_intent_id(user_id, session_id))
        if row is None or row.get('consumerRevision') != RECORD_PROFILE_SOURCE_REVISION:
            raise RecordSourceConflict('the accepted Record source intent is unavailable')
        return row

    async def recover_one(self, intent_id):
        row = await self.store.claim(intent_id, now=self.clock(), owner=uuid.uuid4().hex)
        if row is None:
            return None
        try:
            if row['consumerRevision'] != RECORD_PROFILE_SOURCE_REVISION:
                raise RecordSourceConflict('the accepted Record source version is unavailable')
            if self.clock() >= row['deadlineAt']:
                return await self._save(row, {'status': 'failed', 'errorCode': 'record_source_deadline_exceeded'})
            committed = await self._call(
                row,
                self.completion_evidence.is_committed(
                    user_id=row['userId'],
                    session_id=row['sessionId'],
                    completion_id=row['completionId'],
                    draft_id=row['draftId'],
                ),
            )
            if committed is not True:
                return await self._save(
                    row,
                    {'status': 'awaiting_completion', 'nextAttemptAt': self.clock() + row['policy']['poll_seconds']},
                )
            row = await self._prepare(row)
            if not row.get('event'):
                material = json.loads(row['materialJson'])
                event = {
                    'schemaVersion': 'profile-source-event.v1',
                    'eventId': row['_id'] + ':publication:1',
                    'scope': row['scope'],
                    'sourceType': 'record',
                    'sourceId': row['sessionId'],
                    'sourceRevision': material['sourceRevision'],
                    'legacySourceRevision': None,
                    'publicationRevision': 1,
                    'priorRevision': None,
                    'eventKind': 'publish',
                    'occurredAt': datetime.fromtimestamp(row['createdAt'], UTC).isoformat(),
                    'contentDigest': content_digest(material),
                    'snapshotRef': row['snapshotRef'],
                }
                row = await self._save(row, {'event': event, 'completionConfirmed': True})
            if not row.get('authorization'):
                state = await self._call(row, self.admission.authorize(deepcopy(row['reservation'])))
                row = await self._save(row, {'authorization': _check_admission(state, row, 'ready')})
            if row.get('receipt'):
                receipt = await self._call(row, self.receiver.get_receipt(deepcopy(row['event'])))
            else:
                if row['dispatchAttempts'] - row.get('dispatchLimitBase', 0) >= row['policy']['max_dispatches']:
                    return await self._save(row, {'status': 'failed', 'errorCode': 'record_source_dispatch_exhausted'})
                # Reserve an attempt before dispatch, including cancellation and lost ACK.
                row = await self._save(row, {'dispatchAttempts': row['dispatchAttempts'] + 1})
                receipt = await self._call(row, self.receiver.receive(deepcopy(row['event'])))
            receipt = self._validate_receipt(receipt, row)
            state = receipt['status']
            status = state if state in _TERMINAL else 'waiting_receipt'
            return await self._save(
                row,
                {
                    'receipt': receipt,
                    'status': status,
                    'nextAttemptAt': self.clock() + row['policy']['poll_seconds'],
                    'errorCode': receipt.get('errorCode'),
                },
            )
        except RecordSourceLeaseLost:
            return None
        except Exception as error:
            return await self._settle_failure(row, error)
        finally:
            await self._release(row)

    async def _settle_failure(self, row, error):
        latest = await self.store.get(row['_id'])
        if latest is None or latest.get('leaseOwner') != row.get('leaseOwner'):
            return None
        failures = latest['transportFailures'] + 1
        terminal = isinstance(error, (RecordSourceConflict, ValueError, PermissionError))
        terminal = (
            terminal
            or failures - latest.get('transportFailureBase', 0) >= latest['policy']['max_transport_failures']
            or self.clock() >= latest['deadlineAt']
        )
        try:
            return await self._save(
                latest,
                {
                    'status': 'failed' if terminal else 'retry_wait',
                    'transportFailures': failures,
                    'errorCode': (
                        'record_source_deadline_exceeded'
                        if self.clock() >= latest['deadlineAt']
                        else 'record_source_' + type(error).__name__
                    ),
                    'nextAttemptAt': self.clock() + latest['policy']['poll_seconds'],
                },
            )
        except RecordSourceLeaseLost:
            return None

    @staticmethod
    def _validate_receipt(receipt, row):
        event = row['event']
        if not isinstance(receipt, dict) or any(
            receipt.get(key) != event[key]
            for key in ('scope', 'sourceType', 'sourceId', 'eventId', 'publicationRevision')
        ):
            raise RecordSourceConflict('Profile receipt belongs to a different fixed Record event')
        if receipt.get('contractVersion') != 'profile-source-receipt.v1':
            raise RecordSourceConflict('unsupported Profile receipt contract')
        watermark = receipt.get('watermark', {})
        if any(watermark.get(key) != event[key] for key in ('scope', 'sourceType', 'sourceId')):
            raise RecordSourceConflict('Profile watermark belongs to another Record source')
        state = receipt.get('status')
        if state not in {'received', 'waiting', 'processing', 'retry_wait', 'partial', 'applied', 'failed'}:
            raise RecordSourceConflict('unsupported Record source application state')
        if state == 'applied' and (
            receipt.get('applicationState') != 'applied'
            or receipt.get('watermark', {}).get('appliedPublicationRevision', 0) < event['publicationRevision']
        ):
            raise RecordSourceConflict('applied receipt does not cover the Record publication')
        return deepcopy(receipt)

    async def read_source_snapshot(self, snapshot_ref, *, scope):
        return await self.material_reader.read_source_snapshot(snapshot_ref, scope=scope)

    async def redrive_transport(self, intent_id, *, reason, policy):
        if not isinstance(reason, str) or not reason.strip() or not isinstance(policy, RecordPublicationPolicy):
            raise ValueError('explicit redrive requires a reason and complete policy')
        row = await self.store.get(intent_id)
        if row is None or row['status'] != 'failed' or row.get('receipt', {}).get('status') in _TERMINAL:
            raise RecordSourceConflict(
                'only failed transport can be redriven; Profile governance requires its own command'
            )
        now = self.clock()
        if row.get('leaseOwner') and row.get('leaseExpiresAt', 0) > now:
            raise RecordSourceConflict('Record source still has an active owner')
        audit = row.get('redrives', []) + [
            {
                'reason': reason,
                'at': now,
                'previousPolicy': row['policy'],
                'previousDeadlineAt': row['deadlineAt'],
                'dispatchAttempts': row['dispatchAttempts'],
            }
        ]
        replacement = {
            **row,
            'revision': row['revision'] + 1,
            'status': 'retry_wait',
            'policy': asdict(policy),
            'deadlineAt': now + policy.deadline_seconds,
            'nextAttemptAt': now,
            'redrives': audit,
            'dispatchLimitBase': row['dispatchAttempts'],
            'transportFailureBase': row['transportFailures'],
            'leaseOwner': None,
            'leaseExpiresAt': 0,
        }
        if not await self.store.replace({**row, 'leaseOwner': None}, replacement, now=now):
            raise RecordSourceConflict('Record source changed while redrive was being accepted')
        return replacement
