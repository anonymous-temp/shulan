"""Report-owned interpretation over fixed public Document inputs and commands."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from dataclasses import dataclass
from types import SimpleNamespace

from modules.analysis.application.documents import DocumentParsingError
from modules.analysis.application.service import HealthReportUpstreamError
from modules.analysis.contracts import OutputSelection
from modules.analysis.contracts import UnsupportedOutputVersionError
from modules.analysis.materials import DocumentSourceCall
from modules.analysis.report_publication_contracts import DocumentPreparedPublicationInput
from modules.analysis.report_publication_contracts import DocumentReportPublicationManifest
from modules.analysis.report_publication_contracts import DocumentReportPublicationReceipt
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import DocumentSnapshot
from modules.report import parsing
from modules.report.artifacts import note_report_documents
from modules.report.document_projection import ReportSourceInput
from modules.report.document_rendering import REPORT_INTERPRETATION_PROJECTION_REVISION
from modules.report.document_rendering import report_understanding_content_v1
from modules.report.fallback_policy import DEFAULT_REPORT_FALLBACK_POLICY
from modules.report.ports import DocumentServiceError
from modules.report.ports import ReportDocumentSource
from modules.report.schemas import RemoteFileInput

from .document_source import SharedReportDocuments
from .document_source import UnavailableReportDocuments
from .document_source import _parsed
from .document_source import _parsed_output
from .document_source import _processing
from .document_source import _reference

REPORT_FIXED_SELECTION = OutputSelection('report_source', '1', 'report-source-v16')
REPORT_INPUT_SELECTION = OutputSelection('legacy_understanding', '1', 'legacy-understanding-v1')
REPORT_PUBLICATION_SELECTION = OutputSelection('report_source', '1', 'report-source-v16')


@dataclass(frozen=True)
class _Prepared:
    location: tuple
    publication: DocumentPreparedPublicationInput
    source: ReportSourceInput
    bundle: object


class FixedReportDocumentFactory:
    def __init__(
        self,
        *,
        compatibility,
        materials,
        publications,
        projections,
        selection=REPORT_FIXED_SELECTION,
        policy=DEFAULT_REPORT_FALLBACK_POLICY,
    ):
        self.compatibility, self.materials = compatibility, materials
        self.publications, self.projections = publications, projections
        self.selection, self.policy = selection, policy

    def document_input_manifest(self):
        return {
            'consumerRevision': 'report-fixed-documents.v1',
            'inputSelection': asdict(REPORT_INPUT_SELECTION),
            'interpretationProjectionRevision': REPORT_INTERPRETATION_PROJECTION_REVISION,
            'outputSelection': asdict(self.selection),
            'publicationSelection': asdict(REPORT_PUBLICATION_SELECTION),
            'fallbackRevision': 'report-document-fallback.v1',
            'sourceFetchPolicy': {
                'attempts': self.policy.attempts_for(self.policy.source_fetch_attempts),
                'backoffSeconds': list(self.policy.retry_backoff_seconds),
            },
        }

    def create(self, *, user_id, session_id, final_report_id):
        try:
            processor = _FixedProcessor(self, user_id, session_id, final_report_id)
            return FixedReportDocuments(
                processor, user_id, session_id, final_report_id, archive_locator=True, policy=self.policy
            )
        except Exception as error:
            return UnavailableReportDocuments(type(error).__name__)


def create_fixed_report_document_source_factory(
    *,
    compatibility,
    materials,
    publications,
    projections,
    selection=REPORT_FIXED_SELECTION,
    policy=DEFAULT_REPORT_FALLBACK_POLICY,
):
    """Compose without I/O; root owns readers, persistent caches and publication resources."""
    return FixedReportDocumentFactory(
        compatibility=compatibility,
        materials=materials,
        publications=publications,
        projections=projections,
        selection=selection,
        policy=policy,
    )


class _FixedProcessor:
    """Only the Report parser's local shape is adapted; no mutable Document index exists here."""

    def __init__(self, factory, user_id, session_id, final_report_id):
        self.factory, self.user_id, self.session_id, self.final_report_id = (
            factory,
            user_id,
            session_id,
            final_report_id,
        )
        self.selection = factory.selection
        if self.selection not in factory.projections.implementations:
            raise UnsupportedOutputVersionError('the exact Report consumer projection is unavailable')
        self.scope = None
        self.prepared, self.originals, self.failures, self.locks = {}, {}, {}, {}

    def _identity(self, values):
        expected = {
            'user_id': self.user_id,
            'session_id': self.session_id,
            'source_type': 'report',
            'source_id': self.final_report_id,
            'output_profile': 'report_source',
            'output_selection': self.selection,
        }
        if any(values.get(key) != value for key, value in expected.items()):
            raise PermissionError('Report document command identity changed')

    async def _scope(self):
        if self.scope is None:
            scope = await self.factory.compatibility.ensure_self_scope(self.user_id)
            if not isinstance(scope, DocumentScope) or scope.user_id != self.user_id:
                raise PermissionError('Document preparation resolved another owner')
            if self.scope is not None and self.scope != scope:
                raise PermissionError('Document preparation changed its selected subject')
            self.scope = scope
        return self.scope

    def _remember_original(self, source):
        previous = self.originals.get(source.document_id)
        if previous is not None and previous != source:
            raise DocumentServiceError('one document has conflicting fixed Report inputs')
        self.originals[source.document_id] = source

    async def process_document(self, **options):
        self._identity(options)
        index = options['file_index']
        location = tuple((key, options[key]) for key in ('document_id', 'file_id', 'file_type') if key in options)
        lock = self.locks.setdefault(index, asyncio.Lock())
        async with lock:
            scope = await self._scope()
            prepared = self.prepared.get(index)
            if prepared is not None and prepared.location != location:
                raise DocumentServiceError('the accepted attachment changed during retry')
            if prepared is None:
                try:
                    bundle = await self.factory.compatibility.prepare_bundle(
                        scope=scope,
                        selection=REPORT_INPUT_SELECTION,
                        require_text=True,
                        **dict(location),
                        source_type='report',
                        source_id=self.final_report_id,
                        session_id=self.session_id,
                        file_index=index,
                    )
                except DocumentParsingError as error:
                    self.failures[index] = error
                    raise HealthReportUpstreamError('shared document interpretation failed') from error
                except Exception as error:
                    self.failures[index] = error
                    raise
                content = await self.factory.materials.read_original(
                    scope=scope, material_snapshot_ref=bundle.material.snapshot_id
                )
                source = ReportSourceInput.from_bundle(bundle, content)
                if bundle.material.scope != scope:
                    raise PermissionError('prepared Report input belongs to another scope')
                self._remember_original(source)
                prepared = _Prepared(
                    location,
                    DocumentPreparedPublicationInput(
                        bundle.material.snapshot_id,
                        bundle.facts.snapshot_id,
                        bundle.understanding.snapshot_id,
                        REPORT_INPUT_SELECTION,
                        bundle.receipt,
                    ),
                    source,
                    bundle,
                )
                # Freeze refs and the actual receipt before formal publication.
                # Interrupted Report tasks still follow the existing terminal policy.
                self.prepared[index] = prepared
            return await self.project_group((index,))

    async def project_group(self, indexes):
        items = tuple(self.prepared[index] for index in indexes)
        scope = self.scope
        manifest = DocumentReportPublicationManifest(
            scope, tuple(item.publication for item in items), REPORT_PUBLICATION_SELECTION
        )
        receipt = await self.factory.publications.publish_report_source(
            scope=scope, inputs=manifest.inputs, selection=manifest.selection
        )
        if not isinstance(receipt, DocumentReportPublicationReceipt) or receipt.manifest != manifest:
            raise DocumentServiceError('Document publication returned another fixed command')
        projection = await self.factory.publications.read_projection(
            scope=scope, projection_ref=receipt.projection_ref, selection=manifest.selection
        )
        if not isinstance(projection, DocumentSnapshot):
            raise DocumentServiceError('Document returned no immutable publication projection')
        metadata = projection.payload
        if (
            projection.scope != scope
            or projection.contract_version != 'document.legacy-report-source.v1'
            or metadata.get('selection') != asdict(manifest.selection)
            or projection.source_id != receipt.canonical_report_id
            or metadata.get('legacySourceRevision') != receipt.legacy_source_revision
            or metadata.get('processingStatus') != receipt.processing_status
        ):
            raise DocumentServiceError('Document publication projection differs from its receipt')
        for name in ('extractionId', 'outputTraceId'):
            if not isinstance(metadata.get(name), str) or not metadata[name]:
                raise DocumentServiceError('Document publication lacks its actual output reference')
        # Consumer content comes exclusively from Report's versioned private cache.
        # The compatibility projection supplies formal source references only.
        data = await self.factory.projections.project(tuple(item.source for item in items), selection=self.selection)
        first, base = items[0], receipt.base_publications[0]
        report = report_understanding_content_v1(first.bundle.understanding.payload['interpretation'])
        report.update(
            reportId=base.canonical_report_id,
            userId=scope.user_id,
            subjectId=scope.subject_id,
            documentId=first.source.document_id,
        )
        if 'processing' in report or base.legacy_source_revision > 1:
            report['sourceRevision'] = base.legacy_source_revision
        return SimpleNamespace(
            report=report,
            structured_data=data,
            document_id=first.source.document_id,
            trace_id=first.publication.preparation_receipt.trace_id,
            reused=first.publication.preparation_receipt.reused,
            text=first.source.text,
            file_name=first.source.file_name,
            resolved_file_type=first.source.file_type,
            content_sha256=first.source.content_sha256,
            canonical_report_id=receipt.canonical_report_id,
            source_revision=receipt.legacy_source_revision,
            ingestion_status=receipt.ingestion_status,
            ingestion_job_id=receipt.ingestion_job_id,
            extraction_id=metadata['extractionId'],
            output_trace_id=metadata['outputTraceId'],
        )

    async def read_document_payload(self, document_id, *, user_id, subject_id, name='original_file'):
        if (
            name != 'original_file'
            or self.scope is None
            or (user_id, subject_id) != (self.scope.user_id, self.scope.subject_id)
        ):
            raise PermissionError('only the selected Report original is readable')
        source = self.originals.get(document_id)
        if source is None:
            raise DocumentServiceError('no fixed Report material is selected for this document')
        return {'content': source.content}


class FixedReportDocuments(SharedReportDocuments):
    async def read(self, source, *, index):
        # Caller-owned Pydantic values must not change recovery's call identity
        # while prepare/download/publication awaits external work.
        source = RemoteFileInput.model_validate(source.model_dump(mode='python'))
        if type(index) is not int or index < 0:
            raise ValueError('a nonnegative accepted attachment index is required')
        return await super().read(source, index=index)

    def _identity(self):
        return {**super()._identity(), 'output_selection': self.processor.selection}

    async def _recover(self, source, index, started):
        if not self.final_report_id or self.processor.scope is None:
            return None
        failure = self.processor.failures.get(index)
        if failure is None:
            return None
        call = DocumentSourceCall(
            entrypoint='report',
            source_id=self.final_report_id,
            session_id=self.session_id,
            file_index=index,
            requested_document_id=source.document_id,
            file_url=None if source.document_id else source.url,
            started_at=started,
        )
        material = await self.processor.factory.materials.capture_failed_material(
            scope=self.processor.scope, call=call, failure=failure
        )
        if material is None:
            return None
        content = await self.processor.factory.materials.read_original(
            scope=self.processor.scope, material_snapshot_ref=material.snapshot_id
        )
        fixed = ReportSourceInput.from_failed_material(material, content)
        self.processor._remember_original(fixed)
        data = await self.processor.factory.projections.project((fixed,), selection=self.processor.selection)
        parsed = _parsed(data)
        if not (parsed.metrics or parsed.taxa or parsed.disease_results or parsed.supplementary_hospital_data):
            return None
        parsed.source_errors = [f'fileList[{index}]: SHARED_INTERPRETATION_FALLBACK']
        trace = material.payload['failureTrace']
        reference = {
            'fileIndex': index,
            'documentId': fixed.document_id,
            'traceId': trace['traceId'],
            'canonicalReportId': material.source_id,
            'reportId': None,
            'processingStatus': 'fallback_source_only',
            'sharedProcessingStatus': 'failed',
            'ingestionStatus': 'not_submitted',
            'jobId': None,
            'errorType': trace['errorType'],
            'fallbackReason': 'SHARED_INTERPRETATION_FALLBACK',
            'sourceExtractorVersion': self.processor.selection.implementation_revision,
        }
        originals = ()
        if fixed.file_type == 'pdf':
            try:
                originals = (await asyncio.to_thread(parsing._extract_pdf, fixed.file_name, content, ocr=False),)
            except Exception:
                pass
        result = ReportDocumentSource(
            parsed,
            fixed.text,
            fixed.file_name,
            'pdf' if fixed.file_type == 'pdf' else 'image',
            (reference,),
            evaluation_original_documents=originals,
        )
        self.recovered[index] = (fixed, result)
        note_report_documents([reference])
        return result

    async def combine(self, indexes):
        indexes = tuple(indexes)
        if any(index in self.recovered for index in indexes):
            if any(
                index not in self.recovered
                and _processing(self.outcomes[index]).get('status', 'complete') != 'complete'
                for index in indexes
            ):
                raise DocumentServiceError('cannot combine raw fallback with a quarantined source')
            inputs, references = [], []
            for index in indexes:
                if index in self.recovered:
                    fixed, result = self.recovered[index]
                    inputs.append(fixed)
                    references.extend(result.references)
                else:
                    inputs.append(self.processor.prepared[index].source)
                    references.append(_reference(self.outcomes[index], index))
            data = await self.processor.factory.projections.project(tuple(inputs), selection=self.processor.selection)
            parsed = _parsed(data)
            parsed.source_errors = ['documentGroup: SHARED_INTERPRETATION_FALLBACK']
            references = [dict(item, groupProcessingStatus='fallback_source_only') for item in references]
            note_report_documents(references)
            return ReportDocumentSource(parsed, '', parsed.source_name or '', 'image', tuple(references))
        outcome = await self.processor.project_group(indexes)
        inputs = tuple(self.outcomes[index] for index in indexes)
        processing = _processing(outcome, *inputs)
        parsed = _parsed_output(outcome, location='documentGroup', inputs=inputs)
        references = []
        for index in indexes:
            reference = _reference(self.outcomes[index], index)
            reference.update(
                groupExtractionId=outcome.extraction_id,
                groupOutputTraceId=outcome.output_trace_id,
                groupCanonicalReportId=outcome.canonical_report_id,
                groupSourceRevision=outcome.source_revision,
                groupIngestionStatus=outcome.ingestion_status,
                groupJobId=outcome.ingestion_job_id,
                groupProcessingStatus='completed'
                if processing.get('status', 'complete') == 'complete'
                else processing['status'],
                groupProcessing=processing,
            )
            if hasattr(outcome, 'formal_source'):
                reference['groupFormalSource'] = dict(outcome.formal_source)
            references.append(reference)
        note_report_documents(references)
        return ReportDocumentSource(parsed, '', outcome.file_name, 'image', tuple(references))
