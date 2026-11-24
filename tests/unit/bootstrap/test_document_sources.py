"""Real root Document composition; only Mongo, OCR and model boundaries are offline."""

from copy import deepcopy
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace

import pytest
from bson import BSON
from pymongo import ReadPreference
from pymongo import ReturnDocument

from adapters.analysis.document_index import MongoDocumentIndexStore
from adapters.analysis.document_trace import MongoDocumentTraceStore
from bootstrap.document_sources import build_document_source_resources
from health_profile import application as profile_application
from health_profile.document_observations import MongoDocumentObservationStore
from health_profile.ingestion import HealthProfileJobQueue
from health_profile.report_source import HealthReportSourceService
from health_profile.report_source import MongoHealthReportStore
from health_profile.repository import MongoHealthProfileRepository
from health_profile.service import HealthProfileService
from modules.analysis.application.document_reuse import DocumentReuseCoordinator
from modules.analysis.application.output_profiles import DocumentOutputCoordinator
from modules.analysis.application.output_profiles import DocumentOutputProfiles
from modules.analysis.formal_source_contracts import FORMAL_SOURCE_REVISION
from modules.analysis.formal_source_contracts import DocumentFormalSourceCommand
from modules.analysis.publication import PublicationModeError
from modules.analysis.report_publication_contracts import DocumentPreparedPublicationInput
from modules.analysis.snapshots import COMPLETE_SOURCE_CONTRACT
from modules.analysis.snapshots import RETRACTION_CONTRACT
from modules.analysis.snapshots import DocumentScope
from modules.analysis.understanding import UNDERSTANDING_SELECTION
from tests.unit._support.document_legacy_graph import build_document_consumers
from tests.unit._support.document_legacy_graph import build_document_source_graph
from tests.unit.health_profile.test_profile_read_and_subjects import FakeCollection
from tests.unit.health_profile.test_profile_read_and_subjects import FakeCursor
from tests.unit.health_profile.test_profile_read_and_subjects import _matches
from tests.unit.health_profile.test_source_admission import matches as presence_matches
from tests.unit.health_profile.test_source_admission import project
from tests.unit.services.analysis.test_document_formal_source_store import Collection as BsonCollection
from tests.unit.services.analysis.test_document_formal_source_store import Cursor as BsonCursor
from tests.unit.services.analysis.test_document_workflow import extended_flow


class Cursor(BsonCursor):
    def max_time_ms(self, _):
        return self


class Collection(BsonCollection):
    def __init__(self, database, name):
        super().__init__()
        self.database, self.name = database, name
        self.options, self.reads = {}, []

    def with_options(self, **options):
        self.options.update(options)
        for name, value in options.items():
            setattr(self, name, value)
        return self

    def matches(self, row, query):
        # Mongo compares UTC instants after BSON encoding, independent of tzinfo.
        query = BSON.encode(query).decode()
        return super().matches(row, query) and presence_matches(row, query)

    def find(self, query, projection=None):
        self.reads.append(deepcopy(query))
        return Cursor([project(row, projection) for row in self.documents.values() if self.matches(row, query)])

    async def find_one(self, query, projection=None, **kwargs):
        rows = await self.find(query, projection).to_list(1)
        return rows[0] if rows else None

    async def find_one_and_replace(self, query, replacement, *, return_document):
        old = await self.find_one(query)
        if old is None:
            return None
        self.documents[old['_id']] = BSON.encode(replacement).decode()
        return deepcopy(old if return_document is ReturnDocument.BEFORE else self.documents[old['_id']])


class Database:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        if name not in self.collections:
            self.collections[name] = Collection(self, name)
        return self.collections[name]


class ProfileCursor(FakeCursor):
    def max_time_ms(self, _):
        return self

    def limit(self, count):
        self.documents = self.documents[:count]
        return self


class ProfileCollection(FakeCollection):
    def __init__(self):
        super().__init__()
        self.reads = []

    def with_options(self, **options):
        return self

    def find(self, query, projection=None):
        self.reads.append(deepcopy(query))
        return ProfileCursor(
            [
                project(row, projection)
                for row in self.documents
                if _matches(row, query) and presence_matches(row, query)
            ]
        )

    async def find_one(self, query, projection=None, **kwargs):
        rows = await self.find(query, projection).to_list(1)
        return rows[0] if rows else None


def environment(monkeypatch):
    database = Database()
    profile = {name: ProfileCollection() for name in ('subjects', 'facts', 'events', 'views', 'jobs', 'publications')}
    bindings = {
        'collection_health_subjects': profile['subjects'],
        'collection_health_profile_facts': profile['facts'],
        'collection_health_profile_events': profile['events'],
        'collection_health_profile_views': profile['views'],
        'collection_health_profile_jobs': profile['jobs'],
        'collection_health_profile_source_publications': profile['publications'],
        'collection_health_reports': database['health_reports'],
        'collection_health_report_revisions': database['health_report_revisions'],
    }
    for name, collection in bindings.items():
        monkeypatch.setattr(profile_application, name, collection)
    repository = MongoHealthProfileRepository(**profile)
    profile_service = HealthProfileService(repository)
    flow = extended_flow()
    processing = flow.service
    processing._health_profile_service = profile_service
    processing._report_source = HealthReportSourceService(
        MongoHealthReportStore(database['health_reports'], database['health_report_revisions']),
        HealthProfileJobQueue(repository),
        observation_store=MongoDocumentObservationStore(database['observations']),
    )
    processing._document_trace_store = MongoDocumentTraceStore(database['traces'], database['trace_chunks'])
    processing._document_reuse = DocumentReuseCoordinator(
        processing, MongoDocumentIndexStore(database['documents']), flow.downloader
    )
    processing.output_profiles = DocumentOutputProfiles()
    processing._document_outputs = DocumentOutputCoordinator(
        processing,
        MongoDocumentIndexStore(database['outputs'], identity_key='extractionKey'),
        processing.output_profiles,
    )
    resources = build_document_source_resources(
        documents=database['documents'],
        traces=database['traces'],
        trace_chunks=database['trace_chunks'],
        observations=database['observations'],
        reports=database['report_artifacts'],
    )
    # The production processing factory receives this gate at construction.
    processing._publication_ledger = resources.ledger
    graph = build_document_source_graph(
        resources=resources,
        processing=processing,
        profile_service=profile_service,
        agent_runtime=None,
        profile_contracts_factory=profile_application.create_application_profile_public_contracts,
        profile_admission_factory=profile_application.create_application_profile_source_admission,
    )
    return SimpleNamespace(database=database, profile=profile, flow=flow, graph=graph, profile_service=profile_service)


async def prepare(env):
    subject = (await env.profile_service.ensure_self_profile('user-1')).subject
    scope = DocumentScope('user-1', subject.subject_id)
    bundle = await env.graph.compatibility.prepare_bundle(
        scope=scope, selection=UNDERSTANDING_SELECTION, file_id='https://example.test/fixed.pdf'
    )
    item = DocumentPreparedPublicationInput(
        bundle.material.snapshot_id,
        bundle.facts.snapshot_id,
        bundle.understanding.snapshot_id,
        UNDERSTANDING_SELECTION,
        bundle.receipt,
    )
    return scope, bundle, item


async def finish(graph, command):
    now = [datetime.now(UTC)]
    graph.formal.clock = lambda: now[0].timestamp()
    await graph.formal.submit(command)
    for _ in range(8):
        await graph.formal.recover_pending()
        result = await graph.formal.read_receipt(scope=command.scope, command_id=command.command_id)
        if result['status'] != 'pending':
            return result
        now[0] += timedelta(seconds=31)
    pytest.fail(f'bounded formal recovery did not finish: {result}')


def test_real_factories_construct_without_io_and_share_one_gate(monkeypatch):
    env = environment(monkeypatch)
    graph = env.graph
    for collection in (*env.database.collections.values(), *env.profile.values()):
        assert not collection.documents and not collection.indexes and not collection.reads
    assert not env.flow.graph.calls and not env.flow.client.submit_calls
    assert env.flow.downloader.download.await_count == 0
    ledger = graph.resources.ledger
    assert graph.compatibility.legacy_publication.ledger is ledger
    assert graph.publication.ledger is graph.fresh.ledger is graph.managed.publisher.ledger is ledger
    assert graph.publications.compatibility is graph.compatibility
    assert graph.resolvers['document', COMPLETE_SOURCE_CONTRACT] is graph.resolvers['document', RETRACTION_CONTRACT]
    assert graph.profile.inbox.document_source is graph.resolvers['document', COMPLETE_SOURCE_CONTRACT]
    assert (
        graph.profile.worker.applier.resolvers['document', COMPLETE_SOURCE_CONTRACT]
        is graph.profile.inbox.document_source
    )
    for collection in (ledger.sources, ledger.intents):
        assert collection.read_preference == ReadPreference.PRIMARY
        assert collection.write_concern.document == {'w': 'majority', 'j': True, 'wtimeout': 5000}


@pytest.mark.asyncio
async def test_fresh_formal_base_reaches_actual_profile_through_readers_built_before_commands(monkeypatch):
    env = environment(monkeypatch)
    graph = env.graph
    scope, bundle, item = await prepare(env)
    command = DocumentFormalSourceCommand(scope, 'fresh-base', 'base', (item,), FORMAL_SOURCE_REVISION)
    completed = await finish(graph, command)
    assert completed['status'] == 'completed', completed
    base = completed['result']['basePublications'][0]
    assert base['kind'] == 'archive' and base['publicationRevision'] == 1
    assert not env.profile['jobs'].documents
    assert env.database['health_reports'].documents
    assert await graph.profile.worker.recover_pending() == 1
    snapshot = await graph.profile.reader.read(scope.user_id, subject_id=scope.subject_id)
    assert snapshot.status == 'ready' and snapshot.facts
    assert any(fact.value for fact in snapshot.facts)
    assert snapshot.source_watermarks[0].applied_publication_revision == 1
    assert len(env.flow.graph.calls) == len(env.flow.client.submit_calls) == 1
    assert await graph.formal.submit(command) == completed
    assert await graph.formal.recover_pending() == 0
    assert await graph.profile.worker.recover_pending() == 0
    assert bundle.material.source_id == snapshot.source_watermarks[0].source_id


@pytest.mark.asyncio
async def test_existing_source_uses_original_legacy_publisher_without_fresh_authorization(monkeypatch):
    env = environment(monkeypatch)
    graph = env.graph
    scope, _, item = await prepare(env)
    old = await graph.compatibility.publish_prepared(
        scope=scope,
        understanding_snapshot_id=item.understanding_snapshot_ref,
        selection=item.selection,
        preparation_receipt=item.preparation_receipt,
    )
    command = DocumentFormalSourceCommand(scope, 'old-base', 'base', (item,), FORMAL_SOURCE_REVISION)
    result = await finish(graph, command)
    assert result['status'] == 'completed', result
    base = result['result']['basePublications'][0]
    assert base['kind'] == 'legacy'
    assert base['receipt']['legacy_source_revision'] == old.legacy_source_revision
    assert len(env.profile['jobs'].documents) == 1
    assert not env.database['documents_source_admissions'].documents
    assert not env.profile['publications'].documents
    assert len(env.flow.graph.calls) == 1


def test_actual_consumers_construct_without_io_and_preserve_exact_old_versions(monkeypatch):
    env = environment(monkeypatch)
    consumers = build_document_consumers(graph=env.graph, legacy_processing_factory=lambda: env.flow.service)
    manifests = consumers.report.supported_document_manifests()
    current = consumers.report.document_input_manifest()
    assert current in manifests and len(manifests) == 3
    assert len({manifest['consumerRevision'] for manifest in manifests}) == 3
    with pytest.raises(ValueError, match='bind'):
        consumers.report.create(user_id='user', session_id='session', final_report_id='final')
    for manifest in manifests:
        with consumers.report.bind_document_manifest(manifest):
            reader = consumers.report.create(user_id='user', session_id='session', final_report_id='final')
            assert reader is not None
    unknown = {**current, 'consumerRevision': 'unknown'}
    with pytest.raises(ValueError, match='unavailable'):
        with consumers.report.bind_document_manifest(unknown):
            pytest.fail('unknown accepted version must fail closed')
    for collection in (*env.database.collections.values(), *env.profile.values()):
        assert not collection.documents and not collection.indexes and not collection.reads
    assert not env.flow.graph.calls


@pytest.mark.asyncio
async def test_managed_source_blocks_old_public_factory_on_the_same_ledger(monkeypatch):
    from bootstrap.report_documents import build_report_document_source_factory

    env = environment(monkeypatch)
    graph = env.graph
    scope, _, item = await prepare(env)
    command = DocumentFormalSourceCommand(scope, 'managed', 'base', (item,), FORMAL_SOURCE_REVISION)
    assert (await finish(graph, command))['status'] == 'completed'
    legacy = build_report_document_source_factory(
        processing_service=env.flow.service,
        profile_service=env.profile_service,
        document_collection=graph.resources.documents,
        traces=graph.resources.traces,
        trace_chunks=graph.resources.trace_chunks,
        report_artifact_collection=graph.resources.reports,
        publication_ledger=graph.resources.ledger,
    )
    assert legacy.compatibility.legacy_publication.ledger is graph.resources.ledger
    assert legacy.publications.compatibility is legacy.compatibility
    before = deepcopy(env.database['health_reports'].documents)
    for old_api in (legacy.compatibility, graph.compatibility):
        with pytest.raises(PublicationModeError):
            await old_api.publish_prepared(
                scope=scope,
                understanding_snapshot_id=item.understanding_snapshot_ref,
                selection=item.selection,
                preparation_receipt=item.preparation_receipt,
            )
    assert env.database['health_reports'].documents == before
    assert not env.profile['jobs'].documents


@pytest.mark.asyncio
async def test_real_record_consumer_persists_fixed_preparation_before_actual_archive(monkeypatch):
    from modules.record.documents import LEGACY_RECORD_DOCUMENT_CONSUMER
    from modules.record.documents import RECORD_DOCUMENT_CHECKPOINT
    from modules.record.documents import RECORD_FORMAL_DOCUMENT_CHECKPOINT

    env = environment(monkeypatch)
    graph = env.graph
    consumers = build_document_consumers(graph=graph, legacy_processing_factory=lambda: env.flow.service)
    record = consumers.record
    assert record.for_consumer_revision(RECORD_FORMAL_DOCUMENT_CHECKPOINT) is record
    assert record.for_consumer_revision(RECORD_DOCUMENT_CHECKPOINT) is not record
    assert record.for_consumer_revision(LEGACY_RECORD_DOCUMENT_CONSUMER) is not record
    saved = []

    async def checkpoint(prepared):
        assert not env.database['health_reports'].documents
        assert not env.profile['jobs'].documents
        saved.append(prepared.to_checkpoint())

    result = await record.process_attachment(
        user_id='user-1',
        session_id='record',
        file_index=0,
        attachment={'url': 'https://example.test/record.pdf'},
        settled_document_ids=set(),
        on_prepared=checkpoint,
    )
    assert len(saved) == 1 and saved[0]['contractVersion'] == RECORD_FORMAL_DOCUMENT_CHECKPOINT
    assert result.publication.ingestion_status == 'source_managed'
    assert result.publication.legacy_source_revision == 1 and result.publication.ingestion_job_id is None
    assert env.database['health_reports'].documents and not env.profile['jobs'].documents
    assert await graph.profile.worker.recover_pending() == 1
    snapshot = await graph.profile.reader.read('user-1')
    assert snapshot.facts and snapshot.source_watermarks[0].applied_publication_revision == 1
    assert len(env.flow.graph.calls) == len(env.flow.client.submit_calls) == 1


@pytest.mark.asyncio
async def test_real_analysis_consumer_returns_alias_and_keeps_managed_source_off_legacy_queue(monkeypatch):
    env = environment(monkeypatch)
    graph = env.graph
    consumers = build_document_consumers(graph=graph, legacy_processing_factory=lambda: env.flow.service)
    scope, bundle, _ = await prepare(env)
    result = await consumers.analysis.service.process(
        user_id=scope.user_id,
        subject_id=scope.subject_id,
        document_id=bundle.material.payload['documentId'],
        report_id='analysis-alias',
    )
    assert result['reportId'] == 'analysis-alias' and 'sourceRevision' not in result
    assert result['processing']['status'] == 'complete'
    records = env.database['documents_analysis_deliveries'].documents
    assert len(records) == 2 and next(iter(records.values()))['status'] == 'pending'
    now = [datetime.now(UTC)]
    graph.formal.clock = consumers.analysis.delivery.clock = lambda: now[0].timestamp()
    for _ in range(5):
        await graph.formal.recover_pending()
        await consumers.analysis.delivery.recover_pending()
        now[0] += timedelta(seconds=31)
    assert next(iter(records.values()))['status'] == 'completed'
    assert not env.profile['jobs'].documents
    assert await graph.profile.worker.recover_pending() == 1
    assert (await graph.profile.reader.read(scope.user_id)).facts
    assert len(env.flow.graph.calls) == len(env.flow.client.submit_calls) == 1


@pytest.mark.asyncio
async def test_real_report_consumer_keeps_projection_while_fresh_source_waits_for_application(monkeypatch):
    from modules.report.schemas import RemoteFileInput

    env = environment(monkeypatch)
    graph = env.graph
    consumers = build_document_consumers(graph=graph, legacy_processing_factory=lambda: env.flow.service)
    manifest = consumers.report.document_input_manifest()
    with consumers.report.bind_document_manifest(manifest):
        reader = consumers.report.create(user_id='user-1', session_id='report', final_report_id='final')
        result = await reader.read(RemoteFileInput(url='https://example.test/report.pdf', file_type='pdf'), index=0)
    reference = result.references[0]
    assert reference['processingStatus'] == 'completed'
    assert reference['formalSource']['commandRevision'] == 'document-formal-source-v2'
    assert reference['formalSource']['status'] == 'pending'
    assert result.parsed.supplementary_hospital_data
    assert env.database['report_artifacts_document_projections'].documents
    assert not env.profile['jobs'].documents
    assert await graph.profile.worker.recover_pending() == 1
    assert (await graph.profile.reader.read('user-1')).facts
    assert len(env.flow.graph.calls) == len(env.flow.client.submit_calls) == 1
