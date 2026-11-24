"""Public Profile resolution consumes exact Chat-owned immutable raw snapshots."""

from copy import deepcopy

import pytest

from adapters.chat.source_material import ChatSourceSnapshotReader
from adapters.chat.source_material import MongoChatSourceMaterialStore
from adapters.chat.source_material import material_from_saved_session
from modules.chat.profile_source import prepare_user_message_source
from modules.health_profile.application import ProfileUserSourceResolver
from modules.health_profile.contracts import ProfileScope
from modules.health_profile.contracts import ProfileSourceEventV1
from tests.unit.services.analysis.test_document_material_reader import BsonCollection
from tests.unit.services.chat.test_profile_source_draft import source

SCOPE = ProfileScope(user_id='u', subject_id='self')


def material():
    session, message = source()
    prepare_user_message_source(session, message)
    return material_from_saved_session(session, 'm', scope=SCOPE)


@pytest.mark.asyncio
async def test_real_public_resolver_reads_exact_raw_source_without_session_or_model():
    store = MongoChatSourceMaterialStore(BsonCollection())
    value = material()
    saved = await store.put_once(value)
    event = ProfileSourceEventV1.model_validate(saved['event'])
    reader = ChatSourceSnapshotReader(store)
    resolved = await ProfileUserSourceResolver(reader).resolve(event)
    assert resolved.snapshot == value['snapshot']
    assert resolved.snapshot['content'] == '  original\ntext  '
    assert resolved.snapshot['timestamp'] == '2026-11-10T12:00:00'
    await store.put_once(value)
    assert len(store.collection.documents) == 1 and not store.collection.indexes
    resolved.snapshot['content'] = 'tampered'
    assert (await reader.read_source_snapshot(event.snapshot_ref, scope=SCOPE))['content'] == value['snapshot'][
        'content'
    ]


@pytest.mark.asyncio
async def test_public_reader_cannot_cross_owner_or_substitute_a_version():
    store = MongoChatSourceMaterialStore(BsonCollection())
    value = await store.put_once(material())
    event = ProfileSourceEventV1.model_validate(value['event'])
    reader = ChatSourceSnapshotReader(store)
    with pytest.raises(FileNotFoundError):
        await reader.read_source_snapshot(event.snapshot_ref, scope=ProfileScope(user_id='other', subject_id='self'))
    with pytest.raises(ValueError):
        await reader.read_source_snapshot(
            event.snapshot_ref.model_copy(update={'contract_version': 'profile.chat-source.v1'}), scope=SCOPE
        )


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['text', 'scope', 'revision', 'event_ref', 'policy'])
async def test_immutable_snapshot_or_event_conflicts_are_never_overwritten(change):
    store = MongoChatSourceMaterialStore(BsonCollection())
    value = await store.put_once(material())
    changed = deepcopy(value)
    if change == 'text':
        changed['snapshot']['content'] = 'changed'
    elif change == 'scope':
        changed['snapshot']['scope']['subjectId'] = 'other'
    elif change == 'revision':
        changed['snapshot']['sourceRevision'] = 2
    elif change == 'event_ref':
        changed['event']['snapshotRef']['snapshotId'] = 'missing'
    else:
        changed['deliveryPolicy']['max_attempts'] += 1
    with pytest.raises(ValueError):
        await store.put_once(changed)
    assert list(store.collection.documents.values())[0]['snapshot'] == value['snapshot']


def test_scope_binding_rejects_request_for_another_user():
    session, message = source()
    prepare_user_message_source(session, message)
    with pytest.raises(PermissionError):
        material_from_saved_session(session, 'm', scope=ProfileScope(user_id='other', subject_id='self'))


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['policy', 'created_at', 'event_id', 'event_time', 'draft_digest'])
async def test_first_write_cannot_accept_a_changed_delivery_identity(change):
    store = MongoChatSourceMaterialStore(BsonCollection())
    value = material()
    if change == 'policy':
        value['deliveryPolicy']['max_attempts'] = 999
    elif change == 'created_at':
        value['createdAt'] = '2020-01-01T00:00:00+00:00'
    elif change == 'event_id':
        value['event']['eventId'] = 'changed'
    elif change == 'event_time':
        value['event']['occurredAt'] = '2020-01-01T00:00:00Z'
    else:
        value['draftDigest'] = '0' * 64
    with pytest.raises(ValueError):
        await store.put_once(value)
    assert not store.collection.documents
