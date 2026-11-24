"""Root-owned Document/Profile composition, with one real gate and no deferred ports."""

from dataclasses import dataclass
from types import SimpleNamespace

from pymongo import ReadPreference
from pymongo.write_concern import WriteConcern

from adapters.analysis.profile_ingestion import ProfileDocumentObservationIngestionReader
from adapters.analysis.publication import MongoDocumentPublicationLedger
from adapters.analysis.publication_runtime import create_document_publication_runtime
from bootstrap.document_contracts import build_document_material_reader


@dataclass(frozen=True, slots=True)
class DocumentSourceResources:
    documents: object
    traces: object
    trace_chunks: object
    observations: object
    reports: object
    ledger: MongoDocumentPublicationLedger

    def collection(self, suffix):
        return durable_collection(self.documents.database[self.documents.name + suffix])


def durable_collection(collection):
    return collection.with_options(
        read_preference=ReadPreference.PRIMARY,
        write_concern=WriteConcern(w='majority', j=True, wtimeout=5000),
    )


def build_document_source_resources(*, documents, traces, trace_chunks, observations, reports):
    ledger = MongoDocumentPublicationLedger(
        sources=durable_collection(documents.database[documents.name + '_publication_sources']),
        intents=durable_collection(documents.database[documents.name + '_publication_intents']),
    )
    return DocumentSourceResources(documents, traces, trace_chunks, observations, reports, ledger)


def create_application_document_source_resources():
    from db.mongodb import collection_document_index
    from db.mongodb import collection_document_observations
    from db.mongodb import collection_document_trace_chunks
    from db.mongodb import collection_document_traces
    from db.mongodb import collection_report_artifacts

    return build_document_source_resources(
        documents=collection_document_index,
        traces=collection_document_traces,
        trace_chunks=collection_document_trace_chunks,
        observations=collection_document_observations,
        reports=collection_report_artifacts,
    )


def build_current_document_source_graph(
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
    """Build the current readers, then the shared Profile receiver and Document commands."""
    from adapters.analysis.basic_info import create_document_basic_info_prepared_service
    from adapters.analysis.basic_info import create_document_basic_info_profile_runtime
    from adapters.analysis.basic_info import create_document_basic_info_source_readers
    from adapters.analysis.basic_info import create_document_basic_info_understanding_service
    from modules.health_profile.application import ProfileCurrentDocumentSourceResolver

    materials = build_document_material_reader(
        document_collection=resources.documents,
        traces=resources.traces,
        trace_chunks=resources.trace_chunks,
    )
    understanding = create_document_basic_info_understanding_service(
        snapshots=resources.collection('_snapshots'),
        snapshot_chunks=resources.collection('_snapshot_chunks'),
        preparation=processing,
    )
    prepared = create_document_basic_info_prepared_service(
        understanding=understanding, preparation_records=resources.collection('_current_preparations')
    )
    readers = create_document_basic_info_source_readers(
        prepared=prepared,
        ledger=resources.ledger,
        admission_records=resources.collection('_current_admissions'),
        material_reader=materials,
        verify_original_occurrences=True,
    )
    resolvers = {
        **(source_resolvers or {}),
        **ProfileCurrentDocumentSourceResolver(readers.profile).registrations(),
    }
    profile = profile_contracts_factory(resolvers=resolvers, agent_runtime=agent_runtime)
    admission = profile_admission_factory(inbox=profile.inbox, owner_fence_readers=owner_fence_readers or {})
    current = create_document_basic_info_profile_runtime(readers=readers, profile_inbox=profile.inbox)
    publication = create_document_publication_runtime(
        ledger=resources.ledger,
        observation_collection=resources.observations,
        snapshots=understanding.snapshots,
        profile_inbox=profile.inbox,
        legacy_ingestion_reader=ProfileDocumentObservationIngestionReader(profile.legacy_ingestion),
    )
    return SimpleNamespace(
        resources=resources,
        processing=processing,
        profile_service=profile_service,
        materials=materials,
        readers=readers,
        resolvers=resolvers,
        profile=profile,
        admission=admission,
        publication=publication,
        current=current,
    )


async def _read_fixed_subject(profiles, scope):
    from modules.health_profile.contracts import ProfileScope

    epoch = await profiles.resolve_lifecycle(ProfileScope(user_id=scope.user_id, subject_id=scope.subject_id))
    if epoch is None:
        return None
    return SimpleNamespace(user_id=scope.user_id, subject_id=scope.subject_id, status='active', lifecycle_epoch=epoch)


def build_current_document_consumers(*, graph, report_batches):
    """Use a single current projection and publication path for the upload consumers."""
    from adapters.analysis.analysis_current import create_analysis_current_processing_service
    from adapters.record.current_documents import create_current_record_document_port
    from adapters.report.current_document_checkpoints import MongoReportDocumentCheckpoints
    from adapters.report.current_documents import CurrentReportDocumentFactory
    from adapters.report.current_documents import ReportDocumentSubject
    from modules.analysis.snapshots import DocumentScope
    from modules.report.document_dispatch import ReportDocumentFactoryDispatcher
    from modules.report.schemas import SubjectInfo

    current = graph.current
    ensure_self = graph.profile_service.ensure_self_profile
    record = create_current_record_document_port(
        prepared=graph.readers.prepared, documents=current.consumption, ensure_self_profile=ensure_self
    )

    async def select_report_subject(user_id):
        subject = (await ensure_self(user_id)).subject
        if subject.user_id != user_id or subject.kind != 'self' or subject.status != 'active':
            raise ValueError('Report requires an active self subject')
        info = await graph.profile.basic_info.read(user_id, subject_id=subject.subject_id)
        if info.lifecycle_epoch != subject.lifecycle_epoch:
            raise ValueError('the selected Report subject lifecycle changed')
        return ReportDocumentSubject(
            DocumentScope(user_id, subject.subject_id),
            subject.lifecycle_epoch,
            SubjectInfo(name=info.fields['name'].value, sex=info.fields['gender'].value, age=info.fields['age'].value),
        )

    report_factory = CurrentReportDocumentFactory(
        prepared=graph.readers.prepared,
        report_projections=current.report_projections,
        consumer=current.consumption,
        select_scope=select_report_subject,
        checkpoints=MongoReportDocumentCheckpoints(report_batches),
        material_reader=graph.materials,
    )
    analysis = create_analysis_current_processing_service(
        processing=graph.processing,
        prepared=graph.readers.prepared,
        consumption=current.consumption,
        resolve_subject=lambda scope: _read_fixed_subject(graph.profile.reader, scope),
        command_records=graph.resources.collection('_analysis_current_deliveries'),
    )
    return SimpleNamespace(
        record=record, report=ReportDocumentFactoryDispatcher(default=report_factory, supported=()), analysis=analysis
    )
