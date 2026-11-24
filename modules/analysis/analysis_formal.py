"""Independent Analysis HTTP use case reuses the existing validation and display/error pipeline."""

from modules.analysis.analysis_formal_contracts import ANALYSIS_FORMAL_REVISION
from modules.analysis.analysis_formal_contracts import AnalysisFormalCommand
from modules.analysis.application.documents import ProcessedHealthReport
from modules.analysis.report_publication_contracts import DocumentPreparedPublicationInput
from modules.analysis.snapshots import DocumentScope


class AnalysisFormalPublication:
    revision = ANALYSIS_FORMAL_REVISION
    stores_content_response = True

    def __init__(self, *, compatibility, formal, delivery, aliases):
        self.compatibility, self.formal, self.delivery, self.aliases = compatibility, formal, delivery, aliases

    async def accept(self, *, document, trace, report_id, target_round, interpretation_revision):
        command = AnalysisFormalCommand(
            DocumentScope(document['userId'], document['subjectId']),
            trace.trace_id,
            document['_id'],
            document['canonicalReportId'],
            document['contentSha256'],
            interpretation_revision,
            target_round,
            report_id,
        )
        await self.delivery.submit(command)
        return command

    async def finish(self, *, command, document, result, raw, trace, report_id, require_text):
        ready, bundle = await self.delivery.steps.readiness.prepare(command)
        prepared = DocumentPreparedPublicationInput.from_dict(ready['prepared'])
        preparation = prepared.preparation_receipt
        raw = bundle.material.payload['parsedDocument']
        base_command = command.base_command(prepared)
        public = ready['response']
        ingestion = (
            'not_submitted' if bundle.understanding.payload['quality'].get('status') == 'unavailable' else 'pending'
        )
        await trace.update(
            stage='completed',
            status='completed',
            storedDocumentId=document['_id'],
            formalCommandId=base_command.command_id,
            analysisDeliveryId=command.command_id,
            profileIngestion={'status': ingestion, 'jobId': None},
        )
        return ProcessedHealthReport(
            report=public,
            text=raw['text'] if require_text else None,
            document_id=document['_id'],
            trace_id=trace.trace_id,
            ingestion_status=ingestion,
            ingestion_job_id=None,
            reused=preparation.reused,
            archive_trace_id=bundle.material.payload['parseArtifactRef'],
            file_name=document['fileName'],
            resolved_file_type=document['fileType'],
            content_sha256=document['contentSha256'],
            canonical_report_id=document['canonicalReportId'],
            source_revision=None,
            preparation_binding=None,
            response_is_projected=True,
        )


class AnalysisFormalProcessingService:
    """Only explicit AI Analysis requests enter this wrapper; no query invokes it."""

    consumer_revision = ANALYSIS_FORMAL_REVISION

    def __init__(self, *, processing, publication):
        self.processing, self.publication = processing, publication

    async def process(self, **kwargs):
        return await self.processing.process(_analysis_publication=self.publication, **kwargs)
