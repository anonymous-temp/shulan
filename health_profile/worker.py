"""用户画像持久化摄取任务 Worker。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Protocol
from typing import runtime_checkable
from uuid import uuid4

from utils.log import getLogger
from utils.log.context import task_log_context

from .ingestion import HealthProfileJobLeaseError
from .ingestion import HealthProfileJobQueue
from .models import HealthProfileJob
from .models import ProfileIngestionSourceType
from .models import ProfileJobStatus
from .processing import ProfileIngestionProcessingError
from .processing import ProfileIngestionProcessingResult

logger = getLogger(__name__)


@runtime_checkable
class ProfileIngestionProcessor(Protocol):
    async def process(
        self,
        job: HealthProfileJob,
    ) -> ProfileIngestionProcessingResult: ...


@runtime_checkable
class ProfilePendingDeliverySource(Protocol):
    async def recover_pending(self, *, limit: int = 100) -> int: ...


class ProfileIngestionProcessorRouter:
    """按任务来源把同一持久化队列路由到对应处理器。"""

    def __init__(
        self,
        processors: Mapping[
            ProfileIngestionSourceType,
            ProfileIngestionProcessor,
        ],
        *,
        source_guard=None,
    ) -> None:
        if not isinstance(processors, Mapping):
            raise TypeError('processors must be a mapping')
        normalized = {}
        for source_type, processor in processors.items():
            if not isinstance(source_type, ProfileIngestionSourceType):
                raise TypeError(
                    'processor keys must be ProfileIngestionSourceType',
                )
            if not isinstance(processor, ProfileIngestionProcessor):
                raise TypeError(
                    'processor values must implement ProfileIngestionProcessor',
                )
            normalized[source_type] = processor
        if not normalized:
            raise ValueError('processors cannot be empty')
        self._processors = normalized
        self._source_guard = source_guard

    @property
    def source_types(self) -> tuple[ProfileIngestionSourceType, ...]:
        return tuple(self._processors)

    async def process(
        self,
        job: HealthProfileJob,
    ) -> ProfileIngestionProcessingResult:
        try:
            processor = self._processors[job.source_type]
        except KeyError as error:
            raise ProfileIngestionProcessingError(
                'source.unsupported',
                '当前画像任务来源没有注册处理器',
            ) from error
        if self._source_guard is not None and await self._source_guard(
            job.user_id,
            job.subject_id,
            job.source_type.value,
            job.source_id,
        ):
            return ProfileIngestionProcessingResult(
                graph_version='profile-source-cutover-v1',
                policy_version='not_applied',
                extractor_version='not_called',
                candidate_count=0,
                fact_count=0,
                replayed_fact_count=0,
                rejected_candidate_count=0,
                reason_codes=('source.managed_by_publication',),
            )
        return await processor.process(job)


class HealthProfileIngestionWorker:
    """按租约串行领取并执行一小批画像摄取任务。"""

    def __init__(
        self,
        queue: HealthProfileJobQueue,
        processor: ProfileIngestionProcessor,
        *,
        worker_id: str | None = None,
        batch_size: int = 10,
        source_types: Sequence[ProfileIngestionSourceType] | None = None,
        pending_sources: Mapping[
            ProfileIngestionSourceType,
            ProfilePendingDeliverySource,
        ]
        | None = None,
        recovery_batch_size: int = 100,
        recovery_interval_seconds: float = 30.0,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        if not isinstance(queue, HealthProfileJobQueue):
            raise TypeError('queue must be a HealthProfileJobQueue')
        if not isinstance(processor, ProfileIngestionProcessor):
            raise TypeError(
                'processor must implement ProfileIngestionProcessor',
            )
        if isinstance(batch_size, bool) or not isinstance(batch_size, int):
            raise TypeError('batch_size must be an integer')
        if batch_size < 1 or batch_size > 100:
            raise ValueError('batch_size must be between 1 and 100')
        self._queue = queue
        self._processor = processor
        self._worker_id = worker_id or f'profile_worker_{uuid4().hex}'
        self._batch_size = batch_size
        self._source_types = None
        if source_types is not None:
            self._source_types = tuple(dict.fromkeys(source_types))
            if not self._source_types:
                raise ValueError('source_types cannot be empty')
            if any(not isinstance(item, ProfileIngestionSourceType) for item in self._source_types):
                raise TypeError(
                    'source_types must contain ProfileIngestionSourceType',
                )
        self._pending_sources: dict[
            ProfileIngestionSourceType,
            ProfilePendingDeliverySource,
        ] = {}
        if pending_sources is not None:
            if not isinstance(pending_sources, Mapping):
                raise TypeError('pending_sources must be a mapping')
            for source_type, source in pending_sources.items():
                if not isinstance(source_type, ProfileIngestionSourceType):
                    raise TypeError(
                        'pending source keys must be ProfileIngestionSourceType',
                    )
                if not isinstance(source, ProfilePendingDeliverySource):
                    raise TypeError(
                        'pending source values must support recover_pending',
                    )
                self._pending_sources[source_type] = source
        if isinstance(recovery_batch_size, bool) or not isinstance(recovery_batch_size, int):
            raise TypeError('recovery_batch_size must be an integer')
        if recovery_batch_size < 1 or recovery_batch_size > 1000:
            raise ValueError(
                'recovery_batch_size must be between 1 and 1000',
            )
        if isinstance(recovery_interval_seconds, bool) or not isinstance(recovery_interval_seconds, (int, float)):
            raise TypeError('recovery_interval_seconds must be numeric')
        if recovery_interval_seconds <= 0:
            raise ValueError('recovery_interval_seconds must be positive')
        self._recovery_batch_size = recovery_batch_size
        self._recovery_interval_seconds = float(recovery_interval_seconds)
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._next_recovery_at = 0.0
        self._drain_lock = asyncio.Lock()
        self._accepting = True

    @property
    def accepting(self) -> bool:
        return self._accepting

    def stop_accepting(self) -> None:
        """停止领取新任务；已经领取的任务继续自然执行完成。"""
        self._accepting = False

    @task_log_context(clear=True)
    async def drain_pending(self) -> int:
        """消费最多一个批次；重叠调度直接跳过。"""
        if not self._accepting or self._drain_lock.locked():
            return 0
        async with self._drain_lock:
            await self._recover_pending_sources_if_due()
            processed = 0
            for _ in range(self._batch_size):
                if not self._accepting:
                    break
                try:
                    if self._source_types is None:
                        job = await self._queue.claim(self._worker_id)
                    else:
                        job = await self._queue.claim(
                            self._worker_id,
                            source_types=self._source_types,
                        )
                except Exception as error:
                    logger.error(
                        f'领取用户画像摄取任务失败: worker_id={self._worker_id}, error_type={type(error).__name__}',
                    )
                    break
                if job is None:
                    break
                await self._process_claimed(job)
                processed += 1
            return processed

    async def _recover_pending_sources_if_due(self) -> None:
        if not self._pending_sources:
            return
        now = self._monotonic_clock()
        if now < self._next_recovery_at:
            return
        self._next_recovery_at = now + self._recovery_interval_seconds
        for source_type, source in self._pending_sources.items():
            try:
                recovered = await source.recover_pending(
                    limit=self._recovery_batch_size,
                )
            except Exception as error:
                logger.error(
                    '补偿用户画像 pending 来源失败: '
                    f'source_type={source_type.value}, '
                    f'error_type={type(error).__name__}',
                )
                continue
            if recovered:
                logger.info(
                    f'用户画像 pending 来源已补偿: source_type={source_type.value}, recovered_count={recovered}',
                )

    @task_log_context(job='job', task='profile-ingestion')
    async def _process_claimed(self, job: HealthProfileJob) -> None:
        assert job.lease_token is not None
        try:
            result = await self._processor.process(job)
        except ProfileIngestionProcessingError as error:
            logger.warning(
                '用户画像摄取任务处理失败: '
                f'job_id={job.job_id}, '
                f'source_type={job.source_type.value}, '
                f'source_id={job.source_id}, '
                f'attempt={job.attempts}, '
                f'error_code={error.error_code}, '
                f'error_type={type(error).__name__}',
            )
            await self._fail(job, error.error_code, retryable=error.retryable)
            return
        except Exception as error:
            logger.error(
                '用户画像摄取任务异常: '
                f'job_id={job.job_id}, '
                f'source_type={job.source_type.value}, '
                f'source_id={job.source_id}, '
                f'attempt={job.attempts}, '
                f'error_type={type(error).__name__}',
            )
            await self._fail(job, 'worker.unexpected_error')
            return
        try:
            completed = await self._queue.complete(
                job,
                worker_id=self._worker_id,
                lease_token=job.lease_token,
                graph_version=result.graph_version,
                policy_version=result.policy_version,
                extractor_version=result.extractor_version,
                processing_summary=result.summary(),
            )
            logger.debug(
                '用户画像摄取任务已完成: '
                f'job_id={completed.job_id}, '
                f'source_type={completed.source_type.value}, '
                f'candidate_count={result.candidate_count}, '
                f'fact_count={result.fact_count}, '
                f'replayed_fact_count={result.replayed_fact_count}, '
                f'rejected_candidate_count={result.rejected_candidate_count}',
            )
        except HealthProfileJobLeaseError as error:
            logger.warning(
                f'用户画像摄取任务完成时租约已失效: job_id={job.job_id}, error_type={type(error).__name__}',
            )
        except Exception as error:
            # 事实写入已经完成时不把任务改成失败；保留 running 状态，租约
            # 到期后由幂等重放再次完成。
            logger.error(
                f'写回用户画像摄取任务完成状态失败: job_id={job.job_id}, error_type={type(error).__name__}',
            )

    async def _fail(self, job: HealthProfileJob, error_code: str, *, retryable: bool | None = None) -> None:
        assert job.lease_token is not None
        try:
            failed = await self._queue.fail(
                job,
                worker_id=self._worker_id,
                lease_token=job.lease_token,
                error_code=error_code,
                **({'retryable': False} if retryable is False else {}),
            )
            message = (
                '用户画像摄取任务已进入死信'
                if failed.status is ProfileJobStatus.DEAD_LETTER
                else '用户画像摄取任务等待重试'
            )
            log = logger.error if failed.status is ProfileJobStatus.DEAD_LETTER else logger.warning
            log(
                f'{message}: job_id={failed.job_id}, '
                f'source_type={failed.source_type.value}, '
                f'attempt={failed.attempts}, '
                f'error_code={error_code}, '
                f'next_retry_at={failed.next_retry_at}',
            )
        except HealthProfileJobLeaseError as error:
            logger.warning(
                '用户画像摄取任务失败写回时租约已失效: '
                f'job_id={job.job_id}, '
                f'error_code={error_code}, '
                f'error_type={type(error).__name__}',
            )
        except Exception as error:
            logger.error(
                f'写回用户画像摄取任务失败状态异常: job_id={job.job_id}, error_type={type(error).__name__}',
            )
