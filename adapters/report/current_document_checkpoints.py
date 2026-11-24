"""Report batches own durable attachment bindings and source submission retries."""

import asyncio
from datetime import timedelta
from uuid import uuid4

from pymongo import ReturnDocument

from adapters.report.durable_store import as_utc
from adapters.report.durable_store import utcnow
from modules.analysis.snapshots import digest
from modules.report.durable_ports import ACTIVE_GENERATION
from modules.report.ports import DocumentServiceError

MAX_DOCUMENT_ATTACHMENTS = 11
MAX_DOCUMENT_SUBMISSIONS = 2 * MAX_DOCUMENT_ATTACHMENTS
RETRY_DELAYS_SECONDS = (30, 120, 600, 3600)
MAX_SUBMISSION_ATTEMPTS = 1 + len(RETRY_DELAYS_SECONDS)
_MAX_CAS_ATTEMPTS = 16


class MongoReportDocumentCheckpoints:
    def __init__(self, batches, *, clock=utcnow, lease_seconds=120):
        self.batches = batches
        self.clock = clock
        self.lease_seconds = lease_seconds
        self._index_ready = False
        self._index_lock = asyncio.Lock()

    @staticmethod
    def _scope(batch_id, user_id, session_id):
        return {'_id': batch_id, 'userId': user_id, 'sessionId': session_id, 'durableVersion': 1}

    async def _batch(self, batch):
        row = await self.batches.find_one(self._scope(*batch))
        if row is None:
            raise DocumentServiceError('the admitted Report batch is unavailable')
        return row

    def _generation_filter(self, row):
        lease = ACTIVE_GENERATION.get()
        if lease is None or lease != (row['_id'], row.get('leaseToken')):
            raise DocumentServiceError('Report document work requires its original generation lease')
        return {
            '_id': row['_id'],
            'leaseToken': lease[1],
            'leaseExpiresAt': {'$gt': self.clock()},
            'generationState': 'running',
        }

    @staticmethod
    def _check_manifest(row, manifest):
        admitted = (row.get('executionManifest') or {}).get('documentConsumer')
        bound = row.get('documentBindingManifestKey')
        if (admitted is not None and admitted != manifest) or (bound is not None and bound != digest(manifest)):
            raise DocumentServiceError('the admitted Report document implementation differs')

    async def require_generation(self, batch, manifest):
        row = await self._batch(batch)
        self._check_manifest(row, manifest)
        if await self.batches.find_one(self._generation_filter(row)) is None:
            raise DocumentServiceError('Report document work requires its original generation lease')

    async def read_binding(self, batch, key, manifest):
        row = await self._batch(batch)
        self._check_manifest(row, manifest)
        return row.get('documentBindings', {}).get(key)

    async def put_binding_once(self, batch, key, value, manifest):
        for _ in range(_MAX_CAS_ATTEMPTS):
            row = await self._batch(batch)
            self._check_manifest(row, manifest)
            saved = row.get('documentBindings', {})
            if key in saved:
                return saved[key]
            if len(saved) >= 1 + MAX_DOCUMENT_ATTACHMENTS:
                raise DocumentServiceError('Report attachment checkpoint limit exceeded')
            result = await self.batches.update_one(
                {**self._generation_filter(row), **self._revision(row, 'documentBindingRevision')},
                {
                    '$set': {
                        'documentBindings': {**saved, key: value},
                        'documentBindingManifestKey': digest(manifest),
                    },
                    '$inc': {'documentBindingRevision': 1},
                },
            )
            if not result.acknowledged:
                raise ConnectionError('Report attachment binding write is unacknowledged')
            if result.matched_count:
                return value
        raise DocumentServiceError('Report attachment binding changed repeatedly or generation lease expired')

    @staticmethod
    def _revision(row, field):
        return {field: row[field] if field in row else {'$exists': False}}

    @staticmethod
    def _remaining(submissions):
        return min(
            (item['nextAttemptAt'] for item in submissions.values() if 'nextAttemptAt' in item),
            key=as_utc,
            default=None,
        )

    async def queue_once(self, batch, command_id, command, manifest):
        for _ in range(_MAX_CAS_ATTEMPTS):
            row = await self._batch(batch)
            self._check_manifest(row, manifest)
            submissions = row.get('documentSubmissions', {})
            if command_id in submissions:
                saved = submissions[command_id]
                if saved['command'] != command:
                    raise DocumentServiceError('the accepted Report contribution changed')
                return saved.get('outcome')
            if len(submissions) >= MAX_DOCUMENT_SUBMISSIONS:
                raise DocumentServiceError('Report contribution limit exceeded')
            due = self.clock()
            submissions = {**submissions, command_id: {'command': command, 'nextAttemptAt': due, 'attempts': 0}}
            result = await self.batches.update_one(
                {**self._generation_filter(row), **self._revision(row, 'documentSubmissionRevision')},
                {
                    '$set': {
                        'documentSubmissions': submissions,
                        'documentSubmissionManifestKey': digest(manifest),
                        'documentSubmissionDueAt': self._remaining(submissions),
                    },
                    '$inc': {'documentSubmissionRevision': 1},
                },
            )
            if not result.acknowledged:
                raise ConnectionError('Report contribution checkpoint write is unacknowledged')
            if result.matched_count:
                return None
        raise DocumentServiceError('Report contribution changed repeatedly or generation lease expired')

    async def claim_submission(self, *, manifest, batch=None):
        if not self._index_ready:
            async with self._index_lock:
                if not self._index_ready:
                    await self.batches.create_index(
                        [
                            ('documentSubmissionManifestKey', 1),
                            ('documentSubmissionDueAt', 1),
                            ('documentSubmissionLeaseUntil', 1),
                        ],
                        name='report_document_submissions_due',
                    )
                    self._index_ready = True
        now = self.clock()
        query = {
            'durableVersion': 1,
            'documentSubmissionManifestKey': digest(manifest),
            'documentSubmissionDueAt': {'$lte': now},
            '$or': [
                {'documentSubmissionLeaseUntil': {'$exists': False}},
                {'documentSubmissionLeaseUntil': {'$lte': now}},
            ],
        }
        if batch is not None:
            query.update(self._scope(*batch))
        # A crashed process leaves its exact pending command reclaimable.
        row = await self.batches.find_one_and_update(
            query,
            {
                '$set': {
                    'documentSubmissionLeaseToken': uuid4().hex,
                    'documentSubmissionLeaseUntil': now + timedelta(seconds=self.lease_seconds),
                }
            },
            sort=[('documentSubmissionDueAt', 1), ('_id', 1)],
            return_document=ReturnDocument.AFTER,
        )
        if row is None:
            return None
        key, item = min(
            ((key, item) for key, item in row['documentSubmissions'].items() if 'nextAttemptAt' in item),
            key=lambda pair: (as_utc(pair[1]['nextAttemptAt']), pair[0]),
        )
        return row, key, item

    def _submission_filter(self, row, claimed_row):
        return {
            '_id': row['_id'],
            'documentSubmissionLeaseToken': claimed_row['documentSubmissionLeaseToken'],
            'documentSubmissionLeaseUntil': {'$gt': self.clock()},
            **self._revision(row, 'documentSubmissionRevision'),
        }

    async def start_submission(self, claim):
        claimed_row, key, item = claim
        batch = (claimed_row['_id'], claimed_row['userId'], claimed_row['sessionId'])
        for _ in range(_MAX_CAS_ATTEMPTS):
            row = await self._batch(batch)
            submissions = row['documentSubmissions']
            if submissions[key] != item:
                return None
            started = {**item, 'attempts': item['attempts'] + 1}
            result = await self.batches.update_one(
                self._submission_filter(row, claimed_row),
                {
                    '$set': {'documentSubmissions': {**submissions, key: started}},
                    '$inc': {'documentSubmissionRevision': 1},
                },
            )
            if not result.acknowledged:
                raise ConnectionError('Report contribution attempt write is unacknowledged')
            if result.matched_count:
                return claimed_row, key, started
        return None

    async def finish_submission(self, claim, outcome):
        claimed_row, key, item = claim
        batch = (claimed_row['_id'], claimed_row['userId'], claimed_row['sessionId'])
        for _ in range(_MAX_CAS_ATTEMPTS):
            row = await self._batch(batch)
            submissions = row['documentSubmissions']
            if submissions[key] != item:
                return False
            attempts = item['attempts']
            saved = {'command': item['command'], 'attempts': attempts}
            if outcome is None and 0 < attempts < MAX_SUBMISSION_ATTEMPTS:
                saved['nextAttemptAt'] = self.clock() + timedelta(seconds=RETRY_DELAYS_SECONDS[attempts - 1])
            else:
                saved['outcome'] = outcome or {'ingestionStatus': 'unknown', 'ingestionErrorType': 'RetryExhausted'}
            submissions = {**submissions, key: saved}
            next_due = self._remaining(submissions)
            fields = {'documentSubmissions': submissions}
            if next_due is not None:
                fields['documentSubmissionDueAt'] = next_due
            unset = {'documentSubmissionLeaseUntil': '', 'documentSubmissionLeaseToken': ''}
            if next_due is None:
                unset['documentSubmissionDueAt'] = ''
            result = await self.batches.update_one(
                self._submission_filter(row, claimed_row),
                {'$set': fields, '$unset': unset, '$inc': {'documentSubmissionRevision': 1}},
            )
            if not result.acknowledged:
                raise ConnectionError('Report contribution result write is unacknowledged')
            if result.matched_count:
                return True
        return False
