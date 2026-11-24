"""Recoverable QA creation with expiring Redis receipts and fenced publication."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from copy import deepcopy
from datetime import datetime
from datetime import timezone
from uuid import uuid4

from agents.errors import AgentProtocolError
from modules.analysis.application.documents import DocumentBusyError
from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.application.documents import DocumentParsingError
from modules.analysis.application.documents import DocumentSubmissionConflictError
from modules.analysis.publication import PublicationConflictError
from modules.record.application.validation import RecordGenerationError
from modules.record.documents import RecordCurrentDocumentConflict
from modules.record.documents import RecordDocumentPublicationPending
from modules.record.documents import record_prepared_document_from_checkpoint
from utils.log import getLogger
from utils.log.context import LogContext
from utils.log.context import bind_log_context
from utils.log.context import task_log_context

logger = getLogger(__name__)
CREATION_TTL = 7200
# HTTP wait budget; expiry returns processing while the shielded worker continues.
RESPONSE_WAIT_SECONDS = 120
ATTEMPT_TIMEOUT_SECONDS = 600
MAX_CREATION_ATTEMPTS = 3
RECOVERY_INTERVAL_SECONDS = 15


def is_creation(session):
    return bool(session and session.get('qaCreation') and session.get('status') in ('creating', 'create_failed'))


def public_creation(session, now=None):
    now = time.time() if now is None else now
    creation = session['qaCreation']
    failed = session['status'] == 'create_failed'
    error = creation.get('error') or {}
    return {
        'userId': session['userId'],
        'sessionId': session['sessionId'],
        'status': 'failed' if failed else 'processing',
        'stage': creation['stage'],
        'message': error.get('message', '正在准备建档，请稍后查询状态。'),
        'errorCode': error.get('code'),
        'retryable': bool(error.get('retryable')) if failed else False,
        'retryAfterSeconds': max(1, math.ceil(creation.get('nextAttemptAt', now + 3) - now))
        if failed and error.get('retryable')
        else (3 if not failed else None),
        'expiresAt': datetime.fromtimestamp(creation['expiresAt'], timezone.utc).isoformat(),
    }


def creation_http_status(result):
    if result.get('status') == 'processing':
        return 202
    if result.get('status') != 'failed':
        return 200
    return {
        'DOCUMENT_NOT_FOUND': 404,
        'DOCUMENT_FORMAT_UNSUPPORTED': 415,
        'DOCUMENT_SOURCE_CONFLICT': 409,
        'DOCUMENT_SUBMISSION_UNCONFIRMED': 409,
        'DOCUMENT_SUBMISSION_RETRY_EXHAUSTED': 409,
        'CREATION_RETRY_EXHAUSTED': 409,
        'CREATION_INPUT_INVALID': 400,
        'CREATION_TIMEOUT': 504,
        'CREATION_INTERNAL_ERROR': 503,
        'DOCUMENT_PROCESSING_BUSY': 503,
    }.get(result.get('errorCode'), 502)


def failure_info(error):
    chain = []
    current = error
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__
    conflict = next(
        (
            item
            for item in chain
            if isinstance(item, DocumentSubmissionConflictError) or getattr(item, 'submission_unknown', False)
        ),
        None,
    )
    if conflict is not None:
        retry_after = getattr(conflict, 'retry_after_seconds', None)
        return {
            'code': 'DOCUMENT_SUBMISSION_UNCONFIRMED'
            if retry_after is not None
            else 'DOCUMENT_SUBMISSION_RETRY_EXHAUSTED',
            'message': '报告提交状态正在核查，稍后将尝试恢复。'
            if retry_after is not None
            else '报告提交暂时无法确认，已停止自动重试，请联系工作人员核查。',
            'retryable': retry_after is not None,
            'delay': max(1, retry_after or 0),
        }
    if isinstance(error, DocumentNotFoundError):
        return {'code': 'DOCUMENT_NOT_FOUND', 'message': '报告引用不存在或不可访问。', 'retryable': False, 'delay': 0}
    if any(type(item).__name__ == 'HealthReportUnsupportedFormatError' for item in chain):
        return {
            'code': 'DOCUMENT_FORMAT_UNSUPPORTED',
            'message': '不支持此报告文件格式。',
            'retryable': False,
            'delay': 0,
        }
    if isinstance(error, DocumentBusyError):
        return {
            'code': 'DOCUMENT_PROCESSING_BUSY',
            'message': '报告仍在处理中，稍后继续恢复。',
            'retryable': True,
            'delay': 15,
        }
    if any(
        isinstance(item, (PublicationConflictError, RecordCurrentDocumentConflict))
        or type(item).__name__ == 'HealthReportSourceConflictError'
        for item in chain
    ):
        return {
            'code': 'DOCUMENT_SOURCE_CONFLICT',
            'message': '报告来源与已保存的记录不一致。',
            'retryable': False,
            'delay': 0,
        }
    if isinstance(error, ValueError):
        return {
            'code': 'CREATION_INPUT_INVALID',
            'message': '建档资料无效，请检查输入或联系工作人员。',
            'retryable': False,
            'delay': 0,
        }
    if isinstance(error, TimeoutError):
        return {
            'code': 'CREATION_TIMEOUT',
            'message': '本次建档准备超时，正在安排恢复。',
            'retryable': True,
            'delay': 15,
        }
    if isinstance(error, AgentProtocolError):
        return {
            'code': 'INITIAL_QUESTION_FAILED',
            'message': '首个问题生成失败，正在安排重试。',
            'retryable': True,
            'delay': 5,
        }
    parsing = next((item for item in chain if isinstance(item, DocumentParsingError)), None)
    if parsing is not None:
        return {
            'code': 'DOCUMENT_PROCESSING_FAILED',
            'message': '报告处理失败，请稍后重试；如持续失败请联系工作人员。',
            'retryable': getattr(parsing, 'automatic_retryable', getattr(parsing, 'retryable', True)),
            'delay': 15,
        }
    return {
        'code': 'CREATION_INTERNAL_ERROR',
        'message': '建档准备暂时失败，请稍后查询状态或重试。',
        'retryable': True,
        'delay': 15,
    }


class CreationExpiredError(RuntimeError):
    pass


class CreationAttempt:
    def __init__(self, owner, session, lease_guard):
        self.owner = owner
        self.session = session
        self.lease_guard = lease_guard
        self.lock = asyncio.Lock()

    @property
    def data(self):
        return self.session['qaCreation']['checkpoint']

    async def checkpoint(self, stage, **fields):
        async with self.lock:
            draft = deepcopy(self.session)
            creation = draft['qaCreation']
            creation['stage'] = stage
            creation['nextAttemptAt'] = self.owner.clock() + RECOVERY_INTERVAL_SECONDS
            if 'documentReference' in fields:
                index, document_id = fields.pop('documentReference')
                creation['checkpoint'].setdefault('references', {})[str(index)] = document_id
            if 'preparedDocument' in fields:
                index, prepared = fields.pop('preparedDocument')
                frozen = record_prepared_document_from_checkpoint(prepared)
                if (
                    creation.get('documentConsumerRevision', frozen.to_checkpoint()['contractVersion'])
                    != frozen.to_checkpoint()['contractVersion']
                ):
                    raise ValueError('prepared document differs from the accepted consumer revision')
                frozen.check_request(
                    user_id=draft['userId'],
                    session_id=draft['sessionId'],
                    file_index=index,
                    attachment=frozen.to_checkpoint()['attachment'],
                )
                creation['checkpoint'].setdefault('preparedDocuments', {})[str(index)] = frozen.to_checkpoint()
            if 'completedFile' in fields:
                index, outcome = fields.pop('completedFile')
                creation['checkpoint'].setdefault('files', {})[str(index)] = outcome
            creation['checkpoint'].update(fields)
            await self._save(draft)

    async def _save(self, draft, *, published=False):
        await self.lease_guard()
        creation = self.session['qaCreation']
        now = self.owner.clock()
        remaining = creation['expiresAt'] - now
        if remaining <= 0:
            raise CreationExpiredError('创建记录已过期')
        revision = creation['revision']
        if not published:
            draft['qaCreation']['revision'] = uuid4().hex
        saved = await self.owner.store.compare_save(
            self.session['userId'],
            self.session['sessionId'],
            revision,
            draft,
            ttl_seconds=CREATION_TTL if published else max(1, math.ceil(remaining)),
        )
        if not saved:
            raise CreationExpiredError('创建记录已过期或执行者已变化')
        self.session = draft

    async def publish(self, session):
        async with self.lock:
            session = deepcopy(session)
            session['creationRequestHash'] = self.session['qaCreation']['requestHash']
            await self._save(session, published=True)


class QACreationCoordinator:
    def __init__(self, manager, store, *, task_spawner=None, clock=time.time):
        self.manager = manager
        self.store = store
        self.task_spawner = task_spawner or asyncio.create_task
        self.clock = clock
        self.response_wait = RESPONSE_WAIT_SECONDS
        self.attempt_timeout = ATTEMPT_TIMEOUT_SECONDS
        self.tasks = {}
        self.recovering = False

    @staticmethod
    def fingerprint(inputs):
        return hashlib.sha256(
            json.dumps(inputs, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        ).hexdigest()

    def _validate_existing(self, existing, request_hash):
        from .service import QASessionConflictError

        saved_hash = (
            existing['qaCreation']['requestHash'] if is_creation(existing) else existing.get('creationRequestHash')
        )
        if saved_hash and saved_hash != request_hash:
            raise QASessionConflictError('相同 sessionId 的创建资料不一致', code='CREATION_REQUEST_CONFLICT')
        if not is_creation(existing):
            if existing.get('type', 'qa') != 'qa' and not (saved_hash and existing.get('qaCompletedAt')):
                raise ValueError('普通对话会话不能用于创建问答')
            if existing.get('status') == 'completed' and not saved_hash:
                raise ValueError('会话已完成，不允许重新创建')
            return self.manager._buildSessionResponse(existing)
        return None

    async def create(self, user_id, session_id, inputs):
        from .service import QASessionConflictError

        if not all(isinstance(value, str) and value.strip() for value in (user_id, session_id)):
            raise ValueError('userId 和 sessionId 必须为非空字符串')
        request_hash = self.fingerprint(inputs)
        # A retry must not compete with the long-running creation worker's lease.
        existing = await self.manager._load_session(user_id, session_id)
        if existing is None:
            try:
                async with self.manager._sessionLease(user_id, session_id) as guard:
                    await self.manager._assertNoQuestionnaireOwner(user_id, session_id)
                    existing = await self.manager._load_session(user_id, session_id)
                    if existing is None:
                        now = self.clock()
                        existing = {
                            'userId': user_id,
                            'sessionId': session_id,
                            'type': 'qa',
                            'status': 'creating',
                            'qaCreation': {
                                'id': uuid4().hex,
                                'revision': uuid4().hex,
                                'requestHash': request_hash,
                                'inputs': deepcopy(inputs),
                                'acceptedAt': now,
                                'expiresAt': now + CREATION_TTL,
                                'attempts': 0,
                                'stage': 'accepted',
                                'nextAttemptAt': now,
                                'checkpoint': {},
                                'agentSelection': self.manager.record_agent.manifest,
                                'documentConsumerRevision': self.manager._recordDocumentConsumerRevision(
                                    user_id=user_id, session_id=session_id
                                ),
                                'profileConsumerRevision': self.manager._recordProfileConsumerRevision(
                                    user_id=user_id, session_id=session_id
                                ),
                                'error': None,
                            },
                        }
                        await guard()
                        if not await self.store.compare_save(
                            user_id, session_id, None, existing, ttl_seconds=CREATION_TTL
                        ):
                            raise QASessionConflictError('会话已变化，请查询状态后重试')
            except QASessionConflictError:
                existing = await self.manager._load_session(user_id, session_id)
                if existing is None:
                    raise
        result = self._validate_existing(existing, request_hash)
        if result is not None:
            return result
        if self._eligible(existing):
            task = self._start(user_id, session_id)
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=self.response_wait)
            except TimeoutError:
                pass
        current = await self.manager._load_session(user_id, session_id)
        if current is None:
            raise CreationExpiredError('创建记录已过期，请重新建档')
        return self.manager._buildSessionResponse(current)

    def _eligible(self, session):
        creation = session['qaCreation']
        return (
            creation['expiresAt'] > self.clock()
            and (session['status'] == 'creating' or (creation.get('error') or {}).get('retryable'))
            and creation['nextAttemptAt'] <= self.clock()
        )

    def _start(self, user_id, session_id):
        key = (user_id, session_id)
        existing = self.tasks.get(key)
        if existing and not existing.done():
            return existing
        coroutine = self._run(user_id, session_id)
        try:
            task = self.task_spawner(coroutine)
        except Exception:
            coroutine.close()
            raise
        self.tasks[key] = task

        def finished(done):
            if self.tasks.get(key) is done:
                self.tasks.pop(key, None)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        return task

    @task_log_context(task='qa-creation')
    async def _run(self, user_id, session_id):
        from .service import QASessionConflictError

        try:
            async with self.manager._sessionLease(user_id, session_id) as guard:
                session = await self.manager._load_session(user_id, session_id)
                if not is_creation(session) or not self._eligible(session):
                    return
                attempt = CreationAttempt(self, session, guard)
                draft = deepcopy(session)
                if draft['qaCreation']['attempts'] >= MAX_CREATION_ATTEMPTS:
                    draft['status'] = 'create_failed'
                    draft['qaCreation']['error'] = {
                        'code': 'CREATION_RETRY_EXHAUSTED',
                        'message': '建档准备多次中断，已停止自动重试，请联系工作人员。',
                        'retryable': False,
                        'delay': 0,
                    }
                    await attempt._save(draft)
                    return
                draft['status'] = 'creating'
                draft['qaCreation']['attempts'] += 1
                draft['qaCreation']['error'] = None
                draft['qaCreation']['nextAttemptAt'] = self.clock() + RECOVERY_INTERVAL_SECONDS
                await attempt._save(draft)
                try:
                    timeout = min(self.attempt_timeout, session['qaCreation']['expiresAt'] - self.clock())
                    async with asyncio.timeout(max(0.01, timeout)):
                        await self.manager._createSessionUnderLease(
                            user_id,
                            session_id,
                            **session['qaCreation']['inputs'],
                            lease_guard=guard,
                            creation_attempt=attempt,
                        )
                except RecordDocumentPublicationPending:
                    if not is_creation(attempt.session):
                        return
                    waiting = deepcopy(attempt.session)
                    creation = waiting['qaCreation']
                    waiting['status'] = 'creating'
                    creation['stage'] = 'processing_reports'
                    creation['error'] = None
                    # A verified durable wait consumed no transport retry. Unknown
                    # exceptions and cancellation retain their reserved attempt.
                    creation['attempts'] -= 1
                    creation['nextAttemptAt'] = self.clock() + RECOVERY_INTERVAL_SECONDS
                    await attempt._save(waiting)
                except CreationExpiredError:
                    return
                except asyncio.CancelledError:
                    # Keep the recoverable receipt; the lease is released by the enclosing context.
                    raise
                except Exception as error:
                    if not is_creation(attempt.session):
                        return
                    details = failure_info(error)
                    failed = deepcopy(attempt.session)
                    creation = failed['qaCreation']
                    if creation['attempts'] >= MAX_CREATION_ATTEMPTS and details['retryable']:
                        details.update(
                            code='CREATION_RETRY_EXHAUSTED',
                            message='建档准备多次失败，已停止自动重试，请联系工作人员。',
                            retryable=False,
                        )
                    failed['status'] = 'create_failed'
                    creation['error'] = details
                    if isinstance(error, RecordGenerationError):
                        # Internal receipt only; public_creation uses an explicit allowlist.
                        creation['generationDiagnostics'] = deepcopy(error.diagnostics)
                    creation['nextAttemptAt'] = self.clock() + details['delay']
                    await attempt._save(failed)
                    logger.warning(
                        '建档创建暂未完成: session_id=%s code=%s attempt=%s error_type=%s generation=%s',
                        session_id,
                        details['code'],
                        creation['attempts'],
                        type(error).__name__,
                        json.dumps(error.diagnostics, ensure_ascii=False)
                        if isinstance(error, RecordGenerationError)
                        else '-',
                    )
        except (QASessionConflictError, CreationExpiredError):
            return
        except Exception as error:
            logger.error('建档创建恢复中断: session_id=%s error_type=%s', session_id, type(error).__name__)

    @task_log_context(clear=True)
    async def recover(self, *, limit=20):
        if self.recovering:
            return 0
        self.recovering = True
        try:
            scheduled = 0
            for user_id, session_id in await self.store.pending(now=self.clock(), limit=limit):
                with bind_log_context(LogContext(user_id=user_id, session_id=session_id, task_id='qa-creation')):
                    current = await self.manager._load_session(user_id, session_id)
                    if is_creation(current) and self._eligible(current):
                        self._start(user_id, session_id)
                        scheduled += 1
            return scheduled
        finally:
            self.recovering = False
