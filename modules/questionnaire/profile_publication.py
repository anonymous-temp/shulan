"""Questionnaire-owned durable delivery after business save; queries never drive recovery."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from typing import Protocol

from modules.questionnaire.profile_source import ATTACHMENT_SELECTION
from modules.questionnaire.profile_source import CONSUMER_REVISION
from modules.questionnaire.profile_source import MATERIAL_CONTRACT
from modules.questionnaire.profile_source import QuestionnaireSourceConflict
from modules.questionnaire.profile_source import QuestionnaireSourcePolicy
from modules.questionnaire.profile_source import source_digest

TERMINAL = {'applied', 'partial', 'failed', 'superseded'}


class QuestionnaireSourceAdmissionPort(Protocol):
    async def prepare_self_scope(self, user_id: str) -> dict: ...
    async def reserve(self, material: dict) -> dict: ...
    async def authorize(self, reservation: dict) -> dict: ...


class QuestionnaireAttachmentPublicationPort(Protocol):
    async def prepare(
        self,
        *,
        scope: dict,
        request_id: str,
        submitted_reference: dict,
        submitted_digest: str,
        questionnaire_event: dict,
        selection: str,
    ) -> dict: ...
    async def publish(self, *, scope: dict, command_id: str) -> dict: ...


class QuestionnaireSourceLeaseLost(RuntimeError):
    pass


def source_task_id(user_id, questionnaire_id):
    return 'questionnaire_task_' + source_digest([user_id, questionnaire_id])


class QuestionnaireSourcePublicationService:
    """Ports own storage and public Profile translation; this workflow owns retry policy."""

    def __init__(self, *, store, owner, materials, admission, receiver, submitted, attachments, clock=time.time):
        self.store, self.owner, self.materials = store, owner, materials
        self.admission, self.receiver, self.clock = admission, receiver, clock
        self.submitted, self.attachments = submitted, attachments

    async def register(self, *, user_id, questionnaire_id):
        binding = await self.owner.read_source(user_id=user_id, questionnaire_id=questionnaire_id)
        if binding is None:
            return None
        policy = QuestionnaireSourcePolicy(**binding['selection']['policy'])
        created = datetime.fromisoformat(binding['createdAt']).timestamp()
        row = await self.store.insert(
            {
                '_id': source_task_id(user_id, questionnaire_id),
                'recordKind': 'task',
                'revision': 1,
                'userId': user_id,
                'questionnaireId': questionnaire_id,
                'binding': binding,
                'consumerRevision': CONSUMER_REVISION,
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
        if row.get('binding') != binding or row.get('consumerRevision') != CONSUMER_REVISION:
            raise QuestionnaireSourceConflict('the Questionnaire source task has a different saved identity')
        return row

    async def ensure_queued(self, *, user_id, questionnaire_id):
        # A failed local registration is repaired by the independent saved-submission scan.
        # No Profile call, worker scheduling or business document mutation belongs in this path.
        try:
            return await self.register(user_id=user_id, questionnaire_id=questionnaire_id)
        except Exception:
            return None

    async def _save(self, row, changes):
        replacement = {**deepcopy(row), **deepcopy(changes), 'revision': row['revision'] + 1}
        if not await self.store.replace(row, replacement, now=self.clock()):
            raise QuestionnaireSourceLeaseLost('Questionnaire source owner or lease changed')
        row.clear()
        row.update(replacement)
        return row

    async def _call(self, row, operation):
        remaining = row['deadlineAt'] - self.clock()
        if remaining <= 0:
            operation.close()
            raise TimeoutError('Questionnaire source deadline exceeded')
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
            raise TimeoutError('Questionnaire source deadline exceeded')
        return result

    async def recover_pending(self, *, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Questionnaire source batch must be in 1..1000')
        # Discovery is independent of task status and advances past invalid/failed
        # submissions. A full seek cycle wraps so newly committed lower identities appear.
        scan = await self.store.scan_state()
        page = await self.owner.discover(after=scan.get('after'), limit=limit)
        for item in page:
            try:
                await self.register(user_id=item['userId'], questionnaire_id=item['questionnaireId'])
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
            if row.get('consumerRevision') != CONSUMER_REVISION:
                raise QuestionnaireSourceConflict('the frozen Questionnaire source consumer is unavailable')
            if self.clock() >= row['deadlineAt']:
                return await self._save(
                    row, {'status': 'failed', 'errorCode': 'questionnaire_source_deadline_exceeded'}
                )
            if row['attempts'] - row.get('attemptBase', 0) > row['policy']['max_attempts']:
                return await self._save(
                    row, {'status': 'failed', 'errorCode': 'questionnaire_source_attempts_exhausted'}
                )
            binding = await self._call(
                row, self.owner.read_source(user_id=row['userId'], questionnaire_id=row['questionnaireId'])
            )
            if binding != row['binding']:
                raise QuestionnaireSourceConflict(
                    'the saved Questionnaire submission or fixed delivery identity changed'
                )
            if not row.get('scope'):
                scope = await self._call(row, self.admission.prepare_self_scope(row['userId']))
                if not isinstance(scope, dict) or scope.get('userId') != row['userId'] or not scope.get('subjectId'):
                    raise QuestionnaireSourceConflict('Profile scope does not belong to the saved submission')
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
            await self._attachments(row)
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
        except QuestionnaireSourceLeaseLost:
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
                            'questionnaire_source_deadline_exceeded'
                            if expired
                            else 'questionnaire_source_' + type(error).__name__
                        ),
                        'nextAttemptAt': self.clock() + row['policy']['retry_seconds'],
                    },
                )
            except QuestionnaireSourceLeaseLost:
                return None
        finally:
            latest = await self.store.get(identity)
            if latest and latest.get('leaseOwner') == row.get('leaseOwner'):
                try:
                    await self._save(latest, {'leaseOwner': None, 'leaseExpiresAt': 0})
                except QuestionnaireSourceLeaseLost:
                    pass

    @staticmethod
    def _admission(value, row, statuses):
        event = row['material']['event']
        auth = value.get('authorization', {}) if isinstance(value, dict) else {}
        expected = {
            'admissionKind': 'fresh',
            'scope': row['scope'],
            'sourceType': 'questionnaire',
            'sourceId': row['questionnaireId'],
            'materialContractVersion': MATERIAL_CONTRACT,
            'materialSnapshotRef': event['snapshotRef'],
            'materialContentDigest': event['contentDigest'],
        }
        if (
            not isinstance(value, dict)
            or value.get('status') not in statuses
            or any(auth.get(k) != v for k, v in expected.items())
        ):
            raise QuestionnaireSourceConflict('fresh admission does not bind the fixed Questionnaire source')
        if not auth.get('authorizationId') or not auth.get('ownerFenceRef'):
            raise QuestionnaireSourceConflict('fresh admission has no durable owner identity')
        if row.get('reservation') and auth != row['reservation']['authorization']:
            raise QuestionnaireSourceConflict('fresh authorization changed after reservation')
        return deepcopy(value)

    @staticmethod
    def _receipt(value, row):
        event = row['material']['event']
        if not isinstance(value, dict) or value.get('contractVersion') != 'profile-source-receipt.v1':
            raise QuestionnaireSourceConflict('unsupported Profile receipt')
        if any(value.get(k) != event[k] for k in ('scope', 'sourceType', 'sourceId', 'eventId', 'publicationRevision')):
            raise QuestionnaireSourceConflict('Profile receipt belongs to another Questionnaire event')
        watermark = value.get('watermark', {})
        if any(watermark.get(k) != event[k] for k in ('scope', 'sourceType', 'sourceId')):
            raise QuestionnaireSourceConflict('Profile watermark belongs to another Questionnaire source')
        if value.get('status') not in TERMINAL | {'received', 'waiting', 'processing', 'retry_wait'}:
            raise QuestionnaireSourceConflict('unsupported Profile application status')
        if value['status'] == 'applied' and (
            value.get('applicationState') != 'applied' or watermark.get('appliedPublicationRevision', 0) < 1
        ):
            raise QuestionnaireSourceConflict('Profile did not completely apply the Questionnaire event')
        if value['status'] == 'superseded':
            retraction = value.get('supersededByPublicationRevision')
            if (
                value.get('applicationState') != 'superseded_by_retraction'
                or type(retraction) is not int
                or retraction <= event['publicationRevision']
                or watermark.get('appliedRetractionRevision', 0) < retraction
                or watermark.get('settledPublicationRevision', 0) < retraction
            ):
                raise QuestionnaireSourceConflict('Profile did not settle the superseded Questionnaire event')
        return deepcopy(value)

    async def _attachments(self, row):
        if row.get('attachmentReceipt'):
            self._attachment_receipt(row['attachmentReceipt'], row)
            return
        if not row.get('submittedMaterial'):
            value = await self._call(
                row,
                self.submitted.read_submission(
                    user_id=row['userId'],
                    questionnaire_id=row['questionnaireId'],
                    source_revision=1,
                    selection={
                        'contractVersion': 'questionnaire.submitted-material.v1',
                        'implementationRevision': 'questionnaire-submitted-material-v1',
                    },
                ),
            )
            if value.get('payload', {}).get('submissionProof') != row['material']['submissionProof']:
                raise QuestionnaireSourceConflict('Attachment submission differs from the saved answer material')
            if source_digest(value['payload']) != value.get('contentDigest'):
                raise QuestionnaireSourceConflict('Submitted attachment material digest differs')
            selected, excluded = value['payload'].get('attachments'), value['payload'].get('excluded')
            if not isinstance(selected, list) or not isinstance(excluded, list) or len(selected) + len(excluded) > 5:
                raise QuestionnaireSourceConflict('Submitted attachment inventory is unsupported')
            items = [
                {'ordinal': index, **{key: item[key] for key in ('artifactId', 'contentKey', 'interpretationDigest')}}
                for index, item in enumerate(selected)
            ]
            await self._save(
                row,
                {
                    'submittedMaterial': {
                        'reference': deepcopy(value['reference']),
                        'contentDigest': value['contentDigest'],
                        'items': items,
                        'excluded': deepcopy(excluded),
                    },
                    'attachmentStatus': 'pending',
                },
            )
        if not row.get('attachmentCommand'):
            fixed = row['submittedMaterial']
            command = await self._call(
                row,
                self.attachments.prepare(
                    scope=deepcopy(row['scope']),
                    request_id=row['binding']['attachmentIntent']['requestId'],
                    submitted_reference=deepcopy(fixed['reference']),
                    submitted_digest=fixed['contentDigest'],
                    questionnaire_event=deepcopy(row['material']['event']),
                    selection=ATTACHMENT_SELECTION,
                ),
            )
            if (
                not isinstance(command, dict)
                or command.get('contractVersion') != 'document.questionnaire-attachment-command.v1'
                or command.get('status') != 'prepared'
                or not isinstance(command.get('commandId'), str)
                or not command['commandId'].strip()
                or not isinstance(command.get('commandDigest'), str)
                or re.fullmatch(r'[0-9a-f]{64}', command['commandDigest']) is None
                or command.get('scope') != row['scope']
                or command.get('questionnaireId') != row['questionnaireId']
                or type(command.get('questionnaireSourceRevision')) is not int
                or command['questionnaireSourceRevision'] != 1
            ):
                raise QuestionnaireSourceConflict('Attachment command does not bind this Questionnaire source')
            actual = command.get('items')
            if (
                not isinstance(actual, list)
                or len(actual) != len(fixed['items'])
                or command.get('excluded') != fixed['excluded']
                or any(
                    any(item.get(key) != value for key, value in expected.items())
                    for item, expected in zip(actual, fixed['items'], strict=True)
                )
            ):
                raise QuestionnaireSourceConflict('Attachment command does not cover the fixed submitted inventory')
            await self._save(row, {'attachmentCommand': command, 'attachmentStatus': 'prepared'})
        # A lost ACK replays the exact durable command, never a fresh submitted/latest lookup.
        receipt = await self._call(
            row,
            self.attachments.publish(
                scope=deepcopy(row['scope']),
                command_id=row['attachmentCommand']['commandId'],
            ),
        )
        receipt = self._attachment_receipt(receipt, row)
        await self._save(row, {'attachmentReceipt': receipt, 'attachmentStatus': 'accepted'})

    @staticmethod
    def _attachment_receipt(value, row):
        command = row['attachmentCommand']
        if (
            not isinstance(value, dict)
            or value.get('contractVersion') != 'document.questionnaire-attachment-receipt.v1'
            or value.get('status') != 'accepted'
            or value.get('commandId') != command['commandId']
            or value.get('commandDigest') != command['commandDigest']
        ):
            raise QuestionnaireSourceConflict('Attachments were not durably accepted for the fixed command')
        expected, actual = command.get('items'), value.get('items')
        if (
            not isinstance(expected, list)
            or not isinstance(actual, list)
            or len(expected) != len(actual)
            or len(expected) > 5
        ):
            raise QuestionnaireSourceConflict('Attachment acceptance inventory is incomplete')
        for source, receipt in zip(expected, actual, strict=True):
            if (
                any(receipt.get(key) != source.get(key) for key in ('artifactId', 'reportId'))
                or receipt.get('inputInterpretationDigest') != source.get('interpretationDigest')
                or type(receipt.get('legacySourceRevision')) is not int
                or receipt['legacySourceRevision'] < 1
                or not isinstance(receipt.get('storedResultDigest'), str)
                or re.fullmatch(r'[0-9a-f]{64}', receipt['storedResultDigest']) is None
                or not isinstance(source.get('interpretationDigest'), str)
                or re.fullmatch(r'[0-9a-f]{64}', source['interpretationDigest']) is None
                or receipt.get('ingestionStatus') not in {'pending', 'queued'}
                or receipt['ingestionStatus'] == 'queued'
                and not receipt.get('ingestionJobId')
            ):
                raise QuestionnaireSourceConflict('Attachment receipt lacks a real compatible source record')
        if value.get('excluded') != command.get('excluded'):
            raise QuestionnaireSourceConflict('Attachment exclusion inventory changed')
        return deepcopy(value)

    async def redrive_transport(self, identity, *, reason, policy):
        if not isinstance(reason, str) or not reason.strip() or not isinstance(policy, QuestionnaireSourcePolicy):
            raise ValueError('redrive requires an explicit reason and policy')
        row = await self.store.get(identity)
        if row is None or row['status'] != 'failed' or row.get('receipt', {}).get('status') in TERMINAL:
            raise QuestionnaireSourceConflict('only failed delivery can be redriven; Profile governance is independent')
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
            raise QuestionnaireSourceConflict('Questionnaire delivery changed while redrive was accepted')
        return replacement
