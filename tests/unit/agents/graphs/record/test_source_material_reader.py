"""Record material resolution can be installed before Profile's write-side ports."""

import asyncio
from copy import deepcopy
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.record.completion import MongoRecordCompletionStore
from adapters.record.profile_source import create_record_profile_source_bundle
from adapters.record.profile_source import create_record_source_material_reader
from adapters.record.profile_source_store import MongoRecordCompletionEvidence
from adapters.record.profile_source_store import MongoRecordSourceStore
from modules.health_profile.application import ProfileUserSourceResolver
from modules.health_profile.contracts import ProfileSourceEventV1
from modules.record.profile_publication import RecordSourceConflict
from modules.record.profile_publication import frozen_json
from modules.record.profile_source import RECORD_PROFILE_SOURCE_REVISION
from modules.record.profile_source import RecordSourceDraftV1
from modules.record.profile_source import content_digest
from tests.unit.agents.graphs.record.test_profile_publication import Admission
from tests.unit.agents.graphs.record.test_profile_publication import Collection
from tests.unit.agents.graphs.record.test_profile_publication import Receiver
from tests.unit.agents.graphs.record.test_profile_source_draft import session


def fixed_row():
    scope = {'userId': 'user', 'subjectId': 'self'}
    material = RecordSourceDraftV1.capture(session()).profile_material(scope)
    return {
        '_id': 'record-intent',
        'scope': scope,
        'snapshotRef': {
            'provider': 'record',
            'contractVersion': 'profile.record-source.v2',
            'snapshotId': 'record_material_' + content_digest(material),
        },
        'materialJson': frozen_json(material),
    }, material


def event(row, material):
    return ProfileSourceEventV1(
        event_id='record-event',
        scope=row['scope'],
        source_type='record',
        source_id=material['sourceId'],
        source_revision=material['sourceRevision'],
        publication_revision=1,
        event_kind='publish',
        occurred_at=datetime(2026, 11, 10, tzinfo=UTC),
        content_digest=content_digest(material),
        snapshot_ref=row['snapshotRef'],
    )


def test_factory_requires_only_find_snapshot_and_never_constructs_publication_service(monkeypatch):
    import modules.record.profile_publication as publication

    def forbidden(*args, **kwargs):
        raise AssertionError('a read-only resolver must not construct the publication service')

    monkeypatch.setattr(publication, 'RecordSourcePublicationService', forbidden)
    store = SimpleNamespace(find_snapshot=AsyncMock(side_effect=AssertionError('constructor must not read')))
    reader = create_record_source_material_reader(store=store)
    assert callable(reader.read_source_snapshot)
    store.find_snapshot.assert_not_awaited()
    with pytest.raises(ValueError, match='explicit Record source store'):
        create_record_source_material_reader(store=None)


@pytest.mark.asyncio
async def test_real_public_resolver_registered_before_material_exists_reads_it_without_writes():
    collection = Collection()
    store = MongoRecordSourceStore(collection)
    reader = create_record_source_material_reader(store=store)
    resolver = ProfileUserSourceResolver(reader)
    row, material = fixed_row()
    fixed_event = event(row, material)
    with pytest.raises(LookupError, match='authorized fixed Record material'):
        await resolver.resolve(fixed_event)
    await store.insert(row)
    before = deepcopy(collection.documents)
    resolved = await resolver.resolve(fixed_event)
    assert resolved.snapshot == material
    assert resolved.scope == fixed_event.scope
    assert resolved.source_id == material['sessionId']
    assert resolved.source_revision == material['sourceRevision']
    assert collection.documents == before and not collection.indexes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'changed',
    [{'provider': 'chat'}, {'contractVersion': 'profile.record-source.v1'}, {'contractVersion': 'future.v99'}],
)
async def test_wrong_provider_or_contract_never_reads_or_selects_another_version(changed):
    row, _ = fixed_row()
    store = SimpleNamespace(
        find_snapshot=AsyncMock(side_effect=AssertionError('unsupported selection must stop early'))
    )
    reader = create_record_source_material_reader(store=store)
    with pytest.raises(RecordSourceConflict, match='unsupported Record material reference'):
        await reader.read_source_snapshot({**row['snapshotRef'], **changed}, scope=row['scope'])
    store.find_snapshot.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['missing', 'user', 'subject', 'reference', 'digest', 'duplicate'])
async def test_standalone_reader_preserves_missing_ownership_digest_and_unique_store_errors(fault):
    row, _ = fixed_row()
    collection = Collection()
    store = MongoRecordSourceStore(collection)
    reader = create_record_source_material_reader(store=store)
    ref, scope = deepcopy(row['snapshotRef']), deepcopy(row['scope'])
    if fault == 'user':
        scope['userId'] = 'other-user'
    if fault == 'subject':
        scope['subjectId'] = 'other-subject'
    if fault == 'reference':
        row['snapshotRef']['contractVersion'] = 'profile.record-source.v1'
    if fault == 'digest':
        row['materialJson'] = '{}'
    if fault != 'missing':
        await store.insert(row)
    if fault == 'duplicate':
        await store.insert({**row, '_id': 'duplicate-intent'})
    before = deepcopy(collection.documents)
    expected = RecordSourceConflict if fault in {'digest', 'duplicate'} else LookupError
    with pytest.raises(expected):
        await reader.read_source_snapshot(ref, scope=scope)
    assert collection.documents == before and not collection.indexes


@pytest.mark.asyncio
async def test_read_returns_detached_original_arrays_and_object_order():
    row, material = fixed_row()
    store = SimpleNamespace(find_snapshot=AsyncMock(return_value=row))
    reader = create_record_source_material_reader(store=store)
    first = await reader.read_source_snapshot(row['snapshotRef'], scope=row['scope'])
    assert frozen_json(first) == row['materialJson']
    first['qaHistory'].clear()
    first['hospitalData'][0]['nested'].reverse()
    second = await reader.read_source_snapshot(row['snapshotRef'], scope=row['scope'])
    assert second == material
    assert list(second['hospitalData'][0]) == ['z', 'nested']
    assert frozen_json(second) == row['materialJson']


@pytest.mark.asyncio
async def test_caller_cannot_change_ref_or_scope_while_fixed_lookup_is_awaited():
    row, material = fixed_row()
    entered, release = asyncio.Event(), asyncio.Event()
    selected = []

    async def lookup(snapshot_id):
        selected.append(snapshot_id)
        entered.set()
        await release.wait()
        return deepcopy(row)

    reader = create_record_source_material_reader(store=SimpleNamespace(find_snapshot=lookup))
    ref, scope = deepcopy(row['snapshotRef']), deepcopy(row['scope'])
    pending = asyncio.create_task(reader.read_source_snapshot(ref, scope=scope))
    await entered.wait()
    ref['snapshotId'], scope['subjectId'] = 'new-latest', 'other'
    release.set()
    assert await pending == material
    assert selected == [row['snapshotRef']['snapshotId']]


@pytest.mark.asyncio
async def test_storage_failure_remains_failure_without_other_io_or_material_regeneration():
    row, _ = fixed_row()
    store = SimpleNamespace(find_snapshot=AsyncMock(side_effect=RuntimeError('offline storage')))
    reader = create_record_source_material_reader(store=store)
    with pytest.raises(RuntimeError, match='offline storage'):
        await reader.read_source_snapshot(row['snapshotRef'], scope=row['scope'])
    store.find_snapshot.assert_awaited_once_with(row['snapshotRef']['snapshotId'])


@pytest.mark.asyncio
async def test_early_resolver_and_later_bundle_share_fixed_material_without_mutable_proxy():
    intents, sessions = Collection(), Collection()
    store = MongoRecordSourceStore(intents)
    early = create_record_source_material_reader(store=store)
    resolver = ProfileUserSourceResolver(early)
    admission, receiver = Admission(), Receiver()
    bundle = create_record_profile_source_bundle(
        legacy=SimpleNamespace(),
        completion_store=MongoRecordCompletionStore(sessions),
        store=store,
        admission=admission,
        receiver=receiver,
        completion_evidence=MongoRecordCompletionEvidence(sessions),
        default_revision=RECORD_PROFILE_SOURCE_REVISION,
        clock=lambda: 1000.0,
    )
    raw = session()
    raw['recordProfileConsumerRevision'] = RECORD_PROFILE_SOURCE_REVISION
    bundle.source.prepare_completed_snapshot(raw)
    raw['recordCompletionId'] = 'fixed-commit'
    assert await bundle.completion_store.publish_once('user', 'session', raw)
    assert not admission.calls and not receiver.events
    assert await bundle.worker.recover_pending() == 1
    row = next(iter(intents.documents.values()))
    before = deepcopy((intents.documents, sessions.documents, admission.calls, receiver.receive_calls))
    fixed_event = ProfileSourceEventV1.model_validate(row['event'])
    resolved = await resolver.resolve(fixed_event)
    assert resolved.snapshot == await bundle.material_reader.read_source_snapshot(
        row['snapshotRef'], scope=row['scope']
    )
    assert resolved.snapshot == await bundle.worker.read_source_snapshot(row['snapshotRef'], scope=row['scope'])
    assert before == (intents.documents, sessions.documents, admission.calls, receiver.receive_calls)
    assert resolved.snapshot['qaHistory'] == raw['qaHistory']
