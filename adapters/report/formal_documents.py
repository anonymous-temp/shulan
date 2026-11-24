"""Report consumes the public formal-source facade; Profile protocol stays in Document."""

import asyncio
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace

from adapters.report.fixed_documents import REPORT_FIXED_SELECTION
from adapters.report.fixed_documents import FixedReportDocumentFactory
from adapters.report.fixed_documents import FixedReportDocuments
from adapters.report.fixed_documents import _FixedProcessor
from modules.analysis.formal_source_contracts import DocumentFormalSourceCommand
from modules.analysis.formal_source_contracts import DocumentFormalSourcePort
from modules.analysis.snapshots import MANAGED_REPORT_CONTRACT
from modules.analysis.snapshots import DocumentSnapshot
from modules.analysis.snapshots import digest
from modules.report.document_projection import REPORT_PREPARED_PROJECTION_REVISION
from modules.report.document_rendering import report_understanding_content_v1
from modules.report.fallback_policy import DEFAULT_REPORT_FALLBACK_POLICY
from modules.report.ports import DocumentServiceError

REPORT_FORMAL_COMMAND_REVISION = 'document-formal-source-v2'
REPORT_MANAGED_PREPARATION_REVISION = 'document-managed-report-v16-v1'


class FormalReportDocumentFactory(FixedReportDocumentFactory):
    def __init__(
        self,
        *,
        compatibility,
        materials,
        formal: DocumentFormalSourcePort,
        projections,
        policy=DEFAULT_REPORT_FALLBACK_POLICY,
    ):
        super().__init__(
            compatibility=compatibility,
            materials=materials,
            publications=None,
            projections=projections,
            selection=REPORT_FIXED_SELECTION,
            policy=policy,
        )
        self.formal = formal

    def document_input_manifest(self):
        manifest = super().document_input_manifest()
        manifest.update(
            consumerRevision='report-formal-documents.v1',
            formalCommandRevision=REPORT_FORMAL_COMMAND_REVISION,
            preparedProjectionRevision=REPORT_PREPARED_PROJECTION_REVISION,
            providerPreparationRevision=REPORT_MANAGED_PREPARATION_REVISION,
        )
        manifest.pop('publicationSelection')
        return manifest

    def create(self, *, user_id, session_id, final_report_id):
        if not isinstance(final_report_id, str) or not final_report_id:
            raise ValueError('a durable Report task identity is required')
        processor = _FormalProcessor(self, user_id, session_id, final_report_id)
        return FixedReportDocuments(
            processor, user_id, session_id, final_report_id, archive_locator=True, policy=self.policy
        )


def create_formal_report_document_source_factory(
    *, compatibility, materials, formal, projections, policy=DEFAULT_REPORT_FALLBACK_POLICY
):
    """Pure construction. Root separately installs Formal recovery and exact old/new dispatcher."""
    return FormalReportDocumentFactory(
        compatibility=compatibility, materials=materials, formal=formal, projections=projections, policy=policy
    )


def _command_receipt(value, command):
    if (
        not isinstance(value, dict)
        or value.get('commandId') != command.command_id
        or value.get('commandRevision') != command.command_revision
        or value.get('status') not in {'pending', 'completed', 'not_published', 'failed'}
    ):
        raise DocumentServiceError('Formal source receipt differs from the admitted command')


def _receipt(value, command, projection, source_ids):
    _command_receipt(value, command)
    result = value.get('result')
    if not isinstance(result, dict) or not isinstance(result.get('basePublications'), list):
        raise DocumentServiceError('Formal source receipt has no explicit source progress')
    if result.get('projectionRef') not in (None, projection.snapshot_id):
        raise DocumentServiceError('Formal source receipt binds another business projection')
    if result.get('processingStatus') not in (None, projection.payload['processingStatus']):
        raise DocumentServiceError('Formal source receipt changes its fixed business quality')
    bases = result['basePublications']
    count, total = value.get('completedBaseCount'), value.get('totalUniqueSources')
    if (
        type(count) is not int
        or count != len(bases)
        or count > len(source_ids)
        or (total is not None and (type(total) is not int or total != len(source_ids)))
    ):
        raise DocumentServiceError('Formal source receipt has inconsistent completion counts')
    seen = set()
    for base in bases:
        if (
            not isinstance(base, dict)
            or base.get('sourceId') not in source_ids
            or base['sourceId'] in seen
            or base.get('kind') not in {'legacy', 'archive', 'not_published'}
        ):
            raise DocumentServiceError('Formal source receipt contains another source')
        seen.add(base['sourceId'])
        if base['kind'] != 'not_published':
            revision = base.get('legacySourceRevision')
            if base.get('canonicalReportId') != base['sourceId'] or not _valid_revision(revision):
                raise DocumentServiceError('Formal base receipt has an invalid archive identity')
    if value['status'] in {'completed', 'not_published'} and (count != len(source_ids) or total != count):
        raise DocumentServiceError('Formal source terminal receipt omits accepted sources')
    return deepcopy(value)


def _valid_revision(value):
    # BSON preserves legacy 53-bit hashes as Int64. Keep the numeric identity,
    # reject booleans and floats, and expose a built-in JSON integer below.
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 2**53 - 1


class _FormalProcessor(_FixedProcessor):
    def __init__(self, *args):
        super().__init__(*args)
        self.group_locks, self.projected = {}, {}

    async def project_group(self, indexes):
        indexes = tuple(indexes)
        async with self.group_locks.setdefault(indexes, asyncio.Lock()):
            if indexes in self.projected:
                return deepcopy(self.projected[indexes])
            items = tuple(self.prepared[index] for index in indexes)
            command = DocumentFormalSourceCommand(
                self.scope,
                'report:' + digest([self.user_id, self.session_id, self.final_report_id, list(indexes)]),
                'report',
                tuple(item.publication for item in items),
                REPORT_FORMAL_COMMAND_REVISION,
            )
            # Durable registration precedes downstream source work. Report restart
            # keeps its seal-only policy; Document recovery owns this command.
            submitted = await self.factory.formal.submit(command)
            _command_receipt(submitted, command)
            projection = await self.factory.formal.prepare_report(command)
            expected = {
                'scope': self.scope.as_dict(),
                'inputs': [item.as_dict() for item in command.inputs],
                'revision': REPORT_MANAGED_PREPARATION_REVISION,
                'interpretationProjectionRevision': 'document-understanding-public.v1',
            }
            if (
                not isinstance(projection, DocumentSnapshot)
                or projection.contract_version != MANAGED_REPORT_CONTRACT
                or projection.scope != self.scope
                or projection.source_id != items[0].bundle.material.source_id
                or projection.payload.get('manifest') != expected
                or projection.payload.get('selection') != asdict(REPORT_FIXED_SELECTION)
                or not isinstance(projection.payload.get('outputTraceId'), str)
                or not projection.payload['outputTraceId']
            ):
                raise DocumentServiceError('Formal preparation differs from the fixed Report inputs')
            # The public immutable output is reused by the consumer's own cache.
            # This performs no second v16 extraction or model call.
            data = await self.factory.projections.project_prepared(
                tuple(item.source for item in items), selection=self.selection, projection=projection
            )
            try:
                received = await self.factory.formal.execute(command)
            except (ValueError, PermissionError):
                raise
            except Exception as error:
                # The durable command survives a lost execution acknowledgement.
                # Unknown source delivery does not invalidate the business facts.
                receipt = None
                formal_state = {'status': 'unknown', 'errorType': type(error).__name__}
            else:
                receipt = _receipt(received, command, projection, {item.bundle.material.source_id for item in items})
                formal_state = {
                    key: deepcopy(receipt.get(key))
                    for key in ('status', 'phase', 'completedBaseCount', 'totalUniqueSources', 'errorCode')
                }
            formal_state.update(
                commandId=command.command_id,
                commandRevision=command.command_revision,
                projectionRef=projection.snapshot_id,
                preparationOutputTraceId=projection.payload['outputTraceId'],
            )
            result = receipt['result'] if receipt else {}
            first = items[0]
            base = next(
                (row for row in result.get('basePublications', []) if row.get('sourceId') == projection.source_id), {}
            )
            supplement = result.get('reportPublication') or {}
            if not isinstance(supplement, dict):
                raise DocumentServiceError('Formal report publication receipt is malformed')
            extraction_id = supplement.get('extractionId')
            publication_trace = supplement.get('outputTraceId')
            if extraction_id is not None and (
                not isinstance(extraction_id, str)
                or not extraction_id
                or not isinstance(publication_trace, str)
                or not publication_trace
            ):
                raise DocumentServiceError('Formal report publication lacks its actual execution trace')
            actual = supplement if supplement.get('legacySourceRevision') is not None else base
            revision = actual.get('legacySourceRevision')
            if revision is not None and not _valid_revision(revision):
                raise DocumentServiceError('Formal source returned an invalid legacy archive revision')
            revision = int(revision) if revision is not None else None
            report = report_understanding_content_v1(first.bundle.understanding.payload['interpretation'])
            report.update(
                reportId=base.get('canonicalReportId'),
                userId=self.scope.user_id,
                subjectId=self.scope.subject_id,
                documentId=first.source.document_id,
            )
            if revision is not None and ('processing' in report or revision > 1):
                report['sourceRevision'] = revision
            outcome = SimpleNamespace(
                report=report,
                structured_data=data,
                document_id=first.source.document_id,
                trace_id=first.publication.preparation_receipt.trace_id,
                reused=first.publication.preparation_receipt.reused,
                text=first.source.text,
                file_name=first.source.file_name,
                resolved_file_type=first.source.file_type,
                content_sha256=first.source.content_sha256,
                canonical_report_id=projection.source_id,
                source_revision=revision,
                ingestion_status=actual.get('ingestionStatus', 'unknown'),
                ingestion_job_id=actual.get('jobId'),
                extraction_id=extraction_id,
                output_trace_id=publication_trace if extraction_id is not None else projection.payload['outputTraceId'],
                formal_source=formal_state,
            )
            self.projected[indexes] = outcome
            return deepcopy(outcome)
