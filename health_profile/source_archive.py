"""Explicit compatibility-history archive for an already managed report source."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import UTC
from datetime import datetime

from utils.report_content import report_source_content

from .contracts import ProfileScope
from .contracts import profile_content_digest
from .report_source import HealthReportSourceConflictError
from .report_source import HealthReportSourceRecord
from .source_admission_contracts import ProfileArchivedSourceMaterialV1
from .source_publications import source_key


class ProfileSourceMaterialArchive:
    """Save immutable legacy identities without queue, observation or fact writes.

    This service deliberately has no JobQueue, observation store or Agent. The
    source owner must retain its admission fence; the persisted Profile fence
    must already be ready. This is not an existing-source migration operation.
    """

    def __init__(self, *, report_store, publication_store, clock=None):
        self._reports = report_store
        self._publications = publication_store
        self._clock = clock or (lambda: datetime.now(UTC))

    async def archive_source_material(
        self,
        *,
        scope: ProfileScope,
        canonical_report_id: str,
        report_id: str,
        source_fingerprint: str,
        document_id: str,
        result: dict,
        source_key_value: str | None = None,
    ) -> ProfileArchivedSourceMaterialV1:
        from .source_presence import _source_identity

        _source_identity(scope, 'health_report', canonical_report_id)
        for value in (report_id, source_fingerprint, document_id):
            if not isinstance(value, str) or not value or value != value.strip() or len(value) > 512:
                raise ValueError('archive identifiers must be normalized nonempty strings')
        if not isinstance(result, dict):
            raise TypeError('result must be a dict')
        result = report_source_content(deepcopy(result))
        # These fields are Profile-owned record metadata, never a caller payload.
        if any(
            name in result
            for name in (
                '_id',
                'reportId',
                'userId',
                'subjectId',
                'sourceRevision',
                'sourceFingerprint',
                'documentId',
                'canonicalReportId',
                'profileIngestion',
                'createdAt',
                'updatedAt',
                'sourceKey',
            )
        ):
            raise ValueError('archive result cannot include record metadata')
        if source_key_value is not None and (
            not isinstance(source_key_value, str)
            or not source_key_value
            or source_key_value != source_key_value.strip()
            or len(source_key_value) > 2048
        ):
            raise ValueError('source_key_value must be a normalized opaque source key')
        if source_key_value is not None and report_id != canonical_report_id:
            raise ValueError('supplemental revisions belong to the canonical report')
        revision = (
            1
            if source_key_value is None
            else 2 + int(hashlib.sha256(source_key_value.encode('utf-8')).hexdigest()[:12], 16)
        )
        marker = await self._publications.get('cutover_' + source_key(scope, 'health_report', canonical_report_id))
        if marker is None or marker.get('status') != 'ready' or marker.get('scope') != scope.model_dump(by_alias=True):
            raise HealthReportSourceConflictError('archive requires a ready source_managed fence')
        authorization = marker.get('authorization') or {}
        if authorization.get('sourceType') != 'health_report' or authorization.get('sourceId') != canonical_report_id:
            raise HealthReportSourceConflictError('archive fence source identity does not match')
        if source_key_value is not None or report_id != canonical_report_id:
            canonical = await self._reports.get_report(canonical_report_id)
            if canonical is None or (
                canonical.user_id != scope.user_id
                or canonical.subject_id != scope.subject_id
                or canonical.report_id != canonical.canonical_report_id
                or canonical.document_id != document_id
            ):
                raise HealthReportSourceConflictError('archive lacks a matching canonical document history')
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('clock must return an aware datetime')
        requested = HealthReportSourceRecord(
            report_id=report_id,
            user_id=scope.user_id,
            subject_id=scope.subject_id,
            source_revision=revision,
            source_fingerprint=source_fingerprint,
            result=result,
            ingestion_status='source_managed',
            ingestion_job_id=None,
            created_at=now,
            updated_at=now,
            document_id=document_id,
            canonical_report_id=canonical_report_id,
        )
        digest = profile_content_digest(_archive_identity(requested))
        if source_key_value is None:
            stored, created = await self._reports.create_report(requested)
        else:
            stored, created = await self._reports.create_revision(requested, source_key_value)
        if _archive_identity(stored) != _archive_identity(requested):
            raise HealthReportSourceConflictError(
                'immutable archived source material differs from the requested payload'
            )
        if stored.ingestion_status != 'source_managed' or stored.ingestion_job_id is not None:
            raise HealthReportSourceConflictError('archive cannot convert an existing legacy delivery record')
        return ProfileArchivedSourceMaterialV1(
            scope=scope,
            canonical_report_id=canonical_report_id,
            report_id=stored.report_id,
            legacy_source_revision=int(stored.source_revision),
            document_id=stored.document_id,
            source_fingerprint=stored.source_fingerprint,
            created=created,
            content_digest=digest,
        )


def _archive_identity(record):
    return {
        'scope': {'userId': record.user_id, 'subjectId': record.subject_id},
        'reportId': record.report_id,
        'canonicalReportId': record.canonical_report_id,
        'legacySourceRevision': record.source_revision,
        'documentId': record.document_id,
        'sourceFingerprint': record.source_fingerprint,
        'result': record.result,
    }
