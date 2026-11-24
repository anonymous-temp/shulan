"""Public observation reads preserve the legacy prefix without mutation."""

from copy import deepcopy
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bson.int64 import Int64
from pymongo import ReadPreference

from adapters.analysis.observation_reader import create_document_observation_reader
from health_profile.document_observations import MongoDocumentObservationStore
from modules.analysis.observation_reader import DocumentObservationReader
from modules.analysis.observation_reader import DocumentObservationReadError
from modules.analysis.snapshots import DocumentScope

SCOPE = DocumentScope('user', 'subject')


def row(i=1, **changes):
    return {
        '_id': f'report:{i:04}',
        **SCOPE.as_dict(),
        'documentId': 'document',
        'documentIds': ['document'],
        'canonicalReportId': 'report',
        'ownerRevision': Int64(7),
        'sourcePointer': f'/observations/{i}',
        'observation': {'itemName': 'test', 'resultValue': str(i)},
        'observedAt': None,
        'dateStatus': 'unknown',
        **changes,
    }


class Collection:
    def __init__(self, rows):
        self.rows = rows
        self.options = {}
        self.find_calls = []
        self.sort_calls = []
        self.lengths = []
        self.create_index = AsyncMock()

    def with_options(self, **options):
        self.options = options
        return self

    def find(self, query):
        self.find_calls.append(query)
        rows = [deepcopy(r) for r in self.rows if all(r.get(k) == v for k, v in query.items())]
        owner = self

        class Cursor:
            def sort(self, spec):
                owner.sort_calls.append(spec)
                rows.sort(key=lambda r: r['_id'])
                return self

            async def to_list(self, *, length):
                owner.lengths.append(length)
                return rows[:length]

        return Cursor()


def setup(rows):
    collection = Collection(rows)
    ingestion = SimpleNamespace(read_status=AsyncMock(return_value='pending'))
    reader = create_document_observation_reader(collection=collection, ingestion_reader=ingestion)
    return reader, collection, ingestion


@pytest.mark.asyncio
async def test_matches_legacy_projection_and_order_but_never_creates_indexes():
    rows = [row(2, dateStatus='known', observedAt='2026-01-01'), row(1)]
    legacy_collection = Collection(rows)
    jobs = SimpleNamespace(find_one=AsyncMock(return_value={'status': 'completed'}))
    expected = await MongoDocumentObservationStore(legacy_collection, jobs).list_observations(
        'user', 'subject', limit=100
    )
    reader, collection, ingestion = setup(rows)
    ingestion.read_status.return_value = 'completed'
    actual = await reader.read(scope=SCOPE)
    assert list(actual.items) == expected['items']
    assert actual.scope == SCOPE and not actual.truncated
    assert collection.find_calls == [SCOPE.as_dict()]
    assert collection.sort_calls == [[('_id', 1)]] and collection.lengths == [101]
    assert collection.options['read_preference'] == ReadPreference.PRIMARY
    collection.create_index.assert_not_awaited()
    ingestion.read_status.assert_awaited_once_with(scope=SCOPE, source_id='report', source_revision=7)


@pytest.mark.asyncio
async def test_first_hundred_only_freezes_values_and_marks_more_without_false_complete_claim():
    reader, collection, ingestion = setup([row(i) for i in range(102)])
    first = await reader.read(scope=SCOPE)
    assert len(first.items) == 100 and first.truncated
    before = first.snapshot_id
    first.items[0]['observation']['resultValue'] = 'changed'
    collection.rows[0]['observation']['resultValue'] = 'different live source'
    assert first.items[0]['observation']['resultValue'] == '0' and first.snapshot_id == before
    second = await reader.read(scope=SCOPE)
    assert second.snapshot_id != before
    assert ingestion.read_status.await_count == 2


@pytest.mark.asyncio
async def test_original_row_is_frozen_before_ingestion_await():
    original = row()
    source = SimpleNamespace(read_first=AsyncMock(return_value=[original]))

    async def status(**kwargs):
        original['observation']['resultValue'] = 'mutated'
        return 'pending'

    reader = DocumentObservationReader(source=source, ingestion=SimpleNamespace(read_status=status))
    assert (await reader.read(scope=SCOPE)).items[0]['observation']['resultValue'] == '1'


@pytest.mark.asyncio
@pytest.mark.parametrize('limit', [0, 101, True, 1.5])
async def test_bad_limit_fails_before_storage(limit):
    reader, collection, ingestion = setup([])
    with pytest.raises(ValueError):
        await reader.read(scope=SCOPE, limit=limit)
    assert not collection.find_calls
    ingestion.read_status.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'rows,error',
    [
        ([row(userId='other')], PermissionError),
        ([row(subjectId='other')], PermissionError),
        ([row(2), row(1)], DocumentObservationReadError),
        ([row(), row()], DocumentObservationReadError),
        ([row(ownerRevision=True)], DocumentObservationReadError),
        ([row(dateStatus='invalid')], DocumentObservationReadError),
    ],
)
async def test_provider_cannot_mix_scope_order_or_invalid_source_identity(rows, error):
    source = SimpleNamespace(read_first=AsyncMock(return_value=rows))
    ingestion = SimpleNamespace(read_status=AsyncMock(return_value='pending'))
    with pytest.raises(error):
        await DocumentObservationReader(source=source, ingestion=ingestion).read(scope=SCOPE)


@pytest.mark.asyncio
async def test_status_read_failure_is_not_misreported_as_pending():
    reader, collection, ingestion = setup([row()])
    ingestion.read_status.side_effect = RuntimeError('status unavailable')
    with pytest.raises(RuntimeError):
        await reader.read(scope=SCOPE)
    collection.create_index.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_read_does_not_resolve_subject_or_ingestion_and_has_aware_time():
    reader, collection, ingestion = setup([])
    result = await reader.read(scope=SCOPE)
    assert result.items == () and not result.truncated
    assert result.read_at.utcoffset() is not None
    ingestion.read_status.assert_not_awaited()
    reader.clock = lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    with pytest.raises(ValueError):
        await reader.read(scope=SCOPE)


@pytest.mark.asyncio
async def test_legacy_supplement_serialization_keeps_observation_field_order():
    import json

    rows = [row(observation={'z_raw': 'first field', 'itemName': 'test', 'resultValue': '1', 'a_raw': 'last field'})]
    legacy_collection = Collection(rows)
    legacy = await MongoDocumentObservationStore(legacy_collection).list_observations('user', 'subject', limit=100)
    reader, _, _ = setup(rows)
    actual = await reader.read(scope=SCOPE)

    def render(item):
        return json.dumps(
            {
                'source': 'uploaded_document',
                'documentId': item['documentId'],
                'dateStatus': item['dateStatus'],
                'observedAt': item['observedAt'],
                'observation': item['observation'],
            },
            ensure_ascii=False,
        )

    assert render(actual.items[0]) == render(legacy['items'][0])
    assert list(actual.items[0]['observation']) == ['z_raw', 'itemName', 'resultValue', 'a_raw']
