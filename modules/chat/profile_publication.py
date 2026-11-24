"""Chat-owned durable delivery after business save; queries never drive recovery."""

from __future__ import annotations

import asyncio
import time
import uuid
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime

from modules.chat.profile_source import CHAT_PROFILE_CONSUMER
from modules.chat.profile_source import ChatProfileDeliveryPolicy
from modules.chat.profile_source import source_digest

TERMINAL = {'applied', 'partial', 'failed', 'superseded'}


class ChatSourceConflict(ValueError):
    pass


class ChatSourceLeaseLost(RuntimeError):
    pass


def source_task_id(user_id, message_id):
    return 'chat_task_' + source_digest([user_id, message_id])


class ChatSourcePublicationService:
    """Ports own storage and public Profile translation; this workflow owns retry policy."""

    def __init__(self, *, store, owner, materials, admission, receiver, clock=time.time):
        self.store, self.owner, self.materials = store, owner, materials
        self.admission, self.receiver, self.clock = admission, receiver, clock

    async def register(self, *, user_id, message_id):
        binding = await self.owner.read_source(user_id=user_id, message_id=message_id)
        if binding is None:
            return None
        policy = ChatProfileDeliveryPolicy(**binding['deliveryPolicy'])
        created = datetime.fromisoformat(binding['createdAt']).timestamp()
        row = await self.store.insert(
            {
                '_id': source_task_id(user_id, message_id),
                'recordKind': 'task',
                'revision': 1,
                'userId': user_id,
                'messageId': message_id,
                'binding': binding,
                'consumerRevision': CHAT_PROFILE_CONSUMER,
                'status': 'pending',
                'createdAt': created,
                'deadlineAt': created + policy.max_age_seconds,
                'policy': asdict(policy),
                'nextAttemptAt': self.clock(),
                'attempts': 0,
                'dispatchAttempts': 0,
                'leaseGeneration': 0,
                'leaseOwner': None,
                'leaseExpiresAt': 0,
            }
        )
        if row.get('binding') != binding or row.get('consumerRevision') != CHAT_PROFILE_CONSUMER:
            raise ChatSourceConflict('the Chat source task has a different saved identity')
        return row

    async def ensure_queued(self, *, user_id, message_id):
        # A failed local registration is repaired by the independent saved-message scan.
        # No Profile call, worker scheduling or session mutation belongs in this path.
        try:
            return await self.register(user_id=user_id, message_id=message_id)
        except Exception:
            return None

    async def _save(self, row, changes):
        replacement = {**deepcopy(row), **deepcopy(changes), 'revision': row['revision'] + 1}
        if not await self.store.replace(row, replacement, now=self.clock()):
            raise ChatSourceLeaseLost('Chat source owner or lease changed')
        row.clear()
        row.update(replacement)
        return row

    async def _call(self, row, operation):
        remaining = row['deadlineAt'] - self.clock()
        if remaining <= 0:
            operation.close()
            raise TimeoutError('Chat source deadline exceeded')
        try:
            await self._save(row, {'leaseExpiresAt': self.clock() + row['policy']['lease_seconds']})
        except BaseException:
            operation.close()
            raise
        # Each call has a deadline shorter than its current lease, including nested
        # admission verification. A late result must still pass the owner CAS.
        async with asyncio.timeout(min(30, row['policy']['lease_seconds'] / 2, remaining)):
            result = await operation
        await self._save(row, {'leaseExpiresAt': self.clock() + row['policy']['lease_seconds']})
        if self.clock() >= row['deadlineAt']:
            raise TimeoutError('Chat source deadline exceeded')
        return result

    async def recover_pending(self, *, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Chat source batch must be in 1..1000')
        # Discovery is independent of task status and advances past invalid/failed
        # messages. A full seek cycle wraps so earlier sessions' new messages appear.
        scan = await self.store.scan_state()
        page = await self.owner.discover(after=scan.get('after'), limit=limit)
        for item in page:
            try:
                await self.register(user_id=item['userId'], message_id=item['messageId'])
            except Exception as error:
                await self.store.record_discovery_error(item, type(error).__name__)
        await self.store.advance_scan(scan, page[-1] if len(page) == limit else None)
        ids = await self.store.pending(now=self.clock(), limit=limit)
        for identity in ids:
            try:
                await self.recover_one(identity)
            except Exception:
                # A storage outage for one task does not abort unrelated owners.
                continue
        return len(ids)

    async def recover_one(self, identity):
        row = await self.store.claim(identity, now=self.clock(), owner=uuid.uuid4().hex)
        if row is None:
            return None
        try:
            if row.get('consumerRevision') != CHAT_PROFILE_CONSUMER:
                raise ChatSourceConflict('the frozen Chat source consumer is unavailable')
            if self.clock() >= row['deadlineAt']:
                return await self._save(row, {'status': 'failed', 'errorCode': 'chat_source_deadline_exceeded'})
            if row['attempts'] - row.get('attemptBase', 0) > row['policy']['max_attempts']:
                return await self._save(row, {'status': 'failed', 'errorCode': 'chat_source_attempts_exhausted'})
            binding = await self._call(row, self.owner.read_source(user_id=row['userId'], message_id=row['messageId']))
            if binding != row['binding']:
                raise ChatSourceConflict('the saved Chat message or fixed delivery identity changed')
            if not row.get('scope'):
                scope = await self._call(row, self.admission.prepare_self_scope(row['userId']))
                if not isinstance(scope, dict) or scope.get('userId') != row['userId'] or not scope.get('subjectId'):
                    raise ChatSourceConflict('Profile scope does not belong to the saved message')
                await self._save(row, {'scope': scope})
            if not row.get('material'):
                value = await self._call(row, self.materials.prepare(row['binding'], scope=row['scope']))
                await self._save(row, {'material': value})
            if not row.get('reservation'):
                value = await self._call(row, self.admission.reserve(row['material']))
                await self._save(row, {'reservation': self._admission(value, row, {'blocked', 'ready'})})
            if row.get('reservation'):
                self._admission(row['reservation'], row, {'blocked', 'ready'})
            if row.get('authorization'):
                self._admission(row['authorization'], row, {'ready'})
            if not row.get('authorization'):
                value = await self._call(row, self.admission.authorize(row['reservation']))
                await self._save(row, {'authorization': self._admission(value, row, {'ready'})})
            event = row['material']['event']
            if row.get('receipt'):
                receipt = await self._call(row, self.receiver.get_receipt(deepcopy(event)))
            else:
                # An ACK-lost first delivery repeats this same frozen event. A lookup
                # without an accepted local receipt cannot prove the payload digest.
                await self._save(row, {'dispatchAttempts': row.get('dispatchAttempts', 0) + 1})
                receipt = await self._call(row, self.receiver.receive(deepcopy(event)))
            receipt = self._receipt(receipt, row)
            status = receipt['status'] if receipt['status'] in TERMINAL else 'waiting_receipt'
            return await self._save(
                row,
                {
                    'receipt': receipt,
                    'status': status,
                    'errorCode': receipt.get('errorCode'),
                    'nextAttemptAt': self.clock() + row['policy']['retry_seconds'],
                },
            )
        except ChatSourceLeaseLost:
            return None
        except Exception as error:
            latest = await self.store.get(identity)
            if latest is None or latest.get('leaseOwner') != row.get('leaseOwner'):
                return None
            row = latest
            expired = self.clock() >= row['deadlineAt']
            exhausted = row['attempts'] - row.get('attemptBase', 0) >= row['policy']['max_attempts']
            permanent = isinstance(error, (ValueError, PermissionError))
            try:
                return await self._save(
                    row,
                    {
                        'status': 'failed' if expired or exhausted or permanent else 'retry_wait',
                        'errorCode': (
                            'chat_source_deadline_exceeded' if expired else 'chat_source_' + type(error).__name__
                        ),
                        'nextAttemptAt': self.clock() + row['policy']['retry_seconds'],
                    },
                )
            except ChatSourceLeaseLost:
                return None
        finally:
            latest = await self.store.get(identity)
            if latest and latest.get('leaseOwner') == row.get('leaseOwner'):
                try:
                    await self._save(latest, {'leaseOwner': None, 'leaseExpiresAt': 0})
                except ChatSourceLeaseLost:
                    pass

    @staticmethod
    def _admission(value, row, statuses):
        event = row['material']['event']
        auth = value.get('authorization', {}) if isinstance(value, dict) else {}
        expected = {
            'admissionKind': 'fresh',
            'scope': row['scope'],
            'sourceType': 'chat',
            'sourceId': row['messageId'],
            'materialContractVersion': 'profile.chat-source.v2',
            'materialSnapshotRef': event['snapshotRef'],
            'materialContentDigest': event['contentDigest'],
        }
        if (
            not isinstance(value, dict)
            or value.get('status') not in statuses
            or any(auth.get(k) != v for k, v in expected.items())
        ):
            raise ChatSourceConflict('fresh admission does not bind the fixed Chat source')
        if not auth.get('authorizationId') or not auth.get('ownerFenceRef'):
            raise ChatSourceConflict('fresh admission has no durable owner identity')
        if row.get('reservation') and auth != row['reservation']['authorization']:
            raise ChatSourceConflict('fresh authorization changed after reservation')
        return deepcopy(value)

    @staticmethod
    def _receipt(value, row):
        event = row['material']['event']
        if not isinstance(value, dict) or value.get('contractVersion') != 'profile-source-receipt.v1':
            raise ChatSourceConflict('unsupported Profile receipt')
        if any(value.get(k) != event[k] for k in ('scope', 'sourceType', 'sourceId', 'eventId', 'publicationRevision')):
            raise ChatSourceConflict('Profile receipt belongs to another Chat event')
        watermark = value.get('watermark', {})
        if any(watermark.get(k) != event[k] for k in ('scope', 'sourceType', 'sourceId')):
            raise ChatSourceConflict('Profile watermark belongs to another Chat source')
        if value.get('status') not in TERMINAL | {'received', 'waiting', 'processing', 'retry_wait'}:
            raise ChatSourceConflict('unsupported Profile application status')
        if value['status'] == 'applied' and (
            value.get('applicationState') != 'applied' or watermark.get('appliedPublicationRevision', 0) < 1
        ):
            raise ChatSourceConflict('Profile did not completely apply the Chat event')
        if value['status'] == 'superseded':
            retraction = value.get('supersededByPublicationRevision')
            if (
                value.get('applicationState') != 'superseded_by_retraction'
                or type(retraction) is not int
                or retraction <= event['publicationRevision']
                or watermark.get('appliedRetractionRevision', 0) < retraction
                or watermark.get('settledPublicationRevision', 0) < retraction
            ):
                raise ChatSourceConflict('Profile did not settle the superseded Chat event')
        return deepcopy(value)

    async def redrive_transport(self, identity, *, reason, policy):
        if not isinstance(reason, str) or not reason.strip() or not isinstance(policy, ChatProfileDeliveryPolicy):
            raise ValueError('redrive requires an explicit reason and policy')
        row = await self.store.get(identity)
        if row is None or row['status'] != 'failed' or row.get('receipt', {}).get('status') in TERMINAL:
            raise ChatSourceConflict('only failed delivery can be redriven; Profile governance is independent')
        now = self.clock()
        replacement = {
            **deepcopy(row),
            'revision': row['revision'] + 1,
            'status': 'retry_wait',
            'policy': asdict(policy),
            'deadlineAt': now + policy.max_age_seconds,
            'nextAttemptAt': now,
            'attemptBase': row['attempts'],
            'leaseOwner': None,
            'leaseExpiresAt': 0,
            'redrives': row.get('redrives', [])
            + [
                {
                    'reason': reason,
                    'at': now,
                    'previousPolicy': row['policy'],
                    'previousDeadlineAt': row['deadlineAt'],
                    'attempts': row['attempts'],
                }
            ],
        }
        if not await self.store.replace({**row, 'leaseOwner': None}, replacement, now=now):
            raise ChatSourceConflict('Chat delivery changed while redrive was accepted')
        return replacement
