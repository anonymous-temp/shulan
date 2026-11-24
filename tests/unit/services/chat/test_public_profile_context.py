"""Chat's real public provider composition preserves legacy model context text."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bson import BSON

from adapters.analysis.observation_reader import MongoDocumentObservationSource
from adapters.chat.profile_context import PublicChatProfilePages
from adapters.chat.profile_context import PublicChatSupplements
from adapters.chat.profile_context import create_chat_profile_context
from health_profile.document_observations import MongoDocumentObservationStore
from health_profile.memory import RepositoryHealthProfileChatMemoryProvider
from health_profile.models import HealthProfileView
from health_profile.models import HealthSubject
from modules.analysis.observation_reader import DocumentObservationReader
from modules.analysis.snapshots import DocumentScope
from modules.chat.profile_memory import ChatProfileReadError
from modules.health_profile.contracts import ProfilePublicationWatermarkV1
from modules.health_profile.contracts import ProfileScope
from tests.unit.health_profile.test_fact_pages import NOW
from tests.unit.health_profile.test_fact_pages import environment
from tests.unit.health_profile.test_fact_pages import stored
from tests.unit.health_profile.test_profile_read_and_subjects import FakeCollection


def observations(flow):
    collection = FakeCollection()
    for index in range(105):
        collection.documents.append(
            {
                '_id': f'row-{index:03}',
                'userId': 'u',
                'subjectId': 'self-u',
                'documentId': 'doc',
                'documentIds': ['doc'],
                'canonicalReportId': 'canonical',
                'ownerRevision': 123456789123,
                'sourcePointer': f'/observations/{index}',
                'dateStatus': 'unknown',
                'observedAt': None,
                'observation': {
                    'itemName': f'item-{index}',
                    'resultValue': str(index),
                    'rawExtra': {'method': 'original'},
                },
            }
        )
    document = DocumentObservationReader(
        source=MongoDocumentObservationSource(collection),
        ingestion=SimpleNamespace(read_status=AsyncMock(return_value='pending')),
    )
    return collection, document


def add_family(flow):
    subject = HealthSubject(
        user_id='u',
        subject_id='father',
        kind='blood_relative',
        display_name='father',
        relationship={'relation_type': 'father', 'is_blood_relative': True},
        created_at=NOW,
        updated_at=NOW,
    )
    flow.collections['subjects'].documents.append(subject.to_document(document_id='father'))
    view = HealthProfileView(user_id='u', subject_id='father', updated_at=NOW)
    flow.collections['views'].documents.append(view.to_document(document_id='view-father'))
    fact = deepcopy(flow.collections['facts'].documents[0])
    fact.update(
        _id='family-fact',
        factId='family-fact',
        subjectId='father',
        factType='family_history.condition',
        category='family_history',
    )
    flow.collections['facts'].documents.append(fact)


@pytest.mark.asyncio
async def test_real_profile_and_document_reads_match_original_full_text_without_writes():
    flow = environment(count=1101)
    add_family(flow)
    # Preserve Python values until Chat's original safe renderer chooses depth.
    flow.collections['facts'].documents[-2]['value'] = {'outer': {'middle': {'deep': {'date': NOW}}}}
    collection, document = observations(flow)
    legacy = RepositoryHealthProfileChatMemoryProvider(
        flow.repository,
        document_observation_store=MongoDocumentObservationStore(collection, flow.collections['jobs']),
    )
    expected = await legacy.build_context('u')
    before = stored(flow), deepcopy(collection.documents), deepcopy(collection.indexes)
    provider = create_chat_profile_context(profile_reader=flow.reader, observation_reader=document)
    assert (stored(flow), collection.documents, collection.indexes) == before
    actual = await provider.build_context('u')
    assert actual == expected and 'family-fact' in actual and 'uploaded_document' in actual
    assert len(actual) <= 6000
    assert (stored(flow), collection.documents, collection.indexes) == before
    document.ingestion.read_status.assert_awaited_once_with(
        scope=DocumentScope('u', 'self-u'),
        source_id='canonical',
        source_revision=123456789123,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['user', 'subject', 'version', 'sort'])
async def test_real_adapter_rejects_wrong_scope_and_unknown_page_contract(mutation):
    flow = environment()
    page = await flow.reader.read_page('u')
    changes = {
        'user': {'user_id': 'other'},
        'subject': {'scope': ProfileScope(user_id='u', subject_id='other')},
        'version': {'contract_version': 'profile-fact-page.v2'},
        'sort': {'sort_version': 'future-sort'},
    }
    flow.reader.read_page = AsyncMock(return_value=page.model_copy(update=changes[mutation]))
    with pytest.raises((PermissionError, ChatProfileReadError)):
        await PublicChatProfilePages(flow.reader).read_page('u', subject_id='self-u')


@pytest.mark.asyncio
async def test_public_revision_change_restarts_once_without_partial_prefix():
    flow = environment(count=5)
    original = flow.repository.list_fact_page
    calls = 0

    async def changed(*args, **kwargs):
        nonlocal calls
        result = await original(*args, **kwargs)
        calls += 1
        if calls == 2:
            flow.collections['views'].documents[0]['profileVersion'] += 1
        return result

    flow.repository.list_fact_page = changed
    actual = await create_chat_profile_context(profile_reader=flow.reader, page_size=2).build_context('u')
    assert actual == await RepositoryHealthProfileChatMemoryProvider(flow.repository).build_context('u')
    assert calls == 5


@pytest.mark.asyncio
async def test_view_token_normalizes_complete_source_identity_and_includes_store_availability():
    flow = environment()
    rows = tuple(
        ProfilePublicationWatermarkV1(
            scope=ProfileScope(user_id='u', subject_id=anchor),
            source_type='health_report',
            source_id='same-source',
            affected_subject_ids=('self-u',),
        )
        for anchor in ('anchor-a', 'anchor-b')
    )
    page = await flow.reader.read_page('u')
    first = page.model_copy(update={'source_watermarks': rows})
    reverse = page.model_copy(update={'source_watermarks': tuple(reversed(rows))})
    available = reverse.model_copy(update={'coverage': {**reverse.coverage, 'publicationStateAvailable': True}})
    flow.reader.read_page = AsyncMock(side_effect=(first, reverse, available))
    adapter = PublicChatProfilePages(flow.reader)
    a, b, c = await adapter.read_page('u'), await adapter.read_page('u'), await adapter.read_page('u')
    assert a.view_token == b.view_token and b.view_token != c.view_token


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['scope', 'version', 'oversize', 'short_truncated'])
async def test_document_public_adapter_checks_scope_fixed_contract_and_first_page_bound(mutation):
    flow = environment()
    _, document = observations(flow)
    page = await document.read(scope=DocumentScope('u', 'self-u'), limit=100)
    if mutation == 'scope':
        page = replace(page, scope=DocumentScope('other', 'self-u'))
    if mutation == 'version':
        page = replace(page, contract_version='document.observation-read.v2')
    if mutation == 'short_truncated':
        page = replace(page, _items_json='[]', truncated=True)
    document.read = AsyncMock(return_value=page)
    with pytest.raises((PermissionError, ChatProfileReadError)):
        await PublicChatSupplements(document).read(
            user_id='u', subject_id='self-u', limit=99 if mutation == 'oversize' else 100
        )


@pytest.mark.asyncio
async def test_profile_unavailable_is_not_empty_but_optional_document_failure_keeps_facts():
    flow = environment()
    broken = SimpleNamespace(read=AsyncMock(side_effect=RuntimeError('private provider details')))
    assert await create_chat_profile_context(profile_reader=flow.reader, observation_reader=broken).build_context(
        'u'
    ) == (await RepositoryHealthProfileChatMemoryProvider(flow.repository).build_context('u'))
    flow.collections['views'].documents.clear()
    with pytest.raises(ChatProfileReadError, match='unavailable'):
        await create_chat_profile_context(profile_reader=flow.reader).build_context('u')


@pytest.mark.asyncio
async def test_bson_utc_pages_preserve_full_traversal_and_timestamp_meaning():
    class BsonCollection(FakeCollection):
        @staticmethod
        def bson(value):
            return BSON.encode({'value': value}).decode()['value']

        async def find_one(self, filters):
            return self.bson(await super().find_one(self.bson(filters)))

        def find(self, filters):
            return super().find(self.bson(filters))

    flow = environment(count=7, collection_type=BsonCollection)
    for index, fact in enumerate(flow.collections['facts'].documents):
        fact['factType'] = f'symptom.item-{index}'
        fact['category'] = 'symptom'
    for collection in flow.collections.values():
        collection.documents = collection.bson(collection.documents)
    context = await create_chat_profile_context(profile_reader=flow.reader, page_size=2).build_context('u')
    assert all(f'fact-{i:04}' in context for i in range(7))
    assert '2026-11-10T00:00:00+00:00' in context


@pytest.mark.asyncio
async def test_unknown_demographic_date_reaches_the_public_chat_context():
    flow = environment(count=1)
    fact = flow.collections['facts'].documents[0]
    fact.update(category='demographic', factType='demographic.gender', effectivePeriod={'start': None, 'end': None})
    context = await create_chat_profile_context(profile_reader=flow.reader).build_context('u')
    assert 'demographic.gender' in context
    assert '"effectiveAt":null' in context.replace(' ', '')
