"""Compose the current standalone Analysis entry with fixed Document publication."""

from copy import deepcopy
from dataclasses import dataclass
from dataclasses import replace
from types import SimpleNamespace

from adapters.analysis.formal_source_store import MongoDocumentFormalSourceStore
from modules.analysis.analysis_formal_contracts import ANALYSIS_CURRENT_PUBLICATION
from modules.analysis.analysis_formal_contracts import AnalysisFormalCommand
from modules.analysis.analysis_formal_delivery import AnalysisFormalDelivery
from modules.analysis.analysis_formal_preparation import _verify
from modules.analysis.analysis_formal_readiness import AnalysisFormalReadiness
from modules.analysis.application.current_interpreter import CURRENT_HEALTH_REPORT_SELECTION
from modules.analysis.application.documents import DocumentBusyError
from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.application.documents import ProcessedHealthReport
from modules.analysis.basic_info_contracts import ANALYSIS_PUBLIC_CONFIRMATION
from modules.analysis.current_understanding import DocumentPreparedInput
from modules.analysis.formal_source import DocumentFormalSourceWait
from modules.analysis.formal_source_contracts import DocumentFormalSourceConflict
from modules.analysis.profile_delivery import has_clinical_delivery
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import digest
from modules.analysis.understanding import DocumentPreparationReceipt
from modules.health_profile.application import HealthReportSourceConflictError


@dataclass(frozen=True, slots=True)
class AnalysisCurrentCommand(AnalysisFormalCommand):
    publication_revision_name = ANALYSIS_CURRENT_PUBLICATION
    command_revision: str = ANALYSIS_CURRENT_PUBLICATION
    lifecycle_epoch: int = 0
    confirmation_basis: str | None = None

    def __post_init__(self):
        AnalysisFormalCommand.__post_init__(self)
        if type(self.lifecycle_epoch) is not int or self.lifecycle_epoch < 1:
            raise ValueError('the original Analysis subject lifecycle is required')
        if self.interpretation_revision != CURRENT_HEALTH_REPORT_SELECTION.implementation_revision:
            raise ValueError('the fixed current Analysis interpreter is required')
        if self.confirmation_basis not in (None, ANALYSIS_PUBLIC_CONFIRMATION):
            raise ValueError('the fixed Analysis confirmation basis is invalid')

    def as_dict(self):
        result = AnalysisFormalCommand.as_dict(self)
        if self.confirmation_basis is None:
            del result['confirmation_basis']
        return result

    @classmethod
    def from_dict(cls, raw):
        if isinstance(raw, dict) and 'confirmation_basis' not in raw:
            raw = {**raw, 'confirmation_basis': None}
        return super(AnalysisCurrentCommand, cls).from_dict(raw)

    @property
    def command_id(self):
        return 'analysis_current_' + digest([self.scope.as_dict(), self.request_id])


class AnalysisCurrentDelivery(AnalysisFormalDelivery):
    command_type = AnalysisCurrentCommand


class AnalysisCurrentReadiness(AnalysisFormalReadiness):
    prepared_type = DocumentPreparedInput

    def __init__(self, *, store, prepared, index, responses):
        super().__init__(store=store, compatibility=None, index=index, responses=responses)
        self.prepared = prepared

    @staticmethod
    def _receipt(command):
        return DocumentPreparationReceipt(
            command.document_id, command.trace_id, command.document_id != command.trace_id
        )

    async def _capture(self, command):
        scope = command.scope
        document = await self.index.get(command.document_id, scope.user_id, scope.subject_id)
        if document is None or (document['canonicalReportId'], document['contentSha256']) != (
            command.canonical_report_id,
            command.content_sha256,
        ):
            raise DocumentFormalSourceConflict('the owned Analysis document identity changed')
        if command.report_id is not None and command.report_id not in document.get('reportAliases', []):
            raise DocumentFormalSourceConflict('the Analysis report identity was not reserved')
        try:
            saved = await self.prepared.records.get_preparation(scope, command.trace_id)
        except DocumentNotFoundError:
            saved = None
        if saved is not None:
            item = self.prepared_type.from_dict(saved)
            return item, await self._bundle(command, item)
        try:
            bundle = await self.prepared.understanding.capture_round_bundle(
                scope=scope, document_id=command.document_id, interpretation_round=command.interpretation_round
            )
        except DocumentNotFoundError as error:
            result_round = document.get('interpretationResultRound', document.get('interpretationRound', 0))
            if result_round > command.interpretation_round or (
                document['status'] == 'failed' and document.get('retryable') is False
            ):
                raise DocumentFormalSourceConflict('the accepted Analysis round is no longer available') from error
            raise DocumentFormalSourceWait() from error
        _verify(command, bundle)
        item = await self.prepared.remember(replace(bundle, receipt=self._receipt(command)))
        return item, await self._bundle(command, item)

    async def _bundle(self, command, prepared):
        if (
            prepared.preparation_receipt != self._receipt(command)
            or prepared.selection != CURRENT_HEALTH_REPORT_SELECTION
        ):
            raise DocumentFormalSourceConflict('the prepared Analysis binding identifies a different execution')
        bundle = await self.prepared.validate_prepared(scope=command.scope, prepared=prepared)
        _verify(command, bundle)
        return bundle


class AnalysisCurrentSteps:
    def __init__(self, *, readiness, consumption, resolve_subject):
        self.readiness, self.consumption = readiness, consumption
        self.resolve_subject = resolve_subject

    async def subject_epoch(self, scope):
        subject = await self.resolve_subject(scope)
        if subject is None or (subject.user_id, subject.subject_id, subject.status) != (
            scope.user_id,
            scope.subject_id,
            'active',
        ):
            raise DocumentNotFoundError('the Analysis subject is unavailable')
        return subject.lifecycle_epoch

    async def step(self, command, row):
        if row['phase'] == 'prepare':
            ready, bundle = await self.readiness.prepare(command)
            await self.bind_result(command)
            facts = bundle.facts.payload
            if not has_clinical_delivery(facts) and not facts['basicInfo']:
                return {'status': 'not_published'}
            return {'phase': 'publish', 'progress': {'prepared': ready['prepared']}}
        if row['phase'] != 'publish':
            raise DocumentFormalSourceConflict('unknown current Analysis delivery phase')
        item = DocumentPreparedInput.from_dict(row['progress']['prepared'])
        await self.readiness.prepare(command, prepared=item)
        try:
            lifecycle_epoch = await self.subject_epoch(command.scope)
        except DocumentNotFoundError as error:
            raise DocumentFormalSourceConflict('the accepted Analysis subject is unavailable') from error
        if lifecycle_epoch != command.lifecycle_epoch:
            raise DocumentFormalSourceConflict('the accepted Analysis subject lifecycle ended')
        try:
            receipt = await self.consumption.consume_prepared(
                scope=command.scope,
                prepared=item,
                lifecycle_epoch=command.lifecycle_epoch,
                request_id=command.request_id,
                confirmation_basis=command.confirmation_basis,
            )
        except DocumentBusyError as error:
            raise DocumentFormalSourceWait() from error
        status = 'not_published' if receipt['status'] == 'no_contribution' else 'completed'
        return {'status': status, 'progress': {**row['progress'], 'receipt': receipt}}

    @staticmethod
    def _alias_identity(scope, report_id):
        return 'analysis_current_result_' + digest([scope.as_dict(), report_id])

    async def _saved_result(self, scope, report_id):
        identity = self._alias_identity(scope, report_id)
        row = await self.readiness.store.get(identity)
        if row is None:
            return None
        command = AnalysisCurrentCommand.from_dict(row['command'])
        if (
            row['_id'] != identity
            or command.scope != scope
            or (command.report_id or command.canonical_report_id) != report_id
        ):
            raise DocumentFormalSourceConflict('the Analysis result identity changed')
        return command

    async def check_result_document(self, scope, report_id, document):
        saved = await self._saved_result(scope, report_id)
        if saved and (saved.document_id, saved.canonical_report_id, saved.content_sha256) != (
            document['_id'],
            document['canonicalReportId'],
            document['contentSha256'],
        ):
            raise HealthReportSourceConflictError('reportId already identifies another fixed document')

    async def bind_result(self, command):
        scope, report_id = command.scope, command.report_id or command.canonical_report_id
        identity = self._alias_identity(scope, report_id)
        await self.readiness.store.insert({'_id': identity, 'status': 'completed', 'command': command.as_dict()})
        await self.check_result_document(
            scope,
            report_id,
            {
                '_id': command.document_id,
                'canonicalReportId': command.canonical_report_id,
                'contentSha256': command.content_sha256,
            },
        )

    async def read_result(self, *, scope, report_id):
        command = await self._saved_result(scope, report_id)
        if command is None:
            raise DocumentNotFoundError('the fixed Analysis result is unavailable')
        ready = await self.readiness.store.get(command.command_id + ':ready')
        if ready is None:
            raise DocumentNotFoundError('the fixed Analysis response is unavailable')
        fixed, _ = await self.readiness._validate(command, ready)
        return fixed['response']


class AnalysisCurrentPublication:
    revision = ANALYSIS_CURRENT_PUBLICATION
    stores_content_response = True

    def __init__(self, *, delivery):
        self.delivery = delivery

    async def subject_epoch(self, scope):
        return await self.delivery.steps.subject_epoch(scope)

    async def check_result_document(self, scope, report_id, document):
        await self.delivery.steps.check_result_document(scope, report_id, document)

    async def accept(self, *, document, trace, report_id, target_round, interpretation_revision, lifecycle_epoch):
        command = AnalysisCurrentCommand(
            DocumentScope(document['userId'], document['subjectId']),
            trace.trace_id,
            document['_id'],
            document['canonicalReportId'],
            document['contentSha256'],
            interpretation_revision,
            target_round,
            report_id,
            lifecycle_epoch=lifecycle_epoch,
            confirmation_basis=ANALYSIS_PUBLIC_CONFIRMATION,
        )
        await self.delivery.submit(command)
        return command

    async def finish(self, *, command, document, result, raw, trace, report_id, require_text):
        ready, bundle = await self.delivery.steps.readiness.prepare(command)
        await self.delivery.steps.bind_result(command)
        facts = bundle.facts.payload
        ingestion = 'pending' if has_clinical_delivery(facts) or facts['basicInfo'] else 'not_submitted'
        await trace.update(
            stage='completed',
            status='completed',
            storedDocumentId=document['_id'],
            analysisDeliveryId=command.command_id,
            profileIngestion={'status': ingestion, 'jobId': None},
        )
        return ProcessedHealthReport(
            report=deepcopy(ready['response']),
            text=bundle.material.payload['parsedDocument']['text'] if require_text else None,
            document_id=document['_id'],
            trace_id=trace.trace_id,
            ingestion_status=ingestion,
            ingestion_job_id=None,
            reused=document['_id'] != trace.trace_id,
            archive_trace_id=bundle.material.payload['parseArtifactRef'],
            file_name=document['fileName'],
            resolved_file_type=document['fileType'],
            content_sha256=document['contentSha256'],
            canonical_report_id=document['canonicalReportId'],
            source_revision=None,
            response_is_projected=True,
        )


class AnalysisCurrentProcessingService:
    consumer_revision = ANALYSIS_CURRENT_PUBLICATION

    def __init__(self, *, processing, publication):
        self.processing, self.publication = processing, publication

    async def process(self, **kwargs):
        return await self.processing.process(_analysis_publication=self.publication, **kwargs)

    async def process_document(self, **kwargs):
        return await self.processing.process_document(_analysis_publication=self.publication, **kwargs)

    async def read_result(self, *, scope, report_id):
        if not isinstance(scope, DocumentScope):
            raise TypeError('an owned Analysis result scope is required')
        if not isinstance(report_id, str) or not report_id.strip():
            raise ValueError('an explicit Analysis result identity is required')
        return await self.publication.delivery.steps.read_result(
            scope=scope, report_id=self.processing._report_source.resolve_report_id(report_id)
        )


def create_analysis_current_processing_service(
    *, processing, prepared, consumption, resolve_subject, command_records, clock=None
):
    if not processing.supports_document_reuse or prepared.understanding.preparation is not processing:
        raise ValueError('Analysis must use the current Document preparation provider')
    store = MongoDocumentFormalSourceStore(command_records)
    readiness = AnalysisCurrentReadiness(
        store=store,
        prepared=prepared,
        index=processing._document_reuse.index,
        responses=processing._document_trace_store,
    )
    delivery = AnalysisCurrentDelivery(
        store=store,
        steps=AnalysisCurrentSteps(readiness=readiness, consumption=consumption, resolve_subject=resolve_subject),
        **({'clock': clock} if clock is not None else {}),
    )
    publication = AnalysisCurrentPublication(delivery=delivery)
    return SimpleNamespace(
        service=AnalysisCurrentProcessingService(processing=processing, publication=publication), delivery=delivery
    )
