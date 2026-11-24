"""Report-owned permanent delegation identities and fenced frozen artifacts."""

from __future__ import annotations

from copy import deepcopy

from pymongo.errors import DuplicateKeyError

from adapters.report.durable_store import MongoReportWorkflowStore
from modules.report.artifacts import CONFLICT
from modules.report.artifacts import IDEMPOTENT
from modules.report.artifacts import SAVED
from modules.report.delegation import GUT_BARRIER_FOLLOWUP_TYPES
from modules.report.delegation import FollowupConflictError


class MongoReportFollowupStore(MongoReportWorkflowStore):
    """One canonical job document fences artifact commits; outbox rows are projections.

    Use report_followup_jobs/artifacts/deliveries, never public report collections.
    Delegation identity does not expire with the ordinary report reuse window.
    """

    def __init__(self, **kwargs):
        super().__init__(expected_report_types=GUT_BARRIER_FOLLOWUP_TYPES, **kwargs)

    async def admit_delegation(
        self,
        request,
        callback_url: str,
        execution_version: str,
        generation_timeout_seconds: float,
        *,
        execution_manifest=None,
    ) -> dict:
        execution_manifest = deepcopy(execution_manifest)
        await self.ensure_indexes()
        now = self.clock()
        document = {
            '_id': request.task_id,
            'finalReportId': request.task_id,
            'sourceFinalReportId': request.source_final_report_id,
            'userId': request.user_id,
            'sessionId': request.session_id,
            'request': deepcopy(request.body),
            'inputDigest': request.input_digest,
            'contractVersion': request.contract_version,
            'executionVersion': execution_version,
            'executionManifest': execution_manifest,
            'generationTimeoutSeconds': generation_timeout_seconds,
            'callbackUrl': callback_url,
            'deliveryEnabled': True,
            'deliveryRevision': 0,
            'durableVersion': 1,
            'schemaVersion': 1,
            'generationState': 'queued',
            'generationStatus': 'queued',
            'reconcileNeeded': True,
            'documents': [],
            'artifactRevision': 0,
            'frozenArtifacts': {},
            'createdAt': now,
            'updatedAt': now,
        }
        try:
            result = await self.batches.insert_one(document)
            if not result.acknowledged:
                raise RuntimeError('follow-up admission was not acknowledged')
            return document
        except DuplicateKeyError:
            existing = await self.get_owned(request.task_id, user_id=request.user_id, session_id=request.session_id)
            if existing['inputDigest'] != request.input_digest:
                raise FollowupConflictError('delegation input conflicts with its durable receipt') from None
            confirmed = await self.batches.update_one(
                {'_id': request.task_id, 'inputDigest': request.input_digest},
                {'$set': {'lastRequestedAt': now}},
            )
            if not confirmed.acknowledged or not confirmed.matched_count:
                raise RuntimeError('follow-up receipt could not be durably reconfirmed')
            return existing

    async def get_owned(self, task_id: str, *, user_id: str, session_id: str) -> dict:
        job = await self.batches.find_one({'_id': task_id, 'userId': user_id, 'sessionId': session_id})
        if job is None:
            raise KeyError('follow-up task not found')
        return job

    async def save_frozen_artifact(self, job: dict, record) -> str:
        if record.report_type not in GUT_BARRIER_FOLLOWUP_TYPES:
            return CONFLICT
        for _ in range(8):
            current = await self.get_owned(job['_id'], user_id=job['userId'], session_id=job['sessionId'])
            frozen = current.get('frozenArtifacts', {})
            prior = frozen.get(record.report_type)
            if prior is not None:
                return IDEMPOTENT if prior['contentHash'] == record.content_hash else CONFLICT
            revision = current.get('artifactRevision', 0)
            artifact = {
                '_id': f'{job["_id"]}:{record.report_type}',
                'finalReportId': job['_id'],
                'userId': record.user_id,
                'sessionId': record.session_id,
                'reportType': record.report_type,
                'generationStatus': record.generation_status,
                'reviewStatus': record.review_status,
                'contentHash': record.content_hash,
                'savedAt': self.clock(),
                'schemaVersion': record.schema_version,
                'body': deepcopy(record.body),
                'documents': list(record.documents),
            }
            query = {**self._generation_lease(job), 'artifactRevision': revision}
            saved = await self.batches.update_one(
                query,
                {
                    '$set': {
                        'frozenArtifacts': {**frozen, record.report_type: artifact},
                        'artifactRevision': revision + 1,
                        'reconcileNeeded': True,
                        'updatedAt': self.clock(),
                    }
                },
            )
            if saved.acknowledged and saved.matched_count:
                return SAVED
            if current.get('leaseToken') != job.get('leaseToken') or current.get('generationState') == 'completed':
                return CONFLICT
        return CONFLICT

    async def artifacts_for(self, final_report_id: str) -> list[dict]:
        job = await self.batches.find_one({'_id': final_report_id})
        return [] if job is None else list(job.get('frozenArtifacts', {}).values())

    async def ensure_delivery(self, final_report_id: str, report_type: str) -> None:
        job = await self.batches.find_one({'_id': final_report_id})
        artifact = (job or {}).get('frozenArtifacts', {}).get(report_type)
        if artifact is None:
            return
        try:
            inserted = await self.artifacts.insert_one(deepcopy(artifact))
            if not inserted.acknowledged:
                raise RuntimeError('follow-up artifact projection was not acknowledged')
        except DuplicateKeyError:
            existing = await self.artifacts.find_one({'_id': artifact['_id']})
            if not existing or existing['contentHash'] != artifact['contentHash']:
                raise FollowupConflictError('frozen follow-up artifact projection conflicts') from None
        await super().ensure_delivery(final_report_id, report_type)
