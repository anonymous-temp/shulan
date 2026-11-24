"""Managed Document composition. Construction performs no I/O or source cutover."""

import json
from dataclasses import dataclass

from pymongo import ASCENDING
from pymongo import ReadPreference
from pymongo.write_concern import WriteConcern

from adapters.analysis.complete_source import ProfileDocumentCompleteSourceReader
from adapters.analysis.source_admission import MongoDocumentAdmissionStore
from adapters.analysis.source_admission import ProfileDocumentOwnerFenceReader
from modules.analysis.complete_source import DocumentCompleteSourceService
from modules.analysis.complete_source_contracts import DocumentCompleteSourceUnavailable
from modules.analysis.complete_source_publication import DocumentCompleteSourcePublisher
from modules.analysis.managed_archive import DocumentManagedArchive
from modules.analysis.managed_report import DocumentManagedReportPreparation
from modules.analysis.managed_source import DocumentManagedSourceService
from modules.analysis.publication_lifecycle import DocumentPublicationLifecycleReader
from modules.analysis.snapshots import canonical_json
from modules.analysis.source_admission import DocumentFreshSourceAdmission
from modules.health_profile.contracts import ProfileScope


class MongoDocumentManagedSourceStore(MongoDocumentAdmissionStore):
    def __init__(self, collection):
        super().__init__(collection)
        self._document_lookup_ready = False

    async def put(self, kind, scope, identity, value):
        if kind == 'managed_command':
            await self.ensure_document_lookup_index()
        await super().put(kind, scope, identity, value)

    async def ensure_document_lookup_index(self):
        """Explicit startup/write-side initialization; archive reads never issue DDL."""
        if not self._document_lookup_ready:
            await self.collection.create_index(
                [
                    ('userId', ASCENDING),
                    ('subjectId', ASCENDING),
                    ('kind', ASCENDING),
                    ('payload.inputs.preparationReceipt.document_id', ASCENDING),
                    ('payload.sourceId', ASCENDING),
                    ('_id', ASCENDING),
                ],
                name='document_managed_input_source_seek',
            )
            self._document_lookup_ready = True

    async def read_document_sources(self, *, scope, document_id, after_source_id=None, limit=20):
        """Indexed distinct seek over fixed input manifests; no DDL or writes on reads."""
        if type(limit) is not int or not 1 <= limit <= 101:
            raise ValueError('document source lookup must be bounded')
        result, after = [], after_source_id
        for _ in range(limit):
            query = {
                **scope.as_dict(),
                'kind': 'managed_command',
                'payload.inputs.preparationReceipt.document_id': document_id,
            }
            if after is not None:
                query['payload.sourceId'] = {'$gt': after}
            rows = (
                await self.collection.find(query)
                .hint('document_managed_input_source_seek')
                .sort(
                    [
                        ('payload.sourceId', ASCENDING),
                        ('_id', ASCENDING),
                    ]
                )
                .to_list(length=1)
            )
            if not rows:
                break
            source_id = rows[0]['payload']['sourceId']
            if not isinstance(source_id, str) or not source_id or after is not None and source_id <= after:
                raise ValueError('document source lookup escaped its seek order')
            result.append(source_id)
            after = source_id
        return result

    async def get(self, kind, scope, identity):
        value = await super().get(kind, scope, identity)
        # BSON's int64 subclass is a storage detail, not a different public
        # revision type. All proof payload timestamps were already fixed ISO strings.
        return None if value is None else json.loads(canonical_json(value))

    async def bind_trace(self, scope, key, content_digest, trace_id):
        value = {'contentDigest': content_digest, 'traceId': trace_id}
        stored = await self._insert('managed_report_trace', scope, key, value)
        if (
            stored['contentDigest'] != content_digest
            or not isinstance(stored.get('traceId'), str)
            or not stored['traceId']
        ):
            raise DocumentCompleteSourceUnavailable('concurrent fixed v16 outputs disagree')
        return stored['traceId']


class ProfileManagedDocumentArchive:
    def __init__(self, archive):
        self.archive_service = archive

    async def archive(self, *, scope, request):
        receipt = await self.archive_service.archive_source_material(
            scope=ProfileScope(user_id=scope.user_id, subject_id=scope.subject_id),
            canonical_report_id=request['canonicalReportId'],
            report_id=request['reportId'],
            source_fingerprint=request['sourceFingerprint'],
            document_id=request['documentId'],
            result=request['result'],
            source_key_value=request['sourceKey'],
        )
        # created is an attempt-local observation. Preserve the fixed identity
        # acknowledgement across retries where the actual archive already exists.
        value = receipt.model_dump(mode='json', by_alias=True)
        value.pop('created')
        return value


@dataclass(frozen=True, slots=True)
class DocumentManagedSourceRuntime:
    commands: DocumentManagedSourceService
    reports: DocumentManagedReportPreparation
    source_reader: DocumentCompleteSourceService
    publisher: DocumentCompleteSourcePublisher
    archive: DocumentManagedArchive
    lifecycle: DocumentPublicationLifecycleReader


@dataclass(frozen=True, slots=True)
class DocumentManagedSourceReaders:
    source_reader: ProfileDocumentCompleteSourceReader
    owner_fence_reader: ProfileDocumentOwnerFenceReader


def create_document_managed_source_readers(
    *, compatibility, report_publications, ledger, admission_records, command_records
):
    """Build Profile's exact readers before Profile inbox/admission/archive exists.

    Only Document resources are required. No deferred proxy or mutable Profile
    binding is installed. Internal historical readers have no command-side ports;
    the returned public facades expose reads only. The later command runtime uses
    these same durable collections and ledger, not these internal service objects.
    """
    if compatibility.legacy_publication.ledger is not ledger or report_publications.compatibility is not compatibility:
        raise ValueError('readers and every Document publisher must share the same provider and gate')
    records = MongoDocumentManagedSourceStore(command_records.with_options(read_preference=ReadPreference.PRIMARY))
    snapshots = compatibility.understanding.snapshots
    complete = DocumentCompleteSourceService(
        snapshots=snapshots,
        understanding=compatibility.understanding,
        report_publications=report_publications,
        admission=None,
    )
    admission = DocumentFreshSourceAdmission(
        compatibility=compatibility,
        contributions=complete,
        ledger=ledger,
        records=MongoDocumentAdmissionStore(admission_records.with_options(read_preference=ReadPreference.PRIMARY)),
        history=None,
        profile_presence=None,
    )
    archive = DocumentManagedArchive(records=records, profile_archive=None)
    reports = DocumentManagedReportPreparation(
        compatibility=compatibility,
        materials=None,
        snapshots=snapshots,
        records=records,
        profiles=None,
        trace_store=None,
        archive=archive,
    )
    history = DocumentManagedSourceService(
        records=records,
        ledger=ledger,
        initial_admission=admission,
        complete=complete,
        reports=reports,
        archive=archive,
        publisher=None,
    )
    complete.admission, complete.managed_reports = history, reports
    return DocumentManagedSourceReaders(
        ProfileDocumentCompleteSourceReader(complete), ProfileDocumentOwnerFenceReader(admission)
    )


def create_document_managed_source_runtime(
    *,
    compatibility,
    report_publications,
    material_reader,
    source_admission,
    publication_runtime,
    profile_archive,
    command_records,
):
    """Build an opt-in provider; consumers and workers remain a root responsibility.

    Inject the same durable ledger as all legacy writers and the existing outbox.
    command_records needs PRIMARY reads, majority writes, unique _id and no TTL;
    deployment supplies indexes/resources. Large input artifacts remain in the
    existing chunked immutable snapshot store. This creates no client, index,
    background task, model call, authorization or default consumer installation.
    """
    if compatibility.legacy_publication.ledger is not publication_runtime.ledger:
        raise ValueError('managed and legacy publication must share one source gate')
    if (
        report_publications.compatibility is not compatibility
        or source_admission.ledger is not publication_runtime.ledger
    ):
        raise ValueError('managed preparation/admission must share the actual fixed Document provider')
    records = MongoDocumentManagedSourceStore(
        command_records.with_options(
            read_preference=ReadPreference.PRIMARY,
            write_concern=WriteConcern(w='majority'),
        )
    )
    snapshots = compatibility.understanding.snapshots
    archive = DocumentManagedArchive(records=records, profile_archive=ProfileManagedDocumentArchive(profile_archive))
    reports = DocumentManagedReportPreparation(
        compatibility=compatibility,
        materials=material_reader,
        snapshots=snapshots,
        records=records,
        profiles=report_publications.coordinator.profiles,
        trace_store=report_publications.coordinator.service._document_trace_store,
        archive=archive,
    )
    complete = DocumentCompleteSourceService(
        snapshots=snapshots,
        understanding=compatibility.understanding,
        report_publications=report_publications,
        admission=source_admission,
        managed_reports=reports,
    )
    publisher = DocumentCompleteSourcePublisher(
        source_reader=complete,
        snapshots=snapshots,
        ledger=publication_runtime.ledger,
        recovery=publication_runtime.recovery,
    )
    commands = DocumentManagedSourceService(
        records=records,
        ledger=publication_runtime.ledger,
        initial_admission=source_admission,
        complete=complete,
        reports=reports,
        archive=archive,
        publisher=publisher,
    )
    complete.admission = commands
    lifecycle = DocumentPublicationLifecycleReader(ledger=publication_runtime.ledger, snapshots=snapshots)
    return DocumentManagedSourceRuntime(commands, reports, complete, publisher, archive, lifecycle)
