"""Independent, single-document Mongo jobs and callback outbox.

No existing report collection or report-type enumeration is touched. Completion
and callback eligibility are committed atomically in the same document.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from copy import deepcopy
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from adapters.gut_barrier.render_context import callback_render_context
from adapters.gut_barrier.render_context import render_context_headers
from modules.gut_barrier.policy import EXPLANATION_TYPE
from modules.gut_barrier.policy import is_explanation_callback
from modules.gut_barrier.publication import CALLBACK_TYPE
from modules.gut_barrier.publication import SUPPORTED_CALLBACK_TYPES
from modules.report.fallback_policy import DEFAULT_REPORT_FALLBACK_POLICY


def _content_hash(body):
    return hashlib.sha256(
        json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    ).hexdigest()


def _utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _main_report_filter():
    """Leave retired explanation jobs untouched, including older untagged receipts."""
    return {
        '$nor': [
            {'jobType': EXPLANATION_TYPE},
            {'body.type': EXPLANATION_TYPE},
            {
                'body.type': CALLBACK_TYPE,
                'body.schemaVersion': {'$exists': False},
                'body.report': {'$exists': False},
                '$or': [
                    {'body.diagnostic_summary': {'$exists': True}},
                    {'body.diagnostic_basis': {'$exists': True}},
                    {'body.diagnostic_advice': {'$exists': True}},
                ],
            },
        ]
    }


class MongoGutBarrierStore:
    def __init__(
        self,
        collection,
        *,
        clock=None,
        lease_seconds=120,
        reuse_seconds=DEFAULT_REPORT_FALLBACK_POLICY.completed_result_ttl_seconds,
        on_delivery_ready=None,
    ):
        self.collection = collection
        self.clock = clock or (lambda: datetime.now(UTC))
        self.lease_seconds = lease_seconds
        self.reuse_seconds = reuse_seconds
        self.on_delivery_ready = on_delivery_ready
        self._ready = False
        self._lock = asyncio.Lock()

    async def ensure_indexes(self):
        if self._ready:
            return
        async with self._lock:
            if not self._ready:
                await self.collection.create_index(
                    [('activeFingerprint', 1)],
                    name='gut_barrier_active_fingerprint',
                    unique=True,
                    partialFilterExpression={'activeFingerprint': {'$type': 'string'}},
                )
                for fields, name in (
                    ([('userId', 1), ('createdAt', -1), ('_id', -1)], 'gut_barrier_owner'),
                    ([('status', 1), ('leaseUntil', 1)], 'gut_barrier_generation'),
                    ([('deliveryStatus', 1), ('nextDeliveryAt', 1)], 'gut_barrier_delivery'),
                    ([('delegationStatus', 1), ('nextDelegationAt', 1)], 'gut_barrier_delegation'),
                ):
                    await self.collection.create_index(fields, name=name)
                self._ready = True

    async def admit(
        self, *, fingerprint, request, callback_url, version, delegation_policy=None, execution_manifest=None
    ):
        request, delegation_policy, execution_manifest = deepcopy((request, delegation_policy, execution_manifest))
        await self.ensure_indexes()
        for _ in range(5):
            now = self.clock()
            await self.collection.update_one(
                {
                    'activeFingerprint': fingerprint,
                    'reuseUntil': {'$lte': now},
                    'status': {'$in': ['success', 'partial', 'failed']},
                },
                {'$unset': {'activeFingerprint': ''}},
            )
            existing = await self.collection.find_one({'activeFingerprint': fingerprint})
            if existing is not None:
                if existing.get('request') != request or existing.get('version') != version:
                    raise ValueError('gut-barrier idempotency conflict')
                replay = existing.get('status') in {'success', 'partial', 'failed'} and bool(
                    existing.get('callbackUrl')
                )
                change = {'$set': {'lastRequestedAt': now}}
                if replay:
                    change['$inc'] = {'deliveryRequestRevision': 1}
                    change['$set']['deliveryReplayPending'] = True
                acknowledgement = await self.collection.update_one(
                    {'_id': existing['_id'], 'activeFingerprint': fingerprint}, change
                )
                if not acknowledgement.acknowledged:
                    raise RuntimeError('gut-barrier admission was not acknowledged')
                if acknowledgement.matched_count:
                    if replay:
                        # Never steal an in-flight delivery lease. Its ACK will
                        # observe the revision change and enqueue the replay.
                        await self.collection.update_one(
                            {'_id': existing['_id'], 'deliveryStatus': {'$in': ['delivered', 'pending', 'retry_wait']}},
                            {'$set': {'deliveryStatus': 'pending', 'nextDeliveryAt': now, 'updatedAt': now}},
                        )
                        self._wake_delivery()
                    return existing
                continue
            document = {
                '_id': 'gbr_' + uuid4().hex,
                'fingerprint': fingerprint,
                'activeFingerprint': fingerprint,
                'userId': request['userId'],
                'sessionId': request['sessionId'],
                'request': request,
                'version': version,
                'status': 'queued',
                'createdAt': now,
                'updatedAt': now,
                'lastRequestedAt': now,
                'generationAttempts': 0,
                'callbackUrl': callback_url,
                'deliveryStatus': 'waiting_generation' if callback_url else 'not_configured',
                'deliveryAttempts': 0,
                'deliveryRequestRevision': 0,
                'delegationPolicy': delegation_policy,
                **({'executionManifest': execution_manifest} if execution_manifest is not None else {}),
            }
            try:
                acknowledgement = await self.collection.insert_one(document)
                if not acknowledgement.acknowledged:
                    raise RuntimeError('gut-barrier admission was not acknowledged')
                return document
            except DuplicateKeyError:
                continue
        raise RuntimeError('gut-barrier admission contention')

    async def claim_generation(self, *, expired=False, version=None):
        await self.ensure_indexes()
        now = self.clock()
        query = (
            {'status': {'$in': ['generating', 'recovering']}, 'leaseUntil': {'$lte': now}}
            if expired
            else {'status': 'queued'}
        )
        if version is not None:
            query['version'] = version
        query.update(_main_report_filter())
        return await self.collection.find_one_and_update(
            query,
            {
                '$set': {
                    'status': 'recovering' if expired else 'generating',
                    'leaseToken': uuid4().hex,
                    'leaseUntil': now + timedelta(seconds=self.lease_seconds),
                    'updatedAt': now,
                },
                '$inc': {'generationAttempts': 1},
            },
            sort=[('createdAt', 1)],
            return_document=ReturnDocument.AFTER,
        )

    def _generation_lease(self, job):
        return {
            **_main_report_filter(),
            '_id': job['_id'],
            'status': {'$in': ['generating', 'recovering']},
            'leaseToken': job['leaseToken'],
            'leaseUntil': {'$gt': self.clock()},
        }

    async def renew_generation(self, job):
        result = await self.collection.update_one(
            self._generation_lease(job),
            {'$set': {'leaseUntil': self.clock() + timedelta(seconds=self.lease_seconds)}},
        )
        return result.matched_count == 1

    async def mark_generation_started(self, job):
        query = self._generation_lease(job)
        query.update(status='generating', generationStartedAt={'$exists': False})
        result = await self.collection.update_one(query, {'$set': {'generationStartedAt': self.clock()}})
        return result.matched_count == 1

    async def complete(self, job, *, body, audit, delegation_policy=None):
        if is_explanation_callback(body):
            return False
        now = self.clock()
        version = audit.get('displayTemplateVersion')
        render_context = (
            callback_render_context(body, version)
            if version is not None and body.get('status') in {'success', 'partial'}
            else None
        )
        state = 'partial' if body['status'] == 'partial_success' else body['status']
        delegation = {}
        policy = job.get('delegationPolicy') or delegation_policy
        if policy is not None:
            from modules.gut_barrier.delegation import DelegationPolicy

            policy = DelegationPolicy(**policy).snapshot()
            delegation = {
                'delegationPolicy': policy,
                'delegationStatus': 'pending',
                'delegationAttempts': 0,
                'delegationOutcome': 'not_submitted',
                'nextDelegationAt': now,
                'delegationDeadlineAt': now + timedelta(seconds=policy['deadline_seconds']),
                'delegationCompatibility': None if job.get('delegationPolicy') else 'legacy-unfinished-main.v1',
            }
        result = await self.collection.update_one(
            self._generation_lease(job),
            {
                '$set': {
                    'status': state,
                    'body': body,
                    'audit': audit,
                    'contentHash': _content_hash(body),
                    'renderContext': render_context,
                    'updatedAt': now,
                    'completedAt': now,
                    'reuseUntil': now + timedelta(seconds=self.reuse_seconds),
                    'deliveryStatus': 'pending' if job.get('callbackUrl') else 'not_configured',
                    'nextDeliveryAt': now,
                    **delegation,
                },
                '$unset': {'leaseToken': '', 'leaseUntil': '', 'html': ''},
            },
        )
        if result.matched_count and job.get('callbackUrl'):
            self._wake_delivery()
        return result.matched_count == 1

    def _wake_delivery(self):
        if self.on_delivery_ready is not None:
            try:
                self.on_delivery_ready()
            except Exception:
                # Polling/startup recovery still owns the durable outbox.
                pass

    async def release_generation(self, job, *, known_not_started=False):
        """Release only a known unstarted claim, never retry clinical work.

        The caller must know no generator was scheduled; the persisted start
        marker is a second guard against accidentally releasing started work.
        A rejected/uncertain release remains owned by lease recovery.
        """
        if known_not_started is not True:
            return False
        query = self._generation_lease(job)
        query.update(status='generating', generationStartedAt={'$exists': False})
        result = await self.collection.update_one(
            query,
            {'$set': {'status': 'queued', 'updatedAt': self.clock()}, '$unset': {'leaseToken': '', 'leaseUntil': ''}},
        )
        return result.matched_count == 1

    async def generation_queue_snapshot(self):
        """Read-only aggregate for release gates; never read patient bodies.

        Deliberately does not call ensure_indexes: an operations check must
        work with a read-only DB role and must not initialize/write anything.
        """
        cursor = await self.collection.aggregate(
            [
                {
                    '$match': {
                        '$or': [
                            {'status': {'$in': ['queued', 'generating', 'recovering']}},
                            {'status': {'$in': ['success', 'partial']}, 'explanationStatus': 'pending'},
                        ]
                    }
                },
                {
                    '$group': {
                        '_id': {
                            'version': '$version',
                            'status': '$status',
                            'jobType': '$jobType',
                            'explanationVersion': '$explanationVersion',
                            'explanationStatus': '$explanationStatus',
                        },
                        'count': {'$sum': 1},
                        'oldestCreatedAt': {'$min': '$createdAt'},
                    }
                },
                {'$sort': {'_id.version': 1, '_id.status': 1, '_id.jobType': 1, '_id.explanationVersion': 1}},
                {'$limit': 1001},
            ],
            maxTimeMS=10000,
        )
        rows = await cursor.to_list(length=1001)
        if len(rows) > 1000:
            raise ValueError('too many generation version/state groups for a bounded queue check')
        result = []
        for row in rows:
            identity = row['_id']
            item = {
                'version': identity.get('version'),
                'status': identity['status'],
                'count': row['count'],
                'oldestCreatedAt': _utc(row['oldestCreatedAt']).isoformat() if row.get('oldestCreatedAt') else None,
            }
            if identity.get('jobType') is not None:
                item['jobType'] = identity['jobType']
            if identity.get('jobType') == EXPLANATION_TYPE or identity.get('explanationStatus') == 'pending':
                # Keep retired explanation records visible to read-only audits.
                item['explanationVersion'] = identity.get('explanationVersion')
            if identity.get('explanationStatus') == 'pending':
                item['explanationStatus'] = 'pending'
            result.append(item)
        return result

    async def claim_delivery(self, owner=None):
        await self.ensure_indexes()
        now = self.clock()
        job = await self.collection.find_one_and_update(
            {
                **_main_report_filter(),
                '$or': [
                    {'deliveryStatus': {'$in': ['pending', 'retry_wait']}, 'nextDeliveryAt': {'$lte': now}},
                    {'deliveryStatus': 'sending', 'deliveryLeaseUntil': {'$lte': now}},
                    # The intent is atomic with admission. It remains discoverable
                    # if the process exits before the immediate pending transition.
                    {'deliveryReplayPending': True, 'deliveryStatus': {'$in': ['delivered', 'pending', 'retry_wait']}},
                ],
            },
            {
                '$set': {
                    'deliveryStatus': 'sending',
                    'deliveryToken': uuid4().hex,
                    'deliveryOwner': owner,
                    'updatedAt': now,
                    'deliveryLeaseUntil': now + timedelta(seconds=self.lease_seconds),
                },
                '$inc': {'deliveryAttempts': 1},
                '$unset': {'deliveryReplayPending': ''},
            },
            sort=[('nextDeliveryAt', 1)],
            return_document=ReturnDocument.AFTER,
        )
        if job is None:
            return None
        body = job.get('body')
        if (
            job.get('contentHash') is None
            and isinstance(body, dict)
            and body.get('type') in SUPPORTED_CALLBACK_TYPES
            and body.get('finalReportId') == job['_id']
            and body.get('userId') == job['userId']
            and body.get('sessionId') == job['sessionId']
            and body.get('status') in {'success', 'partial', 'failed'}
        ):
            # Compatibility with already persisted jobs from the first new-API
            # implementation. Only hash the frozen body, never rewrite it.
            content_hash = _content_hash(job['body'])
            patched = await self.collection.update_one(
                {'_id': job['_id'], 'deliveryToken': job['deliveryToken'], 'contentHash': None},
                {'$set': {'contentHash': content_hash}},
            )
            if patched.matched_count:
                job['contentHash'] = content_hash
        # Adapt the independent single-document outbox to the existing worker;
        # request/audit/HTML are never included in the delivery view or body.
        delivery_version = 'v2' if isinstance(body, dict) and body.get('schemaVersion') == '2.0' else 'v1'
        # Frozen historical callbacks keep their original type and dedup key.
        delivery_type = body.get('type', CALLBACK_TYPE) if isinstance(body, dict) else CALLBACK_TYPE
        report_id = job['_id']
        return {
            '_id': report_id + ':' + delivery_type + ':' + delivery_version,
            'jobId': job['_id'],
            'finalReportId': report_id,
            'userId': job['userId'],
            'sessionId': job.get('sessionId'),
            'reportType': delivery_type,
            'targetUrl': job['callbackUrl'],
            'attempts': job['deliveryAttempts'],
            'deliveryToken': job['deliveryToken'],
            'deliveryRequestRevision': job.get('deliveryRequestRevision', 0),
            'contentHash': job.get('contentHash'),
        }

    def _delivery_lease(self, job):
        return {
            **_main_report_filter(),
            '_id': job['jobId'],
            'deliveryStatus': 'sending',
            'deliveryToken': job['deliveryToken'],
            'deliveryLeaseUntil': {'$gt': self.clock()},
        }

    async def delivery_body(self, job):
        query = self._delivery_lease(job)
        query.update(userId=job['userId'], contentHash=job['contentHash'])
        stored = await self.collection.find_one(query)
        if (
            not stored
            or not isinstance(stored.get('body'), dict)
            or _content_hash(stored['body']) != stored.get('contentHash')
        ):
            raise ValueError('immutable gut-barrier artifact is unavailable')
        return stored['body']

    async def delivery_render_context(self, body, *, idempotency_key):
        """Read the binding for this exact receipt without rewriting old jobs."""
        delivery_version = 'v2' if body.get('schemaVersion') == '2.0' else 'v1'
        report_id = body.get('finalReportId')
        if (
            is_explanation_callback(body)
            or body.get('type') not in SUPPORTED_CALLBACK_TYPES
            or not isinstance(report_id, str)
            or idempotency_key != f'{report_id}:{body["type"]}:{delivery_version}'
        ):
            raise ValueError('callback identity does not match render binding')
        stored = await self.collection.find_one(
            {
                '_id': report_id,
                'userId': body.get('userId'),
                'sessionId': body.get('sessionId'),
                'contentHash': _content_hash(body),
            },
            {'renderContext': 1, 'audit.displayTemplateVersion': 1, '_id': 0},
        )
        if stored is None:
            raise ValueError('immutable callback render binding is unavailable')
        context = stored.get('renderContext')
        if 'renderContext' not in stored and body.get('status') in {'success', 'partial'}:
            # Pre-upgrade receipts may already have a pinned audit version.
            # Read it, but never patch their frozen JSON, hash or idempotency key.
            version = (stored.get('audit') or {}).get('displayTemplateVersion')
            if version is not None:
                context = callback_render_context(body, version)
        render_context_headers(body, context)  # Validate before delivery.
        return context

    async def acknowledge_delivery(self, job):
        lease = self._delivery_lease(job)
        revision = job.get('deliveryRequestRevision', 0)
        same_request = dict(lease)
        same_request['$or'] = [{'deliveryRequestRevision': revision}]
        if revision == 0:
            same_request['$or'].append({'deliveryRequestRevision': {'$exists': False}})
        result = await self.collection.update_one(
            same_request,
            {
                '$set': {'deliveryStatus': 'delivered', 'deliveredAt': self.clock(), 'updatedAt': self.clock()},
                '$unset': {
                    'deliveryToken': '',
                    'deliveryOwner': '',
                    'deliveryLeaseUntil': '',
                    'nextDeliveryAt': '',
                    'deliveryError': '',
                },
            },
        )
        if result.matched_count:
            return True
        # A new submission arrived while the old frozen body was in flight.
        # Its durable revision survives this ACK and requests the same body/key.
        changed_request = dict(lease)
        changed_request['deliveryRequestRevision'] = {'$ne': revision}
        result = await self.collection.update_one(
            changed_request,
            {
                '$set': {
                    'deliveryStatus': 'pending',
                    'nextDeliveryAt': self.clock(),
                    'deliveredAt': self.clock(),
                    'updatedAt': self.clock(),
                },
                '$unset': {'deliveryToken': '', 'deliveryOwner': '', 'deliveryLeaseUntil': '', 'deliveryError': ''},
            },
        )
        if result.matched_count:
            self._wake_delivery()
        return bool(result.matched_count)

    async def retry_delivery(self, job, *, next_retry_at, error):
        lease = self._delivery_lease(job)
        revision = job.get('deliveryRequestRevision', 0)
        same_request = dict(lease)
        same_request['$or'] = [{'deliveryRequestRevision': revision}]
        if revision == 0:
            same_request['$or'].append({'deliveryRequestRevision': {'$exists': False}})
        result = await self.collection.update_one(
            same_request,
            {
                '$set': {
                    'deliveryStatus': 'retry_wait',
                    'nextDeliveryAt': next_retry_at,
                    'deliveryError': error,
                    'updatedAt': self.clock(),
                },
                '$unset': {'deliveryToken': '', 'deliveryOwner': '', 'deliveryLeaseUntil': ''},
            },
        )
        if result.matched_count:
            return True
        changed_request = dict(lease)
        changed_request['deliveryRequestRevision'] = {'$ne': revision}
        result = await self.collection.update_one(
            changed_request,
            {
                '$set': {
                    'deliveryStatus': 'pending',
                    'nextDeliveryAt': self.clock(),
                    'deliveryError': error,
                    'updatedAt': self.clock(),
                },
                '$unset': {'deliveryToken': '', 'deliveryOwner': '', 'deliveryLeaseUntil': ''},
            },
        )
        if result.matched_count:
            self._wake_delivery()
        return bool(result.matched_count)

    async def backlog(self):
        query = {
            **_main_report_filter(),
            '$or': [
                {'deliveryStatus': {'$in': ['pending', 'retry_wait', 'sending']}},
                {'deliveryReplayPending': True, 'deliveryStatus': 'delivered'},
            ],
        }
        count = await self.collection.count_documents(query)
        oldest = await self.collection.find_one(query, sort=[('completedAt', 1)])
        since = _utc(oldest.get('completedAt', oldest['createdAt'])) if oldest else None
        return {
            'pending': count,
            'oldestPendingAt': since.isoformat() if since else None,
            'oldestWaitSeconds': int(max(0, (_utc(self.clock()) - since).total_seconds())) if since else 0,
        }
