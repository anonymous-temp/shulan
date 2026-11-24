"""Regressions for current source publication and complete initial admission."""

from dataclasses import replace

import pytest

from modules.analysis.complete_source_contracts import DocumentCompleteSourceUnavailable
from modules.analysis.publication import PublicationConflictError
from tests.unit._support.current_document import capture
from tests.unit._support.current_document_profile import clinical_facts
from tests.unit._support.current_document_profile import composition
from tests.unit._support.current_document_profile import info
from tests.unit._support.current_document_profile import managed
from tests.unit._support.current_document_profile import publish
from tests.unit._support.current_document_profile import settle
from tests.unit.services.analysis.test_document_basic_info_runtime import runtime
from tests.unit.services.analysis.test_document_snapshots import SCOPE


@pytest.mark.asyncio
async def test_old_source_snapshot_cannot_publish_a_new_revision_after_managed_retirement(monkeypatch):
    flow = await composition(monkeypatch)
    await publish(flow)
    initial_ref = flow.capture.source_snapshot_ref
    scope = flow.scope
    source_id = flow.env.bundle.material.source_id
    initial = await flow.readers.source.read_source_observations(scope=scope, snapshot_id=initial_ref)
    command = managed(flow, retire=[row['contributionId'] for row in initial['payload']['contributions']])
    retired = await flow.runtime.managed.execute(command)
    await settle(flow)
    assert (await info(flow)).fields['name'].value is None
    assert not await clinical_facts(flow)
    before = await flow.ledger.read_source(scope, source_id)
    assert before['publicationRevision'] == before['appliedRevision'] == 2

    with pytest.raises(PublicationConflictError, match='verified transition'):
        await flow.runtime.publisher.publish(
            scope=scope,
            snapshot_id=initial_ref,
            request_id='replay-as-new',
            expected_prior_revision=2,
            event_kind='correct',
        )
    with pytest.raises(PublicationConflictError, match='verified transition'):
        await flow.runtime.publisher.publish(
            scope=scope,
            snapshot_id=retired['sourceSnapshotRef'],
            request_id='reuse-transition-as-new',
            expected_prior_revision=2,
            event_kind='correct',
        )
    assert await flow.ledger.read_source(scope, source_id) == before
    assert await flow.ledger.read_reserved(scope, source_id, 'replay-as-new') is None
    assert (await info(flow)).fields['name'].value is None
    assert not await clinical_facts(flow)
    replay = await flow.runtime.publisher.publish(
        scope=scope,
        snapshot_id=retired['sourceSnapshotRef'],
        request_id=command.command_id,
        expected_prior_revision=1,
        event_kind='correct',
    )
    assert replay.state == 'applied' and replay.publication_revision == 2
    assert await flow.runtime.managed.execute(command) == retired


@pytest.mark.asyncio
async def test_complete_source_size_is_checked_before_binding_and_a_smaller_round_can_publish():
    flow = await runtime()
    env = flow.env
    original_rows = env.raw['observations']
    env.raw['observations'] = [{'itemName': f'synthetic-{i}', 'resultValue': 'x' * 3600} for i in range(1000)]
    size_probe = await capture(env)
    pairs = await flow.api.readers.source._contributions(SCOPE, size_probe.understanding.snapshot_id)
    clinical = next(row for row, _ in pairs if row.payload['kind'] == 'clinical')
    env.raw['observations'][0]['resultValue'] += 'x' * (3_998_500 - len(clinical._payload_json.encode('utf-8')))
    oversized = await capture(env)
    oversized_input = await flow.prepared_service.remember(
        replace(oversized, receipt=replace(env.original.receipt, trace_id='oversized-prepare'))
    )
    with pytest.raises(DocumentCompleteSourceUnavailable, match='source byte bound exceeded'):
        await flow.api.admission.prepare_source_plan(
            scope=SCOPE, prepared=oversized_input, request_id='oversized-admission', lifecycle_epoch=3
        )
    assert await flow.ledger.read_source(SCOPE, flow.source_id) is None
    assert not flow.records.documents
    assert not flow.receiver.calls

    env.raw['observations'] = original_rows
    env.document['interpretationResultRound'] = 2
    smaller = await capture(env)
    smaller_input = await flow.prepared_service.remember(
        replace(smaller, receipt=replace(env.original.receipt, trace_id='smaller-prepare'))
    )
    plan_ref = await flow.api.admission.prepare_source_plan(
        scope=SCOPE, prepared=smaller_input, request_id='eligible-admission', lifecycle_epoch=3
    )
    result = await flow.api.source.capture_source(scope=SCOPE, verified_plan_ref=plan_ref)
    publication = await flow.api.publisher.publish(
        scope=SCOPE,
        snapshot_id=result.source_snapshot_ref,
        request_id='eligible-publication',
        expected_prior_revision=None,
    )
    assert publication.state == 'applied' and publication.publication_revision == 1
