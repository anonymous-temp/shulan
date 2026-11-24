"""Record questionnaire application service.

在建档的过程中，基于用户的当前信息（体检报告或其它已知的数据）
进行深度挖掘，了解用户表面未体现出来的健康信息。
"""

import asyncio
import hashlib
import json
import math
import time
from contextlib import aclosing
from contextlib import asynccontextmanager
from contextlib import suppress
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime
from typing import Optional
from uuid import uuid4

from agents import AgentEventType
from agents import AgentRequest
from agents import AgentRunContext
from agents import AgentRuntime
from agents.errors import AgentCapabilityNotFoundError
from agents.errors import AgentProtocolError
from agents.errors import AionGatewayError
from agents.graphs.record import RECORD_PLAN_CAPABILITY
from agents.graphs.record import RECORD_QUESTION_CAPABILITY
from agents.llm.reasoning import validate_reasoning_effort
from agents.llm.text_quality import check_generated_value
from modules.analysis.application.documents import DocumentParsingError
from modules.analysis.application.documents import DocumentSubmissionConflictError
from modules.record.agent import LEGACY_RECORD_AGENT_MANIFEST
from modules.record.agent import RECORD_PLAN_SELECTION
from modules.record.agent import RECORD_QUESTION_SELECTION
from modules.record.application import fallback as fallback_policy
from modules.record.application import flow as flow_policy
from modules.record.application import intake as intake_policy
from modules.record.application import interview as interview_policy
from modules.record.application.creation import QACreationCoordinator
from modules.record.application.creation import is_creation
from modules.record.application.creation import public_creation
from modules.record.application.dialogue import build_dialogue_context
from modules.record.application.output_repair import AnalysisRepair
from modules.record.application.output_repair import PresentationRepair
from modules.record.application.presentation import bounded_report_context
from modules.record.application.presentation import is_supplement_question
from modules.record.application.presentation import validate_message
from modules.record.application.public_text import public_assistant_message
from modules.record.application.public_text import public_history
from modules.record.application.public_text import public_response
from modules.record.application.public_text import strip_internal_notices
from modules.record.application.validation import RecordGenerationError
from modules.record.application.validation import repair_feedback
from modules.record.application.validation import validation_diagnostics
from modules.record.document_context import document_processing_summary
from modules.record.documents import LEGACY_RECORD_DOCUMENT_CONSUMER
from modules.record.documents import RECORD_DOCUMENT_CHECKPOINT
from modules.record.documents import RECORD_FORMAL_DOCUMENT_CHECKPOINT
from modules.record.documents import RecordDocumentFormalUnavailable
from modules.record.documents import RecordDocumentPublicationPending
from modules.record.documents import RecordDocumentVersionError
from modules.record.documents import supports_record_prepared_document_checkpoint
from modules.record.ports import HealthProfileMemory
from modules.record.ports import PromptRenderer
from modules.record.ports import RecordCompletionStore
from modules.record.ports import RecordCreationStore
from modules.record.ports import RecordDocumentConsumerRegistry
from modules.record.ports import RecordDocumentPort
from modules.record.ports import RecordPreparedDocumentPort
from modules.record.ports import RecordProfileSource
from modules.record.ports import RecordReplyRecoveryStore
from modules.record.ports import RecordSessionCache
from modules.record.ports import RecordSessionRepository
from utils.exceptions import format_exception_summary
from utils.identifiers import create_session_lease_id
from utils.log import getLogger
from utils.report_files import report_file_category
from utils.report_files import resolve_report_filename

logger = getLogger(__name__)

# Redis 会话缓存 TTL（2小时）
SESSION_CACHE_TTL = 7200

# 单次建档允许携带的报告数量与解析并发上限。
MAX_QA_REPORT_FILES = 5
QA_REPORT_PARSE_CONCURRENCY = 2

# One shared budget for analysis, retrieval, presentation and their repairs.
# The durable receipt keeps retries/recovery from starting a fresh budget.
QA_GENERATION_TIMEOUT_SECONDS = 30.0

# 报告结构化结果会完整保存在会话快照中；送入模型的文本另行限制长度，
# 避免多份大型检验报告无上限占用上下文。
MAX_QA_REPORT_CONTEXT_CHARS = 12000
MAX_QA_ATTACHED_DATA_CONTEXT_CHARS = 12000

# Redis 中保存活动问答，MongoDB 保存完成快照。
QA_STATUS_ACTIVE = 'active'
QA_STATUS_COMPLETED = 'completed'


class QASessionConflictError(ValueError):
    """问题、流程版本或会话执行权与当前状态冲突。"""

    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


class QASessionManager:
    """问答会话管理器。"""

    def __init__(
        self,
        agent_runtime: AgentRuntime,
        *,
        session_repository: RecordSessionRepository,
        session_cache: RecordSessionCache,
        prompt_renderer: PromptRenderer,
        record_profile_source: RecordProfileSource | None = None,
        record_document_port: RecordDocumentPort | None = None,
        health_profile_memory_provider: HealthProfileMemory | None = None,
        creation_store: RecordCreationStore | None = None,
        creation_task_spawner=None,
        questionnaire_gate=None,
        reply_recovery_store: RecordReplyRecoveryStore | None = None,
        completion_store: RecordCompletionStore | None = None,
    ):
        """初始化会话管理器。"""
        if not isinstance(agent_runtime, AgentRuntime):
            raise TypeError('agent_runtime must be an AgentRuntime')
        if not isinstance(session_repository, RecordSessionRepository):
            raise TypeError(
                'session_repository must implement RecordSessionRepository',
            )
        if not isinstance(session_cache, RecordSessionCache):
            raise TypeError(
                'session_cache must implement RecordSessionCache',
            )
        if not isinstance(prompt_renderer, PromptRenderer):
            raise TypeError(
                'prompt_renderer must implement PromptRenderer',
            )
        if record_profile_source is not None and not isinstance(
            record_profile_source,
            RecordProfileSource,
        ):
            raise TypeError(
                'record_profile_source must implement RecordProfileSource',
            )
        if record_document_port is not None and not isinstance(record_document_port, RecordDocumentPort):
            raise TypeError('record_document_port must implement RecordDocumentPort')
        self._agent_runtime = agent_runtime
        self._session_repository = session_repository
        self._session_cache = session_cache
        self._questionnaire_gate = questionnaire_gate
        self._prompt_renderer = prompt_renderer
        self._record_profile_source = record_profile_source
        self._record_document_port = record_document_port
        if health_profile_memory_provider is not None and not isinstance(
            health_profile_memory_provider,
            HealthProfileMemory,
        ):
            raise TypeError(
                'health_profile_memory_provider must implement HealthProfileMemory',
            )
        self._health_profile_memory_provider = health_profile_memory_provider
        if creation_store is not None and not isinstance(creation_store, RecordCreationStore):
            raise TypeError('creation_store must implement RecordCreationStore')
        self._creation = (
            QACreationCoordinator(self, creation_store, task_spawner=creation_task_spawner)
            if creation_store is not None
            else None
        )

        if (reply_recovery_store is None) != (completion_store is None):
            raise TypeError('reply recovery and immutable completion stores must be configured together')
        if reply_recovery_store is not None and not isinstance(reply_recovery_store, RecordReplyRecoveryStore):
            raise TypeError('reply_recovery_store must implement RecordReplyRecoveryStore')
        if completion_store is not None and not isinstance(completion_store, RecordCompletionStore):
            raise TypeError('completion_store must implement RecordCompletionStore')
        self._reply_recovery_store = reply_recovery_store
        self._completion_store = completion_store
        from modules.record.agent import RecordAgent

        self.record_agent = RecordAgent(self)
        self._agent_ready = True
        self._record_mutation = ContextVar(f'record-mutation-{id(self)}', default=None)
        self._record_recovery_attempt = ContextVar(f'record-recovery-{id(self)}', default=None)
        from modules.record.application.recovery import ReplyRecoveryCoordinator

        self._reply_recovery = (
            ReplyRecoveryCoordinator(self, reply_recovery_store) if reply_recovery_store is not None else None
        )

    async def _assertNoQuestionnaireOwner(self, user_id, session_id):
        gate = getattr(self, '_questionnaire_gate', None)
        if gate is not None and await gate.hasSession(user_id, session_id):
            raise QASessionConflictError(
                'Session belongs to the whole-questionnaire workflow',
                code='SESSION_OWNER_CONFLICT',
            )

    @staticmethod
    def _get_redis_session_key(user_id: str, session_id: str) -> str:
        """生成 Redis 会话键名。

        Args:
            user_id: 用户ID
            session_id: 会话ID

        Returns:
            Redis 键名
        """
        return f'shulan:session:{user_id}:{session_id}'

    async def _load_session_from_cache(self, user_id: str, session_id: str) -> Optional[dict]:
        """从缓存（Redis）加载会话数据。

        Args:
            user_id: 用户ID
            session_id: 会话ID

        Returns:
            会话数据字典，不存在返回 None
        """
        try:
            return await self._session_cache.get(user_id, session_id)
        except Exception as error:
            logger.error(f'从 Redis 加载会话失败: {error}')
            return None

    async def _save_session_to_cache(self, user_id: str, session_id: str, session_data: dict) -> bool:
        """保存会话数据到缓存（Redis）。

        Args:
            user_id: 用户ID
            session_id: 会话ID
            session_data: 会话数据

        Returns:
            是否保存成功
        """
        try:
            return await self._session_cache.save(
                user_id,
                session_id,
                session_data,
                ttl_seconds=SESSION_CACHE_TTL,
            )
        except Exception as error:
            logger.error(f'保存会话到 Redis 失败: {error}')
            return False

    async def _delete_session_from_cache(self, user_id: str, session_id: str) -> bool:
        """从缓存（Redis）删除会话数据。

        Args:
            user_id: 用户ID
            session_id: 会话ID

        Returns:
            是否删除成功
        """
        try:
            return await self._session_cache.delete(user_id, session_id)
        except Exception as error:
            logger.error(f'从 Redis 删除会话失败: {error}')
            return False

    async def hasPendingSession(self, user_id: str, session_id: str) -> bool:
        """严格检查 Redis 中是否存在尚未完成的问答会话。

        Chat 使用该方法作为 fail-closed 门禁。这里不能复用会吞掉 Redis
        异常的普通读取方法，否则 Redis 故障时可能把建档 session 当成新 Chat。
        """
        session_data = await self._session_cache.get(user_id, session_id)
        if not session_data:
            return False
        if session_data.get('type', 'qa') != 'qa' or session_data.get('status') == QA_STATUS_COMPLETED:
            return False
        completed = await self._session_repository.get(user_id, session_id)
        return not completed or not (completed.get('status') == QA_STATUS_COMPLETED or completed.get('qaCompletedAt'))

    async def assertNoPendingSession(
        self,
        user_id: str,
        session_id: str,
    ) -> None:
        """供 Chat 在创建普通会话前执行的严格问答门禁。"""
        if await self.hasPendingSession(user_id, session_id):
            raise ValueError('问答会话未完成，无法进行普通对话')

    async def _load_session(self, user_id: str, session_id: str) -> Optional[dict]:
        """加载会话数据（先 Redis 后 MongoDB）。

        Args:
            user_id: 用户ID
            session_id: 会话ID

        Returns:
            会话数据字典，不存在返回 None
        """
        cached = await self._session_cache.get(user_id, session_id)
        mutation = self._record_mutation.get()
        if mutation is not None and mutation['identity'] == (user_id, session_id):
            mutation.update(expected=deepcopy(cached), loaded=True)
        stored = await self._session_repository.get(user_id, session_id)
        if stored and (
            stored.get('status') == QA_STATUS_COMPLETED
            or (stored.get('type') == 'chat' and stored.get('qaCompletedAt'))
        ):
            return stored
        return cached or stored

    async def _save_session(
        self,
        user_id: str,
        session_id: str,
        session_data: dict,
        completed: bool = False,
        *,
        lease_guard=None,
    ) -> bool:
        """保存会话数据。

        Args:
            user_id: 用户ID
            session_id: 会话ID
            session_data: 会话数据
            completed: 会话是否已完成

        Returns:
            是否保存成功
        """
        if lease_guard is not None:
            await lease_guard()
        if self._reply_recovery_store is not None:
            attempt = self._record_recovery_attempt.get()
            if attempt is not None:
                await attempt.freeze(session_data, completed)
            else:
                await self._freezeForegroundReply(session_data, completed)
            candidate = deepcopy(session_data)
            candidate.pop('qaRecovery', None)
            if completed:
                return await self._saveCompletedFenced(candidate, lease_guard)
            await self._compareRecordSession(candidate)
            return True
        if completed:
            # 先持久化完成快照，再删除 Redis。反向顺序会产生一个既无 Redis
            # 又无 MongoDB 的窗口，Chat 可能在该窗口误创建同 session。
            session_data = dict(session_data)
            session_data.pop('_id', None)
            session_data['userId'] = user_id
            session_data['sessionId'] = session_id
            saved = await self._session_repository.save(
                user_id,
                session_id,
                session_data,
            )
            if saved:
                if not await self._delete_session_from_cache(user_id, session_id):
                    logger.error(
                        f'完成快照已写入 MongoDB，但删除 Redis 失败: user_id={user_id}, session_id={session_id}',
                    )
                logger.info(f'会话已完成，存入 MongoDB: user_id={user_id}, session_id={session_id}')
                return True
            else:
                logger.error(f'保存会话到 MongoDB 失败: user_id={user_id}, session_id={session_id}')
                return False
        else:
            # 会话未完成：保存到 Redis
            success = await self._save_session_to_cache(user_id, session_id, session_data)
            if success:
                logger.debug(f'会话未完成，保存到 Redis: user_id={user_id}, session_id={session_id}')
            return success

    def set_agent_readiness(self, ready):
        if type(ready) is not bool:
            raise TypeError('Record Agent readiness must be a boolean')
        self._agent_ready = ready

    def _requireRecordAgentAvailable(self):
        if not self._agent_ready:
            raise QASessionConflictError('建档智能体暂不可用', code='RECORD_AGENT_VERSION_UNAVAILABLE')
        for selection in (RECORD_PLAN_SELECTION, RECORD_QUESTION_SELECTION):
            try:
                self._agent_runtime.registry.get(selection.capability, selection=selection)
            except AgentCapabilityNotFoundError as error:
                raise QASessionConflictError('建档子任务版本不可用', code='RECORD_AGENT_VERSION_UNAVAILABLE') from error

    async def _freezeForegroundReply(self, result, completed):
        from modules.record.application.recovery import RECOVERY_VERSION
        from modules.record.application.recovery import reply_identity

        mutation = self._record_mutation.get()
        current = deepcopy(mutation['expected']) if mutation is not None else None
        pending = ((current or {}).get('qaFlow') or {}).get('pendingSubmission')
        if not pending or (result.get('qaFlow') or {}).get('pendingSubmission'):
            return
        if current.get('qaCommit') or (current.get('qaRecovery') or {}).get('resultJson') is not None:
            return
        frozen = deepcopy(result)
        frozen.pop('qaRecovery', None)
        current['qaRecovery'] = {
            'version': RECOVERY_VERSION,
            'agentSelection': self.record_agent.manifest,
            'identity': reply_identity(current),
            'status': 'result_ready',
            'resultJson': self._reply_recovery_store.encode_snapshot(frozen),
            'completed': completed,
            'attempts': 1,
            'maxAttempts': 1,
            'deadlineAt': pending['deadlineAt'],
            'retrySeconds': 15,
            'nextAttemptAt': 0,
            'commitAttempts': 0,
            'commitDeadlineAt': time.time() + 120,
        }
        await self._compareRecordSession(current)

    async def _compareRecordSession(self, session):
        mutation = self._record_mutation.get()
        if mutation is None or not mutation['loaded']:
            raise RuntimeError('a loaded session mutation is required')
        await mutation['guard']()
        user_id, session_id = mutation['identity']
        if not await self._reply_recovery_store.compare_session(
            user_id,
            session_id,
            mutation['expected'],
            session,
            ttl_seconds=SESSION_CACHE_TTL,
        ):
            raise QASessionConflictError('会话快照已变化，无法覆盖新的处理结果')
        mutation['expected'] = deepcopy(session)

    async def _saveCompletedFenced(self, session, guard):
        session.pop('_id', None)
        session.pop('recordCompletionId', None)
        encoded = self._reply_recovery_store.encode_snapshot(session)
        identity = hashlib.sha256(encoded.encode()).hexdigest()
        session['recordCompletionId'] = identity
        mutation = self._record_mutation.get()
        existing = (mutation['expected'] or {}).get('qaCommit')
        if existing is None:
            envelope = deepcopy(mutation['expected'])
            envelope.pop('qaRecovery', None)
            envelope['qaCommit'] = {
                'version': 'record.completion.v1',
                'commitId': identity,
                'snapshotJson': self._reply_recovery_store.encode_snapshot(session),
                'nextAttemptAt': 0,
                'status': 'pending',
                'attempts': 0,
                'maxAttempts': 8,
                'deadlineAt': time.time() + 120,
            }
            await self._compareRecordSession(envelope)
        elif existing['commitId'] != identity:
            raise QASessionConflictError('会话已有不同的完成结果')
        return await self._publishFrozenCompletion(guard)

    async def _publishFrozenCompletion(self, guard):
        mutation = self._record_mutation.get()
        envelope = deepcopy(mutation['expected'])
        intent = envelope['qaCommit']
        if intent.get('version') != 'record.completion.v1':
            intent.update(status='failed', errorCode='RECORD_COMPLETION_VERSION_UNAVAILABLE')
            await self._compareRecordSession(envelope)
            raise QASessionConflictError('不支持的建档完成版本', code='RECORD_REPLY_RECOVERY_FAILED')
        if intent.get('status') == 'failed':
            raise QASessionConflictError('完成结果自动提交已停止', code='RECORD_REPLY_RECOVERY_FAILED')
        if intent['attempts'] >= intent['maxAttempts'] or time.time() >= intent['deadlineAt']:
            intent.update(status='failed', errorCode='RECORD_COMPLETION_COMMIT_EXHAUSTED')
            await self._compareRecordSession(envelope)
            raise QASessionConflictError('完成结果自动提交已停止', code='RECORD_REPLY_RECOVERY_FAILED')
        intent.update(attempts=intent['attempts'] + 1, status='processing', nextAttemptAt=time.time() + 15)
        await self._compareRecordSession(envelope)
        snapshot = self._reply_recovery_store.decode_snapshot(intent['snapshotJson'])
        self._validateSessionProfileConsumer(snapshot)
        user_id, session_id = mutation['identity']
        try:
            await guard()
            if not await self._completion_store.publish_once(user_id, session_id, snapshot):
                raise RuntimeError('建档完成结果尚未可靠保存')
        except Exception as error:
            intent.update(
                status='failed' if isinstance(error, ValueError) else 'retry_wait',
                errorCode=type(error).__name__,
            )
            await self._compareRecordSession(envelope)
            raise
        # The immutable Mongo snapshot already contains the existing Profile
        # pending marker. Its established source worker owns eventual ingestion.
        if await self._reply_recovery_store.compare_delete(user_id, session_id, envelope):
            mutation['expected'] = None
        return True

    async def recoverPendingReplies(self, *, limit=20) -> int:
        return await self._reply_recovery.recover_pending(limit=limit) if self._reply_recovery is not None else 0

    def stopReplyRecovery(self):
        if self._reply_recovery is not None:
            self._reply_recovery.stop_accepting()

    async def _acquireSessionLease(self, user_id: str, session_id: str):
        lease_id = create_session_lease_id(user_id, session_id)
        lease = await self._agent_runtime.lease_manager.try_acquire(
            lease_id,
            str(uuid4()),
        )
        if lease is None:
            raise QASessionConflictError('当前会话正在处理另一个请求，请稍后重试')
        return lease_id, lease

    async def acquireSessionLease(self, user_id: str, session_id: str):
        """供共享同一业务 session 的其他服务获取协调 lease。"""
        return await self._acquireSessionLease(user_id, session_id)

    async def _releaseSessionLease(self, lease_id: str, lease) -> None:
        try:
            released = await self._agent_runtime.lease_manager.release(lease)
            if not released:
                logger.warning(f'会话 lease 已过期或所有者变化: {lease_id}')
        except Exception as error:
            logger.error(f'释放会话 lease 失败: lease_id={lease_id}, error={error}')

    async def releaseSessionLease(self, lease_id: str, lease) -> None:
        """释放通过 :meth:`acquireSessionLease` 获取的协调 lease。"""
        await self._releaseSessionLease(lease_id, lease)

    @asynccontextmanager
    async def _sessionLease(self, user_id: str, session_id: str):
        lease_id, lease = await self._acquireSessionLease(user_id, session_id)
        lost = asyncio.Event()

        async def ensure_owned():
            if lost.is_set():
                raise QASessionConflictError('会话锁已失效，请刷新状态后重试')
            try:
                renewed = await self._agent_runtime.lease_manager.renew(lease)
            except Exception as error:
                lost.set()
                raise QASessionConflictError('无法确认会话锁，请刷新状态后重试') from error
            if not renewed:
                lost.set()
                raise QASessionConflictError('会话锁已失效，请刷新状态后重试')

        async def heartbeat():
            interval = max(0.01, min(30, getattr(lease, 'ttl_seconds', 300) / 3))
            while True:
                await asyncio.sleep(interval)
                try:
                    await ensure_owned()
                except QASessionConflictError:
                    return

        mutation_token = self._record_mutation.set(
            {
                'identity': (user_id, session_id),
                'expected': None,
                'loaded': False,
                'guard': ensure_owned,
            }
        )
        renewal = asyncio.create_task(heartbeat())
        try:
            yield ensure_owned
        finally:
            renewal.cancel()
            with suppress(asyncio.CancelledError):
                await renewal
            await self._releaseSessionLease(lease_id, lease)
            self._record_mutation.reset(mutation_token)

    @staticmethod
    def _requireCurrentFlow(session: dict) -> None:
        if is_creation(session):
            raise QASessionConflictError('建档仍在准备中，请查询创建状态', code='CREATION_NOT_READY')
        if session.get('qaFlow', {}).get('version') != flow_policy.FLOW_VERSION:
            raise QASessionConflictError('建档流程已升级，请使用新的 sessionId 重新创建会话')

    async def createSession(
        self,
        user_id: str,
        session_id: str,
        name=None,
        file_list=None,
        hospital_data=None,
        we_run_data=None,
        doctor_product_packages=None,
        smart_device_data=None,
        basic_info=None,
    ) -> dict:
        if self._creation is not None:
            normalized = self._normalizeHealthData(
                name=name,
                file_list=file_list,
                hospital_data=hospital_data,
                we_run_data=we_run_data,
                doctor_product_packages=doctor_product_packages,
                smart_device_data=smart_device_data,
                basic_info=basic_info,
            )
            return await self._creation.create(
                user_id,
                session_id,
                {
                    'name': normalized['name'],
                    'file_list': normalized['fileList'],
                    'hospital_data': normalized['hospitalData'],
                    'we_run_data': normalized['weRunData'],
                    'doctor_product_packages': normalized['doctorProductPackages'],
                    'smart_device_data': normalized['smartDeviceData'],
                    'basic_info': dict(basic_info or {}),
                },
            )
        async with self._sessionLease(user_id, session_id) as lease_guard:
            return await self._createSessionUnderLease(
                user_id,
                session_id,
                name=name,
                file_list=file_list,
                hospital_data=hospital_data,
                we_run_data=we_run_data,
                doctor_product_packages=doctor_product_packages,
                smart_device_data=smart_device_data,
                basic_info=basic_info,
                lease_guard=lease_guard,
            )

    async def recoverPendingCreations(self) -> int:
        return await self._creation.recover() if self._creation is not None else 0

    async def _createSessionUnderLease(
        self,
        user_id: str,
        session_id: str,
        name: str = None,
        file_list: list = None,
        hospital_data: list = None,
        we_run_data: list = None,
        doctor_product_packages: list = None,
        smart_device_data: dict = None,
        basic_info: dict = None,
        lease_guard=None,
        creation_attempt=None,
    ) -> dict:
        arguments = {
            'user_id': user_id,
            'session_id': session_id,
            'name': name,
            'file_list': file_list,
            'hospital_data': hospital_data,
            'we_run_data': we_run_data,
            'doctor_product_packages': doctor_product_packages,
            'smart_device_data': smart_device_data,
            'basic_info': basic_info,
        }
        if creation_attempt is not None:
            selection = creation_attempt.session['qaCreation'].get('agentSelection', LEGACY_RECORD_AGENT_MANIFEST)
            if selection != self.record_agent.manifest:
                raise QASessionConflictError('建档执行版本不可用', code='RECORD_AGENT_VERSION_UNAVAILABLE')
        return await self.record_agent.invoke(
            'create', arguments, lease_guard=lease_guard, creation_attempt=creation_attempt
        )

    async def _createSessionUnderLeaseImpl(
        self,
        user_id: str,
        session_id: str,
        name: str = None,
        file_list: list = None,
        hospital_data: list = None,
        we_run_data: list = None,
        doctor_product_packages: list = None,
        smart_device_data: dict = None,
        basic_info: dict = None,
        lease_guard=None,
        creation_attempt=None,
    ) -> dict:
        """创建问答会话。

        Args:
            user_id: 用户ID
            session_id: 会话ID（由前端提供）
            name: 患者真实姓名
            file_list: 体检图片/报告
            hospital_data: 院内数据
            we_run_data: 微信运动数据
            doctor_product_packages: 医生产品
            smart_device_data: 华为智能设备
            basic_info: 前端明确提供的用户基本信息

        Returns:
            会话信息：
            {
                'userId': str,
                'sessionId': str,
                'status': 'question',
                'progress': str,
                'questionId': str,
                'question': str,
                'questionType': str,
                'options': dict or list,
            }

        Raises:
            ValueError: 当会话已存在且已完成时

        注意：
            本接口支持幂等性：
            - 会话不存在 → 创建新会话
            - 会话存在且未完成 → 返回现有会话状态（不创建新会话）
            - 会话存在且已完成 → 抛出异常（不允许覆盖已完成的会话）
        """
        # 检查会话是否已存在（先 Redis 后 MongoDB）
        await self._assertNoQuestionnaireOwner(user_id, session_id)
        existing_session = await self._load_session(user_id, session_id)

        if existing_session and not (creation_attempt is not None and is_creation(existing_session)):
            # 会话已存在
            if existing_session.get('type', 'qa') != 'qa':
                raise ValueError('普通对话会话不能用于创建问答')
            if existing_session['status'] == QA_STATUS_COMPLETED:
                # 已完成的会话不允许重新创建
                raise ValueError('会话已完成，不允许重新创建')
            else:
                self._requireCurrentFlow(existing_session)
                # 未完成的会话，返回当前状态（幂等）
                logger.info(f'会话已存在且未完成，返回现有状态: user_id={user_id}, session_id={session_id}')
                return self._buildSessionResponse(existing_session)

        # 在处理本次新报告前固定当前长期画像，避免后台 Worker 恰好摄取
        # 本次报告后又把它作为“既有画像”重复注入当前建档。
        checkpoint = creation_attempt.data if creation_attempt is not None else {}
        if checkpoint.get('firstQuestionSession') is not None:
            fixed = deepcopy(checkpoint['firstQuestionSession'])
            await creation_attempt.publish(fixed)
            return self._buildSessionResponse(fixed)
        self._requireRecordAgentAvailable()
        profile_consumer = self._selectRecordProfileConsumerRevision(
            creation_attempt, user_id=user_id, session_id=session_id
        )
        if 'healthProfileContext' in checkpoint:
            health_profile_context = checkpoint['healthProfileContext']
        else:
            health_profile_context = await self._loadHealthProfileContext(user_id)
            if creation_attempt is not None:
                await creation_attempt.checkpoint('processing_reports', healthProfileContext=health_profile_context)

        health_data = self._normalizeHealthData(
            name=name,
            file_list=file_list,
            hospital_data=hospital_data,
            we_run_data=we_run_data,
            doctor_product_packages=doctor_product_packages,
            smart_device_data=smart_device_data,
            basic_info=basic_info,
        )

        # 只解析本次创建请求显式携带的报告。独立上传的历史报告只能通过
        # 用户画像参与后续步骤，不能被建档会话隐式选中。
        documents = []
        document_processing = []
        health_reports = await self._processHealthReports(
            user_id=user_id,
            session_id=session_id,
            file_list=health_data['fileList'],
            documents=documents,
            document_processing=document_processing,
            creation_attempt=creation_attempt,
        )

        extracted_info = self._extractBasicInfo(health_reports)
        extracted_info_sources = {
            field: 'health_report' for field, value in extracted_info.items() if value is not None and value != ''
        }
        explicit_info = {
            key: value
            for key, value in dict(basic_info or {}).items()
            if key in ('name', 'gender', 'age', 'height', 'weight')
        }
        if name is not None:
            explicit_info['name'] = name
        for field in ('name', 'gender', 'age', 'height', 'weight'):
            value = explicit_info.get(field)
            if value is not None and value != '':
                extracted_info[field] = value
                extracted_info_sources[field] = 'user_input'

        session_data = {
            'sessionId': session_id,
            'userId': user_id,
            'type': 'qa',  # 标记为问答类型
            'status': QA_STATUS_ACTIVE,
            'createdAt': datetime.now().isoformat(),
            'currentQuestion': None,
            'qaHistory': [],
            'qaFlow': flow_policy.new_flow(),
            'round': 1,
            'hasHealthReport': bool(health_reports),
            # 当前画像只在创建时读取一次，保证同一 QA 会话依据稳定。
            'healthProfileContext': health_profile_context,
            # 当前建档报告的固定结构化快照。继续问答和生成后续问题只能
            # 使用这里的数据，不能重新查询用户后来上传的报告。
            'parsedHealthReports': health_reports,
            'documents': documents,
            'documentProcessing': document_processing,
            'extractedInfo': extracted_info,
            # 画像摄取必须区分报告抽取值和用户明确输入，不能仅凭最终
            # merged 值把报告数据升级成“用户已确认”。
            'extractedInfoSources': extracted_info_sources,
            'healthData': health_data,  # 保存用户建档数据
            'recordProfileConsumerRevision': profile_consumer,
        }

        if creation_attempt is not None:
            # The active session replaces the temporary creation receipt. Retain
            # only the fixed provenance/receipts, not another copy of report bodies.
            projections = [
                deepcopy(saved['documentProjection'])
                for index in range(len(health_data['fileList']))
                if (saved := creation_attempt.data.get('files', {}).get(str(index), {})).get('documentProjection')
            ]
            if projections:
                session_data['recordDocumentProjections'] = projections
            await creation_attempt.checkpoint('generating_question')
        async for event in self._generateTurn(session_data, user_id=user_id):
            if event['type'] == 'draft':
                session_data = event['session']
        if creation_attempt is not None:
            await creation_attempt.checkpoint('publishing_question', firstQuestionSession=session_data)
            await creation_attempt.publish(session_data)
        elif not await self._save_session(user_id, session_id, session_data, lease_guard=lease_guard):
            raise RuntimeError('保存会话失败')
        return self._buildSessionResponse(session_data)

    async def continueSession(self, user_id, session_id, round_number, thinking=False) -> dict:
        raise QASessionConflictError('继续问答接口已弃用，请通过答案提交接口继续当前会话')

    async def completeSession(self, user_id, session_id, round_number) -> dict:
        async with self._sessionLease(user_id, session_id):
            session = await self._load_session(user_id, session_id)
            if session is None:
                raise ValueError('会话不存在')
            completed = session.get('status') == QA_STATUS_COMPLETED or (
                session.get('type') == 'chat' and session.get('qaCompletedAt')
            )
            if not completed:
                self._requireCurrentFlow(session)
                raise QASessionConflictError('建档尚未自动完成，请提交当前问题的回答')
            if session.get('qaRound', session.get('round', 1)) != round_number:
                raise QASessionConflictError('轮次不匹配，请刷新会话状态')
            await self._ensureRecordProfileQueued(user_id, session_id)
            return self._buildCompletedResponse(session)

    async def _ensureRecordProfileQueued(self, user_id, session_id) -> None:
        if self._record_profile_source is None:
            return
        try:
            await self._record_profile_source.ensure_queued(user_id=user_id, session_id=session_id)
        except Exception as error:
            logger.error('画像任务投递失败，保留完成快照待补偿: %s', type(error).__name__)

    @staticmethod
    def _buildCompletedResponse(session_data: dict) -> dict:
        flow = session_data.get('qaFlow', {})
        return {
            'userId': session_data['userId'],
            'sessionId': session_data['sessionId'],
            'status': 'finished',
            'round': session_data.get('qaRound', session_data.get('round', 1)),
            'progress': '100%',
            'phase': 'finished',
            'interactionCount': flow.get('interactionCount', len(session_data.get('qaHistory', []))),
            'intakeQuestionCount': flow.get('intakeQuestionCount', flow.get('intakeCount', 0)),
            'qaRoundCount': flow.get('qaRoundCount', 0),
            'assistantMessage': public_assistant_message(flow.get('finalMessage', ''), completed=True),
            'reasoning': flow.get('finalReasoning', ''),
            'endReason': flow.get('endReason'),
            'completedAt': session_data.get('qaCompletedAt') or session_data.get('completedAt', ''),
            'documents': deepcopy(session_data.get('documents', [])),
            'documentProcessing': deepcopy(session_data.get('documentProcessing', [])),
        }

    def _buildSessionResponse(self, session_data: dict) -> dict:
        if is_creation(session_data):
            return public_creation(session_data)
        if session_data.get('status') == QA_STATUS_COMPLETED or (
            session_data.get('type') == 'chat' and session_data.get('qaCompletedAt')
        ):
            return self._buildCompletedResponse(session_data)
        self._requireCurrentFlow(session_data)
        # Derive counters for old version-2 snapshots without migrating a read.
        flow = dict(session_data['qaFlow'])
        interview_policy.ensure_counters({'qaFlow': flow, 'qaHistory': session_data.get('qaHistory', [])})
        current = session_data['currentQuestion']
        return public_response(
            {
                'userId': session_data['userId'],
                'sessionId': session_data['sessionId'],
                'status': 'question',
                'round': 1,
                'phase': flow['phase'],
                'interactionCount': flow['interactionCount'],
                'intakeQuestionCount': flow.get('intakeQuestionCount', flow.get('intakeCount', 0)),
                'qaRoundCount': flow.get('qaRoundCount', 0),
                'progress': interview_policy.progress(flow),
                **{key: current[key] for key in ('questionId', 'question', 'questionType', 'options')},
                'assistantMessage': current.get('assistantMessage', ''),
                'reasoning': current.get('reasoning', ''),
                'documents': deepcopy(session_data.get('documents', [])),
                'documentProcessing': deepcopy(session_data.get('documentProcessing', [])),
            },
            flow=flow,
            question=current,
        )

    async def submitAnswer(
        self,
        user_id,
        session_id,
        question_id,
        answer=None,
        other=None,
        thinking=False,
        reasoning_effort='low',
    ) -> dict:
        result = None
        async for event in self._submitTurn(
            user_id, session_id, question_id, answer, other, thinking=thinking, reasoning_effort=reasoning_effort
        ):
            if event['type'] == 'result':
                result = event['result']
        if result is not None:
            return result
        raise RuntimeError('未生成会话结果')

    async def _submitTurn(
        self,
        user_id,
        session_id,
        question_id,
        answer=None,
        other=None,
        *,
        thinking=False,
        reasoning_effort='low',
        stream=False,
    ):
        async with aclosing(
            self.record_agent.submit(
                {
                    'user_id': user_id,
                    'session_id': session_id,
                    'question_id': question_id,
                    'answer': answer,
                    'other': other,
                    'thinking': thinking,
                    'reasoning_effort': reasoning_effort,
                    'stream': stream,
                }
            )
        ) as events:
            async for event in events:
                yield event

    async def _submitTurnImpl(
        self,
        user_id,
        session_id,
        question_id,
        answer=None,
        other=None,
        *,
        thinking=False,
        reasoning_effort='low',
        stream=False,
    ):
        validate_reasoning_effort(reasoning_effort)
        if not isinstance(thinking, bool):
            raise ValueError('thinking 必须为布尔值')
        final_answer = other if other else answer
        if final_answer is None or not isinstance(final_answer, (str, dict, list)):
            raise ValueError('answer 和 other 至少提供一个有效回答')
        if not final_answer or (isinstance(final_answer, str) and not final_answer.strip()):
            raise ValueError('回答不能为空')
        fingerprint = hashlib.sha256(flow_policy.answer_text(final_answer).encode('utf-8')).hexdigest()
        async with self._sessionLease(user_id, session_id) as lease_guard:
            session = await self._load_session(user_id, session_id)
            if session is None:
                raise ValueError('会话不存在')
            mutation = self._record_mutation.get()
            if self._reply_recovery_store is not None and (mutation['expected'] or {}).get('qaCommit'):
                await self._publishFrozenCompletion(lease_guard)
                await self._ensureRecordProfileQueued(user_id, session_id)
                session = await self._load_session(user_id, session_id)
            self._requireCurrentFlow(session)
            last = session['qaFlow'].get('lastSubmission') or {}
            if last.get('questionId') == question_id:
                if last.get('answerHash') != fingerprint:
                    raise QASessionConflictError('该问题已提交不同答案，请刷新会话状态')
                yield {
                    'type': 'result',
                    'result': public_response(
                        last['result'], flow=session['qaFlow'], question=session.get('currentQuestion')
                    ),
                }
                return
            if session.get('type', 'qa') != 'qa' or session.get('status') == QA_STATUS_COMPLETED:
                raise QASessionConflictError('建档已完成，请查询会话状态')
            current = session.get('currentQuestion')
            if not current or current.get('questionId') != question_id:
                raise QASessionConflictError('questionId 已过期或不匹配，请刷新会话状态')
            receipt = session['qaFlow'].get('pendingSubmission')
            if receipt is not None:
                self._validateSubmissionReceipt(session, receipt)
                if receipt['answerHash'] != fingerprint:
                    raise QASessionConflictError('该问题已提交不同答案，请刷新会话状态')
            else:
                self._requireRecordAgentAvailable()
                session = deepcopy(session)
                session['qaFlow']['pendingSubmission'] = {
                    'question': deepcopy(current),
                    'answer': deepcopy(final_answer),
                    'answerHash': fingerprint,
                    'thinking': thinking,
                    'reasoningEffort': reasoning_effort,
                    'createdAt': datetime.now().isoformat(),
                    'deadlineAt': time.time() + QA_GENERATION_TIMEOUT_SECONDS,
                    'agentSelection': self.record_agent.manifest,
                }
                if not await self._save_session(user_id, session_id, session, lease_guard=lease_guard):
                    raise RuntimeError('保存回答失败，请重试')
            async with aclosing(
                self._completeSubmission(session, user_id, session_id, lease_guard, stream=stream)
            ) as events:
                async for event in events:
                    yield event

    @staticmethod
    def _validateSubmissionReceipt(session: dict, receipt: dict) -> None:
        from modules.record.agent import RECORD_AGENT_MANIFEST

        if (
            isinstance(receipt, dict)
            and receipt.get('agentSelection', LEGACY_RECORD_AGENT_MANIFEST) != RECORD_AGENT_MANIFEST
        ):
            raise QASessionConflictError('建档执行版本不可用', code='RECORD_AGENT_VERSION_UNAVAILABLE')
        current = session.get('currentQuestion') or {}
        if (
            not isinstance(receipt, dict)
            or not isinstance(receipt.get('question'), dict)
            or receipt['question'].get('questionId') != current.get('questionId')
            or not current.get('questionId')
            or not isinstance(receipt.get('answer'), (str, dict, list))
            or not receipt['answer']
            or not isinstance(receipt.get('deadlineAt'), (int, float))
            or isinstance(receipt.get('deadlineAt'), bool)
            or not math.isfinite(receipt['deadlineAt'])
            or not isinstance(receipt.get('createdAt'), str)
        ):
            raise QASessionConflictError('已保存的回答回执无效，请刷新会话状态')
        fingerprint = hashlib.sha256(flow_policy.answer_text(receipt['answer']).encode('utf-8')).hexdigest()
        if receipt.get('answerHash') != fingerprint:
            raise QASessionConflictError('已保存的回答不一致，无法恢复回复')
        validate_reasoning_effort(receipt.get('reasoningEffort', 'low'))
        if not isinstance(receipt.get('thinking', False), bool):
            raise QASessionConflictError('已保存的回答参数无效，无法恢复回复')

    async def _completeSubmission(self, session, user_id, session_id, lease_guard, *, stream=False, recovery=False):
        """Publish a durable pending answer and its successor exactly once."""
        if session.get('userId') != user_id or session.get('sessionId') != session_id:
            raise QASessionConflictError('会话身份不匹配，无法恢复回复')
        receipt = session['qaFlow']['pendingSubmission']
        self._validateSubmissionReceipt(session, receipt)
        recovery_receipt = session.get('qaRecovery') or {}
        if recovery_receipt.get('status') in {'exhausted', 'failed'}:
            raise QASessionConflictError('回复自动恢复已停止，请联系工作人员处理', code='RECORD_REPLY_RECOVERY_FAILED')
        if recovery_receipt.get('resultJson') is not None and self._reply_recovery_store is not None:
            from modules.record.application.recovery import reply_identity

            if recovery_receipt.get('identity') != reply_identity(session):
                raise QASessionConflictError('回复恢复身份不匹配')
            draft = self._reply_recovery_store.decode_snapshot(recovery_receipt['resultJson'])
            await self._save_session(
                user_id, session_id, draft, completed=recovery_receipt['completed'], lease_guard=lease_guard
            )
            if recovery_receipt['completed']:
                await self._ensureRecordProfileQueued(user_id, session_id)
            yield {'type': 'result', 'result': self._buildSessionResponse(draft), 'session': draft}
            return
        async with aclosing(
            self._generateTurn(
                session,
                user_id=user_id,
                answer=receipt['answer'],
                thinking=False if recovery else receipt.get('thinking', False),
                reasoning_effort=receipt.get('reasoningEffort', 'low'),
                stream=stream,
                deadline_at=receipt['deadlineAt'],
            )
        ) as events:
            async for event in events:
                if event['type'] != 'draft':
                    yield event
                    continue
                draft = event['session']
                flow = draft['qaFlow']
                question_id = receipt['question']['questionId']
                draft['qaHistory'].append(
                    {
                        'question': deepcopy(receipt['question']),
                        'answer': deepcopy(receipt['answer']),
                        'answeredAt': receipt['createdAt'],
                        'interactionCount': flow['interactionCount'],
                    }
                )
                draft.setdefault('answers', {})[question_id] = deepcopy(receipt['answer'])
                completed = flow['phase'] == 'finished'
                if completed:
                    draft['status'] = QA_STATUS_COMPLETED
                    draft['completedAt'] = datetime.now().isoformat()
                    draft.pop('healthProfileContext', None)
                    self._prepareRecordProfileSourceSnapshot(draft)
                result = self._buildSessionResponse(draft)
                flow['lastSubmission'] = {
                    'questionId': question_id,
                    'answerHash': receipt['answerHash'],
                    'result': deepcopy(result),
                }
                flow.pop('pendingSubmission', None)
                if not await self._save_session(
                    user_id, session_id, draft, completed=completed, lease_guard=lease_guard
                ):
                    raise RuntimeError('保存会话失败，请查询状态后重试')
                if completed:
                    await self._ensureRecordProfileQueued(user_id, session_id)
                yield {'type': 'result', 'result': result, 'session': draft}

    async def submitAnswerStream(
        self,
        user_id,
        session_id,
        question_id,
        answer=None,
        other=None,
        thinking=False,
        reasoning_effort='low',
    ):
        request_id = uuid4().hex
        started = datetime.now()

        def envelope(data, code=200):
            return {
                'code': code,
                'message': 'success' if code == 200 else data.get('error', 'error'),
                'uuid': request_id,
                'timestamp': int(datetime.now().timestamp()),
                'elapsed_ms': int((datetime.now() - started).total_seconds() * 1000),
                'data': {'userId': user_id, 'sessionId': session_id, **data},
            }

        yield envelope({'type': 'start'})
        try:
            async with aclosing(
                self._submitTurn(
                    user_id,
                    session_id,
                    question_id,
                    answer,
                    other,
                    thinking=thinking,
                    reasoning_effort=reasoning_effort,
                    stream=True,
                )
            ) as events:
                async for event in events:
                    if event['type'] == 'reasoning':
                        yield envelope({'type': 'reasoning', 'content': event['content']})
                    elif event['type'] == 'result':
                        result = event['result']
                        if result['status'] == 'question':
                            yield envelope({'type': 'question', **result})
                            yield envelope({'type': 'finish'})
                        else:
                            yield envelope({'type': 'finish', **result})
        except QASessionConflictError as error:
            yield envelope({'type': 'error', 'error': str(error)}, 409)
        except RecordGenerationError as error:
            yield envelope({'type': 'error', 'error': str(error)}, 502)
        except ValueError as error:
            yield envelope({'type': 'error', 'error': str(error)}, 400)
        except Exception as error:
            logger.error('动态建档提交失败: %s', type(error).__name__)
            yield envelope({'type': 'error', 'error': '生成或保存失败，请查询状态后重试'}, 500)

    def _patientContext(self, session: dict, answer=None) -> dict:
        # Deliberate allowlist: never serialize qaFlow or numbered history into
        # the patient-analysis request. Operational state belongs to code.
        # Only our labels are localized; patient quotes and source data stay intact.
        health_reports = self._getSessionHealthReports(session)
        reports, status = self._formatHealthReports(health_reports)
        focus = session['qaFlow'].get('focus') or {}
        return {
            '本次报告': reports if health_reports else None,
            '附件资料状态': [
                {'fileIndex': item['fileIndex'], 'status': item['status']}
                for item in (session.get('documentProcessing') or [])
            ],
            '报告总体情况': {
                'abnormal': '存在异常提示',
                'normal': '未见明显异常',
                'pending': '待确认',
                'unknown': '未知',
            }.get(status, '未知')
            if health_reports
            else None,
            '长期健康画像': session.get('healthProfileContext') or None,
            '附带健康资料': self._formatAttachedHealthData(session.get('healthData')) or None,
            '基本信息': self._formatBasicInfo(session.get('extractedInfo')),
            '历史问答': [
                {'问题': strip_internal_notices(item['question']['question']), '用户回答': item['answer']}
                for item in session.get('qaHistory', [])
            ],
            '当前问题': strip_internal_notices((session.get('currentQuestion') or {}).get('question')),
            '本次回答': answer,
            '当前关注': {
                '主题': focus['topic'],
                '角度': flow_policy.ASPECT_LABELS[focus['aspect']],
            }
            if focus
            else {},
            '已有疑问': [
                {
                    '编号': item['id'],
                    '问题': item['text'],
                    '用户原话': item['evidence'],
                    '处理状态': {
                        'pending': '尚未回应',
                        'answered': '已回应',
                        'limited': '已作一般说明',
                    }.get(item['status'], '尚未回应'),
                    '已有回应': strip_internal_notices(item.get('response')),
                }
                for item in session['qaFlow']['concerns']
            ],
            '尚未回应的疑问编号': [item['id'] for item in session['qaFlow']['concerns'] if item['status'] == 'pending'],
            **intake_policy.patient_projection(session['qaFlow']),
        }

    def _presentationContext(self, draft: dict, previous: dict, answer, target=None) -> dict:
        """Keep the answered question separate from the newly selected purpose."""
        patient = self._patientContext(draft, answer)
        patient.pop('当前问题', None)
        patient['上一题'] = {'问题': strip_internal_notices((previous.get('currentQuestion') or {}).get('question'))}
        questions = [item['question'] for item in previous.get('qaHistory', [])]
        if previous.get('currentQuestion'):
            questions.append(previous['currentQuestion'])
        shown = []
        for question in questions:
            text = strip_internal_notices(question.get('acknowledgementText', question.get('assistantMessage', '')))
            if text and text not in shown:
                shown.append(text)
        # Expression-only reference. It must never become patient evidence or
        # feed the intake/health-profile extraction projection.
        patient['已展示说明'] = shown
        patient['已展示答复'] = [
            {'疑问编号': item['id'], '问题': item['text'], '答复': cleaned}
            for item in previous['qaFlow']['concerns']
            for text in dict.fromkeys(
                [*item.get('responseHistory', [])]
                + ([item['response']] if item.get('status') != 'pending' and item.get('response') else [])
            )
            if (cleaned := strip_internal_notices(text))
        ]
        patient.update(
            build_dialogue_context(
                patient,
                previous,
                target,
                intake=draft['qaFlow']['intake'],
                reports=self._getSessionHealthReports(draft),
            )
        )
        return patient

    @staticmethod
    def _presentationRetrievalQuery(patient: dict, decision: dict) -> str:
        """Avoid another model call for routine intake; retain evidence routing."""
        responses = [item for item in decision['responses'] if not item.get('reuseResponse')]
        abnormal_report = patient.get('报告总体情况') == '存在异常提示'
        if not responses and not abnormal_report:
            return ''
        # The response task already contains the interpreted concerns and target.
        # Do not send the whole patient history through a second understanding pass.
        return json.dumps(
            {
                'responseTask': {**decision, 'responses': responses},
                '本次回答': patient.get('本次回答'),
                '报告总体情况': patient.get('报告总体情况'),
                '异常报告资料': patient.get('本次报告') if abnormal_report else None,
            },
            ensure_ascii=False,
        )

    async def _generateTurn(
        self,
        session: dict,
        *,
        user_id: str,
        answer=None,
        thinking=False,
        stream=False,
        answer_already_recorded=False,
        reasoning_effort='low',
        deadline_at=None,
    ):
        validate_reasoning_effort(reasoning_effort)
        self._validateSessionProfileConsumer(session)
        controlled = fallback_policy.handle_control_answer(
            session, answer, answer_already_recorded=answer_already_recorded
        )
        if controlled is not None:
            yield {'type': 'draft', 'session': controlled}
            return
        completed = flow_policy.finish_without_more(session, answer)
        if completed is not None:
            yield {'type': 'draft', 'session': completed}
            return
        self._requireRecordAgentAvailable()
        started = asyncio.get_running_loop().time()
        remaining = (
            QA_GENERATION_TIMEOUT_SECONDS
            if deadline_at is None
            else min(QA_GENERATION_TIMEOUT_SECONDS, max(0, deadline_at - time.time()))
        )
        deadline = started + remaining
        patient = self._patientContext(session, answer)
        prompt = self._prompt_renderer.render(
            'record/context.txt',
            patient_context=json.dumps(patient, ensure_ascii=False),
        )
        # Preserve the original first-attempt reasoning, including when its
        # structured analysis needs repair. It is a process record, not evidence.
        reasoning_parts = []
        source_text = json.dumps(
            [patient[key] for key in ('本次报告', '长期健康画像', '附带健康资料', '基本信息', '本次回答')]
            + [item['用户回答'] for item in patient['历史问答']],
            ensure_ascii=False,
        )
        analysis_repair = AnalysisRepair(session, answer, sources=patient, source_text=source_text)
        presentation_repair = None
        draft = decision = None
        analysis_partial = False
        failure_kind = 'validation'
        repaired = False
        for attempt in range(2):
            context = AgentRunContext.create(user_id=user_id, session_id=session['sessionId'])
            expose_reasoning = thinking and attempt == 0
            request = AgentRequest(
                {'prompt': prompt, 'thinking': expose_reasoning, 'reasoning_effort': reasoning_effort, 'stream': stream}
            )
            try:
                if stream:
                    chunks = []
                    async with self._ownedModelStream(
                        self._agent_runtime.hub.stream(
                            RECORD_PLAN_CAPABILITY, request, context, selection=RECORD_PLAN_SELECTION
                        ),
                        deadline,
                    ) as next_event:
                        while True:
                            try:
                                event = await next_event()
                            except StopAsyncIteration:
                                break
                            if event.type is AgentEventType.CONTENT_DELTA:
                                chunks.append(event.data['content'])
                            elif event.type is AgentEventType.REASONING_DELTA and expose_reasoning:
                                reasoning_parts.append(event.data['content'])
                                yield {'type': 'reasoning', 'content': event.data['content']}
                    content = ''.join(chunks)
                else:
                    result = await self._awaitModel(
                        self._agent_runtime.hub.invoke(
                            RECORD_PLAN_CAPABILITY, request, context, selection=RECORD_PLAN_SELECTION
                        ),
                        deadline,
                    )
                    content = result.data.get('content', '')
                    if expose_reasoning:
                        reasoning_parts.append(result.data.get('reasoning_content', '') or '')
            except (AionGatewayError, AgentProtocolError, TimeoutError) as error:
                failure_kind = 'timeout' if isinstance(error, TimeoutError) else 'upstream'
                break
            try:
                assessment = (
                    analysis_repair.ingest(content)
                    if attempt
                    else flow_policy.parse_model_object(content, flow_policy.PatientAssessment, source_text=source_text)
                )
                if assessment is None:
                    raise ValueError('analysis repair incomplete')
                flow_policy.validate_assessment(assessment, session, answer, sources=patient)
                draft, decision = flow_policy.advance_flow(
                    session, assessment, answer=answer, answer_already_recorded=answer_already_recorded
                )
                analysis_partial = bool(attempt and analysis_repair.is_partial and not analysis_repair.last_complete)
                break
            except ValueError as error:
                repaired = True
                diagnostics = self._logGenerationRejection(
                    error,
                    schema=flow_policy.PatientAssessment,
                    stage='analysis',
                    attempt=attempt + 1,
                    context=context,
                    content=content,
                )
                # Normalization and merging retain valid items without asking a
                # model to regenerate them. Cross-field policy still validates.
                assessment = analysis_repair.ingest(content)
                logger.warning(
                    'Record analysis repair: session_id=%s details=%s',
                    session['sessionId'],
                    json.dumps(analysis_repair.diagnostics, ensure_ascii=False),
                )
                if assessment is not None and (attempt or not analysis_repair.is_partial):
                    try:
                        flow_policy.validate_assessment(assessment, session, answer, sources=patient)
                        draft, decision = flow_policy.advance_flow(
                            session, assessment, answer=answer, answer_already_recorded=answer_already_recorded
                        )
                        analysis_partial = analysis_repair.is_partial and not analysis_repair.last_complete
                        break
                    except ValueError:
                        pass
                if attempt:
                    break
                prompt += (
                    '\n仅修正未通过校验的字段或条目，返回完整 JSON；保留已有有效内容，'
                    '不要重新编号、猜测 ID、补造患者事实。检查疑问依据、实际来源和信息缺口。'
                    '保留回答的存在性、具体程度和否认范围；名称未明确时实体留空，不能改为没有或追加澄清追问。'
                )
                prompt += repair_feedback(diagnostics, content=content)
                prompt += '\n局部校验结果：' + json.dumps(analysis_repair.diagnostics, ensure_ascii=False)
        if draft is None:
            result = fallback_policy.build_fallback(
                session,
                answer=answer,
                answer_already_recorded=answer_already_recorded,
                partial_analysis=analysis_repair,
                reasoning=''.join(reasoning_parts),
                failure_kind=failure_kind,
            )
            self._logGenerationOutcome(result, started, failure_kind)
            yield {'type': 'draft', 'session': result}
            return
        presentation_patient = self._presentationContext(draft, session, answer, decision.get('target'))
        presentation_prompt = json.dumps(
            {'patient': presentation_patient, 'responseTask': decision}, ensure_ascii=False
        )
        presentation_repair = PresentationRepair(decision, presentation_patient, source_text=source_text)
        knowledge_context = None

        def publish(presentation):
            candidate = self._validatedPresentation(
                draft, decision, presentation, presentation_patient, source_text, ''.join(reasoning_parts)
            )
            if analysis_partial:
                return fallback_policy.build_fallback(
                    session,
                    answer=answer,
                    answer_already_recorded=answer_already_recorded,
                    validated_draft=draft,
                    decision=decision,
                    analysis_partial=True,
                    presentation_patient=presentation_patient,
                    partial_presentation={
                        'responses': presentation.responses,
                        'assistant_message': presentation.assistantMessage,
                        'question': presentation.question,
                    },
                    reasoning=''.join(reasoning_parts),
                )
            fallback_policy.mark_generation_success(candidate, repaired=repaired)
            return candidate

        for attempt in range(2):
            context = AgentRunContext.create(user_id=user_id, session_id=session['sessionId'])
            try:
                result = await self._awaitModel(
                    self._agent_runtime.hub.invoke(
                        RECORD_QUESTION_CAPABILITY,
                        AgentRequest(
                            {
                                'prompt': presentation_prompt,
                                'thinking': False,
                                'retrieval_query': self._presentationRetrievalQuery(presentation_patient, decision),
                                'retain_knowledge_context': True,
                                'reuse_knowledge': bool(attempt),
                                'knowledge_context': knowledge_context,
                            }
                        ),
                        context,
                        selection=RECORD_QUESTION_SELECTION,
                    ),
                    deadline,
                )
                knowledge_context = result.data.get('knowledge_context')
            except (AionGatewayError, AgentProtocolError, TimeoutError) as error:
                failure_kind = 'timeout' if isinstance(error, TimeoutError) else 'upstream'
                break
            content = result.data.get('content', '')
            try:
                presentation = (
                    presentation_repair.ingest(content)
                    if attempt
                    else flow_policy.parse_model_object(content, flow_policy.TurnPresentation, source_text=source_text)
                )
                if presentation is None:
                    raise ValueError('presentation repair incomplete')
                candidate = publish(presentation)
                self._logGenerationOutcome(candidate, started)
                yield {'type': 'draft', 'session': candidate}
                return
            except ValueError as error:
                repaired = True
                diagnostics = self._logGenerationRejection(
                    error,
                    schema=flow_policy.TurnPresentation,
                    stage='presentation',
                    attempt=attempt + 1,
                    context=context,
                    content=content,
                )
                presentation = presentation_repair.ingest(content)
                logger.warning(
                    'Record presentation repair: session_id=%s details=%s',
                    session['sessionId'],
                    json.dumps(presentation_repair.diagnostics, ensure_ascii=False),
                )
                if presentation is not None:
                    try:
                        candidate = publish(presentation)
                    except ValueError:
                        pass
                    else:
                        self._logGenerationOutcome(candidate, started)
                        yield {'type': 'draft', 'session': candidate}
                        return
                if attempt:
                    break
                presentation_prompt += (
                    '\n请修正完整 JSON：逐项回应 responseTask.responses 中所有 ID，严格遵守 action 的提问要求。'
                    '修正乱码、混杂字符和不可见控制字符，保留正确的医学术语；选项不含操作提示且清理后不能重复。'
                    '快捷选项只提供明确答案，不能含其他、其它或补充、更正等输入入口占位；'
                    '题干、承接、理由和答复不得提示在按钮中填写或点击后输入、展开输入框。'
                    'assistantMessage 的 acknowledgement 通常是一至两句简洁、主动、连贯的交流，'
                    '允许面向用户表达帮助分析困扰、了解可以改善方面的意图，不承诺疗效或确定病因；'
                    '承接不用我们一起看看、和您一起了解等空泛陪伴话术，'
                    '减少无实际连接含义的也；不机械替换为一并、共同或逐句以我想开头。'
                    '真实合并考虑等语义中的一起可以保留，不逐字禁用一起或也，'
                    '不对固定自由回答提示、QA正文或用户原话作这类词语清理。'
                    'rationale 留空且不展示，evidence 保留实际来源。'
                    '不要科普、给建议、解释采集方法或泛泛介绍主题的重要性；'
                    '可简短引用真实关注点，不复述整段背景，不写已记录回执。'
                    '实际提问只放question；个人疾病史主要承接主诉，家族史宽泛衔接且focusRef=null。'
                    '生活题依据真实关联和前文自然展开，明确引用时选相关focusRef，关联相当时交替关注点；'
                    '上下文已建立且未明确引用时允许null，不强迫重新点名主诉或说明帮助目的。'
                    '结合最近完整对话和已展示说明避免重复开头、句式或关注点，不能只更换名词或同义词，'
                    '不强制关注点加话题切换的结构；历史AI表达仅用于衔接和去重，不是事实来源。'
                    '自然延续不是先总结上题再宣布下题，不写聊得差不多了、已经清楚了；'
                    '用户记不清时直接推进，不重复安慰、先放一放或先不急着确认，也不逐轮强调记录和信息用途。'
                    'acknowledgement只是字段名，不要求确认上一条回答；不写我知道了、心里有数了，'
                    '不以某习惯与某关注有关、很重要等泛泛健康关联代替面向用户的交流。'
                    '缺少报告或疾病信息时自然关心日常生活，不预设患病。'
                    '实际疑问直接简答一般影响、生活场景与建议，不要求用户补齐资料；'
                    '任何句子均不强调现有信息不足或因而无法判断，即使后面已有建议也必须改写。'
                    '有正式答复时 acknowledgement、rationale 置空，由答复首句自然承接，不重复列举病史、亲属或诉求。'
                    '仅回应本轮指定疑问，主动重问时参考 previousResponse 简短补充，不能原样重复历史答复。'
                    '上一题及本次回答只作交流语境，不复述刚回答的数量、行为或记不清等状态，也不评价其好坏。'
                    '提问轮evidence仅列实际使用的依据，没有引用事实的自然延续或中性引入可为空；'
                    '选focusRef时附其source和quote，'
                    '不为引用历史目标或报告而另加本次回答回执。首次自然邀请表达关注，不编造用户回答；'
                    '不改写数值、单位、否认或不确定性，不把内部选题理由拼成公开说明。'
                    '所有新问题都使用 radio 或 checkbox，并提供至少两个匹配的快捷选项及自由补充空间，不能使用 text。'
                    'questionMode=fixed 时 question=null、rationale 为空，使用服务端邀请；尊重背景补充意愿。'
                    '报告已有的内容不重复询问；允许了解其他科室主诉的相关背景，不默认转向排便或预设菌群治疗效果。'
                )
                presentation_prompt += repair_feedback(diagnostics, content=content)
                presentation_prompt += (
                    '\n保留已通过校验的内容，仅修正缺失或冲突部分，不要按顺序猜测 ID。局部校验结果：'
                    + json.dumps(presentation_repair.diagnostics, ensure_ascii=False)
                )
                if decision['action'] == 'open_intake':
                    presentation_prompt += (
                        '未明确主诉时首题使用 checkbox，不能使用 text；选择题至少需要两个非空且不重复的字符串选项，'
                        f'最多 {flow_policy.OPENING_MAX_OPTIONS} 个。按相关内容决定数量，不必凑满。'
                        '首题承接本次关注和健康目标，不限定为消化或排便体验；已有明确诉求不再询问同一诉求。'
                        '主诉选项使用程序提供的具体候选与明确状态，不能新增泛健康方向；不要求用户先有症状。'
                    )
                presentation_prompt += (
                    '所有题目的按钮都使用简短、独立、可直接记录的答案；'
                    '不能使用某个/某种、括号说明、斜杠列举不同答案或多个行为拼接。'
                    '数量范围保留单位，不把未知内容或未选择项目推断成没有；选项外内容通过文字或语音回答。'
                )
        result = fallback_policy.build_fallback(
            session,
            answer=answer,
            answer_already_recorded=answer_already_recorded,
            validated_draft=draft,
            decision=decision,
            analysis_partial=analysis_partial,
            presentation_patient=presentation_patient,
            partial_presentation={
                'responses': presentation_repair.responses,
                'assistant_message': presentation_repair.assistant_message,
                'question': presentation_repair.question,
            },
            reasoning=''.join(reasoning_parts),
            failure_kind=failure_kind,
        )
        self._logGenerationOutcome(result, started, failure_kind)
        yield {'type': 'draft', 'session': result}

    @asynccontextmanager
    async def _ownedModelStream(self, events, deadline):
        pending = None

        async def next_event():
            nonlocal pending
            pending = asyncio.ensure_future(anext(events))
            return await self._awaitModel(pending, deadline)

        def close_when_stopped(_):
            # A provider can acknowledge cancellation late. Close only after its
            # outstanding anext stops, without extending the generation budget.
            task = asyncio.create_task(events.aclose())

            def observe(closed):
                if not closed.cancelled() and closed.exception() is not None:
                    logger.warning('Record model stream close failed: %s', type(closed.exception()).__name__)

            task.add_done_callback(observe)

        try:
            yield next_event
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                pending.add_done_callback(close_when_stopped)
            else:
                await events.aclose()

    @staticmethod
    async def _awaitModel(awaitable, deadline):
        """Bound waiting even if a provider delays acknowledging cancellation.

        Model tasks never publish session state. A cancelled/late result cannot
        overwrite the deterministic fallback saved by the calling coroutine.
        """
        task = asyncio.ensure_future(awaitable)

        def discard(finished):
            if not finished.cancelled():
                finished.exception()

        try:
            remaining = max(0, deadline - asyncio.get_running_loop().time())
            if not remaining:
                raise TimeoutError('Record generation budget exhausted')
            done, _ = await asyncio.wait({task}, timeout=remaining)
            if not done or asyncio.get_running_loop().time() > deadline:
                raise TimeoutError('Record generation budget exhausted')
            return task.result()
        finally:
            if not task.done():
                task.cancel()
            task.add_done_callback(discard)

    @staticmethod
    def _validatedPresentation(draft, decision, presentation, patient, source_text, reasoning):
        validate_message(
            presentation.assistantMessage, patient, action=decision['action'], has_responses=bool(decision['responses'])
        )
        candidate = deepcopy(draft)
        flow_policy.apply_presentation(candidate, decision, presentation, reasoning)
        published = candidate.get('currentQuestion') or {'assistantMessage': candidate['qaFlow']['finalMessage']}
        check_generated_value(
            {key: value for key, value in published.items() if key != 'reasoning'},
            chinese_context=True,
            source_text=source_text,
        )
        return candidate

    @staticmethod
    def _logGenerationOutcome(session, started, failure_kind=None):
        quality = session['qaFlow'].get('generationQuality', {})
        logger.info(
            'Record generation outcome: session_id=%s mode=%s cause=%s elapsed_ms=%s',
            session['sessionId'],
            quality.get('mode', 'normal'),
            failure_kind,
            int((asyncio.get_running_loop().time() - started) * 1000),
        )

    @staticmethod
    def _logGenerationRejection(error, *, schema, stage, attempt, context, content):
        diagnostics = validation_diagnostics(
            error, schema=schema, stage=stage, attempt=attempt, run_id=context.run_id, content=content
        )
        logger.warning(
            'Record output rejected: session_id=%s stage=%s attempt=%s diagnostics=%s',
            context.session_id,
            stage,
            attempt,
            json.dumps(diagnostics, ensure_ascii=False),
        )
        return diagnostics

    @staticmethod
    def _answerMissingReply(session):
        from modules.record.application.recovery import missing_reply

        return missing_reply(session)

    @staticmethod
    def _hasPendingSubmission(session):
        from modules.record.application.recovery import pending_submission

        return pending_submission(session)

    async def _recoverReplyUnderLease(self, session, user_id, session_id, lease_guard):
        return await self.record_agent.invoke(
            'recover', {'user_id': user_id, 'session_id': session_id}, session=session, lease_guard=lease_guard
        )

    async def _recoverReplyUnderLeaseImpl(self, session, user_id, session_id, lease_guard):
        if self._hasPendingSubmission(session):
            async for event in self._completeSubmission(session, user_id, session_id, lease_guard, recovery=True):
                if event['type'] == 'result':
                    return event['session']
            raise RuntimeError('未生成恢复结果')
        last = self._answerMissingReply(session)
        if last is not None:
            return await self._recoverAnswerReply(session, last, user_id, session_id, lease_guard)
        return session

    def _assertReplyReadable(self, session):
        if session and (
            session.get('qaCommit')
            or self._hasPendingSubmission(session)
            or self._answerMissingReply(session) is not None
        ):
            from modules.record.application.recovery import RECOVERY_VERSION

            recovery = session.get('qaRecovery') or {}
            if not isinstance(recovery, dict) or recovery and recovery.get('version') != RECOVERY_VERSION:
                raise QASessionConflictError('回复恢复版本不可用', code='RECORD_REPLY_RECOVERY_FAILED')
            if (
                recovery.get('status') in {'exhausted', 'failed'}
                or (session.get('qaCommit') or {}).get('status') == 'failed'
            ):
                raise QASessionConflictError(
                    '回复自动恢复已停止，请联系工作人员处理', code='RECORD_REPLY_RECOVERY_FAILED'
                )
            raise QASessionConflictError('回复仍在恢复中，请稍后重试', code='RECORD_REPLY_PENDING')

    async def _recoverAnswerReply(self, session, last, user_id, session_id, lease_guard) -> dict:
        """Publish a missing successor without accepting the user's answer twice."""
        if session.get('userId') != user_id or session.get('sessionId') != session_id:
            raise QASessionConflictError('会话身份不匹配，无法恢复回复')
        draft = deepcopy(session)
        legacy = not draft.get('qaFlow')
        if legacy:
            draft['qaFlow'] = flow_policy.new_flow(legacy=True)
            draft['qaFlow']['interactionCount'] = len(draft['qaHistory'])
            draft['qaFlow']['intakeCount'] = len(draft['qaHistory'])
        flow = draft['qaFlow']
        interview_policy.ensure_counters(draft)
        question_id, answer = last['question']['questionId'], last['answer']
        fingerprint = hashlib.sha256(flow_policy.answer_text(answer).encode('utf-8')).hexdigest()
        published = flow.get('lastSubmission') or {}
        result = published.get('result') or {}
        pending = [item for item in flow['concerns'] if item['status'] == 'pending']
        fixed_invitation = is_supplement_question(result)
        if published.get('questionId') == question_id and published.get('answerHash') != fingerprint:
            raise QASessionConflictError('已保存的回答不一致，无法恢复回复')
        if (
            published.get('questionId') == question_id
            and result.get('status') == 'question'
            and (flow['phase'] != 'qa' or fixed_invitation or len(pending) <= 1)
        ):
            # A successful result may already exist even when the cached current
            # question was lost. Reuse its identity and text, without a model call.
            draft['currentQuestion'] = {
                key: deepcopy(result[key])
                for key in (
                    'questionId',
                    'question',
                    'questionType',
                    'options',
                    'assistantMessage',
                    'reasoning',
                    'phase',
                )
                if key in result
            }
            flow_policy.GeneratedQuestion.model_validate(
                {key: draft['currentQuestion'][key] for key in ('question', 'questionType', 'options')}
            )
            focus_dimension = (flow.get('focus') or {}).get('dimension')
            if (
                interview_policy.is_expanded(flow)
                and flow['phase'] == draft['currentQuestion'].get('phase') == 'intake'
                and focus_dimension in intake_policy.DIMENSION_LABELS
            ):
                # The receipt omits internal selection metadata. Restore it
                # from the same saved draft, without changing the published
                # question or treating recovery as a new selection.
                draft['currentQuestion'].update(
                    intakeDimension=focus_dimension,
                    intakeQuestionNumber=flow['intakeQuestionCount'] + 1,
                )
            # The public result omits the internal QA target. Fixed invitations
            # have no target; only a true clarification can restore a unique one.
            if flow['phase'] == 'qa' and pending and not fixed_invitation:
                draft['currentQuestion']['concernId'] = pending[0]['id']
        elif flow.get('phase') == 'finished' and flow.get('finalMessage'):
            draft['currentQuestion'] = None
        elif (
            legacy
            and session['status'] == 'awaiting_decision'
            and len(draft['qaHistory']) < interview_policy.budget(flow)['maxInteractions']
        ):
            # The old flow has already ended its planned round. Replace the
            # obsolete continue/finish gate with the existing supplement entry.
            flow['phase'] = 'supplement'
            flow_policy.apply_presentation(
                draft,
                {'action': 'invite_supplement', 'responses': [], 'target': None},
                flow_policy.TurnPresentation(
                    assistantMessage=flow_policy.PatientMessage(
                        acknowledgement='接下来可以聊聊您关心的问题。',
                        rationale='',
                        evidence=[],
                    ),
                    responses=[],
                    question=None,
                ),
                '',
            )
        else:
            # The answer is a durable receipt, not a new submission. Present it
            # exactly once to analysis, and restore the original history below.
            draft['qaHistory'] = deepcopy(session['qaHistory'][:-1])
            draft['currentQuestion'] = deepcopy(last['question'])
            phase = last['question'].get('phase', flow['phase'])
            if phase in ('intake', 'supplement', 'qa'):
                flow['phase'] = phase
            recovered = None
            async for event in self._generateTurn(
                draft, user_id=user_id, answer=answer, thinking=False, answer_already_recorded=True
            ):
                if event['type'] == 'draft':
                    recovered = event['session']
            if recovered is None:
                raise RuntimeError('未生成恢复结果，请稍后查询状态重试')
            draft = recovered
            draft['qaHistory'] = deepcopy(session['qaHistory'])
            flow = draft['qaFlow']
        completed = flow['phase'] == 'finished'
        draft['status'] = QA_STATUS_COMPLETED if completed else QA_STATUS_ACTIVE
        if completed:
            draft['completedAt'] = datetime.now().isoformat()
            draft.pop('healthProfileContext', None)
            self._prepareRecordProfileSourceSnapshot(draft)
        result = self._buildSessionResponse(draft)
        flow['lastSubmission'] = {'questionId': question_id, 'answerHash': fingerprint, 'result': deepcopy(result)}
        if not await self._save_session(user_id, session_id, draft, completed=completed, lease_guard=lease_guard):
            raise RuntimeError('保存恢复结果失败，请稍后查询状态重试')
        if completed:
            await self._ensureRecordProfileQueued(user_id, session_id)
        logger.info('已恢复缺失的建档回复: session_id=%s', session_id)
        return draft

    async def getSession(self, user_id, session_id) -> Optional[dict]:
        session = await self._load_session(user_id, session_id)
        self._assertReplyReadable(session)
        return None if session is None else self._buildSessionResponse(session)

    async def getHistory(self, user_id, session_id) -> Optional[dict]:
        session = await self._load_session(user_id, session_id)
        self._assertReplyReadable(session)
        if session is None:
            return None
        current = self._buildSessionResponse(session)
        if is_creation(session):
            return {**current, 'history': []}
        result = {
            key: current[key]
            for key in (
                'userId',
                'sessionId',
                'status',
                'round',
                'phase',
                'interactionCount',
                'intakeQuestionCount',
                'qaRoundCount',
                'documents',
                'documentProcessing',
            )
        }
        result.update(
            {
                'createdAt': session['createdAt'],
                'history': [
                    {
                        'questionId': item['question']['questionId'],
                        'question': item['question']['question'],
                        'questionType': item['question']['questionType'],
                        'assistantMessage': item['question'].get('assistantMessage', ''),
                        'phase': item['question'].get('phase', 'intake'),
                        'interactionCount': item.get('interactionCount'),
                        'answer': item['answer'],
                        'answeredAt': item['answeredAt'],
                    }
                    for item in public_history(session.get('qaHistory', []))
                ],
            }
        )
        if current['status'] == 'finished':
            result.update({key: current[key] for key in ('completedAt', 'assistantMessage', 'endReason')})
        return result

    @staticmethod
    def _extractBasicInfo(health_report_data) -> dict:
        """从本次建档报告字段中提取可靠的用户基本信息。

        不推断缺失值。兼容报告可能使用的 ``subject``、``basicInfo``、
        ``patientInfo`` 以及历史顶层字段，并统一为建档使用的字段名。
        """
        result = {
            'name': None,
            'gender': None,
            'age': None,
            'height': None,
            'weight': None,
        }
        sources = []
        reports = [health_report_data] if isinstance(health_report_data, dict) else health_report_data
        if not isinstance(reports, (list, tuple)):
            return result
        for report in reports:
            if not isinstance(report, dict):
                continue
            for key in ('subject', 'basicInfo', 'patientInfo'):
                value = report.get(key)
                if isinstance(value, dict):
                    sources.append(value)
            sources.append(report)

        aliases = {
            'name': ('name', 'patientName'),
            'gender': ('gender', 'sex'),
            'age': ('age',),
            'height': ('height', 'heightCm'),
            'weight': ('weight', 'weightKg'),
        }
        for field, candidates in aliases.items():
            for source in sources:
                value = next(
                    (source.get(key) for key in candidates if source.get(key) not in (None, '')),
                    None,
                )
                if value is not None:
                    result[field] = value
                    break
        return result

    @staticmethod
    def _normalizeFileList(file_list) -> list[dict]:
        """校验并复制建档创建请求中的报告列表。"""
        if file_list is None:
            return []
        if not isinstance(file_list, list):
            raise ValueError('fileList 必须为数组')
        if len(file_list) > MAX_QA_REPORT_FILES:
            raise ValueError(
                f'fileList 最多包含 {MAX_QA_REPORT_FILES} 份报告',
            )

        normalized = []
        seen_urls = set()
        for index, item in enumerate(file_list):
            if not isinstance(item, dict):
                raise ValueError(f'fileList[{index}] 必须为对象')
            if 'documentId' in item:
                document_id = item['documentId']
                if not isinstance(document_id, str) or not document_id.strip():
                    raise ValueError(f'fileList[{index}].documentId 必须为非空字符串')
                if 'url' in item or 'fileType' in item:
                    raise ValueError(f'fileList[{index}] 的 documentId 不能与 url 或 fileType 同时提供')
                normalized.append({'documentId': document_id.strip()})
                continue
            url = item.get('url')
            if not isinstance(url, str) or not url.strip():
                raise ValueError(
                    f'fileList[{index}].url 必须为非空字符串',
                )
            url = url.strip()
            if url in seen_urls:
                raise ValueError('fileList 不能包含重复的报告 URL')
            seen_urls.add(url)

            file_type = item.get('fileType')
            if file_type is not None:
                if not isinstance(file_type, str) or not file_type.strip():
                    raise ValueError(
                        f'fileList[{index}].fileType 必须为非空字符串',
                    )
                file_type = file_type.strip().lower()
                if file_type not in {'pdf', 'image'}:
                    raise ValueError(f'fileList[{index}].fileType 仅支持 pdf 或 image')

            _, extension = resolve_report_filename(url)
            inferred_category = report_file_category(extension)
            if file_type is not None and inferred_category is not None and file_type != inferred_category:
                raise ValueError(f'fileList[{index}].fileType 与文件 URL 中的扩展名类别不一致')
            if file_type is None:
                file_type = inferred_category
                if not extension:
                    raise ValueError(f'fileList[{index}] 无法识别文件格式，请提供 fileType（pdf 或 image）')

            copied = dict(item)
            copied['url'] = url
            if file_type is not None:
                copied['fileType'] = file_type
            normalized.append(copied)
        return normalized

    @classmethod
    def _normalizeHealthData(
        cls,
        *,
        name,
        file_list,
        hospital_data,
        we_run_data,
        doctor_product_packages,
        smart_device_data,
        basic_info,
    ) -> dict:
        if basic_info is not None and not isinstance(basic_info, dict):
            raise ValueError('basicInfo 必须为对象')
        return {
            'name': name,
            'fileList': cls._normalizeFileList(file_list),
            'hospitalData': cls._normalizeOptionalList(
                'hospitalData',
                hospital_data,
            ),
            'weRunData': cls._normalizeOptionalList(
                'weRunData',
                we_run_data,
            ),
            'doctorProductPackages': cls._normalizeOptionalList(
                'doctorProductPackages',
                doctor_product_packages,
            ),
            'smartDeviceData': cls._normalizeOptionalObject(
                'smartDeviceData',
                smart_device_data,
            ),
        }

    @staticmethod
    def _normalizeOptionalList(name: str, value) -> list:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError(f'{name} 必须为数组')
        return list(value)

    @staticmethod
    def _normalizeOptionalObject(name: str, value) -> dict:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError(f'{name} 必须为对象')
        return dict(value)

    async def _loadHealthProfileContext(self, user_id: str) -> str | None:
        """失败降级地读取创建时画像，避免画像故障阻断建档。"""
        provider = self._health_profile_memory_provider
        if provider is None:
            return None
        try:
            context = await provider.build_context(user_id)
        except Exception as error:
            logger.warning(
                f'读取建档健康画像记忆失败，按无画像模式继续: user_id={user_id}, error_type={type(error).__name__}',
            )
            return None
        if context is None:
            return None
        if not isinstance(context, str) or not context.strip():
            logger.warning(
                f'建档健康画像记忆返回无效内容，按无画像模式继续: user_id={user_id}',
            )
            return None
        return context.strip()

    async def _processHealthReports(
        self,
        *,
        user_id: str,
        session_id: str,
        file_list: list[dict],
        documents: list[dict] | None = None,
        document_processing: list[dict] | None = None,
        creation_attempt=None,
    ) -> list[dict]:
        """并发解析本次建档报告，并保持请求中的原始顺序。"""
        if not file_list:
            return []

        semaphore = asyncio.Semaphore(QA_REPORT_PARSE_CONCURRENCY)
        settled_document_ids = set()
        if creation_attempt is not None:
            for saved in creation_attempt.data.get('files', {}).values():
                reference = (saved.get('document') or saved.get('processing') or {}).get('documentId')
                if reference:
                    settled_document_ids.add(reference)
            for prepared in creation_attempt.data.get('preparedDocuments', {}).values():
                if isinstance(prepared, dict) and (reference := prepared.get('documentId')):
                    settled_document_ids.add(reference)

        async def parse_one(index: int, item: dict):
            async with semaphore:
                on_document_ready = None
                if creation_attempt is not None:
                    reference = creation_attempt.data.get('references', {}).get(str(index))
                    if reference:
                        item = {'documentId': reference}

                    async def remember_document(document_id):
                        await creation_attempt.checkpoint('processing_reports', documentReference=(index, document_id))

                    on_document_ready = remember_document
                document_port = self._selectRecordDocumentPort(creation_attempt, user_id=user_id, session_id=session_id)
                preparation_options = {}
                saved_preparation = (
                    creation_attempt.data.get('preparedDocuments', {}).get(str(index))
                    if creation_attempt is not None
                    else None
                )
                if isinstance(document_port, RecordPreparedDocumentPort):
                    if not supports_record_prepared_document_checkpoint(document_port.prepared_checkpoint_version):
                        raise RecordDocumentVersionError('the selected document preparation contract is unavailable')
                    if creation_attempt is None:
                        raise RuntimeError('prepared document consumption requires durable Record creation')

                    async def remember_prepared(prepared):
                        await creation_attempt.checkpoint(
                            'processing_reports', preparedDocument=(index, prepared.to_checkpoint())
                        )

                    preparation_options = {
                        'prepared_checkpoint': saved_preparation,
                        'on_prepared': remember_prepared,
                    }
                elif saved_preparation is not None:
                    # A rollback may read completed old projections, but cannot
                    # send an already prepared publication through the old facade.
                    raise RecordDocumentVersionError('the stored document preparation requires its exact consumer')
                return await document_port.process_attachment(
                    user_id=user_id,
                    session_id=session_id,
                    file_index=index,
                    attachment=item,
                    settled_document_ids=settled_document_ids,
                    on_document_ready=on_document_ready,
                    **preparation_options,
                )

        async def process_one(index, item):
            saved = creation_attempt.data.get('files', {}).get(str(index)) if creation_attempt is not None else None
            if saved is not None:
                return deepcopy(saved)
            try:
                result = await parse_one(index, item)
            except Exception as error:
                failure = self._documentContentFailure(error)
                if failure is None:
                    raise
                reference = (
                    creation_attempt.data.get('references', {}).get(str(index))
                    if creation_attempt is not None
                    else None
                )
                processing = document_processing_summary(
                    index,
                    getattr(failure, 'processing', None),
                    status='unavailable',
                    retryable=getattr(failure, 'retryable', False),
                    document_id=getattr(failure, 'document_id', None) or reference or item.get('documentId'),
                )
                outcome = {'report': None, 'document': None, 'processing': processing}
                logger.warning(
                    '建档附件暂不可用，使用其余资料继续: file_index=%s error_type=%s', index, type(failure).__name__
                )
                if creation_attempt is not None:
                    await creation_attempt.checkpoint('processing_reports', completedFile=(index, outcome))
                return outcome
            outcome = result.to_checkpoint()
            if creation_attempt is not None:
                await creation_attempt.checkpoint('processing_reports', completedFile=(index, outcome))
            return outcome

        results = await asyncio.gather(
            *(process_one(index, item) for index, item in enumerate(file_list)),
            return_exceptions=True,
        )
        # gather returns a child cancellation as BaseException; preserve it before
        # considering an unrelated attachment's normal durable wait.
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
        failures = [(index, result) for index, result in enumerate(results) if isinstance(result, Exception)]
        if failures:
            for index, error in failures:
                if isinstance(error, RecordDocumentPublicationPending):
                    logger.info('建档报告等待正式归档回执: file_index=%s', index)
                elif isinstance(error, DocumentSubmissionConflictError):
                    logger.warning(
                        '建档报告提交被拒绝: file_index=%s %s',
                        index,
                        format_exception_summary(error),
                    )
                else:
                    logger.error(
                        '建档报告解析失败: file_index=%s %s',
                        index,
                        format_exception_summary(error),
                    )
            # A concurrent transport error still consumes its attempt even when
            # another attachment is normally awaiting a durable prerequisite.
            raise next(
                (error for _, error in failures if not isinstance(error, RecordDocumentPublicationPending)),
                failures[0][1],
            )

        reports, seen = [], set()
        for index, result in enumerate(results):
            processing = result.get('processing') or document_processing_summary(index, None)
            if document_processing is not None:
                document_processing.append(deepcopy(processing))
            if result['report'] is None or processing['status'] == 'unavailable':
                continue
            document = result['document']
            if document is None:
                reports.append(result['report'])
                continue
            if documents is not None:
                documents.append(document)
            if document['documentId'] not in seen:
                seen.add(document['documentId'])
                reports.append(result['report'])
        return reports

    @staticmethod
    def _documentContentFailure(error):
        """Only typed parsing failures may settle an attachment without a report."""
        seen, chain = set(), []
        while error is not None and id(error) not in seen:
            seen.add(id(error))
            chain.append(error)
            error = error.__cause__
        if any(
            isinstance(item, (DocumentSubmissionConflictError, PermissionError))
            or getattr(item, 'submission_unknown', False)
            or getattr(item, 'content_failure', True) is False
            or getattr(item, 'status_code', None) in (401, 403)
            for item in chain
        ):
            return None
        return next(
            (item for item in chain if isinstance(item, (DocumentParsingError, RecordDocumentFormalUnavailable))), None
        )

    def _validateSessionProfileConsumer(self, session):
        from modules.record.profile_source import LEGACY_RECORD_PROFILE_REVISION

        return self._validateRecordProfileConsumerRevision(
            session.get('recordProfileConsumerRevision', LEGACY_RECORD_PROFILE_REVISION)
        )

    def _prepareRecordProfileSourceSnapshot(self, session):
        self._validateSessionProfileConsumer(session)
        if self._record_profile_source is not None:
            self._record_profile_source.prepare_completed_snapshot(session)

    @staticmethod
    def _requiresLegacyRecordSourceIdentity(user_id, session_id):
        return any(
            value is not None
            and (not isinstance(value, str) or not value or value.strip() != value or len(value) > 256)
            for value in (user_id, session_id)
        )

    def _recordProfileConsumerRevision(self, *, user_id=None, session_id=None):
        from modules.record.profile_source import LEGACY_RECORD_PROFILE_REVISION
        from modules.record.profile_source import RECORD_PROFILE_SOURCE_REVISION

        revision = getattr(
            getattr(self, '_record_profile_source', None), 'consumer_revision', LEGACY_RECORD_PROFILE_REVISION
        )
        if revision == RECORD_PROFILE_SOURCE_REVISION and self._requiresLegacyRecordSourceIdentity(user_id, session_id):
            return LEGACY_RECORD_PROFILE_REVISION
        return self._validateRecordProfileConsumerRevision(revision)

    def _selectRecordProfileConsumerRevision(self, creation_attempt, *, user_id=None, session_id=None):
        from modules.record.profile_source import LEGACY_RECORD_PROFILE_REVISION

        if creation_attempt is None:
            return self._recordProfileConsumerRevision(user_id=user_id, session_id=session_id)
        revision = creation_attempt.session['qaCreation'].get('profileConsumerRevision', LEGACY_RECORD_PROFILE_REVISION)
        return self._validateRecordProfileConsumerRevision(revision)

    def _validateRecordProfileConsumerRevision(self, revision):
        from modules.record.profile_source import LEGACY_RECORD_PROFILE_REVISION
        from modules.record.profile_source import RECORD_PROFILE_SOURCE_REVISION

        if revision == LEGACY_RECORD_PROFILE_REVISION:
            return revision
        source = self._record_profile_source
        if (
            revision != RECORD_PROFILE_SOURCE_REVISION
            or not callable(getattr(source, 'supports_revision', None))
            or not source.supports_revision(revision)
            or getattr(self._completion_store, 'supports_record_profile_sources', False) is not True
        ):
            raise QASessionConflictError('建档画像来源版本不可用', code='RECORD_SOURCE_VERSION_UNAVAILABLE')
        return revision

    def _recordDocumentConsumerRevision(self, *, user_id=None, session_id=None):
        port = getattr(self, '_record_document_port', None)
        revision = (
            port.prepared_checkpoint_version
            if isinstance(port, RecordPreparedDocumentPort)
            else LEGACY_RECORD_DOCUMENT_CONSUMER
        )
        if revision in {
            RECORD_DOCUMENT_CHECKPOINT,
            RECORD_FORMAL_DOCUMENT_CHECKPOINT,
        } and self._requiresLegacyRecordSourceIdentity(user_id, session_id):
            return LEGACY_RECORD_DOCUMENT_CONSUMER
        return revision

    def _selectRecordDocumentPort(self, creation_attempt, *, user_id=None, session_id=None):
        port = self._getRecordDocumentPort()
        if creation_attempt is None:
            revision = self._recordDocumentConsumerRevision(user_id=user_id, session_id=session_id)
        else:
            creation = creation_attempt.session['qaCreation']
            prepared = creation_attempt.data.get('preparedDocuments', {})
            versions = {value.get('contractVersion') for value in prepared.values() if isinstance(value, dict)}
            if len(versions) > 1 or len(versions) != bool(prepared):
                raise RecordDocumentVersionError('prepared documents have conflicting consumer revisions')
            inferred = next(iter(versions)) if versions else LEGACY_RECORD_DOCUMENT_CONSUMER
            revision = creation.get('documentConsumerRevision', inferred)
            if prepared and versions != {revision}:
                raise RecordDocumentVersionError('prepared documents conflict with the accepted consumer revision')
        if isinstance(port, RecordDocumentConsumerRegistry):
            port = port.for_consumer_revision(revision)
        actual = (
            port.prepared_checkpoint_version
            if isinstance(port, RecordPreparedDocumentPort)
            else LEGACY_RECORD_DOCUMENT_CONSUMER
        )
        if not isinstance(port, RecordDocumentPort) or actual != revision:
            raise RecordDocumentVersionError('the accepted Record document consumer is unavailable')
        return port

    def _getRecordDocumentPort(self) -> RecordDocumentPort:
        if self._record_document_port is None:
            raise RuntimeError('体检报告处理服务未配置')
        return self._record_document_port

    @staticmethod
    def _getSessionHealthReports(session_data: dict) -> list[dict]:
        reports = session_data.get('parsedHealthReports', [])
        if not isinstance(reports, list):
            return []
        return [report for report in reports if isinstance(report, dict)]

    @staticmethod
    def _formatHealthReports(health_report_data) -> tuple[str, str]:
        """把多份独立报告格式化为有边界的模型上下文。"""
        reports = [health_report_data] if isinstance(health_report_data, dict) else health_report_data
        if not isinstance(reports, (list, tuple)) or not reports:
            return '无', 'unknown'

        reports = [report for report in reports if isinstance(report, dict)]
        if not reports:
            return '无', 'unknown'
        statuses = [report.get('status') or 'unknown' for report in reports]
        sections = [
            bounded_report_context(report, index, MAX_QA_REPORT_CONTEXT_CHARS)
            for index, report in enumerate(reports, 1)
        ]
        context = '\n\n'.join(sections)
        if len(context) > MAX_QA_REPORT_CONTEXT_CHARS:
            # Preserve a bounded contribution from every attachment, not just the first long one.
            allowance = max(1, (MAX_QA_REPORT_CONTEXT_CHARS - 2 * len(sections)) // len(sections))
            context = '\n\n'.join(
                bounded_report_context(report, index, allowance) for index, report in enumerate(reports, 1)
            )

        if 'abnormal' in statuses:
            overall_status = 'abnormal'
        elif statuses and all(status == 'normal' for status in statuses):
            overall_status = 'normal'
        elif 'pending' in statuses:
            overall_status = 'pending'
        else:
            overall_status = 'unknown'
        return context, overall_status

    @staticmethod
    def _formatAttachedHealthData(health_data) -> str | None:
        """把本次创建请求中的其它数据渲染为有边界的只读上下文。"""
        if not isinstance(health_data, dict):
            return None
        sections = []
        for label, key in (
            ('院内数据', 'hospitalData'),
            ('微信运动数据', 'weRunData'),
            ('智能设备数据', 'smartDeviceData'),
        ):
            value = health_data.get(key)
            if value not in (None, [], {}):
                sections.append(
                    f'{label}：' + json.dumps(value, ensure_ascii=False, separators=(',', ':')),
                )

        products = health_data.get('doctorProductPackages')
        if products not in (None, [], {}):
            sections.append(
                '医生产品（仅表示服务配置，不能当作用户健康事实）：'
                + json.dumps(products, ensure_ascii=False, separators=(',', ':')),
            )
        if not sections:
            return None
        context = '\n'.join(sections)
        if len(context) > MAX_QA_ATTACHED_DATA_CONTEXT_CHARS:
            context = context[:MAX_QA_ATTACHED_DATA_CONTEXT_CHARS] + '\n（其余本次建档数据已截断）'
        return context

    @staticmethod
    def _formatBasicInfo(extracted_info: Optional[dict]) -> str | None:
        if not isinstance(extracted_info, dict):
            return None
        labels = {
            'name': '姓名',
            'gender': '性别',
            'age': '年龄',
            'height': '身高',
            'weight': '体重',
        }
        parts = [
            f'{label}：{extracted_info[field]}'
            for field, label in labels.items()
            if extracted_info.get(field) not in (None, '')
        ]
        return '、'.join(parts) if parts else None
