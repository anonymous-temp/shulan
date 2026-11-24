"""No old completed row can revive a managed source; v1 and v2 stay distinct."""

from copy import deepcopy
from dataclasses import asdict
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.analysis.observation_reader import MongoDocumentObservationSource
from adapters.analysis.publication import MongoDocumentPublicationLedger
from modules.analysis.observation_lifecycle import DocumentLifecycleLegacyIngestionGuard
from modules.analysis.observation_lifecycle import DocumentLifecycleObservationReader
from modules.analysis.observation_lifecycle import DocumentObservationCursorV2
from modules.analysis.observation_reader import DocumentObservationReader
from modules.analysis.observation_reader import DocumentObservationReadError
from modules.analysis.publication import PublicationReceipt
from modules.analysis.publication_lifecycle import DocumentPublicationLifecycleReader
from modules.analysis.publication_lifecycle import DocumentSourceLifecycleV1
from modules.analysis.snapshots import DocumentScope
from tests.unit.services.analysis.fakes import MemoryMongoCollection
from tests.unit.services.analysis.test_document_observation_reader import SCOPE
from tests.unit.services.analysis.test_document_observation_reader import row


async def setup(rows, modes=None):
    collection = MemoryMongoCollection()
    for item in rows:
        await collection.insert_one(item)
    ledger = MongoDocumentPublicationLedger(sources=MemoryMongoCollection(), intents=MemoryMongoCollection())
    for source_id, mode in (modes or {}).items():
        current = await ledger._source(SCOPE, source_id)
        frozen = {
            'eventKind': 'publish',
            'sourceRevision': 1,
            'contentDigest': 'd' * 64,
            'snapshotRef': {'snapshotId': 'fixed_' + source_id, 'contractVersion': 'document.facts.v1'},
        }
        intent = {
            '_id': 'intent_' + source_id,
            **SCOPE.as_dict(),
            'event': frozen,
            'receipt': asdict(PublicationReceipt(source_id, 1, 'd' * 64, 'applied', 'receipt'))
            if mode == 'active'
            else None,
        }
        await ledger.intents.insert_one(intent)
        await ledger._cas(
            current,
            {
                'mode': 'events',
                'publicationRevision': 1,
                'appliedRevision': int(mode == 'active'),
                'settledRevision': int(mode == 'active'),
                'acceptedRevision': 1,
                'reservedRetractionRevision': int(mode == 'retracting'),
                'currentIntent': intent,
            },
        )
    lifecycle = DocumentPublicationLifecycleReader(ledger=ledger, snapshots=object())
    ingestion = SimpleNamespace(read_status=AsyncMock(return_value='completed'))
    source = MongoDocumentObservationSource(collection)
    return SimpleNamespace(
        collection=collection,
        ledger=ledger,
        lifecycle=lifecycle,
        ingestion=ingestion,
        source=source,
        reader=DocumentLifecycleObservationReader(source=source, lifecycle=lifecycle, ingestion=ingestion),
    )


@pytest.mark.asyncio
async def test_v1_guard_rejects_managed_source_without_turning_completed_into_pending():
    env = await setup([row()], {'report': 'active'})
    reader = DocumentObservationReader(
        source=env.source,
        ingestion=DocumentLifecycleLegacyIngestionGuard(lifecycle=env.lifecycle, ingestion=env.ingestion),
    )
    with pytest.raises(DocumentObservationReadError, match='v2'):
        await reader.read(scope=SCOPE)
    env.ingestion.read_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_v1_legacy_prefix_semantics_are_unchanged():
    env = await setup([row(i) for i in range(101)])
    reader = DocumentObservationReader(
        source=env.source,
        ingestion=DocumentLifecycleLegacyIngestionGuard(lifecycle=env.lifecycle, ingestion=env.ingestion),
    )
    result = await reader.read(scope=SCOPE)
    assert result.contract_version == 'document.observation-read.v1'
    assert len(result.items) == 100 and result.truncated


@pytest.mark.asyncio
async def test_v2_scans_past_filtered_prefix_and_returns_fixed_active_refs_separately():
    env = await setup(
        [row(i, canonicalReportId='managed') for i in range(5)] + [row(6, canonicalReportId='legacy')],
        {'managed': 'active'},
    )
    before = deepcopy(env.collection.documents)
    page = await env.reader.read(scope=SCOPE, limit=2, scan_budget=10)
    assert page.contract_version == 'document.observation-read.v2'
    assert [item['canonicalReportId'] for item in page.legacy_items] == ['legacy']
    assert len(page.managed_sources) == 1 and page.managed_sources[0]['active_snapshot_ref'] == 'fixed_managed'
    assert page.hidden == 5 and page.scanned == 7 and not page.truncated
    assert env.collection.documents == before
    env.ingestion.read_status.assert_awaited_once()


@pytest.mark.asyncio
async def test_scan_budget_exhaustion_is_explicit_even_with_no_results_and_continuation_progresses():
    env = await setup([row(i, canonicalReportId='managed') for i in range(5)], {'managed': 'retracting'})
    first = await env.reader.read(scope=SCOPE, limit=1, scan_budget=2)
    assert first.truncated and first.scan_budget_exhausted and first.scanned == first.hidden == 2
    assert first.legacy_items == first.managed_sources == ()
    second = await env.reader.read(scope=SCOPE, limit=1, scan_budget=2, cursor=first.continuation)
    third = await env.reader.read(scope=SCOPE, limit=1, scan_budget=2, cursor=second.continuation)
    assert not third.truncated and third.legacy_items == third.managed_sources == ()
    env.ingestion.read_status.assert_not_awaited()


@pytest.mark.asyncio
async def test_sources_with_no_legacy_rows_are_discoverable_and_scoped():
    env = await setup([], {'new': 'active'})
    page = await env.reader.read(scope=SCOPE)
    assert len(page.managed_sources) == 1 and not page.truncated
    with pytest.raises(PermissionError):
        await env.reader.read(scope=DocumentScope('other', 'subject'), cursor=DocumentObservationCursorV2(SCOPE))


@pytest.mark.asyncio
async def test_lifecycle_race_is_restart_required_instead_of_mixed_visibility():
    env = await setup([row(1), row(2)])
    original = env.lifecycle.read
    count = 0

    async def changing(**kwargs):
        nonlocal count
        count += 1
        if count >= 4:
            return DocumentSourceLifecycleV1(SCOPE, 'report', 'events', 'pending')
        return await original(**kwargs)

    env.lifecycle.read = changing
    with pytest.raises(DocumentObservationReadError, match='changed'):
        await env.reader.read(scope=SCOPE, limit=2)


@pytest.mark.asyncio
async def test_v2_result_is_detached_and_does_not_offer_old_rows_as_new_publication_facts():
    env = await setup([row()], {'report': 'active'})
    page = await env.reader.read(scope=SCOPE)
    assert not page.legacy_items
    snapshot = page.snapshot_id
    page.managed_sources[0]['active_snapshot_ref'] = 'changed'
    assert page.snapshot_id == snapshot and page.managed_sources[0]['active_snapshot_ref'] == 'fixed_report'


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [{'limit': True}, {'scan_budget': 0}, {'scan_budget': 1001}])
async def test_v2_invalid_budget_is_rejected_before_read(changes):
    env = await setup([])
    env.source.read_after = AsyncMock(side_effect=AssertionError('no IO'))
    with pytest.raises(ValueError):
        await env.reader.read(scope=SCOPE, **changes)
    env.source.read_after.assert_not_awaited()


@pytest.mark.asyncio
async def test_v2_rejects_scope_escaped_rows_and_unknown_phase():
    env = await setup([])
    env.source.read_after = AsyncMock(return_value=[row(userId='other')])
    with pytest.raises(PermissionError):
        await env.reader.read(scope=SCOPE)
    with pytest.raises(ValueError):
        await env.reader.read(scope=SCOPE, cursor=replace(DocumentObservationCursorV2(SCOPE), phase='unknown'))
