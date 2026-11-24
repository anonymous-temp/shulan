"""Shared source gate assembly is acyclic, exact and offline."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import MethodType
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pymongo import AsyncMongoClient
from pymongo import ReadPreference
from pymongo.write_concern import WriteConcern

from adapters.analysis.managed_source import create_document_managed_source_readers
from adapters.analysis.managed_source import create_document_managed_source_runtime
from adapters.analysis.publication import MongoDocumentPublicationLedger
from adapters.analysis.publication_runtime import create_document_publication_runtime
from adapters.analysis.source_admission import create_document_fresh_source_admission
from health_profile.contracts import ProfileScope
from health_profile.document_source import ProfileDocumentSourceResolver
from health_profile.report_source import MongoHealthReportStore
from health_profile.repository import MongoHealthProfileRepository
from health_profile.service import HealthProfileService
from health_profile.source_admission import ProfileFreshSourceAdmission
from health_profile.source_application import ProfilePublicationWorker
from health_profile.source_application import ProfileSourceApplier
from health_profile.source_archive import ProfileSourceMaterialArchive
from health_profile.source_presence import MongoProfileSourcePresenceReader
from health_profile.source_publications import MongoProfilePublicationStore
from health_profile.source_publications import ProfileSourceInbox
from modules.analysis.snapshots import COMPLETE_SOURCE_CONTRACT
from modules.analysis.snapshots import RETRACTION_CONTRACT
from modules.analysis.snapshots import DocumentScope
from modules.analysis.understanding import UNDERSTANDING_SELECTION
from tests.unit.health_profile.test_profile_read_and_subjects import FakeCollection
from tests.unit.health_profile.test_public_contracts import visible
from tests.unit.health_profile.test_source_admission import Collection
from tests.unit.services.analysis.fakes import MemoryMongoCollection
from tests.unit.services.analysis.test_document_fresh_profile_composition import capture
from tests.unit.services.analysis.test_document_managed_source import command
from tests.unit.services.analysis.test_document_managed_source import item
from tests.unit.services.analysis.test_document_material_reader import BsonCollection
from tests.unit.services.analysis.test_document_report_publication import environment


class OptionsCollection:
    def __init__(self, *, preference=ReadPreference.PRIMARY, concern=WriteConcern(w='majority')):
        self.read_preference = preference
        self.write_concern = concern
        self.options = []

    def with_options(self, **options):
        self.options.append(options)
        return self

    async def find_one(self, *_args, **_kwargs):
        raise AssertionError('factory must not read storage')

    async def insert_one(self, *_args, **_kwargs):
        raise AssertionError('factory must not write storage')


def assemble(**kwargs):
    return create_document_publication_runtime(
        observation_collection=OptionsCollection(),
        snapshots=object(),
        profile_inbox=SimpleNamespace(receive=AsyncMock(), get_cutover=AsyncMock()),
        legacy_ingestion_reader=object(),
        **kwargs,
    )


def test_injected_ledger_keeps_every_identity_and_collection_without_rebinding():
    sources, intents = OptionsCollection(), OptionsCollection(concern=WriteConcern(w='majority', j=True))
    ledger = MongoDocumentPublicationLedger(sources=sources, intents=intents)
    runtime = assemble(ledger=ledger)
    assert runtime.ledger is runtime.recovery.ledger is runtime.lifecycle.ledger is ledger
    assert runtime.observation_v2.lifecycle.ledger is ledger
    assert ledger.sources is sources and ledger.intents is intents
    assert not sources.options and not intents.options
    runtime.recovery.receiver.inbox.receive.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_driver_handles_retain_namespace_and_options_without_connecting():
    client = AsyncMongoClient('mongodb://127.0.0.1:1', connect=False)
    try:
        database = client['offline_document_composition']
        sources = database.get_collection(
            'source', read_preference=ReadPreference.PRIMARY, write_concern=WriteConcern(w='majority')
        )
        intents = database.get_collection(
            'intent', read_preference=ReadPreference.PRIMARY, write_concern=WriteConcern(w='majority', j=True)
        )
        ledger = MongoDocumentPublicationLedger(sources=sources, intents=intents)
        runtime = assemble(ledger=ledger)
        assert runtime.ledger is ledger
        assert runtime.ledger.sources is sources and runtime.ledger.intents is intents
        assert sources.database.client is client and intents.database.client is client
        assert sources.full_name == 'offline_document_composition.source'
        assert intents.full_name == 'offline_document_composition.intent'
    finally:
        await client.close()


@pytest.mark.parametrize('field', ['source_collection', 'intent_collection'])
def test_injected_ledger_refuses_a_second_collection_configuration(field):
    ledger = MongoDocumentPublicationLedger(sources=OptionsCollection(), intents=OptionsCollection())
    with pytest.raises(ValueError, match='not both'):
        assemble(ledger=ledger, **{field: getattr(ledger, 'sources' if field == 'source_collection' else 'intents')})


@pytest.mark.parametrize('field', ['sources', 'intents'])
@pytest.mark.parametrize(
    'options,error',
    [
        ({'preference': ReadPreference.SECONDARY_PREFERRED}, 'PRIMARY'),
        ({'concern': WriteConcern(w=1)}, 'majority'),
        ({'concern': WriteConcern(w=0)}, 'majority'),
        ({'concern': {'w': 'majority'}}, 'majority'),
    ],
)
def test_injected_ledger_rejects_unsafe_actual_collection_options(field, options, error):
    collections = {'sources': OptionsCollection(), 'intents': OptionsCollection()}
    collections[field] = OptionsCollection(**options)
    with pytest.raises(ValueError, match=error):
        assemble(ledger=MongoDocumentPublicationLedger(**collections))
    assert not collections[field].options


@pytest.mark.parametrize(
    'ledger', [object(), SimpleNamespace(sources=OptionsCollection(), intents=OptionsCollection())]
)
def test_injected_gate_requires_the_real_provider_type(ledger):
    with pytest.raises(TypeError, match='MongoDocumentPublicationLedger'):
        assemble(ledger=ledger)


@pytest.mark.parametrize(
    'kwargs', [{}, {'source_collection': OptionsCollection()}, {'intent_collection': OptionsCollection()}]
)
def test_original_collection_mode_requires_both_collections(kwargs):
    with pytest.raises(ValueError, match='both source and intent'):
        assemble(**kwargs)


def configure(collection):
    collection.read_preference = ReadPreference.PRIMARY
    collection.write_concern = WriteConcern(w='majority')

    def with_options(current, **options):
        for key, value in options.items():
            setattr(current, key, value)
        return current

    collection.with_options = MethodType(with_options, collection)
    return collection


@pytest.mark.asyncio
async def test_real_readers_profile_then_runtime_apply_and_retract_without_late_rebinding(monkeypatch):
    env = environment()
    for collection in (
        env.ledger.sources,
        env.ledger.intents,
        env.records,
        env.commands,
        env.flow.observations.collection,
    ):
        configure(collection)
    env.commands.find = MethodType(MemoryMongoCollection.find, env.commands)
    admission_records, command_records = configure(BsonCollection()), configure(BsonCollection())
    # The exact Document facades exist before any Profile provider is constructed.
    readers = create_document_managed_source_readers(
        compatibility=env.api,
        report_publications=env.publisher,
        ledger=env.ledger,
        admission_records=admission_records,
        command_records=command_records,
    )
    resolver = ProfileDocumentSourceResolver(readers.source_reader)
    now = [datetime(2026, 11, 10, tzinfo=UTC)]
    collections = {name: FakeCollection() for name in ('subjects', 'facts', 'events', 'views', 'jobs', 'publications')}
    repository = MongoHealthProfileRepository(**collections)
    inbox = ProfileSourceInbox(
        MongoProfilePublicationStore(collections['publications']),
        repository=repository,
        document_source=resolver,
        clock=lambda: now[0],
    )
    applier = ProfileSourceApplier(
        inbox, resolvers={('document', COMPLETE_SOURCE_CONTRACT): resolver, ('document', RETRACTION_CONTRACT): resolver}
    )
    presence_collections = {
        name: Collection() for name in ('subjects', 'reports', 'revisions', 'jobs', 'facts', 'publications')
    }
    for name in ('subjects', 'jobs', 'facts', 'publications'):
        presence_collections[name].documents = collections[name].documents
    presence = MongoProfileSourcePresenceReader(**presence_collections, clock=lambda: now[0])
    fresh = ProfileFreshSourceAdmission(
        inbox=inbox, presence_reader=presence, owner_fence_readers={'document': readers.owner_fence_reader}
    )
    archive = ProfileSourceMaterialArchive(
        report_store=MongoHealthReportStore(presence_collections['reports'], presence_collections['revisions']),
        publication_store=inbox.store,
        clock=lambda: now[0],
    )
    with monkeypatch.context() as patch:
        patch.setattr(inbox, 'receive', AsyncMock(side_effect=AssertionError('no construction I/O')))
        patch.setattr(env.ledger, 'read_source', AsyncMock(side_effect=AssertionError('no construction I/O')))
        runtime = create_document_publication_runtime(
            ledger=env.ledger,
            observation_collection=env.flow.observations.collection,
            snapshots=env.api.understanding.snapshots,
            profile_inbox=inbox,
            legacy_ingestion_reader=object(),
            clock=lambda: now[0],
        )
        admission = create_document_fresh_source_admission(
            compatibility=env.api,
            report_publications=env.publisher,
            ledger=env.ledger,
            admission_records=admission_records,
            observation_collection=env.flow.observations.collection,
            profile_presence=presence,
            profile_inbox=inbox,
        )
        managed = create_document_managed_source_runtime(
            compatibility=env.api,
            report_publications=env.publisher,
            material_reader=env.materials,
            source_admission=admission,
            publication_runtime=runtime,
            profile_archive=archive,
            command_records=command_records,
        )
    assert managed.publisher.ledger is admission.ledger is runtime.ledger is env.api.legacy_publication.ledger
    assert inbox.document_source is resolver and fresh.owners['document'] is readers.owner_fence_reader
    # Commands begin only after the whole immutable dependency graph is assembled.
    subject = await HealthProfileService(repository, clock=lambda: now[0]).ensure_self_profile('u')
    scope = ProfileScope(user_id='u', subject_id=subject.subject.subject_id)
    doc_scope = DocumentScope(scope.user_id, scope.subject_id)
    bundle = await env.api.prepare_bundle(
        scope=doc_scope, selection=UNDERSTANDING_SELECTION, file_id='https://example.test/acyclic.pdf'
    )
    profile = SimpleNamespace(
        scope=scope, now=now, repository=repository, worker=ProfilePublicationWorker(inbox, applier)
    )
    flow = SimpleNamespace(
        env=env,
        profile=profile,
        scope=doc_scope,
        bundle=bundle,
        item=item(bundle),
        admission=admission,
        api=managed.source_reader,
        resolver=resolver,
    )
    plan_ref, captured, authorization = await capture(flow)
    await fresh.authorize_fresh_source(authorization, scope=scope)
    await admission.activate(
        scope=doc_scope,
        plan_ref=plan_ref,
        source_reader=managed.source_reader,
        source_snapshot_id=captured.source_snapshot_ref,
    )
    await managed.publisher.publish(
        scope=doc_scope, snapshot_id=captured.source_snapshot_ref, request_id='first', expected_prior_revision=None
    )
    assert await profile.worker.recover_pending() == 1
    now[0] += timedelta(minutes=1)
    await runtime.recovery.recover_pending()
    flow.current_ref = captured.source_snapshot_ref
    corrected = await managed.commands.execute(command(flow, bundle, operation='replace_report'))
    assert await profile.worker.recover_pending() == 1
    now[0] += timedelta(minutes=1)
    await runtime.recovery.recover_pending()
    assert await visible(profile)
    assert await readers.source_reader.read_source_observations(scope=scope, snapshot_id=corrected['sourceSnapshotRef'])
    await managed.publisher.retract(
        scope=doc_scope,
        source_id=bundle.material.source_id,
        source_revision=1,
        request_id='retract',
        expected_prior_revision=2,
    )
    assert await profile.worker.recover_pending() == 1
    assert not await visible(profile)
    assert inbox.document_source is resolver
