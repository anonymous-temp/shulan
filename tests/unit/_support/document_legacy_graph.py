"""Explicit old graph used only to verify surviving historical read and domain regressions."""

from types import SimpleNamespace

from adapters.analysis.formal_source import create_document_formal_source_service
from adapters.analysis.managed_source import create_document_managed_source_readers
from adapters.analysis.managed_source import create_document_managed_source_runtime
from adapters.analysis.profile_ingestion import ProfileDocumentObservationIngestionReader
from adapters.analysis.publication_runtime import create_document_publication_runtime
from adapters.analysis.report_publication import create_document_report_publication_service
from adapters.analysis.source_admission import create_document_fresh_source_admission
from adapters.report.document_profiles import configure_report_source_profiles
from bootstrap.document_contracts import build_document_compatibility_service
from bootstrap.document_contracts import build_document_material_reader
from bootstrap.document_sources import durable_collection
from modules.analysis.snapshots import COMPLETE_SOURCE_CONTRACT
from modules.analysis.snapshots import RETRACTION_CONTRACT
from modules.health_profile.application import ProfileDocumentSourceResolver


def build_document_source_graph(
    *,
    resources,
    processing,
    profile_service,
    agent_runtime,
    profile_contracts_factory,
    profile_admission_factory,
    source_resolvers=None,
    owner_fence_readers=None,
):
    """Build readers before Profile, then commands; never patch readers after construction.

    The returned Profile worker is not scheduled separately: the existing ingestion
    worker must be constructed with these exact resolvers and remains the sole
    Profile recovery entry. All default processing instances share resources.ledger.
    """
    configure_report_source_profiles(processing)
    compatibility = build_document_compatibility_service(
        processing_service=processing,
        profile_service=profile_service,
        document_collection=resources.documents,
        publication_ledger=resources.ledger,
    )
    materials = build_document_material_reader(
        document_collection=resources.documents,
        traces=resources.traces,
        trace_chunks=resources.trace_chunks,
    )
    publications = create_document_report_publication_service(
        processing_service=processing,
        compatibility_service=compatibility,
        material_reader=materials,
        command_records=resources.collection('_report_publications'),
    )
    readers = create_document_managed_source_readers(
        compatibility=compatibility,
        report_publications=publications,
        ledger=resources.ledger,
        admission_records=resources.collection('_source_admissions'),
        command_records=resources.collection('_managed_sources'),
    )
    resolver = ProfileDocumentSourceResolver(readers.source_reader)
    resolvers = {
        **(source_resolvers or {}),
        ('document', COMPLETE_SOURCE_CONTRACT): resolver,
        ('document', RETRACTION_CONTRACT): resolver,
    }
    profile = profile_contracts_factory(resolvers=resolvers, agent_runtime=agent_runtime)
    admission = profile_admission_factory(
        inbox=profile.inbox,
        owner_fence_readers={**(owner_fence_readers or {}), 'document': readers.owner_fence_reader},
    )
    publication = create_document_publication_runtime(
        ledger=resources.ledger,
        observation_collection=resources.observations,
        snapshots=compatibility.understanding.snapshots,
        profile_inbox=profile.inbox,
        legacy_ingestion_reader=ProfileDocumentObservationIngestionReader(profile.legacy_ingestion),
    )
    fresh = create_document_fresh_source_admission(
        compatibility=compatibility,
        report_publications=publications,
        ledger=resources.ledger,
        admission_records=resources.collection('_source_admissions'),
        observation_collection=resources.observations,
        profile_presence=admission.presence,
        profile_inbox=profile.inbox,
    )
    managed = create_document_managed_source_runtime(
        compatibility=compatibility,
        report_publications=publications,
        material_reader=materials,
        source_admission=fresh,
        publication_runtime=publication,
        profile_archive=admission.archive,
        command_records=resources.collection('_managed_sources'),
    )
    formal = create_document_formal_source_service(
        compatibility=compatibility,
        source_admission=fresh,
        managed=managed,
        report_publications=publications,
        profile_admission=admission.admission,
        command_records=resources.collection('_formal_sources'),
    )
    return SimpleNamespace(
        resources=resources,
        processing=processing,
        compatibility=compatibility,
        materials=materials,
        publications=publications,
        readers=readers,
        resolvers=resolvers,
        profile=profile,
        admission=admission,
        publication=publication,
        fresh=fresh,
        managed=managed,
        formal=formal,
    )


def build_document_consumers(*, graph, legacy_processing_factory):
    """Install exact consumers after their providers; old implementations remain executable."""
    from adapters.analysis.analysis_formal import create_analysis_formal_processing_service
    from adapters.record.document_compatibility import create_prepared_record_document_port
    from adapters.record.documents import LegacyRecordDocumentAdapter
    from adapters.record.formal_documents import create_formal_record_document_port
    from adapters.report.fixed_documents import create_fixed_report_document_source_factory
    from adapters.report.formal_documents import create_formal_report_document_source_factory
    from adapters.report.projection_store import MongoReportProjectionStore
    from bootstrap.report_documents import UnavailableFixedReportDocumentFactory
    from modules.report.document_dispatch import ReportDocumentFactoryDispatcher
    from modules.report.document_projection import ReportProjectionService

    legacy_record = LegacyRecordDocumentAdapter(processor_factory=legacy_processing_factory)
    prepared_record = create_prepared_record_document_port(graph.compatibility, legacy_port=legacy_record)
    record = create_formal_record_document_port(graph.compatibility, graph.formal, previous_port=prepared_record)
    reports = graph.resources.reports
    projections = ReportProjectionService(
        MongoReportProjectionStore(durable_collection(reports.database[reports.name + '_document_projections']))
    )
    old_report = create_fixed_report_document_source_factory(
        compatibility=graph.compatibility,
        materials=graph.materials,
        publications=graph.publications,
        projections=projections,
    )
    new_report = create_formal_report_document_source_factory(
        compatibility=graph.compatibility,
        materials=graph.materials,
        formal=graph.formal,
        projections=projections,
    )
    report = ReportDocumentFactoryDispatcher(
        default=new_report,
        supported=(old_report, UnavailableFixedReportDocumentFactory()),
    )
    analysis = create_analysis_formal_processing_service(
        processing=graph.processing,
        compatibility=graph.compatibility,
        formal=graph.formal,
        managed=graph.managed,
        profile_archive=graph.admission.archive,
        command_records=graph.resources.collection('_analysis_deliveries'),
    )
    return SimpleNamespace(record=record, report=report, analysis=analysis)
