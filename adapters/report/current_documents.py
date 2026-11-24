"""Explicit Report consumer for current fixed Document material and publication."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from typing import Protocol

from pymongo.errors import AutoReconnect
from pymongo.errors import WTimeoutError

from adapters.report.current_document_checkpoints import MAX_DOCUMENT_ATTACHMENTS
from adapters.report.current_document_checkpoints import MAX_SUBMISSION_ATTEMPTS
from adapters.report.document_source import _parsed
from adapters.report.document_source import _retryable_download_failure
from modules.analysis.application.documents import DocumentBusyError
from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.application.documents import DocumentParsingError
from modules.analysis.compatibility import DocumentPreparationConflictError
from modules.analysis.complete_source_contracts import DocumentCompleteSourceUnavailable
from modules.analysis.complete_source_contracts import exact_text
from modules.analysis.current_report_contribution import report_occurrence_observations
from modules.analysis.current_understanding import DocumentCurrentBundle
from modules.analysis.current_understanding import DocumentPreparedInput
from modules.analysis.materials import DocumentSourceCall
from modules.analysis.publication import PublicationConflictError
from modules.analysis.publication import PublicationReceipt
from modules.analysis.snapshots import CURRENT_REPORT_PROJECTION_CONTRACT
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import DocumentSnapshot
from modules.analysis.snapshots import digest
from modules.report import parsing
from modules.report.document_projection import REPORT_SOURCE_SELECTION
from modules.report.evaluation_source_recovery import evaluation_clinical_source_rows
from modules.report.evaluation_sources import normalized_subject_sex
from modules.report.evaluation_sources import subject_mismatch
from modules.report.evaluation_template_pdf import parse_template_pdf_document
from modules.report.fallback_policy import DEFAULT_REPORT_FALLBACK_POLICY
from modules.report.ports import DocumentServiceError
from modules.report.ports import ReportDocumentSource
from modules.report.schemas import ParsedMicrobiomeReport
from modules.report.schemas import RemoteFileInput
from modules.report.schemas import SubjectInfo
from utils.report_content import report_public_content


class CurrentPreparedConsumer(Protocol):
    async def consume_prepared(
        self,
        *,
        scope: DocumentScope,
        prepared: DocumentPreparedInput,
        lifecycle_epoch: int,
        request_id: str,
        report_projection_ref: str | None = None,
    ) -> dict: ...


@dataclass(frozen=True)
class ReportDocumentSubject:
    scope: DocumentScope
    lifecycle_epoch: int
    identity: SubjectInfo


@dataclass(frozen=True)
class _Attachment:
    location: dict
    bundle: DocumentCurrentBundle
    prepared: DocumentPreparedInput


class CurrentReportDocumentFactory:
    supports_dedicated_evaluation_pdf = True

    def __init__(
        self,
        *,
        prepared,
        report_projections,
        consumer: CurrentPreparedConsumer,
        select_scope,
        checkpoints,
        material_reader,
        policy=DEFAULT_REPORT_FALLBACK_POLICY,
    ):
        self.prepared, self.report_projections, self.consumer = prepared, report_projections, consumer
        self.select_scope, self.policy = select_scope, policy
        self.projection_revision = report_projections.interpretation_projection_revision
        exact_text(self.projection_revision, 'the Report projection revision')
        self.checkpoints = checkpoints
        self.material_reader = material_reader

    def document_input_manifest(self):
        return {
            'consumerRevision': 'report-current-documents.v2',
            'interpretationProjectionRevision': self.projection_revision,
            'outputSelection': asdict(REPORT_SOURCE_SELECTION),
            'sourceFetchPolicy': {
                'attempts': self.policy.attempts_for(self.policy.source_fetch_attempts),
                'backoffSeconds': list(self.policy.retry_backoff_seconds),
            },
        }

    def create(self, *, user_id, session_id, final_report_id):
        return CurrentReportDocuments(self, user_id, session_id, final_report_id)

    async def recover_document_submissions(self):
        for count in range(1, 33):
            claim = await self.checkpoints.claim_submission(manifest=self.document_input_manifest())
            if claim is None:
                return count - 1
            await self._submit_claim(claim)
        return 32

    async def _submit_claim(self, claim):
        if claim[2]['attempts'] >= MAX_SUBMISSION_ATTEMPTS:
            outcome = {'ingestionStatus': 'unknown', 'ingestionErrorType': 'RetryExhausted'}
            return outcome if await self.checkpoints.finish_submission(claim, outcome) else None
        claim = await self.checkpoints.start_submission(claim)
        if claim is None:
            return None
        row, request_id, item = claim
        command = item['command']
        binding = row.get('documentBindings', {}).get('subject')
        try:
            if (
                not isinstance(binding, dict)
                or command['requestId'] != request_id
                or command['userId'] != row['userId']
                or any(command[field] != binding.get(field) for field in ('userId', 'subjectId', 'lifecycleEpoch'))
            ):
                raise ValueError('the fixed Report publication binding differs')
            scope = DocumentScope(command['userId'], command['subjectId'])
            prepared = DocumentPreparedInput.from_dict(command['prepared'])
        except (KeyError, TypeError, ValueError):
            outcome = {'ingestionStatus': 'not_submitted', 'ingestionErrorType': 'DocumentBindingInvalid'}
            return outcome if await self.checkpoints.finish_submission(claim, outcome) else None
        try:
            receipt = await self.consumer.consume_prepared(
                scope=scope,
                prepared=prepared,
                lifecycle_epoch=command['lifecycleEpoch'],
                request_id=command['requestId'],
                report_projection_ref=command['reportProjectionRef'],
            )
        except (DocumentBusyError, OSError, AutoReconnect, WTimeoutError):
            outcome = None
        except (
            PublicationConflictError,
            DocumentCompleteSourceUnavailable,
            DocumentNotFoundError,
            DocumentPreparationConflictError,
        ) as error:
            outcome = {'ingestionStatus': 'not_submitted', 'ingestionErrorType': type(error).__name__}
        else:
            try:
                outcome = CurrentReportDocuments._publication_reference(
                    receipt, command['sourceId'], command['sourceRevision']
                )
            except (DocumentServiceError, TypeError, ValueError, KeyError):
                outcome = {'ingestionStatus': 'unknown', 'ingestionErrorType': 'DocumentReceiptInvalid'}
        if not await self.checkpoints.finish_submission(claim, outcome):
            return None
        return outcome


class CurrentReportDocuments:
    def __init__(self, factory, user_id, session_id, final_report_id):
        self.factory, self.user_id, self.session_id, self.final_report_id = (
            factory,
            user_id,
            session_id,
            final_report_id,
        )
        self._scope_binding = None
        self._scope_lock = asyncio.Lock()
        self._attachment_locks = {}
        self._attachments = {}
        self._projections = {}
        self._readable_groups = {}
        self._report_contributions = set()
        self._single_references = {}
        self._original_subjects = {}
        self._native_originals = {}
        self._native_subjects = {}
        self._clinical_groups = {}
        self._evaluate = False
        self._batch = (final_report_id, user_id, session_id)
        self._manifest = factory.document_input_manifest()

    async def _scope(self):
        async with self._scope_lock:
            if self._scope_binding is None:
                stored = await self.factory.checkpoints.read_binding(self._batch, 'subject', self._manifest)
                if stored is None:
                    await self.factory.checkpoints.require_generation(self._batch, self._manifest)
                    binding = await self.factory.select_scope(self.user_id)
                    if (
                        not isinstance(binding, ReportDocumentSubject)
                        or not isinstance(binding.scope, DocumentScope)
                        or binding.scope.user_id != self.user_id
                        or type(binding.lifecycle_epoch) is not int
                        or binding.lifecycle_epoch < 1
                        or not isinstance(binding.identity, SubjectInfo)
                    ):
                        raise DocumentServiceError('current Report attachment requires a fixed subject lifecycle')
                    stored = await self.factory.checkpoints.put_binding_once(
                        self._batch,
                        'subject',
                        {
                            'userId': binding.scope.user_id,
                            'subjectId': binding.scope.subject_id,
                            'lifecycleEpoch': binding.lifecycle_epoch,
                            'identity': binding.identity.model_dump(mode='json'),
                        },
                        self._manifest,
                    )
                binding = ReportDocumentSubject(
                    DocumentScope(stored['userId'], stored['subjectId']),
                    stored['lifecycleEpoch'],
                    SubjectInfo.model_validate(stored['identity']),
                )
                if (
                    not isinstance(binding, ReportDocumentSubject)
                    or not isinstance(binding.scope, DocumentScope)
                    or binding.scope.user_id != self.user_id
                ):
                    raise PermissionError('current Report attachment belongs to another owner')
                if (
                    type(binding.lifecycle_epoch) is not int
                    or binding.lifecycle_epoch < 1
                    or not isinstance(binding.identity, SubjectInfo)
                ):
                    raise DocumentServiceError('current Report attachment requires a fixed subject lifecycle')
                self._scope_binding = ReportDocumentSubject(
                    binding.scope, binding.lifecycle_epoch, binding.identity.model_copy(deep=True)
                )
            return self._scope_binding

    def _request_id(self, indexes, phase):
        if not isinstance(self.final_report_id, str) or not self.final_report_id.strip():
            raise DocumentServiceError('current Report attachment requires the admitted report batch')
        return 'report_current_' + digest([self.user_id, self.session_id, self.final_report_id, indexes, phase])

    async def _prepare_attachment(self, source, index):
        location = (
            {'document_id': source.document_id}
            if source.document_id
            else {'file_id': source.url, 'file_type': source.file_type}
        )
        lock = self._attachment_locks.setdefault(index, asyncio.Lock())
        async with lock:
            previous = self._attachments.get(index)
            if previous is not None:
                if previous.location != location:
                    raise DocumentServiceError('the admitted Report attachment changed')
                return
            scope = (await self._scope()).scope
            key = f'attachment_{index}'
            saved = await self.factory.checkpoints.read_binding(self._batch, key, self._manifest)
            if saved is None:
                await self.factory.checkpoints.require_generation(self._batch, self._manifest)
                bundle = await self._prepare_original(scope, source, location, index)
                if not isinstance(bundle, DocumentCurrentBundle) or bundle.material.scope != scope:
                    raise PermissionError('prepared Report attachment differs from the selected document scope')
                prepared = self.factory.prepared.input(bundle)
                if not isinstance(prepared, DocumentPreparedInput):
                    raise DocumentServiceError('the original durable preparation input is unavailable')
                saved = await self.factory.checkpoints.put_binding_once(
                    self._batch, key, {'location': location, 'prepared': prepared.as_dict()}, self._manifest
                )
            if saved['location'] != location:
                raise DocumentServiceError('the admitted Report attachment changed')
            prepared = DocumentPreparedInput.from_dict(saved['prepared'])
            bundle = await self.factory.prepared.validate_prepared(scope=scope, prepared=prepared)
            if not isinstance(bundle, DocumentCurrentBundle) or bundle.material.scope != scope:
                raise PermissionError('prepared Report attachment differs from the selected document scope')
            self._attachments[index] = _Attachment(location, bundle, prepared)

    async def _prepare_original(self, scope, source, location, index):
        attempts = (
            1 if source.document_id else self.factory.policy.attempts_for(self.factory.policy.source_fetch_attempts)
        )
        for attempt in range(attempts):
            try:
                return await self.factory.prepared.prepare_bundle(
                    scope=scope,
                    **location,
                    source_type='report',
                    source_id=self.final_report_id,
                    session_id=self.session_id,
                    file_index=index,
                    lifecycle_epoch=self._scope_binding.lifecycle_epoch,
                )
            except Exception as error:
                if attempt + 1 == attempts or not _retryable_download_failure(error):
                    raise
                await asyncio.sleep(self.factory.policy.backoff_after(attempt))

    async def _project(self, indexes):
        binding = await self._scope()
        scope, epoch = binding.scope, binding.lifecycle_epoch
        inputs = tuple(self._attachments[index].prepared for index in indexes)
        request_id = self._request_id(indexes, 'projection')
        if fixed_ref := self._projections.get(indexes):
            snapshot = await self.factory.report_projections.read(scope=scope, snapshot_id=fixed_ref)
        else:
            await self.factory.checkpoints.require_generation(self._batch, self._manifest)
            snapshot = await self.factory.report_projections.prepare(
                scope=scope, inputs=inputs, request_id=request_id, lifecycle_epoch=epoch
            )
        primary = self._attachments[indexes[0]].bundle.material
        if (
            not isinstance(snapshot, DocumentSnapshot)
            or snapshot.scope != scope
            or snapshot.contract_version != CURRENT_REPORT_PROJECTION_CONTRACT
            or snapshot.source_id != primary.source_id
            or snapshot.source_revision != primary.source_revision
        ):
            raise DocumentServiceError('the fixed Report projection belongs to different material')
        payload = snapshot.payload
        manifest = payload['manifest']
        if (
            manifest['scope'] != scope.as_dict()
            or manifest['requestId'] != request_id
            or manifest['lifecycleEpoch'] != epoch
            or manifest['inputs'] != [item.as_dict() for item in inputs]
            or manifest['interpretationProjectionRevision'] != self.factory.projection_revision
            or payload['selection'] != asdict(REPORT_SOURCE_SELECTION)
            or payload['processingStatus'] not in {'complete', 'partial', 'unavailable'}
            or not isinstance(payload['traceId'], str)
            or not payload['traceId']
            or (fixed_ref and snapshot.snapshot_id != fixed_ref)
        ):
            raise DocumentServiceError('Report received a different fixed projection command')
        self._projections[indexes] = snapshot.snapshot_id
        return snapshot

    def _excluded_from_generation(self, index):
        facts = self._attachments[index].bundle.facts.payload
        if any(row.get('patientSlot') for row in (*facts['basicInfo'], *facts['observations'])):
            return True
        identity = {row['field']: row['value'] for row in facts['basicInfo']}
        return self._patient_conflict(SubjectInfo(name=identity.get('name'), sex=identity.get('gender')))

    def _patient_conflict(self, identity):
        expected = self._scope_binding.identity
        return subject_mismatch(SubjectInfo(name=expected.name), SubjectInfo(name=identity.name)) and (
            subject_mismatch(SubjectInfo(sex=expected.sex), SubjectInfo(sex=identity.sex))
        )

    @staticmethod
    def _excluded_patient_source(name, checksum):
        return ParsedMicrobiomeReport(
            source_type='pending',
            source_name=name,
            source_checksum=checksum,
            subject=SubjectInfo(),
            source_errors=['current Report document: unresolved or conflicting patient identity'],
        )

    async def _project_source(self, indexes, *, snapshot=None):
        excluded = [index for index in indexes if self._excluded_from_generation(index)]
        for index in excluded:
            # Preserve the attachment's Profile handoff even if excluding it
            # leaves this generation without any usable clinical input.
            await self._queue_fixed_contribution((index,))
        if snapshot is None:
            snapshot = await self._project(indexes)
        first = self._attachments[indexes[0]].bundle.material.payload
        payload = snapshot.payload
        quality = payload['processingStatus']
        if quality == 'unavailable' and not excluded:
            raise DocumentServiceError('current Report projection is unavailable')
        data = payload['projection']
        # These provider correlation pointers are used by Document during publication.
        for row in data.get('supplementaryHospitalData', ()):
            row.pop('analysisObservationRefs', None)
        if not excluded:
            parsed = _parsed(data)
            if self._patient_conflict(parsed.subject):
                excluded = indexes
                for index in excluded:
                    await self._queue_fixed_contribution((index,))
        if excluded:
            parsed = self._excluded_patient_source(first['fileName'], first['contentSha256'])
        if quality == 'partial':
            parsed.source_errors.append('current Report document: SHARED_INTERPRETATION_PARTIAL')
        processing = dict(data.get('processing') or {'status': 'complete', 'retryable': False})
        if len(indexes) > 1:
            # The same issue may appear in several inputs, so a sum would be misleading.
            processing.pop('issueCount', None)
        processing = report_public_content({'processing': processing})['processing']
        if (
            not excluded
            and quality == 'complete'
            and report_occurrence_observations(
                data, projection_ref=snapshot.snapshot_id, dependencies=payload['dependencies']
            )
        ):
            self._report_contributions.add(indexes)
        if len(indexes) == 1:
            index = indexes[0]
            attachment = self._attachments[index]
            material = attachment.bundle.material
            reference = {
                'fileIndex': index,
                'documentId': material.payload['documentId'],
                'contentSha256': material.payload['contentSha256'],
                'canonicalReportId': material.source_id,
                'reportId': material.source_id,
                'traceId': attachment.prepared.preparation_receipt.trace_id,
                'reused': attachment.prepared.preparation_receipt.reused,
                'processingStatus': 'completed' if quality == 'complete' else quality,
                'processing': processing,
                'ingestionStatus': 'not_submitted',
                'jobId': None,
                'sourceRevision': None,
                'extractionId': snapshot.snapshot_id,
                'reportProjectionSnapshotRef': snapshot.snapshot_id,
                'outputTraceId': payload['traceId'],
            }
            if excluded:
                reference['evaluationDisposition'] = 'different_subject_excluded'
            self._single_references[index] = reference
            references = (reference,)
        else:
            references = tuple(
                {
                    **self._single_references[index],
                    'reportProjectionSnapshotRef': snapshot.snapshot_id,
                    'groupExtractionId': snapshot.snapshot_id,
                    'groupOutputTraceId': payload['traceId'],
                    'groupCanonicalReportId': snapshot.source_id,
                    'groupProcessingStatus': 'completed' if quality == 'complete' else quality,
                    'groupProcessing': processing,
                    'groupIngestionStatus': 'not_submitted',
                    'groupJobId': None,
                    'groupSourceRevision': None,
                }
                for index in indexes
            )
        text = first['parsedDocument']['text'] if len(indexes) == 1 and not excluded else ''
        if len(indexes) == 1:
            self._original_subjects[indexes[0]] = parsed.subject.model_copy(deep=True)
        if not excluded:
            self._readable_groups[indexes] = snapshot.snapshot_id
        return ReportDocumentSource(
            parsed, text, parsed.source_name, 'pdf' if first['fileType'] == 'pdf' else 'image', references
        )

    def enable_native_evaluation(self):
        self._evaluate = True

    async def read(self, source, *, index):
        outcome = await self._read(source, index=index)
        return await self._with_evaluation_original(outcome, index) if self._evaluate else outcome

    async def _read(self, source, *, index):
        self._request_id((index,), 'projection')
        if type(index) is not int or not 0 <= index < MAX_DOCUMENT_ATTACHMENTS:
            raise ValueError('a fixed attachment index is required')
        source = RemoteFileInput.model_validate(source.model_dump(mode='python'))
        await self._prepare_attachment(source, index)
        return await self._project_source((index,))

    async def _with_evaluation_original(self, outcome, index):
        if outcome.file_type != 'pdf' or any(
            ref.get('evaluationDisposition') == 'different_subject_excluded' for ref in outcome.references
        ):
            return outcome
        if index not in self._native_originals:
            binding = await self._scope()
            material = self._attachments[index].bundle.material
            original = await self.factory.material_reader.read_original(
                scope=binding.scope, material_snapshot_ref=material.snapshot_id
            )
            try:
                self._native_originals[index] = await asyncio.to_thread(
                    parsing._extract_pdf, outcome.filename, original, ocr=False
                )
            except parsing.SourceParseError:
                self._native_originals[index] = None
        native = self._native_originals[index]
        if native is None:
            return outcome
        self._native_subjects[index] = parsing._document_subject(native)
        if self._patient_conflict(self._native_subjects[index]):
            await self._queue_fixed_contribution((index,))
            return replace(
                outcome,
                parsed=self._excluded_patient_source(outcome.filename, outcome.parsed.source_checksum),
                text='',
                references=tuple(
                    {**ref, 'evaluationDisposition': 'different_subject_excluded'} for ref in outcome.references
                ),
            )
        bundle = self._attachments[index].bundle
        report_type = bundle.understanding.payload['interpretation'].get('reportType')
        rows = evaluation_clinical_source_rows(
            native,
            filename=outcome.filename,
            checksum=bundle.material.payload['contentSha256'],
            report_type=report_type,
        )
        return replace(outcome, evaluation_clinical_rows=rows, evaluation_original_documents=(native,))

    async def read_dedicated_evaluation_pdf(self, source):
        index = MAX_DOCUMENT_ATTACHMENTS - 1
        source = RemoteFileInput.model_validate(source.model_dump(mode='python'))
        binding = await self._scope()
        started = datetime.now(UTC)
        try:
            await self._prepare_attachment(source, index)
        except DocumentParsingError as failure:
            call = DocumentSourceCall(
                entrypoint='report',
                source_id=self.final_report_id,
                session_id=self.session_id,
                file_index=index,
                requested_document_id=source.document_id,
                file_url=None if source.document_id else source.url,
                started_at=started,
                trace_id=getattr(failure, 'trace_id', None),
            )
            if failure.reason_code == 'ocr_query_failed':
                material = await self.factory.material_reader.capture_failed_original(
                    scope=binding.scope, call=call, failure=failure
                )
            else:
                material = await self.factory.material_reader.capture_failed_material(
                    scope=binding.scope, call=call, failure=failure
                )
            if material is None:
                raise
            trace = material.payload['failureTrace']
            return await self._native_evaluation_fallback(
                binding,
                material,
                index=index,
                trace_id=trace['traceId'],
                error_type=trace['errorType'],
            )
        if self._excluded_from_generation(index):
            return await self._project_source((index,))
        snapshot = await self._project((index,))
        if snapshot.payload['processingStatus'] == 'unavailable':
            attachment = self._attachments[index]
            return await self._native_evaluation_fallback(
                binding,
                attachment.bundle.material,
                index=index,
                trace_id=attachment.prepared.preparation_receipt.trace_id,
                error_type='DocumentCompleteSourceUnavailable',
                projection_ref=snapshot.snapshot_id,
            )
        return await self._with_evaluation_original(await self._project_source((index,), snapshot=snapshot), index)

    async def _native_evaluation_fallback(self, binding, material, *, index, trace_id, error_type, projection_ref=None):
        if (
            not isinstance(material, DocumentSnapshot)
            or material.scope != binding.scope
            or material.payload['fileType'] != 'pdf'
        ):
            raise DocumentServiceError('the exact failed PDF material is required')
        original = await self.factory.material_reader.read_original(
            scope=binding.scope, material_snapshot_ref=material.snapshot_id
        )
        payload = material.payload
        native = await asyncio.to_thread(parsing._extract_pdf, payload['fileName'], original, ocr=False)
        parsed = parse_template_pdf_document(native)
        if parsed is None or parsed.source_checksum != payload['contentSha256']:
            raise DocumentServiceError('the fixed original PDF has no verified native evaluation')
        parsed.source_errors = [f'fileList[{index}]: SHARED_INTERPRETATION_FALLBACK']
        reference = {
            'fileIndex': index,
            'documentId': payload['documentId'],
            'contentSha256': payload['contentSha256'],
            'canonicalReportId': material.source_id,
            'traceId': trace_id,
            'reportId': None,
            'processingStatus': 'fallback_source_only',
            'sharedProcessingStatus': 'failed',
            'ingestionStatus': 'not_submitted',
            'jobId': None,
            'errorType': error_type,
            'fallbackReason': 'SHARED_INTERPRETATION_FALLBACK',
            **({'reportProjectionSnapshotRef': projection_ref} if projection_ref else {}),
        }
        if self._patient_conflict(parsed.subject):
            reference['evaluationDisposition'] = 'different_subject_excluded'
            return ReportDocumentSource(
                self._excluded_patient_source(native.filename, payload['contentSha256']),
                '',
                native.filename,
                'pdf',
                (reference,),
            )
        return ReportDocumentSource(
            parsed, native.text, native.filename, 'pdf', (reference,), evaluation_original_documents=(native,)
        )

    async def combine(self, indexes):
        indexes = tuple(indexes)
        if (
            len(indexes) < 2
            or len(set(indexes)) != len(indexes)
            or any(type(index) is not int or index not in self._attachments for index in indexes)
            or tuple(sorted(indexes)) != indexes
        ):
            raise DocumentServiceError('an ordered group of prepared Report attachments is required')
        outcome = await self._project_source(indexes)
        if outcome.parsed.source_type == 'pending' and outcome.parsed.supplementary_hospital_data:
            self._clinical_groups[indexes] = outcome.references
        return outcome

    def _clinical_group_references(self, references):
        by_index = {row['fileIndex']: row for row in references}
        for indexes, group in self._clinical_groups.items():
            if all(
                (row := by_index.get(index)) is not None
                and row.get('evaluationDisposition') != 'different_subject_excluded'
                and row.get('reportProjectionSnapshotRef') == self._readable_groups.get((index,))
                for index in indexes
            ):
                for row in group:
                    by_index[row['fileIndex']] = row
        return [by_index[row['fileIndex']] for row in references]

    async def _queue_fixed_contribution(self, indexes, *, projection_ref=None):
        binding = self._scope_binding
        attachment = self._attachments[indexes[0]]
        phase = 'contribution' if projection_ref is not None else 'analysis_contribution'
        request_id = self._request_id(indexes, phase)
        material = attachment.bundle.material
        command = {
            **binding.scope.as_dict(),
            'lifecycleEpoch': binding.lifecycle_epoch,
            'requestId': request_id,
            'prepared': attachment.prepared.as_dict(),
            'reportProjectionRef': projection_ref,
            'sourceId': material.source_id,
            'sourceRevision': material.source_revision,
        }
        try:
            outcome = await self.factory.checkpoints.queue_once(self._batch, request_id, command, self._manifest)
            if outcome is None:
                claim = await self.factory.checkpoints.claim_submission(batch=self._batch, manifest=self._manifest)
                if claim is not None:
                    resolved = await self.factory._submit_claim(claim)
                    if claim[1] == request_id:
                        outcome = resolved
            if outcome is not None:
                return outcome
            return {'ingestionStatus': 'unknown', 'ingestionErrorType': 'DocumentSubmissionPending'}
        except (OSError, AutoReconnect, WTimeoutError):
            # A missing acknowledgement cannot decide whether a submission persisted.
            return {'ingestionStatus': 'unknown', 'ingestionErrorType': 'DocumentSubmissionUnconfirmed'}

    async def settle_attachments(self, references, *, expected_subject=None):
        references = self._clinical_group_references(references)
        references_by_index = {
            row['fileIndex']: row.get('reportProjectionSnapshotRef')
            for row in references
            if row.get('processingStatus') in {'completed', 'partial'}
            and row.get('evaluationDisposition') != 'different_subject_excluded'
        }
        by_index, eligible = {}, []
        for indexes, fixed_ref in self._readable_groups.items():
            if not all(references_by_index.get(index) == fixed_ref for index in indexes):
                continue
            if self._same_patient(indexes, self._scope_binding.identity, expected_subject):
                eligible.append((indexes, fixed_ref))
                continue
            for index in indexes:
                by_index[index] = {
                    'ingestionStatus': 'not_submitted',
                    'jobId': None,
                    'sourceRevision': None,
                    'ingestionDisposition': 'different_subject_excluded',
                    **(
                        {'groupIngestionStatus': 'not_submitted', 'groupJobId': None, 'groupSourceRevision': None}
                        if len(indexes) > 1
                        else {}
                    ),
                }

        # A Report command includes the primary file's Analysis input.
        # The other attachments contribute their fixed Analysis results separately.
        inline = {indexes[0] for indexes, _ in eligible}
        for index in dict.fromkeys(row['fileIndex'] for row in references):
            if index not in self._attachments or index in inline:
                continue
            await self._queue_fixed_contribution((index,))

        for indexes, fixed_ref in eligible:
            outcome = await self._queue_fixed_contribution(indexes, projection_ref=fixed_ref)
            if indexes not in self._report_contributions:
                continue
            revision = self._attachments[indexes[0]].bundle.material.source_revision
            public = self._clinical_publication_reference(outcome, source_revision=revision)
            if len(indexes) > 1:
                public = {
                    'groupIngestionStatus': public['ingestionStatus'],
                    'groupJobId': public['jobId'],
                    'groupSourceRevision': public['sourceRevision'],
                    **({'ingestionErrorType': public['ingestionErrorType']} if 'ingestionErrorType' in public else {}),
                }
            for index in indexes:
                by_index[index] = {**public, 'submittedProjectionSnapshotRef': fixed_ref}
        return [{**row, **by_index.get(row['fileIndex'], {})} for row in references]

    @staticmethod
    def public_attachment_references(references):
        public_fields = {
            'fileIndex',
            'documentId',
            'reportId',
            'canonicalReportId',
            'traceId',
            'reused',
            'extractionId',
            'outputTraceId',
            'sourceRevision',
            'processingStatus',
            'processing',
            'ingestionStatus',
            'jobId',
            'errorType',
            'recoveryErrorType',
            'sharedProcessingStatus',
            'fallbackReason',
            'sourceExtractorVersion',
            'groupExtractionId',
            'groupOutputTraceId',
            'groupCanonicalReportId',
            'groupSourceRevision',
            'groupIngestionStatus',
            'groupJobId',
            'groupProcessingStatus',
            'groupProcessing',
        }
        return [{key: value for key, value in row.items() if key in public_fields} for row in references]

    @staticmethod
    def _clinical_publication_reference(outcome, *, source_revision):
        state = outcome['ingestionStatus']
        if state in {
            'received',
            'pending',
            'waiting',
            'processing',
            'retry_wait',
            'partial',
            'applied',
            'failed',
            'superseded',
        }:
            state = 'queued'
        elif state == 'unchanged':
            # A fixed contribution can already exist without another queue job.
            state = 'not_submitted'
        return {
            **outcome,
            'ingestionStatus': state,
            'jobId': outcome.get('jobId'),
            'sourceRevision': source_revision if state == 'queued' else None,
        }

    def _same_patient(self, indexes, profile_identity, expected_identity):
        identities = [profile_identity]
        if expected_identity is not None:
            identities.append(expected_identity)
        for index in indexes:
            identities.append(self._original_subjects[index])
            if index in self._native_subjects:
                identities.append(self._native_subjects[index])
            accepted = {
                row['field']: row['value'] for row in self._attachments[index].bundle.facts.payload['basicInfo']
            }
            identities.append(SubjectInfo(name=accepted.get('name'), sex=accepted.get('gender')))
        anchor = SubjectInfo()
        for item in identities:
            if subject_mismatch(anchor, item):
                return False
            anchor = SubjectInfo(name=anchor.name or item.name, sex=anchor.sex or normalized_subject_sex(item.sex))
        return True

    @staticmethod
    def _publication_reference(receipt, source_id, source_revision):
        if (
            not isinstance(receipt, dict)
            or receipt.get('sourceId') != source_id
            or receipt.get('status') not in {'published', 'unchanged', 'no_contribution'}
            or receipt.get('sourceRevision') not in {None, source_revision}
        ):
            raise DocumentServiceError('Document returned a different publication receipt')
        projection = {
            'currentSourceSnapshotRef': receipt['sourceSnapshotRef'],
            'publicationRevision': receipt['publicationRevision'],
        }
        status = receipt['status']
        publication = receipt['publication']
        if (
            status == 'no_contribution'
            and publication is None
            and not any(projection.values())
            and receipt['sourceRevision'] is None
        ):
            return {'ingestionStatus': 'not_submitted'}
        if (
            not isinstance(projection['currentSourceSnapshotRef'], str)
            or not projection['currentSourceSnapshotRef']
            or type(projection['publicationRevision']) is not int
            or projection['publicationRevision'] < 1
            or type(receipt['sourceRevision']) is not int
        ):
            raise DocumentServiceError('Document publication receipt has no fixed source reference')
        if status == 'unchanged' and publication is None:
            return {**projection, 'ingestionStatus': 'unchanged'}
        if status not in {'published', 'unchanged'} or not isinstance(publication, dict):
            raise DocumentServiceError('Document publication receipt state differs from its acknowledgement')
        publication = PublicationReceipt(**publication)
        if publication.source_id != source_id or publication.publication_revision != receipt['publicationRevision']:
            raise DocumentServiceError('Document publication receipt differs from its source binding')
        return {**projection, 'ingestionStatus': publication.state, 'jobId': publication.receipt_ref}
