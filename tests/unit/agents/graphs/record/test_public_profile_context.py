"""Record consumes actual public readers without changing its legacy context."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bson import BSON

from adapters.record.profile_context import RecordDocumentSupplementAdapter
from adapters.record.profile_context import RecordProfilePageAdapter
from adapters.record.profile_context import create_record_profile_memory
from health_profile.document_observations import MongoDocumentObservationStore
from health_profile.memory import RepositoryHealthProfileChatMemoryProvider
from health_profile.memory import _json_safe
from modules.analysis.snapshots import DocumentScope
from modules.health_profile.contracts import ProfilePublicationWatermarkV1
from modules.health_profile.contracts import ProfileScope
from modules.record.profile_memory import RecordProfileMemory
from modules.record.profile_memory import RecordProfileReadError
from tests.unit.health_profile.test_fact_pages import environment
from tests.unit.health_profile.test_fact_pages import stored
from tests.unit.health_profile.test_profile_read_and_subjects import FakeCollection
from tests.unit.services.analysis import test_document_observation_reader as observations


def guard_mutations(flow):
    for name in ('ensure_indexes', 'create_subject', 'create_fact', 'save_view', 'create_ingestion_job'):
        setattr(flow.repository, name, AsyncMock(side_effect=AssertionError('pure reads only')))


@pytest.mark.asyncio
async def test_real_public_paging_reads_all_before_legacy_priority_selection_without_writes():
    flow = environment(count=1102)
    for document in flow.collections['facts'].documents:
        document['category'] = 'condition'
        document['factType'] = 'condition.diagnosis'
    flow.collections['facts'].documents[0]['category'] = 'allergy'
    flow.collections['facts'].documents[0]['factType'] = 'allergy.reported'
    expected = await RepositoryHealthProfileChatMemoryProvider(flow.repository).build_context('u')
    before = stored(flow)
    guard_mutations(flow)
    flow.reader.read_page = AsyncMock(wraps=flow.reader.read_page)
    actual = await create_record_profile_memory(flow.reader).build_context('u')
    assert actual == expected and 'fact-1101' in actual
    assert flow.reader.read_page.await_count == 4 and stored(flow) == before


@pytest.mark.asyncio
async def test_actual_public_subject_relationship_preserves_blood_family_and_excludes_spouse():
    flow = environment(count=1)
    for identity, blood in (('father', True), ('spouse', False)):
        subject = deepcopy(flow.collections['subjects'].documents[0])
        subject.update(
            _id=identity,
            subjectId=identity,
            kind='blood_relative' if blood else 'non_blood_family',
            displayName=identity,
            relationship={'relation_type': identity, 'is_blood_relative': blood},
        )
        # Persisted domain documents use the normal Mongo aliases.
        subject['relationship'] = {'relationType': identity, 'isBloodRelative': blood}
        flow.collections['subjects'].documents.append(subject)
        view = deepcopy(flow.collections['views'].documents[0])
        view.update(_id='view-' + identity, subjectId=identity)
        flow.collections['views'].documents.append(view)
        fact = deepcopy(flow.collections['facts'].documents[0])
        fact.update(
            _id='fact-' + identity,
            factId='fact-' + identity,
            subjectId=identity,
            category='family_history',
            factType='family_history.condition',
        )
        flow.collections['facts'].documents.append(fact)
    public_subjects = await flow.reader.list_subjects('u')
    father = next(item for item in public_subjects if item.subject_id == 'father')
    assert father.relationship == {'relation_type': 'father', 'is_blood_relative': True}
    expected = await RepositoryHealthProfileChatMemoryProvider(flow.repository).build_context('u')
    actual = await create_record_profile_memory(flow.reader).build_context('u')
    assert actual == expected and 'fact-father' in actual and 'fact-spouse' not in actual


class BsonCollection(FakeCollection):
    @staticmethod
    def wire(value):
        return BSON.encode({'value': value}).decode()['value']

    async def find_one(self, filters):
        return self.wire(await super().find_one(self.wire(filters)))

    def find(self, filters):
        return super().find(self.wire(filters))


@pytest.mark.asyncio
async def test_real_bson_dates_are_utc_and_do_not_change_legacy_context_or_skip_pages():
    flow = environment(count=7, collection_type=BsonCollection)
    expected = await RepositoryHealthProfileChatMemoryProvider(flow.repository).build_context('u')
    for collection in flow.collections.values():
        collection.documents = collection.wire(collection.documents)
    memory = RecordProfileMemory(RecordProfilePageAdapter(flow.reader), page_size=2)
    actual = await memory.build_context('u')
    assert actual == expected and '+00:00' in actual


@pytest.mark.asyncio
async def test_python_fact_values_are_sanitized_once_and_detached_before_next_await():
    flow = environment(count=1)
    page = await flow.reader.read_page('u')
    raw = {'nested': {'x': {'time': datetime(2026, 11, 10, tzinfo=UTC)}}, 'text': ['x' * 300]}
    page = page.model_copy(update={'facts': (page.facts[0].model_copy(update={'value': raw}),)})
    reader = SimpleNamespace(read_page=AsyncMock(return_value=page))
    captured = await RecordProfilePageAdapter(reader).read_page('u')
    expected = _json_safe(raw)
    raw['text'][0] = 'mutated'
    assert captured.facts[0].value == expected
    captured.facts[0].value['text'][0] = 'mutated copy'
    assert captured.facts[0].value == expected


@pytest.mark.asyncio
async def test_watermark_order_is_not_a_revision_change_but_actual_state_is():
    flow = environment(count=3)
    base = await flow.reader.read_page('u', page_size=1)
    one = ProfilePublicationWatermarkV1(
        scope=flow.scope,
        source_type='record',
        source_id='x',
        accepted_publication_revision=2,
        applied_publication_revision=1,
        affected_subject_ids=('z', 'self-u'),
    )
    two = ProfilePublicationWatermarkV1(scope=flow.scope, source_type='chat', source_id='x')
    pages = [
        base.model_copy(update={'source_watermarks': (one, two)}),
        base.model_copy(
            update={'source_watermarks': (two, one.model_copy(update={'affected_subject_ids': ('self-u', 'z')}))}
        ),
        base.model_copy(
            update={'source_watermarks': (one.model_copy(update={'settled_publication_revision': 2}), two)}
        ),
        base.model_copy(
            update={'source_watermarks': (one, two), 'coverage': {**base.coverage, 'publicationStateAvailable': True}}
        ),
    ]
    adapter = RecordProfilePageAdapter(SimpleNamespace(read_page=AsyncMock(side_effect=pages)))
    results = [await adapter.read_page('u', page_size=1) for _ in pages]
    assert results[0].view_token == results[1].view_token
    assert results[0].view_token != results[2].view_token != results[3].view_token


@pytest.mark.asyncio
@pytest.mark.parametrize('corruption', ['user', 'scope', 'subject', 'contract', 'sort', 'watermark', 'revision'])
async def test_public_boundary_rejects_wrong_scope_or_unavailable_versions(corruption):
    flow = environment(count=1)
    page = await flow.reader.read_page('u')
    updates = {
        'user': {'user_id': 'other'},
        'scope': {'scope': ProfileScope(user_id='other', subject_id='self-u')},
        'subject': {'subject': page.subject.model_copy(update={'subject_id': 'other'})},
        'contract': {'contract_version': 'profile-fact-page.v2'},
        'sort': {'sort_version': 'new-sort'},
        'watermark': {
            'source_watermarks': (
                ProfilePublicationWatermarkV1(
                    scope=ProfileScope(user_id='u', subject_id='unrelated'), source_type='chat', source_id='x'
                ),
            )
        },
        'revision': {'profile_revision': None},
    }
    adapter = RecordProfilePageAdapter(
        SimpleNamespace(read_page=AsyncMock(return_value=page.model_copy(update=updates[corruption])))
    )
    with pytest.raises((ValueError, PermissionError, RecordProfileReadError)):
        await adapter.read_page('u')


@pytest.mark.asyncio
async def test_actual_provider_change_discards_round_and_restarts_with_new_revision():
    flow = environment(count=4)
    original = flow.reader.read_page
    calls = []

    async def changing(*args, **kwargs):
        calls.append(kwargs.get('cursor'))
        if len(calls) == 2:
            flow.collections['views'].documents[0]['profileVersion'] = 2
        return await original(*args, **kwargs)

    flow.reader.read_page = changing
    memory = RecordProfileMemory(RecordProfilePageAdapter(flow.reader), page_size=2)
    actual = await memory.build_context('u')
    expected = await RepositoryHealthProfileChatMemoryProvider(flow.repository).build_context('u')
    assert actual == expected and len(calls) == 4 and calls[2] is None


@pytest.mark.asyncio
async def test_real_document_observations_preserve_legacy_order_json_and_first_hundred_boundary():
    flow = environment(count=1)
    rows = [
        observations.row(
            i,
            userId='u',
            subjectId='self-u',
            observation={
                'z_raw': 'first',
                'itemName': 'item',
                'resultValue': str(i),
                'a_raw': 'last',
            },
        )
        for i in range(102)
    ]
    public, collection, status = observations.setup(rows)
    old_store = MongoDocumentObservationStore(
        observations.Collection(rows), SimpleNamespace(find_one=AsyncMock(return_value={'status': 'pending'}))
    )
    expected = await RepositoryHealthProfileChatMemoryProvider(
        flow.repository, document_observation_store=old_store
    ).build_context('u')
    actual = await create_record_profile_memory(flow.reader, document_observations=public).build_context('u')
    assert actual == expected
    assert collection.lengths == [101] and len(collection.find_calls) == 1
    collection.create_index.assert_not_awaited()
    status.read_status.assert_awaited_once_with(
        scope=DocumentScope('u', 'self-u'), source_id='report', source_revision=7
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('corruption', ['scope', 'contract', 'oversized'])
async def test_document_boundary_rejects_mismatched_page_and_retains_fact_context(corruption):
    flow = environment(count=1)
    public, _, _ = observations.setup([observations.row(userId='u', subjectId='self-u')])
    page = await public.read(scope=DocumentScope('u', 'self-u'))
    changes = {
        'scope': {'scope': DocumentScope('u', 'other')},
        'contract': {'contract_version': 'document.observation-read.v2'},
        'oversized': {'truncated': True},
    }
    reader = SimpleNamespace(read=AsyncMock(return_value=replace(page, **changes[corruption])))
    with pytest.raises((PermissionError, RecordProfileReadError)):
        await RecordDocumentSupplementAdapter(reader).read(user_id='u', subject_id='self-u')
    expected = await create_record_profile_memory(flow.reader).build_context('u')
    actual = await create_record_profile_memory(flow.reader, document_observations=reader).build_context('u')
    assert actual == expected


def test_factory_only_composes_public_ports_without_io():
    reader = SimpleNamespace(read_page=AsyncMock(), list_subjects=AsyncMock())
    documents = SimpleNamespace(read=AsyncMock())
    memory = create_record_profile_memory(reader, document_observations=documents)
    assert isinstance(memory, RecordProfileMemory)
    reader.read_page.assert_not_called()
    reader.list_subjects.assert_not_called()
    documents.read.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_demographic_date_reaches_the_public_record_context():
    flow = environment(count=1)
    fact = flow.collections['facts'].documents[0]
    fact.update(category='demographic', factType='demographic.gender', effectivePeriod={'start': None, 'end': None})
    context = await create_record_profile_memory(flow.reader).build_context('u')
    assert 'demographic.gender' in context
    assert '"effectiveAt":null' in context.replace(' ', '')
