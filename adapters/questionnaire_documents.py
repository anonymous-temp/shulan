"""Questionnaire-only attachment staging. Never creates subjects or publishes reports."""

from __future__ import annotations

import asyncio
import hashlib
import re
from copy import deepcopy
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from pymongo.errors import DuplicateKeyError

from adapters.analysis.document_download import HttpDocumentDownloader
from adapters.analysis.document_trace import MongoDocumentTraceStore
from modules.analysis.application.documents import DocumentProcessingTrace
from modules.analysis.application.service import _content_file_type
from utils.report_files import report_file_category
from utils.report_files import resolve_report_filename


def _now():
    return datetime.now(timezone.utc)


def _gender_evidence(text):
    aliases = {'\u7537': 'male', '\u5973': 'female', 'male': 'male', 'female': 'female', 'm': 'male', 'f': 'female'}
    pattern = (
        r'(?:\u6027\u522b|\bsex\b|\bgender\b)\s*[:\uff1a]?\s*'
        r'(female|male|\u7537|\u5973|[mf])(?=\s|[,;|\uff0c\uff1b\u3002]|$)'
    )
    return [
        {
            'value': aliases[match.group(1).lower()],
            'pointer': '/docmind_result/text',
            'start': match.start(),
            'end': match.end(),
            'quote': match.group(0),
        }
        for match in re.finditer(pattern, text, re.IGNORECASE)
    ][:20]


class MongoQuestionnaireArchive:
    """Reuse chunked payload storage in dedicated collections; deletion is fenced against pinning."""

    def __init__(self, collection, chunks):
        self.collection = collection
        self.chunks = chunks
        self.payloads = MongoDocumentTraceStore(collection, chunks)
        self._indexes_ready = False
        self._index_lock = asyncio.Lock()

    async def ensure_indexes(self):
        if self._indexes_ready:
            return
        async with self._index_lock:
            if self._indexes_ready:
                return
            await self.collection.create_index(
                [('userId', 1), ('questionnaireId', 1)], name='questionnaire_document_owner'
            )
            await self.collection.create_index(
                [('retained', 1), ('expiresAt', 1)], name='questionnaire_document_cleanup'
            )
            self._indexes_ready = True

    async def create(self, document):
        await self.ensure_indexes()
        await self.payloads.create(document)

    async def update(self, trace_id, user_id, fields):
        await self.payloads.update(trace_id, user_id, fields)

    async def write_payload(self, trace_id, user_id, name, payload):
        await self.payloads.write_payload(trace_id, user_id, name, payload)

    async def read_payload(self, trace_id, user_id, subject_id, name):
        return await self.payloads.read_payload(trace_id, user_id, subject_id, name)

    async def get(self, *, user_id, artifact_id):
        return await self.collection.find_one({'_id': artifact_id, 'userId': user_id})

    async def ready(self, *, user_id, questionnaire_id, artifact_ids, expires_at):
        for artifact_id in artifact_ids:
            result = await self.collection.update_one(
                {
                    '_id': artifact_id,
                    'userId': user_id,
                    'questionnaireId': questionnaire_id,
                    'retained': {'$ne': True},
                    'cleanupState': {'$exists': False},
                },
                {'$set': {'expiresAt': expires_at}},
            )
            if not result.acknowledged or result.matched_count != 1:
                raise RuntimeError('questionnaire artifact unavailable at publication')

    async def pin(self, *, user_id, questionnaire_id, artifact_ids):
        # Check every reference before touching retention; never accept arbitrary client blob IDs.
        for artifact_id in artifact_ids:
            record = await self.collection.find_one(
                {
                    '_id': artifact_id,
                    'userId': user_id,
                    'questionnaireId': questionnaire_id,
                    'cleanupState': {'$exists': False},
                }
            )
            if record is None:
                raise LookupError('问卷附件已过期或不可访问')
        for artifact_id in artifact_ids:
            result = await self.collection.update_one(
                {
                    '_id': artifact_id,
                    'userId': user_id,
                    'questionnaireId': questionnaire_id,
                    'cleanupState': {'$exists': False},
                },
                {'$set': {'retained': True}, '$unset': {'expiresAt': ''}},
            )
            if not result.acknowledged or result.matched_count != 1:
                raise RuntimeError('问卷附件保留失败，请重试提交')

    async def expire(self, *, user_id, questionnaire_id, now=None):
        # Only the owning QuestionnaireService may call this while holding its
        # session lease, after excluding pending submissions. Query the owner,
        # not the saved artifact list: interrupted generation may not save it.
        cursor = self.collection.find(
            {'userId': user_id, 'questionnaireId': questionnaire_id, 'retained': {'$ne': True}},
            {'_id': 1},
        )
        for record in await cursor.to_list(length=100):
            await self._delete_expired(record['_id'], user_id)

    async def _delete_expired(self, artifact_id, user_id):
        claimed = await self.collection.update_one(
            {'_id': artifact_id, 'userId': user_id, 'retained': {'$ne': True}},
            {'$set': {'cleanupState': 'deleting'}},
        )
        if not claimed.acknowledged or claimed.matched_count != 1:
            return 0
        # A crash between these steps is recoverable: the claimed metadata remains discoverable.
        await self.chunks.delete_many({'traceId': artifact_id, 'userId': user_id})
        await self.collection.delete_one({'_id': artifact_id, 'userId': user_id, 'cleanupState': 'deleting'})
        return 1


class MongoOwnedQuestionnaireDocumentReader:
    """Read an existing self-owned immutable archive without the shared publication coordinator."""

    def __init__(self, *, document_index, document_traces, subjects):
        self.index = document_index
        self.traces = document_traces
        self.subjects = subjects

    async def read(self, *, user_id, document_id):
        document = await self.index.find_one({'_id': document_id, 'userId': user_id})
        if document is None:
            raise LookupError('文件引用不存在或不可访问')
        subject_id = document.get('subjectId')
        subject = await self.subjects.find_one(
            {
                'userId': user_id,
                'subjectId': subject_id,
                'kind': 'self',
                'status': 'active',
            }
        )
        if subject is None:
            raise LookupError('文件引用不属于当前用户本人')
        refs = document.get('payloadRefs') or {}
        if not refs.get('original_file'):
            raise LookupError('文件引用缺少原始归档')
        original = await self.traces.read_payload(refs['original_file'], user_id, subject_id, 'original_file')
        content = original.get('content')
        if (
            not isinstance(content, bytes)
            or hashlib.sha256(content).hexdigest() != document.get('contentSha256')
            or len(content) != document.get('byteSize')
        ):
            raise ValueError('文件归档完整性校验失败')
        parsed = None
        if refs.get('docmind_result'):
            parsed = await self.traces.read_payload(refs['docmind_result'], user_id, subject_id, 'docmind_result')
        return {
            'content': content,
            'fileName': document.get('fileName') or 'report',
            'fileType': document.get('fileType'),
            'parsed': parsed,
            'sourceDocumentId': document_id,
        }


class _StagingTrace(DocumentProcessingTrace):
    async def write_payload(self, name, payload):
        if name == 'original_file':
            existing = await self.read_payload(name)
            if existing is not None:
                if existing != payload:
                    raise ValueError('staged original bytes cannot change')
                return
        await super().write_payload(name, payload)

    async def update(self, *, stage=None, **fields):
        if stage == 'submitting':
            fields['submissionStarted'] = True
        await super().update(stage=stage, **fields)

    async def read_payload(self, name):
        record = await self.store.get(user_id=self.user_id, artifact_id=self.trace_id)
        if not record or name not in (record.get('payloads') or {}):
            return None
        return await self.store.read_payload(self.trace_id, self.user_id, None, name)


class QuestionnaireDocumentStager:
    def __init__(
        self,
        *,
        interpreter,
        archive,
        source_reader=None,
        downloader=None,
        ttl_seconds=7200,
        timeout_seconds=180,
        concurrency=2,
        clock=_now,
    ):
        self.interpreter = interpreter
        self.archive = archive
        self.source_reader = source_reader
        self.downloader = downloader or HttpDocumentDownloader()
        self.ttl_seconds = ttl_seconds
        self.timeout_seconds = timeout_seconds
        self.concurrency = concurrency
        self.clock = clock

    async def stage(self, *, user_id, questionnaire_id, file_list):
        if not isinstance(file_list, list) or len(file_list) > 5:
            raise ValueError('fileList 必须是最多五个 URL 或 documentId 引用的列表')
        for item in file_list:
            if (
                not isinstance(item, dict)
                or set(item) - {'url', 'documentId', 'fileType'}
                or bool(item.get('url')) == bool(item.get('documentId'))
                or any(not isinstance(value, str) or not value.strip() for value in item.values())
            ):
                raise ValueError('附件仅支持 URL 或 documentId，不支持本地文件或内联内容')
            if 'documentId' in item and 'fileType' in item:
                raise ValueError('documentId 引用不能同时指定 fileType')
        semaphore = asyncio.Semaphore(self.concurrency)

        async def run(index, item):
            async with semaphore:
                return await self._stage_one(user_id, questionnaire_id, index, item)

        outcomes = await asyncio.gather(*(run(index, item) for index, item in enumerate(file_list)))
        return {'reports': [item[0] for item in outcomes], 'artifacts': [item[1] for item in outcomes]}

    async def _stage_one(self, user_id, questionnaire_id, index, item):
        from modules.analysis.application.documents import parsed_document_text

        identity = repr((user_id, questionnaire_id, index, sorted(item.items()))).encode('utf-8')
        artifact_id = 'qdoc_' + hashlib.sha256(identity).hexdigest()
        artifact = {
            'artifactId': artifact_id,
            'questionnaireId': questionnaire_id,
            'fileIndex': index,
            'kind': 'document_reference' if item.get('documentId') else 'questionnaire_document',
            'sourceDocumentId': item.get('documentId'),
        }
        prior = await self.archive.get(user_id=user_id, artifact_id=artifact_id)
        if prior and prior.get('questionnaireId') != questionnaire_id:
            raise ValueError('附件归属不一致')
        if prior and prior.get('status') == 'completed':
            artifact.update(contentSha256=prior['contentSha256'], byteSize=prior['byteSize'])
            result = await self.archive.read_payload(artifact_id, user_id, None, 'interpretation')
            return self._report(index, artifact_id, result, prior.get('genderEvidence', [])), artifact
        trace = _StagingTrace(self.archive, user_id=user_id)
        trace.trace_id = artifact_id
        if prior:
            # Persisted upstream task IDs are resumed; never resubmit an ambiguous accepted request.
            if prior.get('submissionStarted') and not prior.get('taskId'):
                return {'fileIndex': index, 'status': 'unavailable', 'errorCode': 'submission_unknown'}, artifact
            trace.resume_task_id = prior.get('taskId')
        else:
            try:
                await trace.start(
                    subjectId=None,
                    questionnaireId=questionnaire_id,
                    fileIndex=index,
                    source={'entrypoint': 'questionnaire', 'sourceId': questionnaire_id},
                    retained=False,
                    expiresAt=self.clock() + timedelta(seconds=self.ttl_seconds),
                )
            except DuplicateKeyError:
                return {'fileIndex': index, 'status': 'unavailable', 'errorCode': 'document_busy'}, artifact
        try:
            async with asyncio.timeout(self.timeout_seconds):
                if prior and 'original_file' in (prior.get('payloads') or {}):
                    original = await trace.read_payload('original_file')
                    content, filename = original['content'], prior['fileName']
                    if hashlib.sha256(content).hexdigest() != prior.get('contentSha256') or len(content) != prior.get(
                        'byteSize'
                    ):
                        raise ValueError('暂存文件完整性校验失败')
                    parsed = await trace.read_payload('docmind_result')
                elif item.get('documentId'):
                    if self.source_reader is None:
                        raise LookupError('文件引用读取服务未配置')
                    source = await self.source_reader.read(user_id=user_id, document_id=item['documentId'])
                    content, filename, parsed = source['content'], source['fileName'], source.get('parsed')
                else:
                    content = await self.downloader.download(item['url'])
                    filename = resolve_report_filename(item['url'])[0] or 'report'
                    parsed = None
                file_type = _content_file_type(content)
                if file_type is None:
                    raise ValueError('附件内容不是受支持的 PDF 或图片')
                declared = item.get('fileType')
                if declared and declared not in {
                    file_type,
                    report_file_category(file_type),
                    'jpeg' if file_type == 'jpg' else file_type,
                }:
                    raise ValueError('附件声明类型与文件内容不一致')
                artifact.update(contentSha256=hashlib.sha256(content).hexdigest(), byteSize=len(content))
                await trace.update(
                    fileName=filename,
                    fileType=file_type,
                    byteSize=len(content),
                    contentSha256=artifact['contentSha256'],
                )
                await trace.write_payload('original_file', {'content': content})
                if parsed is None:
                    text = await self.interpreter.extract_document(
                        trace=trace,
                        file_url=item.get('url', ''),
                        filename=filename,
                        file_type=file_type,
                        content=content,
                    )
                    parsed = getattr(trace, 'parsed_document', None)
                else:
                    await trace.write_payload('docmind_result', parsed)
                    trace.parsed_document = parsed
                    text = parsed_document_text(parsed)
                gender_evidence = _gender_evidence(text)
                await trace.update(genderEvidence=gender_evidence)
                result = await self.interpreter.analyze(
                    text,
                    user_id=user_id,
                    session_id=questionnaire_id,
                    subject_id=None,
                    trace=trace,
                    docmind=parsed,
                    source_snapshot={'content': content, 'fileName': filename},
                )
                if not isinstance(result, dict) or not result:
                    raise ValueError('未获得可用解读')
                await trace.write_payload('interpretation', result)
                report = self._report(index, artifact_id, result, gender_evidence)
                await trace.update(stage='completed', status='completed', processingStatus=report['status'])
                return report, artifact
        except Exception as error:
            await trace.update(stage='failed', status='failed', errorType=type(error).__name__)
            return {
                'fileIndex': index,
                'status': 'unavailable',
                'artifactId': artifact_id,
                'errorCode': 'document_unavailable',
            }, artifact

    @staticmethod
    def _report(index, artifact_id, result, gender_evidence=()):
        status = (result.get('processing') or {}).get('status', 'complete')
        if status not in {'complete', 'partial'}:
            return {
                'fileIndex': index,
                'status': 'unavailable',
                'artifactId': artifact_id,
                'errorCode': 'document_unavailable',
            }
        return {
            'fileIndex': index,
            'status': status,
            'artifactId': artifact_id,
            'interpretation': deepcopy(result),
            'genderEvidence': deepcopy(list(gender_evidence)),
        }

    async def ready_artifacts(self, snapshot, expires_at):
        if isinstance(expires_at, (int, float)):
            expires_at = datetime.fromtimestamp(expires_at, timezone.utc)
        elif isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at.replace('Z', '+00:00'))
        if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
            raise ValueError('expires_at must be an aware UTC timestamp')
        await self.archive.ready(
            user_id=snapshot['userId'],
            questionnaire_id=snapshot['questionnaireId'],
            artifact_ids=[item['artifactId'] for item in snapshot.get('artifacts', [])],
            expires_at=expires_at,
        )

    async def pin_artifacts(self, snapshot):
        artifacts = snapshot.get('artifacts', [])
        await self.archive.pin(
            user_id=snapshot['userId'],
            questionnaire_id=snapshot['questionnaireId'],
            artifact_ids=[item['artifactId'] for item in artifacts],
        )

    async def expire_artifacts(self, snapshot):
        await self.archive.expire(
            user_id=snapshot['userId'], questionnaire_id=snapshot['questionnaireId'], now=self.clock()
        )

    async def read_artifact(self, *, user_id, artifact):
        record = await self.archive.get(user_id=user_id, artifact_id=artifact['artifactId'])
        if record is None or record.get('questionnaireId') != artifact['questionnaireId'] or record.get('cleanupState'):
            raise LookupError('问卷附件不存在或不可访问')
        return await self.archive.read_payload(artifact['artifactId'], user_id, None, 'interpretation')
