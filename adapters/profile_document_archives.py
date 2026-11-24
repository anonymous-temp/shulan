"""Translate Document's public archive reader into the Profile-owned read port."""

from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import asdict

from health_profile.document_archive_contracts import ProfileArchivedExtraction
from health_profile.document_archive_contracts import ProfileDocumentArchiveChangedError
from health_profile.document_archive_contracts import ProfileDocumentArchiveDescriptor
from health_profile.document_archive_contracts import ProfileDocumentArchivePage
from health_profile.document_archive_contracts import ProfileDocumentArchiveReadError
from health_profile.document_archive_contracts import ProfileDocumentSourceState
from modules.analysis.archived_extractions import DocumentArchiveChangedError
from modules.analysis.archived_extractions import DocumentArchiveReadError
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import digest


class ProfileDocumentArchiveAdapter:
    def __init__(self, reader):
        self.reader = reader

    @staticmethod
    def _descriptor(scope, value):
        return ProfileDocumentArchiveDescriptor(
            user_id=scope.user_id,
            subject_id=scope.subject_id,
            extraction_id=value.extraction_id,
            document_id=value.document_id,
            canonical_report_id=value.canonical_report_id,
            lifecycle_token=value.lifecycle_token,
            document_ids=tuple(value.document_ids),
            title=value.title,
            report_time=value.report_time,
            processing=deepcopy(value.processing),
            source_status=value.source_status,
            source_revision=value.source_revision,
            publication_revision=value.publication_revision,
            archive_status=value.archive_status,
            reason=value.reason,
            interpretation_round=value.interpretation_round,
            material_ref=value.material_ref,
            result_ref=value.result_ref,
            result_digest=value.result_digest,
            lifecycle_epoch=value.lifecycle_epoch,
            current_source=value.current_source,
            archive_lifecycle_epoch=value.archive_lifecycle_epoch,
            archive_managed_bindings=value.archive_managed_bindings,
        )

    @staticmethod
    @asynccontextmanager
    async def _errors():
        try:
            yield
        except DocumentArchiveChangedError as error:
            raise ProfileDocumentArchiveChangedError('文档资料或来源状态已变化，请重新查询') from error
        except (DocumentArchiveReadError, LookupError, PermissionError) as error:
            raise ProfileDocumentArchiveReadError('文档资料暂不可读') from error

    async def list_archived(self, *, user_id, subject_id, document_id=None, after_id=None, limit=20):
        scope = DocumentScope(user_id, subject_id)
        async with self._errors():
            page = await self.reader.list_archived(scope=scope, document_id=document_id, after_id=after_id, limit=limit)
            return ProfileDocumentArchivePage(
                tuple(self._descriptor(scope, item) for item in page.entries), page.next_after_id
            )

    async def read_fixed(self, *, user_id, subject_id, extraction_id, expected_lifecycle_token=None, document_id=None):
        scope = DocumentScope(user_id, subject_id)
        async with self._errors():
            result = await self.reader.read_fixed(
                scope=scope,
                extraction_id=extraction_id,
                expected_lifecycle_token=expected_lifecycle_token,
                document_id=document_id,
            )
            return ProfileArchivedExtraction(
                self._descriptor(scope, result.descriptor),
                deepcopy(result.result),
                tuple(deepcopy(result.source_observations)),
            )

    async def read_source_state(self, *, user_id, subject_id, source_id):
        scope = DocumentScope(user_id, subject_id)
        async with self._errors():
            state = await self.reader.read_source_lifecycle(scope=scope, canonical_report_id=source_id)
            if state.scope != scope or state.source_id != source_id:
                raise ProfileDocumentArchiveReadError('文档来源不属于请求范围')
            status = state.status
            if status == 'retracting':
                status = 'withdrawn'
            elif status == 'legacy':
                status = 'not_published'
            return ProfileDocumentSourceState(digest(asdict(state)), state.legacy_observations_visible, status)
