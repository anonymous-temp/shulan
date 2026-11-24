"""MongoDB report admission, leases and durable callback outbox.

No transactions or TTL indexes are required. A durable batch is the recovery
anchor; immutable artifacts are authoritative. Reconciliation remains pending
until every terminal artifact has its unique delivery record.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Callable
from urllib.parse import urlsplit
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from modules.report.artifacts import EVALUATE_REPORT_TYPES
from modules.report.durable_ports import ACTIVE_GENERATION


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def delivery_summary(document: dict | None) -> dict:
    document = document or {}
    return {
        'status': document.get('status', 'unknown'),
        'attempts': document.get('attempts', 0),
        'nextRetryAt': _iso(document.get('nextRetryAt')),
        'deliveredAt': _iso(document.get('deliveredAt')),
        'lastError': document.get('lastError'),
    }


def _iso(value):
    return as_utc(value).isoformat() if isinstance(value, datetime) else None


class MongoReportWorkflowStore:
    def __init__(
        self,
        *,
        batches,
        artifacts,
        deliveries,
        clock: Callable = utcnow,
        lease_seconds: float = 120,
        reuse_seconds: float = 900,
        expected_report_types: tuple[str, ...] = EVALUATE_REPORT_TYPES,
    ) -> None:
        self.batches = batches
        self.artifacts = artifacts
        self.deliveries = deliveries
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.reuse_seconds = reuse_seconds
        self.expected_report_types = tuple(expected_report_types)
        self.on_delivery_ready: Callable | None = None
        self._indexes_ready = False
        self._index_lock = asyncio.Lock()

    async def ensure_indexes(self) -> None:
        if self._indexes_ready:
            return
        async with self._index_lock:
            if self._indexes_ready:
                return
            await self.batches.create_index(
                [('activeFingerprint', 1)],
                unique=True,
                partialFilterExpression={'activeFingerprint': {'$type': 'string'}},
                name='report_active_request_unique',
            )
            await self.batches.create_index(
                [('generationState', 1), ('leaseExpiresAt', 1), ('createdAt', 1)],
                name='report_generation_work',
            )
            await self.batches.create_index(
                [('reconcileNeeded', 1), ('updatedAt', 1)],
                name='report_reconciliation',
            )
            await self.deliveries.create_index(
                [('status', 1), ('nextRetryAt', 1), ('leaseExpiresAt', 1)],
                name='report_callback_due',
            )
            await self.deliveries.create_index(
                [('userId', 1), ('finalReportId', 1)],
                name='report_callback_batch',
            )
            await self.deliveries.create_index(
                [('status', 1), ('createdAt', 1)],
                name='report_callback_backlog',
            )
            self._indexes_ready = True

    async def admit(self, request: dict, fingerprint: str, callback_url: str, *, execution_manifest=None) -> dict:
        request, execution_manifest = deepcopy(request), deepcopy(execution_manifest)
        await self.ensure_indexes()
        for _ in range(5):
            now = self.clock()
            # Keep history; only release the unique active-request pointer.
            await self.batches.update_one(
                {'activeFingerprint': fingerprint, 'generationState': 'completed', 'reuseUntil': {'$lte': now}},
                {'$unset': {'activeFingerprint': ''}},
            )
            existing = await self.batches.find_one({'activeFingerprint': fingerprint})
            if existing is not None:
                # Reconfirm the write concern after an earlier admission with
                # an ambiguous response; a plain read is not a durable receipt.
                confirmed = await self.batches.update_one(
                    {'_id': existing['_id'], 'activeFingerprint': fingerprint},
                    {'$set': {'lastRequestedAt': now}},
                )
                if confirmed.acknowledged and confirmed.matched_count:
                    return existing
                continue
            report_id = f'rpt_{uuid4().hex}'
            document = {
                '_id': report_id,
                'finalReportId': report_id,
                'userId': request['userId'],
                'sessionId': request['sessionId'],
                'request': request,
                'executionManifest': execution_manifest,
                'activeFingerprint': fingerprint,
                'callbackUrl': callback_url,
                'deliveryEnabled': True,
                'deliveryRevision': 0,
                'durableVersion': 1,
                'schemaVersion': 2,
                'generationState': 'queued',
                'generationStatus': 'queued',
                'reconcileNeeded': True,
                'documents': [],
                'createdAt': now,
                'updatedAt': now,
            }
            try:
                result = await self.batches.insert_one(document)
                if not result.acknowledged:
                    raise RuntimeError('report admission was not acknowledged')
                return document
            except DuplicateKeyError:
                continue
        raise RuntimeError('concurrent report admission could not be resolved')

    async def claim_generation(self, owner: str, *, expired: bool = False) -> dict | None:
        await self.ensure_indexes()
        now = self.clock()
        query = {'durableVersion': 1}
        if expired:
            query.update(
                {
                    'generationState': {'$in': ['running', 'recovering', 'finalizing']},
                    'leaseExpiresAt': {'$lte': now},
                }
            )
        else:
            query['generationState'] = 'queued'
        return await self.batches.find_one_and_update(
            query,
            {
                '$set': {
                    'generationState': 'recovering' if expired else 'running',
                    'generationStatus': 'running',
                    'leaseOwner': owner,
                    'leaseToken': uuid4().hex,
                    'leaseExpiresAt': now + timedelta(seconds=self.lease_seconds),
                    'updatedAt': now,
                }
            },
            sort=[('createdAt', 1), ('_id', 1)],
            return_document=ReturnDocument.AFTER,
        )

    def _generation_lease(self, job: dict) -> dict:
        return {
            '_id': job['_id'],
            'leaseToken': job['leaseToken'],
            'leaseOwner': job['leaseOwner'],
            'leaseExpiresAt': {'$gt': self.clock()},
            'generationState': {'$in': ['running', 'recovering', 'finalizing']},
        }

    async def renew_generation(self, job: dict) -> bool:
        result = await self.batches.update_one(
            self._generation_lease(job),
            {'$set': {'leaseExpiresAt': self.clock() + timedelta(seconds=self.lease_seconds)}},
        )
        return bool(result.matched_count)

    async def mark_generation_started(self, job: dict) -> bool:
        """Enter the pipeline once, only while this generation still owns its lease."""
        query = self._generation_lease(job)
        query.update(generationState='running', generationStartedAt={'$exists': False})
        result = await self.batches.update_one(
            query,
            {'$set': {'generationStartedAt': self.clock()}},
        )
        return bool(result.matched_count)

    async def release_generation(self, job: dict, *, known_not_started: bool = False) -> bool:
        """Requeue only a known unstarted claim; started clinical work is never retried."""
        if known_not_started is not True:
            return False
        query = self._generation_lease(job)
        query.update(generationState='running', generationStartedAt={'$exists': False})
        result = await self.batches.update_one(
            query,
            {
                '$set': {'generationState': 'queued', 'generationStatus': 'queued', 'updatedAt': self.clock()},
                '$unset': {'leaseOwner': '', 'leaseToken': '', 'leaseExpiresAt': ''},
            },
        )
        return bool(result.matched_count)

    async def allow_artifact_write(self, final_report_id: str) -> bool:
        batch = await self.batches.find_one({'_id': final_report_id})
        if not batch or batch.get('durableVersion') != 1:
            return True
        lease = ACTIVE_GENERATION.get()
        return bool(
            lease
            and lease == (final_report_id, batch.get('leaseToken'))
            and batch.get('generationState') in {'running', 'recovering', 'finalizing'}
            and as_utc(batch['leaseExpiresAt']) > self.clock()
        )

    async def finalize_generation(self, job: dict, error_code: str) -> bool:
        result = await self.batches.update_one(
            self._generation_lease(job),
            {'$set': {'generationState': 'finalizing', 'failureCode': error_code, 'updatedAt': self.clock()}},
        )
        return bool(result.matched_count)

    async def artifacts_for(self, final_report_id: str) -> list[dict]:
        return await self.artifacts.find({'finalReportId': final_report_id}).to_list(length=5)

    async def reconciliation_jobs(self, limit: int = 100) -> list[dict]:
        # Live generations are handled by their owner; expired leases go through
        # claim_generation(expired=True), never through an unfenced scan.
        return (
            await self.batches.find(
                {
                    'reconcileNeeded': True,
                    '$or': [
                        {'generationState': 'completed'},
                        {'durableVersion': {'$exists': False}},
                        {'redrivePending': {'$exists': True}},
                    ],
                }
            )
            .sort('updatedAt', 1)
            .limit(limit)
            .to_list(length=limit)
        )

    async def finish_reconciliation(self, job: dict) -> bool:
        artifacts = await self.artifacts_for(job['_id'])
        types = {item['reportType'] for item in artifacts}
        if job.get('durableVersion') == 1 and not set(self.expected_report_types).issubset(types):
            return False
        deliveries = await self.deliveries.find({'finalReportId': job['_id']}).to_list(length=5)
        if {item['reportType'] for item in deliveries} != types:
            return False
        query = {'_id': job['_id'], 'redrivePending': {'$exists': False}}
        values = {'reconcileNeeded': False, 'updatedAt': self.clock()}
        if job.get('durableVersion') == 1 and job.get('generationState') != 'completed':
            query = self._generation_lease(job)
            query['redrivePending'] = {'$exists': False}
            states = {item['generationStatus'] for item in artifacts}
            status = (
                'success'
                if states <= {'success', 'retried_success'}
                else 'failed'
                if states <= {'failed', 'fallback_empty'}
                else 'partial_success'
            )
            values.update(
                {
                    'generationState': 'completed',
                    'generationStatus': status,
                    'completedAt': self.clock(),
                    'reuseUntil': self.clock() + timedelta(seconds=self.reuse_seconds),
                }
            )
        result = await self.batches.update_one(query, {'$set': values})
        return bool(result.matched_count)

    async def ensure_delivery(self, final_report_id: str, report_type: str) -> None:
        batch = await self.batches.find_one({'_id': final_report_id, 'deliveryEnabled': True})
        if not batch:
            return  # Historical artifacts are never automatically replayed.
        key = f'{final_report_id}:{report_type}'
        artifact = await self.artifacts.find_one({'_id': key}, {'contentHash': 1})
        if not artifact:
            return
        now = self.clock()
        result = await self.deliveries.update_one(
            {'_id': key},
            {
                '$setOnInsert': {
                    'finalReportId': final_report_id,
                    'reportType': report_type,
                    'userId': batch['userId'],
                    'sessionId': batch.get('sessionId', ''),
                    'targetUrl': batch['callbackUrl'],
                    'contentHash': artifact['contentHash'],
                    'targetRevision': batch.get('deliveryRevision', 0),
                    'status': 'pending',
                    'attempts': 0,
                    'nextRetryAt': now,
                    'createdAt': now,
                    'updatedAt': now,
                }
            },
            upsert=True,
        )
        # A retarget may race an insertion which read the old batch snapshot.
        # Read the latest revision and only move an unsent task forward.
        latest = await self.batches.find_one({'_id': final_report_id})
        if latest and latest.get('deliveryRevision', 0):
            await self.deliveries.update_one(
                {
                    '_id': key,
                    'status': {'$ne': 'delivered'},
                    'targetRevision': {'$lt': latest['deliveryRevision']},
                },
                {
                    '$set': {
                        'targetUrl': latest['callbackUrl'],
                        'targetRevision': latest['deliveryRevision'],
                        'status': 'pending',
                        'nextRetryAt': self.clock(),
                    },
                    '$unset': {'leaseToken': '', 'leaseOwner': '', 'leaseExpiresAt': ''},
                },
            )
        if result.upserted_id is not None and self.on_delivery_ready is not None:
            self.on_delivery_ready()

    async def claim_delivery(self, owner: str) -> dict | None:
        await self.ensure_indexes()
        now = self.clock()
        return await self.deliveries.find_one_and_update(
            {
                '$or': [
                    {'status': {'$in': ['pending', 'retry_wait']}, 'nextRetryAt': {'$lte': now}},
                    {'status': 'sending', 'leaseExpiresAt': {'$lte': now}},
                ],
            },
            {
                '$set': {
                    'status': 'sending',
                    'leaseOwner': owner,
                    'leaseToken': uuid4().hex,
                    'leaseExpiresAt': now + timedelta(seconds=self.lease_seconds),
                    'updatedAt': now,
                },
                '$inc': {'attempts': 1},
            },
            sort=[('nextRetryAt', 1), ('_id', 1)],
            return_document=ReturnDocument.AFTER,
        )

    def _delivery_lease(self, job: dict) -> dict:
        return {
            '_id': job['_id'],
            'status': 'sending',
            'leaseToken': job['leaseToken'],
            'leaseExpiresAt': {'$gt': self.clock()},
        }

    async def delivery_body(self, job: dict) -> dict:
        artifact = await self.artifacts.find_one(
            {
                '_id': job['_id'],
                'userId': job['userId'],
                'contentHash': job['contentHash'],
            }
        )
        if not artifact or not isinstance(artifact.get('body'), dict):
            raise ValueError('immutable report artifact is unavailable')
        return artifact['body']

    async def acknowledge_delivery(self, job: dict) -> bool:
        result = await self.deliveries.update_one(
            self._delivery_lease(job),
            {
                '$set': {'status': 'delivered', 'deliveredAt': self.clock(), 'updatedAt': self.clock()},
                '$unset': {
                    'leaseToken': '',
                    'leaseOwner': '',
                    'leaseExpiresAt': '',
                    'nextRetryAt': '',
                    'lastError': '',
                },
            },
        )
        return bool(result.matched_count)

    async def retry_delivery(self, job: dict, *, next_retry_at: datetime, error: dict) -> bool:
        result = await self.deliveries.update_one(
            self._delivery_lease(job),
            {
                '$set': {
                    'status': 'retry_wait',
                    'nextRetryAt': next_retry_at,
                    'lastError': error,
                    'updatedAt': self.clock(),
                },
                '$unset': {'leaseToken': '', 'leaseOwner': '', 'leaseExpiresAt': ''},
            },
        )
        return bool(result.matched_count)

    async def get_delivery(self, *, user_id: str, final_report_id: str, report_type: str) -> dict:
        return delivery_summary(
            await self.deliveries.find_one(
                {
                    '_id': f'{final_report_id}:{report_type}',
                    'userId': user_id,
                }
            )
        )

    async def backlog(self) -> dict:
        query = {'status': {'$in': ['pending', 'retry_wait', 'sending']}}
        count = await self.deliveries.count_documents(query)
        oldest = await self.deliveries.find_one(query, sort=[('createdAt', 1)])
        age = max(0, (self.clock() - as_utc(oldest['createdAt'])).total_seconds()) if oldest else 0
        return {
            'pending': count,
            'oldestPendingAt': _iso(oldest.get('createdAt')) if oldest else None,
            'oldestWaitSeconds': int(age),
        }

    async def redrive(
        self,
        *,
        user_id: str,
        final_report_id: str,
        target_url: str | None,
        operator: str,
        reason: str,
        operation_id: str | None = None,
    ) -> dict:
        batch = await self.batches.find_one({'_id': final_report_id, 'userId': user_id})
        if batch is None:
            raise ValueError('report batch not found for user')
        target = target_url or batch.get('callbackUrl')
        if not target:
            raise ValueError('historical batches require --target-url')
        audit = {
            'id': uuid4().hex,
            'at': self.clock(),
            'operator': operator,
            'reason': reason,
            'action': 'retarget' if target_url else 'retry',
            'targetUrl': target,
        }
        if operation_id is not None:
            audit['operationId'] = operation_id
        # Persist the intent before touching individual deliveries. Re-running
        # the command or the reconciler repairs interruption halfway through.
        batch = await self.batches.find_one_and_update(
            {'_id': final_report_id},
            {
                '$set': {
                    'callbackUrl': target,
                    'deliveryEnabled': True,
                    'reconcileNeeded': True,
                    'redrivePending': audit,
                    'updatedAt': self.clock(),
                },
                '$push': {'deliveryOperations': audit},
                '$inc': {'deliveryRevision': 1},
            },
            return_document=ReturnDocument.AFTER,
        )
        await self.apply_redrive(batch)
        return {'finalReportId': final_report_id, 'status': 'scheduled'}

    async def migrate_pending(
        self, *, target_url: str, operator: str, reason: str, dry_run: bool = False, page_size: int = 100
    ) -> dict:
        """Move outstanding opted-in batches, without re-generating or replaying confirmed results.

        Traverse bounded pages and persist each batch's intent before updating
        deliveries. An interrupted run can be repeated; existing reconciliation
        repairs committed intents and later runs skip already migrated batches.
        """
        if page_size < 1:
            raise ValueError('page_size must be positive')
        started_at = self.clock()
        operation_id = f'callback_migration_{uuid4().hex}'
        target = urlsplit(target_url)
        result = {
            'operationId': operation_id,
            'status': 'preview' if dry_run else 'scheduled',
            'targetOrigin': f'{target.scheme}://{target.netloc}',
            'startedAt': _iso(started_at),
            'scannedBatches': 0,
            'matchedBatches': 0,
            'pendingDeliveries': 0,
            'migratedBatches': 0,
            'failedBatches': 0,
            'errors': [],
        }
        # Historical records without explicit delivery opt-in remain untouched.
        # Bound both admission time and the final key so ongoing generation and
        # new admissions cannot make this maintenance command loop forever.
        scope = {
            'deliveryEnabled': True,
            '$or': [
                {'createdAt': {'$lte': started_at}},
                {'createdAt': {'$exists': False}},
            ],
        }
        last = await self.batches.find_one(scope, {'_id': 1}, sort=[('_id', -1)])
        if last is None:
            return result
        after = None
        while True:
            bounds = {'$lte': last['_id']}
            if after is not None:
                bounds['$gt'] = after
            page = (
                await self.batches.find(
                    {**scope, '_id': bounds},
                    {
                        '_id': 1,
                        'userId': 1,
                        'callbackUrl': 1,
                        'durableVersion': 1,
                    },
                )
                .sort('_id', 1)
                .limit(page_size)
                .to_list(length=page_size)
            )
            if not page:
                break
            after = page[-1]['_id']
            for batch in page:
                result['scannedBatches'] += 1
                try:
                    deliveries = await self.deliveries.find(
                        {'finalReportId': batch['_id']},
                        {
                            'reportType': 1,
                            'status': 1,
                            'targetUrl': 1,
                        },
                    ).to_list(length=5)
                    pending = [row for row in deliveries if row.get('status') in {'pending', 'retry_wait', 'sending'}]
                    # Include accepted generations whose remaining types have
                    # not entered the outbox, including a save/enqueue crash.
                    missing_types = batch.get('durableVersion') == 1 and not set(EVALUATE_REPORT_TYPES).issubset(
                        {row['reportType'] for row in deliveries}
                    )
                    if not pending and not missing_types:
                        continue
                    if batch.get('callbackUrl') == target_url and all(
                        row.get('targetUrl') == target_url for row in pending
                    ):
                        continue
                    result['matchedBatches'] += 1
                    result['pendingDeliveries'] += len(pending)
                    if not dry_run:
                        await self.redrive(
                            user_id=batch['userId'],
                            final_report_id=batch['_id'],
                            target_url=target_url,
                            operator=operator,
                            reason=reason,
                            operation_id=operation_id,
                        )
                        result['migratedBatches'] += 1
                except Exception as error:
                    result['failedBatches'] += 1
                    # Bound output and omit request bodies, URLs and exception
                    # text, which may contain deployment credentials.
                    if len(result['errors']) < 20:
                        result['errors'].append({'finalReportId': batch['_id'], 'errorType': type(error).__name__})
        if result['failedBatches']:
            result['status'] = 'partial_failure'
        return result

    async def apply_redrive(self, batch: dict) -> None:
        intent = batch.get('redrivePending')
        if not intent:
            return
        artifacts = await self.artifacts.find({'finalReportId': batch['_id']}, {'reportType': 1}).to_list(length=5)
        for artifact in artifacts:
            await self.ensure_delivery(batch['_id'], artifact['reportType'])
        # A lease token is revoked as part of retargeting. An old in-flight
        # acknowledgement must not overwrite the newly scheduled destination.
        await self.deliveries.update_many(
            {
                'finalReportId': batch['_id'],
                'status': {'$ne': 'delivered'},
                '$or': [{'targetRevision': {'$lt': batch['deliveryRevision']}}, {'targetRevision': {'$exists': False}}],
            },
            {
                '$set': {
                    'targetUrl': intent['targetUrl'],
                    'status': 'pending',
                    'targetRevision': batch['deliveryRevision'],
                    'nextRetryAt': self.clock(),
                    'updatedAt': self.clock(),
                },
                '$unset': {'leaseToken': '', 'leaseOwner': '', 'leaseExpiresAt': ''},
            },
        )
        await self.batches.update_one(
            {'_id': batch['_id'], 'redrivePending': intent},
            {'$unset': {'redrivePending': ''}},
        )
