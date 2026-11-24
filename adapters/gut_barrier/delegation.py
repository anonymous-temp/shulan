"""Adapters for the public Report use case and GutBarrier's own intent store."""

from __future__ import annotations

from datetime import UTC
from datetime import timedelta
from uuid import uuid4

from pymongo import ReturnDocument

from modules.report.delegation import GutBarrierFollowupRequest


class PublicReportDelegationAdapter:
    def __init__(self, service):
        self.service = service

    @staticmethod
    def request(body):
        return GutBarrierFollowupRequest(
            user_id=body['userId'],
            session_id=body['sessionId'],
            source_final_report_id=body['finalReportId'],
            body=body,
        )

    async def receipt(self, body):
        request = self.request(body)
        if self.service is None:
            raise RuntimeError('Report follow-up service is unavailable')
        try:
            receipt = await self.service.get_receipt(
                task_id=request.task_id,
                user_id=request.user_id,
                session_id=request.session_id,
            )
        except KeyError:
            return None
        return receipt.task_id

    async def submit(self, body):
        return (await self.service.submit(self.request(body))).task_id


class MongoGutBarrierDelegationStore:
    def __init__(self, main_store):
        self.collection, self.clock = main_store.collection, main_store.clock
        self.lease_seconds = main_store.lease_seconds

    def lease(self, job):
        return {
            '_id': job['_id'],
            'delegationStatus': 'submitting',
            'delegationToken': job['delegationToken'],
            'delegationLeaseUntil': {'$gt': self.clock()},
        }

    async def claim(self):
        now = self.clock()
        job = await self.collection.find_one_and_update(
            {
                '$or': [
                    {'delegationStatus': {'$in': ['pending', 'retry_wait']}, 'nextDelegationAt': {'$lte': now}},
                    {'delegationStatus': 'submitting', 'delegationLeaseUntil': {'$lte': now}},
                ]
            },
            {
                '$set': {
                    'delegationStatus': 'submitting',
                    'delegationToken': uuid4().hex,
                    'delegationLeaseUntil': now + timedelta(seconds=self.lease_seconds),
                }
            },
            sort=[('nextDelegationAt', 1)],
            return_document=ReturnDocument.AFTER,
        )

        if job is not None:
            # Mongo's default codec returns naive UTC; deadlines remain absolute.
            for name in ('delegationDeadlineAt', 'delegationLeaseUntil', 'nextDelegationAt'):
                value = job.get(name)
                if value is not None and value.tzinfo is None:
                    job[name] = value.replace(tzinfo=UTC)
        return job

    async def begin_submission(self, job, *, policy):
        query = self.lease(job)
        query.update(delegationAttempts={'$lt': policy.max_attempts}, delegationDeadlineAt={'$gt': self.clock()})
        result = await self.collection.update_one(
            query,
            {
                '$set': {'delegationOutcome': 'unknown'},
                '$inc': {'delegationAttempts': 1},
            },
        )
        if result.matched_count:
            job['delegationAttempts'] += 1
        return bool(result.matched_count)

    async def finish(self, job, *, status, task_id=None, error_code=None):
        result = await self.collection.update_one(
            self.lease(job),
            {
                '$set': {
                    'delegationStatus': status,
                    'delegatedTaskId': task_id,
                    'delegationErrorCode': error_code,
                    'delegationUpdatedAt': self.clock(),
                },
                '$unset': {'delegationToken': '', 'delegationLeaseUntil': ''},
            },
        )
        return bool(result.matched_count)

    async def retry(self, job):
        now = self.clock()
        expired = job['delegationDeadlineAt'] <= now
        return await self.collection.update_one(
            self.lease(job),
            {
                '$set': {
                    'delegationStatus': ('needs_reconciliation' if job['delegationAttempts'] else 'budget_exhausted')
                    if expired
                    else 'retry_wait',
                    'delegationErrorCode': 'DELEGATION_UNAVAILABLE',
                    'nextDelegationAt': now + timedelta(seconds=min(60, 2 ** min(job['delegationAttempts'], 6))),
                    'delegationUpdatedAt': now,
                },
                '$unset': {'delegationToken': '', 'delegationLeaseUntil': ''},
            },
        )
