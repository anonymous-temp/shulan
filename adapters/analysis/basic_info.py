"""Explicit current Document assembly; default switching awaits merged providers."""

from dataclasses import dataclass

from pymongo import ReadPreference
from pymongo.write_concern import WriteConcern

from adapters.analysis.compatibility import MongoDocumentCompatibilityStore
from adapters.analysis.publication_receiver import ProfileCurrentDocumentPublicationReceiver
from adapters.analysis.snapshots import MongoDocumentSnapshotStore
from adapters.analysis.source_admission import MongoDocumentAdmissionStore
from modules.analysis.basic_info_admission import DocumentCurrentAdmission
from modules.analysis.basic_info_managed import DocumentCurrentManagedSource
from modules.analysis.basic_info_managed import DocumentCurrentPlanReader
from modules.analysis.basic_info_publication import DocumentCurrentSourcePublisher
from modules.analysis.basic_info_source import DocumentCurrentContributionCapture
from modules.analysis.basic_info_source import DocumentCurrentSourceArtifacts
from modules.analysis.basic_info_source import DocumentCurrentSourceReader
from modules.analysis.basic_info_source import DocumentCurrentSourceService
from modules.analysis.current_preparation import DocumentPreparedService
from modules.analysis.current_prepared_consumption import DocumentCurrentPreparedConsumption
from modules.analysis.current_report_projection import DocumentCurrentReportProjection
from modules.analysis.current_understanding import DocumentCurrentUnderstandingService
from modules.analysis.publication_recovery import DocumentPublicationRecovery
from modules.analysis.snapshots import DocumentScope
from modules.health_profile.contracts import ProfileScope
from modules.report.document_projection import REPORT_CURRENT_INTERPRETATION_PROJECTION_REVISION
from modules.report.document_projection import REPORT_OBSERVATION_REFS_PROJECTION_REVISION
from modules.report.document_projection import REPORT_SOURCE_SELECTION
from modules.report.document_projection import project_current_report_source
from modules.report.document_projection import project_current_report_source_with_observation_refs


def create_document_basic_info_understanding_service(*, snapshots, snapshot_chunks, preparation):
    return DocumentCurrentUnderstandingService(
        preparation=preparation, snapshots=MongoDocumentSnapshotStore(snapshots=snapshots, chunks=snapshot_chunks)
    )


def create_document_basic_info_source_service(*, understanding, admission):
    return DocumentCurrentSourceService(
        snapshots=understanding.snapshots, understanding=understanding, admission=admission
    )


def create_document_basic_info_prepared_service(*, understanding, preparation_records):
    return DocumentPreparedService(
        understanding=understanding,
        records=MongoDocumentCompatibilityStore(
            preparation_records.with_options(
                read_preference=ReadPreference.PRIMARY, write_concern=WriteConcern(w='majority')
            )
        ),
    )


@dataclass(frozen=True, slots=True)
class DocumentCurrentProfileSourceReader:
    """Translate public Profile scope into Document scope at the owned adapter boundary."""

    source: DocumentCurrentSourceReader

    @staticmethod
    def _scope(scope):
        if not isinstance(scope, ProfileScope):
            raise TypeError('the public Profile scope is required')
        return DocumentScope(scope.user_id, scope.subject_id)

    async def read_source_observations(self, *, scope, snapshot_id):
        return await self.source.read_source_observations(scope=self._scope(scope), snapshot_id=snapshot_id)

    async def read_fixed_observation_refs(self, *, scope, snapshot_id):
        return await self.source.read_fixed_observation_refs(scope=self._scope(scope), snapshot_id=snapshot_id)

    async def read_source_inventory(self, *, scope, snapshot_id):
        return await self.source.read_source_inventory(scope=self._scope(scope), snapshot_id=snapshot_id)

    async def read_retraction(self, *, scope, snapshot_id):
        return await self.source.read_retraction(scope=self._scope(scope), snapshot_id=snapshot_id)


@dataclass(frozen=True, slots=True)
class DocumentCurrentSourceReaders:
    """Complete read graph constructed before the Profile runtime, without late rebinding."""

    prepared: DocumentPreparedService
    ledger: object
    records: MongoDocumentAdmissionStore
    source: DocumentCurrentSourceReader
    admission: DocumentCurrentAdmission
    plans: DocumentCurrentPlanReader
    profile: DocumentCurrentProfileSourceReader
    report_projections: DocumentCurrentReportProjection | None = None
    verify_original_occurrences: bool = False


def create_document_basic_info_source_readers(
    *, prepared, ledger, admission_records, material_reader=None, verify_original_occurrences=False
):
    if verify_original_occurrences and material_reader is None:
        raise ValueError('verified original occurrences require a fixed original reader')
    records = MongoDocumentAdmissionStore(
        admission_records.with_options(read_preference=ReadPreference.PRIMARY, write_concern=WriteConcern(w='majority'))
    )
    snapshots = prepared.understanding.snapshots
    report_projections = (
        DocumentCurrentReportProjection(
            prepared=prepared,
            materials=material_reader,
            snapshots=snapshots,
            records=records,
            projector=(
                project_current_report_source_with_observation_refs
                if verify_original_occurrences
                else project_current_report_source
            ),
            selection=REPORT_SOURCE_SELECTION,
            interpretation_projection_revision=(
                REPORT_OBSERVATION_REFS_PROJECTION_REVISION
                if verify_original_occurrences
                else REPORT_CURRENT_INTERPRETATION_PROJECTION_REVISION
            ),
        )
        if material_reader is not None
        else None
    )
    artifacts = DocumentCurrentSourceArtifacts(
        snapshots=snapshots,
        understanding=prepared.understanding,
        report_projections=report_projections,
    )
    contributions = DocumentCurrentContributionCapture(artifacts)
    admission = DocumentCurrentAdmission(prepared=prepared, contributions=contributions, ledger=ledger, records=records)
    plans = DocumentCurrentPlanReader(records=records, ledger=ledger, initial_admission=admission, artifacts=artifacts)
    source = DocumentCurrentSourceReader(
        snapshots=artifacts.snapshots,
        understanding=artifacts.understanding,
        admission=plans,
        report_projections=report_projections,
    )
    return DocumentCurrentSourceReaders(
        prepared,
        ledger,
        records,
        source,
        admission,
        plans,
        DocumentCurrentProfileSourceReader(source),
        report_projections,
        verify_original_occurrences,
    )


@dataclass(frozen=True, slots=True)
class DocumentCurrentSourceRuntime:
    source: DocumentCurrentSourceService
    admission: DocumentCurrentAdmission
    managed: DocumentCurrentManagedSource
    publisher: DocumentCurrentSourcePublisher
    readers: DocumentCurrentSourceReaders
    recovery: DocumentPublicationRecovery
    consumption: DocumentCurrentPreparedConsumption

    @property
    def report_projections(self):
        return self.readers.report_projections


def create_document_basic_info_source_runtime(
    *, publication_recovery, readers=None, prepared=None, ledger=None, admission_records=None, material_reader=None
):
    """Build commands after pure readers/Profile; never patch an already exposed reader."""
    if readers is None:
        if prepared is None or ledger is None or admission_records is None:
            raise ValueError('fixed current readers or the complete provider inputs are required')
        readers = create_document_basic_info_source_readers(
            prepared=prepared,
            ledger=ledger,
            admission_records=admission_records,
            material_reader=material_reader,
        )
    elif any(value is not None for value in (prepared, ledger, admission_records, material_reader)):
        raise ValueError('provide existing readers or provider inputs, not both')
    if publication_recovery.ledger is not readers.ledger:
        raise ValueError('publication and recovery must use the same existing ledger')
    if publication_recovery.recovery_route != 'current':
        raise ValueError('current recovery route is required')
    source = DocumentCurrentSourceService(
        snapshots=readers.source.snapshots,
        understanding=readers.source.understanding,
        admission=readers.plans,
        report_projections=readers.report_projections,
    )
    publisher = DocumentCurrentSourcePublisher(
        source_reader=readers.source, snapshots=source.snapshots, ledger=readers.ledger, recovery=publication_recovery
    )
    managed = DocumentCurrentManagedSource(
        records=readers.records,
        ledger=readers.ledger,
        initial_admission=readers.admission,
        complete=source,
        publisher=publisher,
        plans=readers.plans,
    )
    consumption = DocumentCurrentPreparedConsumption(
        admission=readers.admission,
        source=source,
        managed=managed,
        publisher=publisher,
        ledger=readers.ledger,
        records=readers.records,
        evidence_projections=readers.report_projections if readers.verify_original_occurrences else None,
    )
    return DocumentCurrentSourceRuntime(
        source, readers.admission, managed, publisher, readers, publication_recovery, consumption
    )


def create_document_basic_info_profile_runtime(*, readers, profile_inbox, policy=None, clock=None):
    """Use the merged public Profile inbox and the exact preconstructed Document readers."""
    receiver = ProfileCurrentDocumentPublicationReceiver(profile_inbox)
    recovery = DocumentPublicationRecovery(
        ledger=readers.ledger, receiver=receiver, policy=policy, clock=clock, recovery_route='current'
    )
    return create_document_basic_info_source_runtime(readers=readers, publication_recovery=recovery)
