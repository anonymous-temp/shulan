"""Read exact legacy job status without exposing Profile's private job documents."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC
from datetime import datetime

from .contracts import ProfileLegacyIngestionStatusV1
from .contracts import ProfileScope
from .models import ProfileIngestionSourceType
from .models import SubjectStatus
from .repository import HealthProfileRepository
from .snapshots import _identifier


class ProfileLegacyIngestionStatusReader:
    def __init__(self, repository: HealthProfileRepository, *, clock: Callable | None = None):
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(UTC))

    async def read_health_report(
        self,
        scope: ProfileScope,
        *,
        canonical_report_id: str,
        legacy_source_revision: int,
    ) -> ProfileLegacyIngestionStatusV1:
        """Missing jobs remain pending only for an existing, correctly owned subject."""
        if not isinstance(scope, ProfileScope):
            raise TypeError('scope must be a ProfileScope')
        canonical_report_id = _identifier(canonical_report_id)
        if len(canonical_report_id) > 512:
            raise ValueError('canonical_report_id cannot exceed 512 characters')
        if (
            isinstance(legacy_source_revision, bool)
            or not isinstance(legacy_source_revision, int)
            or legacy_source_revision < 1
        ):
            raise ValueError('legacy_source_revision must be a positive integer')
        legacy_source_revision = int(legacy_source_revision)
        read_at = self._clock()
        if not isinstance(read_at, datetime) or read_at.tzinfo is None or read_at.utcoffset() is None:
            raise ValueError('clock must return a timezone-aware datetime')
        base = {
            'scope': scope,
            'canonical_report_id': canonical_report_id,
            'legacy_source_revision': legacy_source_revision,
            'read_at': read_at,
        }
        try:
            subject = await self._repository.get_subject(scope.user_id, scope.subject_id)
            if (
                subject is None
                or subject.status is not SubjectStatus.ACTIVE
                or subject.user_id != scope.user_id
                or subject.subject_id != scope.subject_id
            ):
                return ProfileLegacyIngestionStatusV1(**base, status='missing', error_code='subject_missing')
            key = f'report:{canonical_report_id}:{legacy_source_revision}'
            job = await self._repository.get_job_by_idempotency(scope.user_id, key)
            if job is None:
                return ProfileLegacyIngestionStatusV1(**base, status='ready', ingestion_status='pending')
            if (
                job.user_id != scope.user_id
                or job.subject_id != scope.subject_id
                or job.source_type is not ProfileIngestionSourceType.HEALTH_REPORT
                or job.source_id != canonical_report_id
                or job.source_revision != legacy_source_revision
                or job.idempotency_key != key
            ):
                return ProfileLegacyIngestionStatusV1(
                    **base, status='missing', error_code='legacy_job_binding_mismatch'
                )
            return ProfileLegacyIngestionStatusV1(
                **base,
                status='ready',
                ingestion_status=job.status.value,
                job_present=True,
            )
        except Exception:
            return ProfileLegacyIngestionStatusV1(
                **base,
                status='unavailable',
                error_code='legacy_ingestion_read_unavailable',
            )
