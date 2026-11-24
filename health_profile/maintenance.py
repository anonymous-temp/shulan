"""用户画像摄取任务的受控运维能力。"""

from __future__ import annotations

import re
from collections.abc import Callable
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone

from utils.log import getLogger
from utils.log.context import LogContext
from utils.log.context import bind_log_context
from utils.log.context import task_log_context

from .models import HealthProfileJob
from .models import ProfileIngestionSourceType
from .models import ProfileJobRedriveRecord
from .models import ProfileJobStatus
from .repository import HealthProfileJobMaintenanceRepository

logger = getLogger(__name__)

_TERMINAL_STATUSES = (
    ProfileJobStatus.COMPLETED,
    ProfileJobStatus.DEAD_LETTER,
)
_AUDIT_CODE_PATTERN = re.compile(r'[A-Za-z0-9_.:@-]+')


class HealthProfileJobNotFoundError(RuntimeError):
    """指定的画像摄取任务不存在。"""


class HealthProfileJobRedriveConflictError(RuntimeError):
    """任务状态在查询与重驱之间发生变化，或不符合调用预期。"""


class HealthProfileJobMaintenanceService:
    """显式查询终态任务，并在修复原因后把单个任务重新放回队列。"""

    def __init__(
        self,
        repository: HealthProfileJobMaintenanceRepository,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(repository, HealthProfileJobMaintenanceRepository):
            raise TypeError(
                'repository must implement HealthProfileJobMaintenanceRepository',
            )
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def get_job(self, job_id: str) -> HealthProfileJob:
        job = await self._repository.get_job(_required_text('job_id', job_id))
        if job is None:
            raise HealthProfileJobNotFoundError('profile job was not found')
        return job

    @task_log_context(clear=True)
    async def list_recoverable_jobs(
        self,
        *,
        statuses: Sequence[ProfileJobStatus] = _TERMINAL_STATUSES,
        source_types: Sequence[ProfileIngestionSourceType] | None = None,
        limit: int = 100,
    ) -> tuple[HealthProfileJob, ...]:
        normalized_statuses = tuple(dict.fromkeys(statuses))
        if not normalized_statuses:
            raise ValueError('statuses cannot be empty')
        if any(status not in _TERMINAL_STATUSES for status in normalized_statuses):
            raise ValueError(
                'recoverable jobs must be completed or dead_letter',
            )
        jobs = await self._repository.list_jobs(
            statuses=normalized_statuses,
            source_types=source_types,
            limit=limit,
        )
        logger.debug(
            '画像摄取终态任务查询完成: '
            f'statuses={[item.value for item in normalized_statuses]}, '
            f'count={len(jobs)}, limit={limit}',
        )
        return jobs

    async def redrive(
        self,
        *,
        job_id: str,
        expected_status: ProfileJobStatus,
        reason_code: str,
        actor_id: str,
    ) -> HealthProfileJob:
        job_id = _required_text('job_id', job_id)
        if expected_status not in _TERMINAL_STATUSES:
            raise ValueError(
                'expected_status must be completed or dead_letter',
            )
        reason_code = _audit_code('reason_code', reason_code)
        actor_id = _audit_code('actor_id', actor_id)
        current = await self._repository.get_job(job_id)
        if current is None:
            raise HealthProfileJobNotFoundError('profile job was not found')
        with bind_log_context(LogContext(user_id=current.user_id, task_id=current.job_id)):
            if current.status is not expected_status:
                logger.warning(
                    '画像摄取任务重驱被拒绝: '
                    f'job_id={job_id}, '
                    f'expected_status={expected_status.value}, '
                    f'actual_status={current.status.value}, '
                    'reason_code=status_conflict',
                )
                raise HealthProfileJobRedriveConflictError(
                    'profile job status does not match expected_status',
                )
            redriven_at = self._now()
            record = ProfileJobRedriveRecord(
                redriven_at=redriven_at,
                from_status=current.status,
                attempts=current.attempts,
                reason_code=reason_code,
                actor_id=actor_id,
                error_code=current.error_code,
                graph_version=current.graph_version,
                policy_version=current.policy_version,
                extractor_version=current.extractor_version,
                completed_at=current.completed_at,
                processing_summary=current.processing_summary,
            )
            redriven = await self._repository.redrive_job(
                job_id=job_id,
                expected_status=expected_status,
                expected_updated_at=current.updated_at,
                redrive_record=record,
            )
            if redriven is None:
                logger.warning(
                    '画像摄取任务重驱发生并发冲突: '
                    f'job_id={job_id}, '
                    f'expected_status={expected_status.value}, '
                    'reason_code=concurrent_update',
                )
                raise HealthProfileJobRedriveConflictError(
                    'profile job changed before it could be redriven',
                )
            logger.info(
                '画像摄取任务已人工重驱: '
                f'job_id={job_id}, '
                f'from_status={expected_status.value}, '
                f'reason_code={reason_code}, actor_id={actor_id}',
            )
            return redriven

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('clock must return a timezone-aware datetime')
        return now


def _required_text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{name} must be a non-empty string')
    return value.strip()


def _audit_code(name: str, value: str) -> str:
    normalized = _required_text(name, value)
    if len(normalized) > 128 or _AUDIT_CODE_PATTERN.fullmatch(normalized) is None:
        raise ValueError(
            f'{name} may contain only letters, digits, dot, colon, at, dash and underscore',
        )
    return normalized
