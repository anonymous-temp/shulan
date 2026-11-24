"""Managed Document replay under concurrent receipt updates and fixed-history reads."""

import asyncio
from collections import Counter
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from modules.analysis.complete_source_contracts import DocumentCompleteSourceUnavailable
from modules.analysis.publication import PublicationConflictError
from tests.unit._support import current_document as source_fixtures
from tests.unit._support.current_document import capture
from tests.unit._support.current_document_profile import composition
from tests.unit._support.current_document_profile import info
from tests.unit._support.current_document_profile import managed
from tests.unit._support.current_document_profile import publish
from tests.unit._support.current_document_profile import settle
from tests.unit.services.analysis.test_document_basic_info_runtime import command
from tests.unit.services.analysis.test_document_basic_info_runtime import published
from tests.unit.services.analysis.test_document_basic_info_runtime import runtime
from tests.unit.services.analysis.test_document_snapshots import SCOPE


@pytest.mark.asyncio
async def test_concurrent_managed_result_reuses_first_persisted_acknowledgement(monkeypatch):
    flow = await composition(monkeypatch)
    await publish(flow)
    source = await flow.runtime.source.read_source_observations(
        scope=flow.scope, snapshot_id=flow.capture.source_snapshot_ref
    )
    basic_ids = [row['contributionId'] for row in source['payload']['contributions'] if row['kind'] == 'basic_info']
    request = managed(flow, retire=(basic_ids[0],))
    store = flow.runtime.managed.records
    put = store.put
    first_result_ready, release_first_result = asyncio.Event(), asyncio.Event()
    states = []

    async def pause_first_result(kind, scope, identity, value):
        if kind == 'basic_info_managed_result':
            states.append(value['publication']['state'])
            if len(states) == 1:
                first_result_ready.set()
                await release_first_result.wait()
        await put(kind, scope, identity, value)

    monkeypatch.setattr(store, 'put', pause_first_result)
    first = asyncio.create_task(flow.runtime.managed.execute(request))
    try:
        async with asyncio.timeout(10):
            await first_result_ready.wait()
            await settle(flow)
            winner = await flow.runtime.managed.execute(request)
    finally:
        release_first_result.set()
        previous = await first

    assert states == ['received', 'applied']
    assert previous == winner == await flow.runtime.managed.execute(request)
    assert winner['publicationRevision'] == 2 and winner['publication']['state'] == 'applied'
    ledger = await flow.ledger.read_source(flow.scope, request.source_id)
    assert ledger['publicationRevision'] == ledger['appliedRevision'] == 2
    assert len(flow.ledger.intents.documents) == 2
    with pytest.raises(PublicationConflictError, match='changed'):
        await flow.runtime.managed.execute(replace(request, retire_contribution_ids=(basic_ids[1],)))

    saved = next(row for row in flow.records.documents.values() if row['kind'] == 'basic_info_managed_result')
    saved['payload']['publication']['receipt_ref'] = 'another-publication'
    with pytest.raises(DocumentCompleteSourceUnavailable, match='publication'):
        await flow.runtime.managed.read_receipt(scope=flow.scope, command_id=request.command_id)


@pytest.mark.asyncio
async def test_managed_retry_checks_for_same_durable_command_after_concurrent_prior_advance(monkeypatch):
    flow = await runtime()
    _, captured, _, source = await published(flow)
    request = command(flow, captured, retired=(source['payload']['contributions'][0]['contributionId'],))
    store = flow.api.managed.records
    get = store.get
    first_missed_transition, release_first = asyncio.Event(), asyncio.Event()

    async def pause_first_missing_transition(kind, scope, identity):
        value = await get(kind, scope, identity)
        if kind == 'basic_info_managed_transition' and value is None and not first_missed_transition.is_set():
            first_missed_transition.set()
            await release_first.wait()
        return value

    monkeypatch.setattr(store, 'get', pause_first_missing_transition)
    first = asyncio.create_task(flow.api.managed.execute(request))
    try:
        async with asyncio.timeout(10):
            await first_missed_transition.wait()
            winner = await flow.api.managed.execute(request)
    finally:
        release_first.set()
        previous = await first

    assert previous == winner
    assert winner['publicationRevision'] == 2
    assert len(flow.ledger.intents.documents) == 2


@pytest.mark.asyncio
async def test_managed_history_reads_each_fixed_artifact_once_per_read_and_rejects_later_corruption(monkeypatch):
    flow = await runtime()
    _, captured, initial_plan, _ = await published(flow)
    latest = captured
    for revision in range(1, 6):
        flow.env.raw['processing']['status'] = 'partial'
        flow.env.raw['basicInfo']['age'] = 42 + revision
        flow.env.document['interpretationResultRound'] = revision + 1
        newer = await capture(flow.env)
        request = command(
            flow,
            latest,
            understanding=newer.understanding.snapshot_id,
            request_id=f'correction-{revision}',
            prior=revision,
        )
        result = await flow.api.managed.execute(request)
        assert result['publicationRevision'] == revision + 1
        latest = SimpleNamespace(source_snapshot_ref=result['sourceSnapshotRef'])

    reads = Counter()
    get = flow.env.store.get

    async def count(scope, snapshot_id):
        reads[scope, snapshot_id] += 1
        return await get(scope, snapshot_id)

    monkeypatch.setattr(flow.env.store, 'get', count)
    observed = await flow.api.source.read_source_observations(scope=SCOPE, snapshot_id=latest.source_snapshot_ref)
    assert len(observed['payload']['clinicalContributions']) == 1
    assert [row['value'] for row in observed['payload']['basicInfo'] if row['field'] == 'age'] == list(range(42, 48))
    assert len(reads) > 30 and max(reads.values()) == 1

    chunk = flow.env.store.chunks.documents[f'{initial_plan.active[0].proof_ref}:0']
    chunk['data'] += b'corrupt'
    with pytest.raises(RuntimeError, match='snapshot integrity check failed'):
        await flow.api.source.read_source_observations(scope=SCOPE, snapshot_id=latest.source_snapshot_ref)


@pytest.mark.asyncio
@pytest.mark.parametrize('retire_newer', [False, True])
async def test_late_older_round_cannot_publish_even_after_newer_contribution_retires(monkeypatch, retire_newer):
    older_quote, newer_quote = '年龄：42岁', '年龄：43岁'
    monkeypatch.setattr(source_fixtures, 'TEXT', source_fixtures.TEXT + newer_quote + '\n')
    flow = await composition(monkeypatch, edit=lambda raw: source_fixtures.no_value(raw, 'age'))
    await publish(flow)

    flow.env.raw['processing']['status'] = 'partial'
    for field in ('name', 'gender'):
        source_fixtures.no_value(flow.env.raw, field)
    flow.env.raw['basicInfo']['age'] = 42
    flow.env.raw['basicInfoMeta']['age'] = source_fixtures.extracted(42, older_quote, 'medium')['meta']
    flow.env.document['interpretationResultRound'] = 2
    older = await capture(flow.env)
    flow.env.raw['basicInfo']['age'] = 43
    flow.env.raw['basicInfoMeta']['age'] = source_fixtures.extracted(43, newer_quote, 'low')['meta']
    flow.env.document['interpretationResultRound'] = 3
    newer = await capture(flow.env)

    original = managed(flow, understanding=newer.understanding.snapshot_id)
    receipt = await flow.runtime.managed.execute(original)
    await settle(flow)
    age = (await info(flow)).fields['age']
    assert (age.value, age.source_reliability) == (43, 'low')
    current = receipt
    if retire_newer:
        retirement = managed(
            flow,
            retire=(age.source_ref['contributionId'],),
            request='retire-round-three',
            prior=2,
            source=receipt['sourceSnapshotRef'],
        )
        current = await flow.runtime.managed.execute(retirement)
        await settle(flow)
        assert (await info(flow)).fields['age'].value is None

    source_id = original.source_id
    current_ref = current['sourceSnapshotRef']
    current_inventory = await flow.readers.source.read_source_observations(scope=flow.scope, snapshot_id=current_ref)
    historical_inventory = await flow.readers.source.read_source_observations(
        scope=flow.scope, snapshot_id=receipt['sourceSnapshotRef']
    )
    assert historical_inventory['payload']['basicInfo'][-1]['interpretationRound'] == 3
    assert (
        await flow.env.api.read_bundle(scope=flow.scope, understanding_snapshot_id=older.understanding.snapshot_id)
        == older
    )

    ledger = await flow.ledger.read_source(flow.scope, source_id)
    revision = current['publicationRevision']
    assert ledger['publicationRevision'] == ledger['appliedRevision'] == ledger['settledRevision'] == revision
    before = deepcopy((flow.ledger.intents.documents, flow.records.documents, flow.env.store.snapshots.documents))
    selected = (await info(flow)).fields
    late = managed(
        flow,
        understanding=older.understanding.snapshot_id,
        request='late-round-two',
        prior=revision,
        source=current_ref,
    )
    with pytest.raises(PublicationConflictError, match='earlier interpretation round'):
        await flow.runtime.managed.execute(late)
    assert before == (flow.ledger.intents.documents, flow.records.documents, flow.env.store.snapshots.documents)
    assert await flow.ledger.read_source(flow.scope, source_id) == ledger
    assert (await info(flow)).fields == selected
    assert (
        await flow.readers.source.read_source_observations(scope=flow.scope, snapshot_id=current_ref)
        == current_inventory
    )
    assert await flow.runtime.managed.execute(original) == receipt
    assert await flow.ledger.read_source(flow.scope, source_id) == ledger
