"""Public paging stays pure, scope-bound and explicit about interrupted snapshots."""

from copy import deepcopy
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bson import BSON

from health_profile.contracts import ProfilePublicationWatermarkV1
from health_profile.contracts import ProfileScope
from health_profile.models import HealthFact
from health_profile.models import HealthProfileView
from health_profile.models import HealthSubject
from health_profile.pagination import decode_cursor
from health_profile.pagination import encode_cursor
from health_profile.repository import MongoHealthProfileRepository
from modules.health_profile import ProfileSnapshotReader
from modules.health_profile.contracts import ProfileFactPageV1

from .test_profile_read_and_subjects import FakeCollection

NOW = datetime(2026, 11, 10, tzinfo=UTC)


def environment(*, count=3, collection_type=FakeCollection):
    collections = {name: collection_type() for name in ('subjects', 'facts', 'events', 'views', 'jobs')}
    repository = MongoHealthProfileRepository(**collections)
    subject = HealthSubject(subject_id='self-u', user_id='u', kind='self', created_at=NOW, updated_at=NOW)
    view = HealthProfileView(user_id='u', subject_id='self-u', updated_at=NOW)
    collections['subjects'].documents.append(subject.to_document(document_id='self-u'))
    collections['views'].documents.append(view.to_document(document_id='view-u'))
    for index in reversed(range(count)):
        fact = HealthFact(
            fact_id=f'fact-{index:04}',
            user_id='u',
            subject_id='self-u',
            fact_type='anthropometric.weight',
            category='anthropometric',
            display_name='weight',
            value_type='quantity',
            value={'number': 60 + index / 100, 'unit': 'kg'},
            assertion_level='user_confirmed',
            verification_status='confirmed',
            confidence=1,
            effective_period={'start': NOW - timedelta(days=index // 3)},
            status='active',
            sources=({'source_type': 'chat', 'source_id': 'message-1', 'source_revision': 1},),
            created_at=NOW,
            updated_at=NOW,
        )
        collections['facts'].documents.append(fact.to_document(document_id=fact.fact_id))
    return SimpleNamespace(
        collections=collections,
        repository=repository,
        reader=ProfileSnapshotReader(repository, clock=lambda: NOW),
        scope=ProfileScope(user_id='u', subject_id='self-u'),
    )


def stored(flow):
    return deepcopy({name: (item.documents, item.indexes) for name, item in flow.collections.items()})


@pytest.mark.asyncio
async def test_more_than_one_thousand_facts_are_complete_stable_and_pure():
    flow = environment(count=1207)
    before = stored(flow)
    cursor, seen, pages = None, [], []
    while True:
        page = await flow.reader.read_page('u', page_size=1000, cursor=cursor)
        assert isinstance(page, ProfileFactPageV1)
        assert page.status == 'ready' and page.profile_revision == 1 and not page.stale
        seen.extend(fact.fact_id for fact in page.facts)
        pages.append(page)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert [len(page.facts) for page in pages] == [1000, 207]
    assert seen == [f'fact-{index:04}' for index in range(1207)]
    assert pages[0].truncated and not pages[1].truncated
    assert stored(flow) == before
    assert pages[0].facts[0].created_at == NOW
    assert pages[0].facts[0].model_dump(mode='json', by_alias=True)['createdAt'].endswith('Z')
    assert 'creationRequestId' not in pages[0].model_dump_json()


@pytest.mark.asyncio
async def test_filters_keep_structured_values_and_equivalent_query_has_same_continuation():
    flow = environment(count=12)
    # A category and status excluded by this request must not affect page fullness.
    flow.collections['facts'].documents[0]['status'] = 'deleted'
    flow.collections['facts'].documents[1]['category'] = 'laboratory'
    kwargs = {
        'page_size': 2,
        'statuses': ('pending', 'active', 'active'),
        'categories': ('anthropometric',),
        'fact_types': ('anthropometric.weight',),
        'effective_from': NOW - timedelta(days=2),
        'effective_to': NOW,
    }
    page = await flow.reader.read_page('u', **kwargs)
    kwargs['statuses'] = ('active', 'pending')
    resumed = await flow.reader.read_page('u', cursor=page.next_cursor, **kwargs)
    assert [row.fact_id for row in page.facts + resumed.facts] == [f'fact-{index:04}' for index in range(4)]
    assert resumed.facts[-1].value == {'number': 60.03, 'unit': 'kg'}
    assert resumed.coverage['statuses'] == ['active', 'pending']
    assert resumed.coverage['effectiveFrom'] == (NOW - timedelta(days=2)).isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'change',
    [
        {'user_id': 'other'},
        {'subject_id': 'relative'},
        {'page_size': 2},
        {'statuses': ('pending',)},
        {'categories': ('laboratory',)},
        {'fact_types': ('condition.diabetes',)},
        {'effective_from': NOW},
        {'effective_to': NOW},
    ],
)
async def test_cursor_query_and_scope_mismatch_is_rejected_before_repository_io(change):
    flow = environment()
    page = await flow.reader.read_page('u', page_size=1)
    flow.repository.get_self_subject = AsyncMock(side_effect=AssertionError('must not read'))
    flow.repository.get_subject = AsyncMock(side_effect=AssertionError('must not read'))
    args = {'user_id': 'u', 'page_size': 1, 'cursor': page.next_cursor, **change}
    with pytest.raises(ValueError, match='cursor'):
        await flow.reader.read_page(**args)
    flow.repository.get_self_subject.assert_not_awaited()
    flow.repository.get_subject.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'field,value',
    [
        ('v', True),
        ('kind', 'health-profile-facts'),
        ('contractVersion', 'profile-fact-page.v2'),
        ('sortVersion', 'unknown'),
        ('stateKey', 'bad'),
        ('start', '2026-11-10'),
        ('factId', ''),
        ('scope', {'userId': 'u'}),
    ],
)
async def test_malformed_or_unknown_version_cursor_is_rejected_without_io(field, value):
    flow = environment()
    page = await flow.reader.read_page('u', page_size=1)
    payload = decode_cursor('profile-public-fact-page', page.next_cursor)
    payload[field] = value
    changed = encode_cursor('profile-public-fact-page', payload)
    flow.repository.get_self_subject = AsyncMock()
    with pytest.raises(ValueError):
        await flow.reader.read_page('u', page_size=1, cursor=changed)
    flow.repository.get_self_subject.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'options',
    [
        {'page_size': True},
        {'page_size': 1001},
        {'page_size': 0},
        {'statuses': ()},
        {'statuses': ('deleted',)},
        {'statuses': 'active'},
        {'categories': ('unknown',)},
        {'fact_types': ('',)},
        {'effective_from': NOW.replace(tzinfo=None)},
        {'effective_from': NOW, 'effective_to': NOW - timedelta(days=1)},
        {'cursor': 'not-a-cursor'},
        {'cursor': 'x' * 16385},
    ],
)
async def test_invalid_input_never_bootstraps_or_queries(options):
    flow = environment()
    flow.repository.get_self_subject = AsyncMock()
    with pytest.raises(ValueError):
        await flow.reader.read_page('u', **options)
    flow.repository.get_self_subject.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['profile', 'subject', 'watermark'])
async def test_between_page_changes_require_restart_without_returning_mixed_facts(changed):
    flow = environment()
    watermarks = [ProfilePublicationWatermarkV1(scope=flow.scope, source_type='chat', source_id='message-1')]
    flow.reader._publications = SimpleNamespace(list_watermarks=AsyncMock(side_effect=lambda scope: tuple(watermarks)))
    first = await flow.reader.read_page('u', page_size=1)
    if changed == 'profile':
        flow.collections['views'].documents[0]['profileVersion'] += 1
    elif changed == 'subject':
        flow.collections['subjects'].documents[0]['version'] += 1
        flow.collections['subjects'].documents[0]['displayName'] = 'changed'
    else:
        watermarks[0] = watermarks[0].model_copy(update={'accepted_publication_revision': 1})
    flow.repository.list_fact_page = AsyncMock(side_effect=AssertionError('must not read a mixed revision'))
    resumed = await flow.reader.read_page('u', page_size=1, cursor=first.next_cursor)
    assert resumed.status == 'restart_required' and resumed.stale
    assert resumed.error_code == 'profile_page_revision_changed'
    assert not resumed.facts and resumed.next_cursor is None
    flow.repository.list_fact_page.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['profile', 'subject', 'archive', 'watermark'])
async def test_changes_during_one_page_discard_the_page_and_do_not_retry_internally(changed):
    flow = environment()
    watermarks = [ProfilePublicationWatermarkV1(scope=flow.scope, source_type='chat', source_id='message-1')]
    flow.reader._publications = SimpleNamespace(list_watermarks=AsyncMock(side_effect=lambda scope: tuple(watermarks)))
    original = flow.repository.list_fact_page

    async def concurrent(*args, **kwargs):
        facts = await original(*args, **kwargs)
        if changed == 'profile':
            flow.collections['views'].documents[0]['profileVersion'] += 1
        elif changed == 'subject':
            flow.collections['subjects'].documents[0]['version'] += 1
        elif changed == 'archive':
            flow.collections['subjects'].documents[0]['status'] = 'archived'
        else:
            watermarks[0] = watermarks[0].model_copy(update={'applying_publication_revision': 1})
        return facts

    flow.repository.list_fact_page = AsyncMock(side_effect=concurrent)
    page = await flow.reader.read_page('u', page_size=1)
    assert page.status == 'restart_required' and not page.facts and page.next_cursor is None
    assert flow.repository.list_fact_page.await_count == 1


@pytest.mark.asyncio
async def test_missing_subject_and_missing_view_remain_pure_and_distinct():
    flow = environment(count=0)
    before = stored(flow)
    missing = await flow.reader.read_page('other')
    foreign = await flow.reader.read_page('other', subject_id='self-u')
    assert missing.status == foreign.status == 'missing'
    empty = await flow.reader.read_page('u')
    assert empty.status == 'ready' and not empty.facts and not empty.truncated
    assert stored(flow) == before
    flow.collections['views'].documents.clear()
    before = stored(flow)
    unavailable = await flow.reader.read_page('u')
    assert unavailable.status == 'unavailable' and unavailable.error_code == 'profile_revision_unavailable'
    assert stored(flow) == before


@pytest.mark.asyncio
async def test_publication_state_is_explicit_and_database_order_does_not_change_cursor():
    flow = environment()
    rows = [
        ProfilePublicationWatermarkV1(
            scope=flow.scope,
            source_type='chat',
            source_id='z',
            accepted_publication_revision=2,
            applied_publication_revision=1,
            applying_publication_revision=2,
        ),
        ProfilePublicationWatermarkV1(scope=flow.scope, source_type='record', source_id='a'),
    ]
    flow.reader._publications = SimpleNamespace(list_watermarks=AsyncMock(side_effect=lambda scope: tuple(rows)))
    first = await flow.reader.read_page('u', page_size=1)
    rows.reverse()
    second = await flow.reader.read_page('u', page_size=1, cursor=first.next_cursor)
    assert second.status == 'ready' and second.stale
    assert second.coverage['publicationApplying'] and second.coverage['publicationPending']
    assert second.coverage['publicationStateAvailable']
    assert [row.fact_id for row in second.facts] == ['fact-0001']


@pytest.mark.asyncio
async def test_storage_failure_and_foreign_repository_result_leak_no_values():
    flow = environment()
    flow.repository.list_fact_page = AsyncMock(side_effect=RuntimeError('sensitive value and URI'))
    failure = await flow.reader.read_page('u')
    assert failure.status == 'unavailable' and not failure.facts and failure.next_cursor is None
    assert 'sensitive' not in failure.model_dump_json()
    foreign = environment()
    row = deepcopy(foreign.collections['facts'].documents[0])
    row.pop('_id')
    row['userId'] = 'other'
    foreign.repository.list_fact_page = AsyncMock(return_value=(HealthFact.model_validate(row),))
    failure = await foreign.reader.read_page('u')
    assert failure.status == 'unavailable' and not failure.facts


@pytest.mark.asyncio
async def test_bson_naive_utc_seek_keys_and_public_times_roundtrip_without_skipping_ties():
    class BsonCollection(FakeCollection):
        @staticmethod
        def bson(value):
            return BSON.encode({'value': value}).decode()['value']

        async def find_one(self, filters):
            return self.bson(await super().find_one(self.bson(filters)))

        def find(self, filters):
            return super().find(self.bson(filters))

    flow = environment(count=7, collection_type=BsonCollection)
    for collection in flow.collections.values():
        collection.documents = collection.bson(collection.documents)
    cursor, ids = None, []
    for _ in range(4):
        page = await flow.reader.read_page('u', page_size=2, cursor=cursor)
        assert page.status == 'ready'
        assert all(row.effective_period['start'].tzinfo is UTC for row in page.facts)
        assert all(row.created_at.tzinfo is UTC for row in page.facts)
        ids.extend(row.fact_id for row in page.facts)
        cursor = page.next_cursor
    assert cursor is None and ids == [f'fact-{index:04}' for index in range(7)]


@pytest.mark.asyncio
async def test_cross_subject_source_watermark_is_only_visible_for_its_actual_targets():
    flow = environment()
    anchor = ProfileScope(user_id='u', subject_id='source-anchor')
    row = ProfilePublicationWatermarkV1(
        scope=anchor,
        source_type='chat',
        source_id='message-1',
        affected_subject_ids=('self-u',),
    )
    provider = SimpleNamespace(list_watermarks=AsyncMock(return_value=(row,)))
    flow.reader._publications = provider
    assert (await flow.reader.read_page('u')).source_watermarks == (row,)
    for outside in (
        row.model_copy(update={'scope': ProfileScope(user_id='other', subject_id='source-anchor')}),
        row.model_copy(update={'affected_subject_ids': ()}),
    ):
        provider.list_watermarks.return_value = (outside,)
        page = await flow.reader.read_page('u')
        assert page.status == 'unavailable' and not page.source_watermarks and not page.facts


@pytest.mark.asyncio
async def test_same_source_names_with_different_anchors_have_database_order_independent_cursor():
    flow = environment()
    rows = [
        ProfilePublicationWatermarkV1(
            scope=ProfileScope(user_id='u', subject_id=anchor),
            source_type='health_report',
            source_id='same-source',
            affected_subject_ids=('self-u',),
            accepted_publication_revision=revision,
            applied_publication_revision=revision,
            settled_publication_revision=revision,
        )
        for anchor, revision in (('anchor-a', 1), ('anchor-b', 2))
    ]

    async def shuffled(scope):
        assert scope == flow.scope
        rows.reverse()
        return tuple(rows)

    flow.reader._publications = SimpleNamespace(list_watermarks=AsyncMock(side_effect=shuffled))
    first = await flow.reader.read_page('u', page_size=1)
    assert first.status == 'ready' and first.next_cursor
    second = await flow.reader.read_page('u', page_size=1, cursor=first.next_cursor)
    assert second.status == 'ready'
    assert first.source_watermarks == second.source_watermarks
    assert [row.scope.subject_id for row in second.source_watermarks] == ['anchor-a', 'anchor-b']
    assert [row.fact_id for row in first.facts + second.facts] == ['fact-0000', 'fact-0001']


@pytest.mark.asyncio
async def test_pending_coverage_uses_settled_watermark_not_highest_fully_applied_revision():
    flow = environment()
    watermark = ProfilePublicationWatermarkV1(
        scope=flow.scope,
        source_type='chat',
        source_id='message-1',
        accepted_publication_revision=3,
        applied_publication_revision=1,
        settled_publication_revision=3,
    )
    flow.reader._publications = SimpleNamespace(list_watermarks=AsyncMock(return_value=(watermark,)))
    page = await flow.reader.read_page('u')
    assert page.status == 'ready' and not page.stale and not page.coverage['publicationPending']
    assert page.source_watermarks[0].applied_publication_revision == 1
