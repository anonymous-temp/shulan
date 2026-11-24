"""Durable file identities and MongoDB leases shared by every application process."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import timedelta
from datetime import timezone

from pymongo import ASCENDING
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from modules.analysis.application.documents import DocumentBusyError
from modules.health_profile.application import HealthReportSourceConflictError


class MongoDocumentIndexStore:
    def __init__(self, collection, *, identity_key='contentSha256'):
        self._collection = collection
        self.identity_keys = ('userId', 'subjectId', identity_key)
        self._ready = False
        self._lock = asyncio.Lock()

    async def ensure_indexes(self):
        if self._ready:
            return
        async with self._lock:
            if not self._ready:
                await self._collection.create_index(
                    [(key, ASCENDING) for key in self.identity_keys],
                    unique=True,
                    name='document_content_owner_unique',
                )
                await self._collection.create_index(
                    [('status', ASCENDING), ('leaseExpiresAt', ASCENDING)],
                    name='document_execution_recovery',
                )
                await self._collection.create_index(
                    [('userId', ASCENDING), ('subjectId', ASCENDING), ('_id', ASCENDING)],
                    name='document_archive_scope_seek',
                )
                if self.identity_keys[-1] == 'contentSha256':
                    await self._collection.create_index(
                        [('reportAliases', ASCENDING)],
                        unique=True,
                        sparse=True,
                        name='document_report_alias_unique',
                    )
                self._ready = True

    @staticmethod
    def _scope(document):
        return {key: document[key] for key in ('_id', 'userId', 'subjectId')}

    async def resolve(self, document):
        await self.ensure_indexes()
        try:
            result = await self._collection.insert_one(document)
            if not result.acknowledged:
                raise RuntimeError('文件索引未可靠保存')
            return document
        except DuplicateKeyError:
            existing = await self._collection.find_one({key: document[key] for key in self.identity_keys})
            if existing is None:
                raise
            if existing['byteSize'] != document['byteSize']:
                raise RuntimeError('文件摘要与已保存大小不一致')
            return existing

    async def get(self, document_id, user_id, subject_id):
        return await self._collection.find_one({'_id': document_id, 'userId': user_id, 'subjectId': subject_id})

    async def read_archived_documents(self, *, scope, after_id=None, document_id=None, limit=20):
        """Bounded PRIMARY-compatible seek; reads never create indexes or repair records."""
        if type(limit) is not int or not 1 <= limit <= 101:
            raise ValueError('archive scan limit must be between 1 and 101')
        query = scope.as_dict()
        if document_id is not None:
            if after_id is not None and document_id <= after_id:
                return []
            query['_id'] = document_id
        elif after_id is not None:
            query['_id'] = {'$gt': after_id}
        return await self._collection.find(query).sort([('_id', ASCENDING)]).to_list(length=limit)

    async def get_archived_round(self, document_id, user_id, subject_id, interpretation_round):
        if type(interpretation_round) is not int or interpretation_round < 0:
            raise ValueError('a fixed nonnegative interpretation round is required')
        document = await self.get(document_id, user_id, subject_id)
        return deepcopy((document or {}).get('archivedRounds', {}).get(str(interpretation_round)))

    async def put_archived_round(self, document, token, now, archived):
        """Write once under the actual execution lease, including acknowledgement-loss replay."""
        number = archived['interpretationResultRound']
        key = str(number)
        existing = await self.get_archived_round(document['_id'], document['userId'], document['subjectId'], number)
        if existing is not None:
            if existing != archived:
                raise RuntimeError('fixed document round cannot be overwritten')
            return
        result = await self._collection.update_one(
            {
                **self._scope(document),
                'leaseToken': token,
                'leaseExpiresAt': {'$gt': now},
                'interpretationRound': number,
                f'archivedRounds.{key}': {'$exists': False},
            },
            {'$set': {f'archivedRounds.{key}': deepcopy(archived)}},
        )
        saved = await self.get_archived_round(document['_id'], document['userId'], document['subjectId'], number)
        if not result.acknowledged or saved != archived:
            raise DocumentBusyError('document round was not reliably fixed by its execution owner')

    async def find_legacy_outputs(self, *, user_id, subject_id, identity):
        """Find at most two old outputs with exactly the same ordered semantic input."""
        if self.identity_keys[-1] != 'extractionKey':
            raise ValueError('旧输出查询仅适用于文件扩展索引')
        return await self._collection.find(
            {
                **identity,
                'userId': user_id,
                'subjectId': subject_id,
                'cacheIdentityVersion': None,
                'contractVersion': None,
            }
        ).to_list(length=2)

    async def bind_report_id(self, document, report_id):
        """Keep successful content-only responses subject to report ID ownership.

        The unique multikey index fences concurrent writers across files and
        owners; adding the same alias to the same file is idempotent. Sparse
        indexing leaves existing documents without aliases untouched.
        """
        await self.ensure_indexes()
        try:
            result = await self._collection.update_one(
                self._scope(document), {'$addToSet': {'reportAliases': report_id}}
            )
        except DuplicateKeyError as error:
            raise HealthReportSourceConflictError('reportId 已用于其他文件或健康主体') from error
        if not result.acknowledged or result.matched_count != 1:
            raise RuntimeError('报告引用未可靠绑定到文件')

    async def claim(self, document, token, now, expires):
        # A completed attempt may still have incomplete content. Only an explicit
        # caller that observed this exact round can open the next recovery round.
        completed = document.get('status') == 'completed'
        if document.get('status') not in {'pending', 'running', 'failed', 'completed'}:
            return None
        if completed and (
            document.get('processingStatus') not in {'partial', 'unavailable'}
            or not document.get('processingRetryable', False)
        ):
            return None
        round_number = document.get('interpretationRound', 0)
        if document.get('status') not in {'running', 'failed'}:
            round_number += 1
        return await self._collection.find_one_and_update(
            {
                **self._scope(document),
                'status': document['status'],
                'interpretationRound': document.get('interpretationRound'),
                '$or': [{'leaseExpiresAt': None}, {'leaseExpiresAt': {'$lte': now}}],
            },
            {
                '$set': {
                    'status': 'running',
                    'leaseToken': token,
                    'leaseExpiresAt': expires,
                    'updatedAt': now,
                    'interpretationRound': round_number,
                }
            },
            return_document=ReturnDocument.AFTER,
        )

    async def claim_resubmission(self, document, token, now, expires):
        retry_at = document.get('submissionRetryAfter') or document['updatedAt'] + timedelta(seconds=300)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        if retry_at > now:
            return None
        count = document.get('resubmissionCount', 0)
        if count >= 1 + document.get('manualResubmissionGrants', 0):
            return None
        return await self._collection.find_one_and_update(
            {
                **self._scope(document),
                'status': 'submission_unknown',
                'updatedAt': document['updatedAt'],
                'resubmissionCount': document.get('resubmissionCount'),
                '$or': [{'leaseExpiresAt': None}, {'leaseExpiresAt': {'$lte': now}}],
            },
            {
                '$set': {
                    'status': 'running',
                    'leaseToken': token,
                    'leaseExpiresAt': expires,
                    'updatedAt': now,
                    'submissionStarted': False,
                    'taskId': None,
                    'submissionRetryAfter': None,
                    'resubmissionCount': count + 1,
                    'retryable': True,
                    'recoveryHistory': [
                        *document.get('recoveryHistory', []),
                        {
                            'action': 'controlled_resubmission',
                            'at': now,
                            'previousTraceId': document.get('submissionTraceId', document['_id']),
                            'previousErrorType': document.get('errorType'),
                            'attempt': count + 1,
                        },
                    ],
                }
            },
            return_document=ReturnDocument.AFTER,
        )

    async def recover_submission(self, document, now, *, task_id=None, allow_resubmit=False, reason):
        """Operator recovery, scoped and fenced by the reviewed document revision."""
        if bool(task_id) == bool(allow_resubmit):
            raise ValueError('必须选择绑定任务 ID 或授权一次重提')
        if document.get('status') not in ('submission_unknown', 'failed'):
            return None
        if document['status'] == 'failed' and (
            document.get('submissionStarted') or document.get('taskId') or not allow_resubmit
        ):
            return None
        fields = {
            'updatedAt': now,
            'recoveryHistory': [
                *document.get('recoveryHistory', []),
                {
                    'action': 'operator_bind_task' if task_id else 'operator_allow_resubmission',
                    'at': now,
                    'reason': reason,
                    'taskId': task_id,
                },
            ],
        }
        if task_id:
            fields.update(status='failed', taskId=task_id, retryable=True, submissionRetryAfter=None)
        elif document['status'] == 'failed':
            fields.update(retryable=True)
        else:
            fields.update(
                manualResubmissionGrants=document.get('manualResubmissionGrants', 0) + 1,
                submissionRetryAfter=now,
            )
        return await self._collection.find_one_and_update(
            {
                **self._scope(document),
                'status': document['status'],
                'updatedAt': document['updatedAt'],
                '$or': [{'leaseExpiresAt': None}, {'leaseExpiresAt': {'$lte': now}}],
            },
            {'$set': fields},
            return_document=ReturnDocument.AFTER,
        )

    async def update_owned(self, document, token, now, fields):
        result = await self._collection.update_one(
            {**self._scope(document), 'leaseToken': token, 'leaseExpiresAt': {'$gt': now}},
            {'$set': {**fields, 'updatedAt': now}},
        )
        if not result.acknowledged or result.matched_count != 1:
            raise DocumentBusyError('文件处理执行权已失效，请稍后重试')


def create_application_document_index_store():
    from db.mongodb import collection_document_index

    return MongoDocumentIndexStore(collection_document_index)


def create_application_document_output_index_store():
    from db.mongodb import collection_document_extractions

    return MongoDocumentIndexStore(collection_document_extractions, identity_key='extractionKey')
