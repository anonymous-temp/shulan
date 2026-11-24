"""Compose Report's fixed document consumer from public providers; never perform I/O."""

from copy import deepcopy
from dataclasses import asdict

from pymongo import ReadPreference
from pymongo.write_concern import WriteConcern

from adapters.analysis.report_publication import create_document_report_publication_service
from adapters.report.document_profiles import configure_report_source_profiles
from adapters.report.document_source import UnavailableReportDocuments
from adapters.report.fixed_documents import REPORT_FIXED_SELECTION
from adapters.report.fixed_documents import REPORT_INPUT_SELECTION
from adapters.report.fixed_documents import REPORT_PUBLICATION_SELECTION
from adapters.report.fixed_documents import create_fixed_report_document_source_factory
from adapters.report.projection_store import MongoReportProjectionStore
from bootstrap.document_contracts import build_document_compatibility_service
from bootstrap.document_contracts import build_document_material_reader
from modules.report.document_projection import ReportProjectionService
from modules.report.document_rendering import REPORT_INTERPRETATION_PROJECTION_REVISION
from modules.report.fallback_policy import DEFAULT_REPORT_FALLBACK_POLICY
from utils.log import getLogger

logger = getLogger(__name__)


class UnavailableFixedReportDocumentFactory:
    """Pin a failed configuration separately from ready tasks; never silently retry another version."""

    def document_input_manifest(self):
        policy = DEFAULT_REPORT_FALLBACK_POLICY
        return deepcopy(
            {
                'consumerRevision': 'report-fixed-documents-unavailable.v1',
                'requestedConsumerRevision': 'report-fixed-documents.v1',
                'availability': 'unavailable',
                'failureCode': 'fixed_document_configuration_unavailable',
                'inputSelection': asdict(REPORT_INPUT_SELECTION),
                'outputSelection': asdict(REPORT_FIXED_SELECTION),
                'publicationSelection': asdict(REPORT_PUBLICATION_SELECTION),
                'interpretationProjectionRevision': REPORT_INTERPRETATION_PROJECTION_REVISION,
                'fallbackRevision': 'report-document-fallback.v1',
                'sourceFetchPolicy': {
                    'attempts': policy.attempts_for(policy.source_fetch_attempts),
                    'backoffSeconds': list(policy.retry_backoff_seconds),
                },
            }
        )

    def create(self, *, user_id, session_id, final_report_id):
        return UnavailableReportDocuments('fixed_document_configuration_unavailable')


def build_report_document_source_factory(
    *,
    processing_service,
    profile_service,
    document_collection,
    traces,
    trace_chunks,
    report_artifact_collection,
    publication_ledger=None,
):
    """Bind one processor/gate and one Document snapshot namespace, with a Report-owned cache.

    No new client, worker, index, model, subject or storage request is created.
    Configuration errors propagate to the application wrapper, which isolates the
    unavailable capability without replacing it with a legacy/latest consumer.
    """
    configure_report_source_profiles(processing_service)
    compatibility = build_document_compatibility_service(
        processing_service=processing_service,
        profile_service=profile_service,
        document_collection=document_collection,
        publication_ledger=publication_ledger,
    )
    materials = build_document_material_reader(
        document_collection=document_collection,
        traces=traces,
        trace_chunks=trace_chunks,
    )
    options = {
        'write_concern': WriteConcern(w='majority', j=True, wtimeout=5000),
        'read_preference': ReadPreference.PRIMARY,
    }
    commands = document_collection.database[document_collection.name + '_report_publications'].with_options(**options)
    publications = create_document_report_publication_service(
        processing_service=processing_service,
        compatibility_service=compatibility,
        material_reader=materials,
        command_records=commands,
    )
    cache = report_artifact_collection.database[report_artifact_collection.name + '_document_projections'].with_options(
        **options
    )
    return create_fixed_report_document_source_factory(
        compatibility=compatibility,
        materials=materials,
        publications=publications,
        projections=ReportProjectionService(MongoReportProjectionStore(cache)),
    )


def create_application_report_document_source_factory(
    *, processing_service_factory, profile_service, publication_ledger=None
):
    """Keep unavailable document configuration local to Report; ordinary data reads still work."""
    try:
        from db.mongodb import collection_document_index
        from db.mongodb import collection_document_trace_chunks
        from db.mongodb import collection_document_traces
        from db.mongodb import collection_report_artifacts

        return build_report_document_source_factory(
            processing_service=processing_service_factory(),
            profile_service=profile_service,
            document_collection=collection_document_index,
            traces=collection_document_traces,
            trace_chunks=collection_document_trace_chunks,
            report_artifact_collection=collection_report_artifacts,
            publication_ledger=publication_ledger,
        )
    except Exception as error:
        logger.warning('Report fixed document configuration unavailable: error_type=%s', type(error).__name__)
        return UnavailableFixedReportDocumentFactory()
