"""Fixed archive manifests, pure bounded reads, and same-material admission races."""

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.archived_extractions import DocumentArchiveChangedError
from modules.analysis.formal_source_contracts import DocumentFormalSourceConflict
from modules.analysis.publication_lifecycle import DocumentSourceLifecycleV1
from modules.analysis.snapshots import DocumentScope
from modules.analysis.understanding import DocumentSnapshotChangedError
from tests.unit._support.document_profile_composition.analysis.archived_extractions import (
    combined_source as combined_source,
)
from tests.unit._support.document_profile_composition.analysis.archived_extractions import reader as reader
from tests.unit.services.analysis.test_document_formal_source import advance
from tests.unit.services.analysis.test_document_formal_source import apply
from tests.unit.services.analysis.test_document_formal_source import flow
from tests.unit.services.analysis.test_document_formal_source import request
from tests.unit.services.analysis.test_document_snapshots import SCOPE
from tests.unit.services.analysis.test_document_understanding import environment
from tests.unit.services.analysis.test_document_understanding import prepare


@pytest.mark.asyncio
async def test_completed_round_remains_capturable_after_later_failed_round_without_processing():
    env = environment()
    bundle = await prepare(env)
    document_id = bundle.material.payload['documentId']
    document = env.flow.index_collection.documents[document_id]
    round_number = document['interpretationResultRound']
    original = await env.flow.index.get_archived_round(document_id, SCOPE.user_id, SCOPE.subject_id, round_number)
    document.update(status='failed', interpretationRound=round_number + 1, interpretationResultRound=round_number + 1)
    document['payloadRefs'] = {'interpretation': 'later-missing-ref'}
    env.flow.coordinator.process = AsyncMock(side_effect=AssertionError('capture cannot process'))
    actual = await env.api.capture_round_bundle(scope=SCOPE, document_id=document_id, interpretation_round=round_number)
    assert actual.understanding.payload['interpretation'] == bundle.understanding.payload['interpretation']
    assert actual.material.payload['parsedDocument'] == bundle.material.payload['parsedDocument']
    assert (
        await env.flow.index.get_archived_round(document_id, SCOPE.user_id, SCOPE.subject_id, round_number) == original
    )
    with pytest.raises(DocumentNotFoundError):
        await env.api.capture_round_bundle(scope=SCOPE, document_id=document_id, interpretation_round=999)


@pytest.mark.asyncio
async def test_fixed_round_rejects_archive_rewrite_and_cannot_be_overwritten():
    env = environment()
    bundle = await prepare(env)
    document_id = bundle.material.payload['documentId']
    document = env.flow.index_collection.documents[document_id]
    number = document['interpretationResultRound']
    saved = await env.flow.index.get_archived_round(document_id, SCOPE.user_id, SCOPE.subject_id, number)
    await env.flow.index.put_archived_round(document, 'no-longer-owner', env.flow.coordinator.now(), saved)
    changed = deepcopy(saved)
    changed['interpretationDigest'] = 'different'
    with pytest.raises(RuntimeError, match='cannot be overwritten'):
        await env.flow.index.put_archived_round(document, 'no-longer-owner', env.flow.coordinator.now(), changed)
    env.flow.traces.payloads[(saved['payloadRefs']['interpretation'], 'interpretation')]['title'] = 'rewritten'
    with pytest.raises(DocumentSnapshotChangedError, match='archive differs'):
        await env.api.capture_round_bundle(scope=SCOPE, document_id=document_id, interpretation_round=number)


@pytest.mark.asyncio
async def test_unpublished_read_is_bounded_owned_pure_and_pins_fixed_selection():
    env = environment()
    bundle = await prepare(env)
    document_id = bundle.material.payload['documentId']
    state = DocumentSourceLifecycleV1(SCOPE, bundle.material.source_id, 'legacy', 'legacy')
    api = reader(env, state=state)
    env.flow.coordinator.process = AsyncMock(side_effect=AssertionError('read cannot process'))
    env.store.put = AsyncMock(side_effect=AssertionError('read cannot capture'))
    page = await api.list_archived(scope=SCOPE, limit=1)
    assert len(page.entries) == 1 and page.next_after_id is None
    selected = page.entries[0]
    result = await api.read_fixed(
        scope=SCOPE, extraction_id=selected.extraction_id, expected_lifecycle_token=selected.lifecycle_token
    )
    assert result.result == bundle.understanding.payload['interpretation']
    assert selected.document_id == document_id
    assert not (await api.list_archived(scope=DocumentScope('another-user', 'other'), limit=1)).entries
    with pytest.raises(PermissionError):
        await api.read_fixed(scope=DocumentScope('another-user', 'other'), extraction_id=selected.extraction_id)
    document = env.flow.index_collection.documents[document_id]
    document['archivedRounds']['2'] = {**deepcopy(document['archivedRounds']['1']), 'interpretationResultRound': 2}
    with pytest.raises(DocumentArchiveChangedError):
        await api.read_fixed(scope=SCOPE, extraction_id=selected.extraction_id)


@pytest.mark.asyncio
async def test_withdrawn_and_pending_event_sources_never_fall_back_to_saved_originals():
    env = environment()
    bundle = await prepare(env)
    source_id = bundle.material.source_id
    for status in ('withdrawn', 'retracting', 'pending'):
        state = DocumentSourceLifecycleV1(SCOPE, source_id, 'events', status, publication_revision=2)
        api = reader(env, state=state)
        env.flow.traces.read_payload = AsyncMock(side_effect=AssertionError('no fallback to old raw values'))
        page = await api.list_archived(scope=SCOPE)
        result = await api.read_fixed(scope=SCOPE, extraction_id=page.entries[0].extraction_id)
        assert result.result is None and not result.source_observations
        assert result.descriptor.archive_status == ('unavailable' if status == 'pending' else 'withdrawn')


@pytest.mark.asyncio
async def test_source_lifecycle_change_rejects_page_instead_of_returning_old_values():
    env = environment()
    bundle = await prepare(env)
    state = DocumentSourceLifecycleV1(SCOPE, bundle.material.source_id, 'legacy', 'legacy')
    api = reader(env, state=state)
    api.lifecycle.read.side_effect = [state, replace(state, mode='events', status='retracting')]
    with pytest.raises(DocumentArchiveChangedError):
        await api.list_archived(scope=SCOPE)


@pytest.mark.asyncio
async def test_active_source_reads_effective_fixed_result_and_all_contribution_observations():
    current = await flow()
    env = SimpleNamespace(flow=current.env.flow, api=current.env.api.understanding)
    api = reader(env, managed=current.runtime)
    api.lifecycle = current.runtime.lifecycle
    unpublished = await api.list_archived(scope=current.scope)
    command = request(current)
    await current.formal.submit(command)
    assert (await advance(current, command))['status'] == 'completed'
    before_application = await api.list_archived(scope=current.scope)
    assert before_application.entries[0].extraction_id == unpublished.entries[0].extraction_id
    assert before_application.entries[0].source_status == 'pending'
    pending_result = await api.read_fixed(
        scope=current.scope, extraction_id=before_application.entries[0].extraction_id
    )
    assert pending_result.result == current.bundle.understanding.payload['interpretation']
    await apply(current)
    page = await api.list_archived(scope=current.scope)
    assert page.entries[0].source_status == 'active'
    assert page.entries[0].extraction_id == unpublished.entries[0].extraction_id
    with pytest.raises(DocumentArchiveChangedError):
        await api.read_fixed(
            scope=current.scope,
            extraction_id=page.entries[0].extraction_id,
            expected_lifecycle_token=before_application.entries[0].lifecycle_token,
        )
    selected = await api.read_fixed(scope=current.scope, extraction_id=page.entries[0].extraction_id)
    assert selected.result == current.bundle.understanding.payload['interpretation']
    assert len(selected.source_observations) == len(current.bundle.facts.payload['observations'])
    assert all(row['sourcePointer'].startswith('/contributions/') for row in selected.source_observations)
    assert all(
        row['materialSourceRevision'] == current.bundle.material.source_revision for row in selected.source_observations
    )


@pytest.mark.asyncio
async def test_competing_fresh_command_waits_then_reuses_same_applied_material_without_second_event():
    current = await flow()
    first, second = request(current, identity='first'), request(current, identity='second')
    steps = current.formal.steps
    # Persist both route decisions before either command takes the source fence.
    for command in (first, second):
        await current.formal.submit(command)
        row = await current.formal.store.get(command.command_id)
        change = await steps.step(command, row)
        row.update(change)
        change = await steps.step(command, row)
        row.update(change)
        current.formal_records.documents[command.command_id].update(row)
    for _ in range(6):
        await current.formal.advance(first.command_id)
        completed = await current.formal.read_receipt(scope=current.scope, command_id=first.command_id)
        if completed['status'] == 'completed':
            break
        current.profile.now[0] += timedelta(seconds=31)
    assert completed['status'] == 'completed'
    pending = await current.formal.advance(second.command_id)
    assert pending['status'] == 'pending'
    row = await current.formal.store.get(second.command_id)
    assert row['transportFailures'] == 0
    await apply(current)
    current.profile.now[0] += timedelta(seconds=31)
    completed = await advance(current, second)
    assert completed['status'] == 'completed', completed
    assert completed['result']['basePublications'][0]['publicationRevision'] == 1
    state = await current.runtime.lifecycle.read(scope=current.scope, source_id=current.bundle.material.source_id)
    assert state.publication_revision == 1


@pytest.mark.asyncio
async def test_competing_fresh_command_does_not_replace_different_fixed_material():
    from tests.unit.services.analysis.test_document_complete_source import change_interpretation
    from tests.unit.services.analysis.test_document_managed_source import item

    current = await flow()
    first = request(current, identity='first')
    await current.formal.submit(first)
    assert (await advance(current, first))['status'] == 'completed'
    await apply(current)
    changed = await change_interpretation(current.env, current.bundle, scope=current.scope, value='888')
    second = request(current, identity='second', inputs=(item(changed),))
    source = {
        'sourceId': current.bundle.material.source_id,
        'input': item(changed).as_dict(),
        'phase': 'fresh_plan',
        'quality': 'complete',
        'inputVersionStatus': 'known',
        'interpretationRevision': 'analysis-health-report-v12',
    }
    with pytest.raises(DocumentFormalSourceConflict, match='different fixed material'):
        await current.formal.steps._source_step(second, {'createdAt': 0}, source, 0)


@pytest.mark.asyncio
async def test_required_manifest_failure_cannot_complete_preparation_or_publish():
    env = environment()
    env.flow.index.put_archived_round = AsyncMock(side_effect=RuntimeError('archive manifest unavailable'))
    with pytest.raises(RuntimeError, match='archive manifest unavailable'):
        await prepare(env)
    assert not env.flow.collection.documents and not env.flow.jobs.jobs
    assert all(row['status'] == 'failed' for row in env.flow.index_collection.documents.values())


@pytest.mark.asyncio
async def test_manifest_acknowledgement_loss_retries_same_output_without_ocr_or_model():
    env = environment()
    original = env.flow.index.put_archived_round
    lost = True

    async def save(*args):
        nonlocal lost
        await original(*args)
        if lost:
            lost = False
            raise RuntimeError('acknowledgement lost after successful write')

    env.flow.index.put_archived_round = save
    with pytest.raises(RuntimeError, match='acknowledgement lost'):
        await prepare(env)
    bundle = await prepare(env)
    assert bundle.understanding.payload['interpretationRound'] == 1
    assert len(env.flow.graph.calls) == len(env.flow.client.submit_calls) == 1


@pytest.mark.asyncio
async def test_delivery_metadata_change_does_not_rewrite_or_invalidate_fixed_round_on_retry():
    env = environment()
    bundle = await prepare(env)
    document = env.flow.index_collection.documents[bundle.material.payload['documentId']]
    original = deepcopy(document['archivedRounds']['1'])
    document.update(status='failed', retryable=True, sourceRevision=745123)
    repeated = await prepare(env)
    assert repeated.understanding.payload['interpretation'] == bundle.understanding.payload['interpretation']
    assert document['archivedRounds']['1'] == original
    assert len(env.flow.graph.calls) == len(env.flow.client.submit_calls) == 1


@pytest.mark.asyncio
async def test_newer_archived_round_is_visible_before_delivery_over_existing_active_source():
    from modules.analysis.application.document_reuse import _CheckpointTrace
    from tests.unit.services.analysis.test_document_complete_source import change_interpretation

    current = await flow()
    command = request(current)
    await current.formal.submit(command)
    assert (await advance(current, command))['status'] == 'completed'
    await apply(current)
    env = SimpleNamespace(flow=current.env.flow, api=current.env.api.understanding)
    api = reader(env, managed=current.runtime)
    api.lifecycle = current.runtime.lifecycle
    first = (await api.list_archived(scope=current.scope)).entries[0]
    original_state = await api.read_source_lifecycle(scope=current.scope, canonical_report_id=first.canonical_report_id)
    changed = await change_interpretation(current.env, current.bundle, scope=current.scope, value='477')
    document = current.env.flow.index_collection.documents[first.document_id]
    reuse = current.env.flow.service._document_reuse
    document.update(
        status='running',
        interpretationRound=2,
        interpretationResultRound=2,
        leaseToken='new-owner',
        leaseExpiresAt=reuse.now() + timedelta(minutes=2),
    )
    checkpoint = _CheckpointTrace(reuse, document, 'new-owner', SimpleNamespace(trace_id='new-round'))
    await checkpoint.archive_round(changed.understanding.payload['interpretation'])
    selected = (await api.list_archived(scope=current.scope)).entries[0]
    actual = await api.read_fixed(scope=current.scope, extraction_id=selected.extraction_id)
    assert actual.result == changed.understanding.payload['interpretation']
    assert selected.source_status == 'pending' and selected.source_revision is selected.publication_revision is None
    assert selected.extraction_id != first.extraction_id
    assert actual.source_observations and all(
        row['sourceStatus'] == 'active' and row['extractionId'] == first.extraction_id
        for row in actual.source_observations
    )
    stored = api._stored
    api._stored = AsyncMock(side_effect=RuntimeError('temporary pending archive failure'))
    unavailable = await api.read_fixed(scope=current.scope, extraction_id=selected.extraction_id)
    assert unavailable.descriptor.extraction_id == selected.extraction_id
    assert unavailable.descriptor.archive_status == 'unavailable' and unavailable.result is None
    assert unavailable.source_observations == actual.source_observations
    api._stored = stored
    assert await api.read_fixed(scope=current.scope, extraction_id=selected.extraction_id) == actual
    api._stored = AsyncMock(side_effect=DocumentArchiveChangedError('pending archive changed'))
    with pytest.raises(DocumentArchiveChangedError):
        await api.read_fixed(scope=current.scope, extraction_id=selected.extraction_id)
    assert (
        await api.read_source_lifecycle(scope=current.scope, canonical_report_id=first.canonical_report_id)
        == original_state
    )


@pytest.mark.asyncio
async def test_replacement_pending_reads_new_candidate_instead_of_retired_archive():
    from tests.unit.services.analysis.test_document_complete_source import change_interpretation
    from tests.unit.services.analysis.test_document_managed_source import item

    current = await flow()
    first = request(current)
    await current.formal.submit(first)
    assert (await advance(current, first))['status'] == 'completed'
    await apply(current)
    changed = await change_interpretation(current.env, current.bundle, scope=current.scope, value='477')
    correction = request(current, identity='correction', inputs=(item(changed),))
    await current.formal.submit(correction)
    assert (await advance(current, correction))['status'] == 'completed'
    env = SimpleNamespace(flow=current.env.flow, api=current.env.api.understanding)
    api = reader(env, managed=current.runtime)
    api.lifecycle = current.runtime.lifecycle
    page = await api.list_archived(scope=current.scope)
    selected = await api.read_fixed(scope=current.scope, extraction_id=page.entries[0].extraction_id)
    assert selected.descriptor.source_status == 'pending'
    assert selected.result == changed.understanding.payload['interpretation']
    assert selected.result != current.bundle.understanding.payload['interpretation']


@pytest.mark.asyncio
async def test_own_fresh_plan_ack_loss_resumes_original_fence_instead_of_waiting_for_itself():
    current = await flow()
    original = current.admission.prepare_fresh_plan
    lost = True

    async def prepare_once(**kwargs):
        nonlocal lost
        plan_ref = await original(**kwargs)
        if lost:
            lost = False
            raise RuntimeError('fresh-plan acknowledgement lost')
        return plan_ref

    current.admission.prepare_fresh_plan = prepare_once
    command = request(current)
    await current.formal.submit(command)
    completed = await advance(current, command)
    assert completed['status'] == 'completed', completed
    assert completed['result']['basePublications'][0]['publicationRevision'] == 1


@pytest.mark.asyncio
async def test_fixed_response_uses_existing_chunked_archive_and_owner_checks():
    current = await flow()
    store = current.env.flow.service._document_trace_store
    trace_id = current.bundle.material.payload['parseArtifactRef']
    response = {'title': 'synthetic', 'content': 'result' * 400_000}
    await store.write_payload(trace_id, current.scope.user_id, 'analysis_response', response)
    assert (
        await store.read_payload(trace_id, current.scope.user_id, current.scope.subject_id, 'analysis_response')
        == response
    )
    with pytest.raises(LookupError):
        await store.read_payload(trace_id, 'different-user', current.scope.subject_id, 'analysis_response')


@pytest.mark.asyncio
@pytest.mark.parametrize('old_phase', ['route', 'managed'])
async def test_older_fixed_round_archives_alias_without_replacing_later_applied_source(old_phase):
    from adapters.analysis.analysis_formal import AnalysisFormalAliases
    from modules.analysis.analysis_formal_contracts import AnalysisFormalCommand
    from modules.analysis.understanding import UNDERSTANDING_SELECTION
    from tests.unit.services.analysis.test_document_complete_source import change_interpretation
    from tests.unit.services.analysis.test_document_managed_source import item

    current = await flow()
    old = request(current, identity='old-http-cancelled')
    await current.formal.submit(old)
    source = {
        'sourceId': current.bundle.material.source_id,
        'input': current.item.as_dict(),
        'phase': 'route',
        'quality': 'complete',
        'inputVersionStatus': 'known',
        'interpretationRevision': 'analysis-health-report-v12',
    }
    if old_phase == 'managed':
        first = request(current, identity='initial')
        await current.formal.submit(first)
        # Advance only this command, retaining the accepted old command for later replay.
        for _ in range(6):
            await current.formal.advance(first.command_id)
            receipt = await current.formal.read_receipt(scope=current.scope, command_id=first.command_id)
            if receipt['status'] == 'completed':
                break
            current.profile.now[0] += timedelta(seconds=31)
        await apply(current)
        source = await current.formal.steps._source_step(old, {'createdAt': 0}, source, 0)
        assert source['phase'] == 'managed'

    await change_interpretation(current.env, current.bundle, scope=current.scope, value='477')
    document = current.env.flow.index_collection.documents[current.bundle.material.payload['documentId']]
    document.update(interpretationRound=2, interpretationResultRound=2)
    later = await current.env.api.prepare_bundle(
        scope=current.scope, selection=UNDERSTANDING_SELECTION, document_id=document['_id'], resume_incomplete=False
    )
    newer = request(current, identity='later-round', inputs=(item(later),))
    await current.formal.submit(newer)
    # Do not run the old command before the later round is applied.
    for _ in range(6):
        await current.formal.advance(newer.command_id)
        receipt = await current.formal.read_receipt(scope=current.scope, command_id=newer.command_id)
        if receipt['status'] == 'completed':
            break
        current.profile.now[0] += timedelta(seconds=31)
    assert receipt['status'] == 'completed', receipt
    await apply(current)
    lifecycle = await current.runtime.lifecycle.read(scope=current.scope, source_id=current.bundle.material.source_id)
    recovered = await current.formal.steps._source_step(old, {'createdAt': 0}, source, 0)
    base = recovered['result']
    assert recovered['phase'] == 'done' and base['historicalOnly'] is True
    assert await current.runtime.lifecycle.read(scope=current.scope, source_id=lifecycle.source_id) == lifecycle
    aliases = AnalysisFormalAliases(
        compatibility=current.env.api, managed=current.runtime, profile_archive=current.archive
    )
    await current.env.flow.index.bind_report_id(document, 'old-fixed-alias')
    command = AnalysisFormalCommand(
        current.scope,
        current.item.preparation_receipt.trace_id,
        document['_id'],
        document['canonicalReportId'],
        document['contentSha256'],
        'analysis-health-report-v12',
        1,
        'old-fixed-alias',
    )
    await aliases.publish(command, current.item, base)
    alias = await current.env.flow.source.get_owned('old-fixed-alias', current.scope.user_id, current.scope.subject_id)
    assert (
        alias.result['observations'][0]['resultValue']
        == current.bundle.understanding.payload['interpretation']['observations'][0]['resultValue']
    )
    assert await current.runtime.lifecycle.read(scope=current.scope, source_id=lifecycle.source_id) == lifecycle


async def filtered_pages(api, scope, document_id):
    descriptors, after = [], None
    for _ in range(10):
        page = await api.list_archived(scope=scope, document_id=document_id, after_id=after, limit=1)
        descriptors.extend(page.entries)
        after = page.next_after_id
        if after is None:
            break
    assert after is None, 'filtered source scan must terminate'
    return descriptors


@pytest.mark.asyncio
async def test_secondary_file_filter_finds_current_combined_contribution_once_without_primary_only_rows():
    current, second, api, command, published = await combined_source()
    document_id = second.material.payload['documentId']
    descriptors = await filtered_pages(api, current.scope, document_id)
    assert [row.document_id for row in descriptors] == [document_id, current.bundle.material.payload['documentId']]
    own, related = [
        await api.read_fixed(
            scope=current.scope,
            extraction_id=row.extraction_id,
            expected_lifecycle_token=row.lifecycle_token,
            document_id=document_id,
        )
        for row in descriptors
    ]
    assert own.result == second.understanding.payload['interpretation']
    assert related.result is None and related.source_observations
    assert document_id in related.descriptor.document_ids
    assert all(row['contributionKind'] == 'legacy_report_source' for row in related.source_observations)
    assert all(document_id in row['documentIds'] for row in related.source_observations)
    assert all('/reportSource/' in row['sourcePointer'] for row in related.source_observations)
    assert not {row['sourcePointer'] for row in own.source_observations}.intersection(
        row['sourcePointer'] for row in related.source_observations
    )
    # Repeated commands for the same effective source do not duplicate discovery.
    repeated = replace(
        command,
        request_id='repeat-combined',
        prior_publication_revision=published['publicationRevision'],
        prior_source_snapshot_ref=published['sourceSnapshotRef'],
    )
    await current.runtime.commands.execute(repeated)
    assert len(await filtered_pages(api, current.scope, document_id)) == 2
    assert any(
        options.get('name') == 'document_managed_input_source_seek' for _, options in current.command_records.indexes
    )
    current.command_records.create_index = AsyncMock(side_effect=AssertionError('query cannot create indexes'))
    assert len(await filtered_pages(api, current.scope, document_id)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['retire', 'withdraw'])
async def test_secondary_filter_does_not_revive_retired_or_withdrawn_combined_values(action):
    current, second, api, command, published = await combined_source()
    document_id = second.material.payload['documentId']
    related = (await filtered_pages(api, current.scope, document_id))[1]
    if action == 'retire':
        await current.runtime.commands.execute(
            replace(
                command,
                request_id='retire-combined',
                operation='retire_report',
                prior_publication_revision=published['publicationRevision'],
                prior_source_snapshot_ref=published['sourceSnapshotRef'],
            )
        )
    else:
        await current.runtime.publisher.retract(
            scope=current.scope,
            source_id=command.source_id,
            source_revision=current.bundle.material.source_revision,
            request_id='withdraw-combined',
            expected_prior_revision=published['publicationRevision'],
        )
    await apply(current)
    descriptors = await filtered_pages(api, current.scope, document_id)
    assert [row.document_id for row in descriptors] == [document_id]
    with pytest.raises(DocumentArchiveChangedError):
        await api.read_fixed(
            scope=current.scope,
            extraction_id=related.extraction_id,
            expected_lifecycle_token=related.lifecycle_token,
            document_id=document_id,
        )


@pytest.mark.asyncio
async def test_related_cursor_is_scope_and_filter_bound():
    current, second, api, _, _ = await combined_source()
    document_id = second.material.payload['documentId']
    page = await api.list_archived(scope=current.scope, document_id=document_id, limit=1)
    with pytest.raises(ValueError, match='cursor'):
        await api.list_archived(
            scope=current.scope,
            document_id=current.bundle.material.payload['documentId'],
            after_id=page.next_after_id,
            limit=1,
        )
    with pytest.raises(ValueError, match='cursor'):
        await api.list_archived(
            scope=DocumentScope('unrelated-user', 'unrelated-subject'),
            document_id=document_id,
            after_id=page.next_after_id,
            limit=1,
        )
    assert not (
        await api.list_archived(scope=DocumentScope('unrelated-user', 'unrelated-subject'), document_id=document_id)
    ).entries


@pytest.mark.asyncio
async def test_secondary_index_missing_keeps_owned_current_combined_material_readable():
    current, second, api, _, _ = await combined_source()
    document_id = second.material.payload['documentId']
    del current.env.flow.index_collection.documents[document_id]
    descriptors = await filtered_pages(api, current.scope, document_id)
    assert len(descriptors) == 1 and descriptors[0].document_id == current.bundle.material.payload['documentId']
    related = await api.read_fixed(
        scope=current.scope,
        extraction_id=descriptors[0].extraction_id,
        expected_lifecycle_token=descriptors[0].lifecycle_token,
        document_id=document_id,
    )
    assert related.result is None and related.source_observations
    assert all(document_id in row['documentIds'] for row in related.source_observations)
    assert not (
        await api.list_archived(scope=DocumentScope('other-user', 'other-subject'), document_id=document_id)
    ).entries


@pytest.mark.asyncio
async def test_withdrawal_preserves_fixed_extraction_identity_without_reading_original_content():
    current, second, api, command, published = await combined_source()
    primary_id = current.bundle.material.payload['documentId']
    before = (await api.list_archived(scope=current.scope, document_id=primary_id)).entries[0]
    await current.runtime.publisher.retract(
        scope=current.scope,
        source_id=command.source_id,
        source_revision=current.bundle.material.source_revision,
        request_id='identity-withdrawal',
        expected_prior_revision=published['publicationRevision'],
    )
    api._active = AsyncMock(side_effect=AssertionError('withdrawal cannot read source bodies'))
    api._stored = AsyncMock(side_effect=AssertionError('withdrawal cannot read archive bodies'))
    retracting = (await api.list_archived(scope=current.scope, document_id=primary_id)).entries[0]
    assert retracting.extraction_id == before.extraction_id
    await apply(current)
    withdrawn = await api.read_fixed(scope=current.scope, extraction_id=before.extraction_id)
    assert withdrawn.descriptor.extraction_id == before.extraction_id
    assert withdrawn.descriptor.source_status == 'withdrawn' and withdrawn.result is None


@pytest.mark.asyncio
async def test_temporary_archive_failure_preserves_identity_and_recovery_reuses_same_selection():
    env = environment()
    bundle = await prepare(env)
    state = DocumentSourceLifecycleV1(SCOPE, bundle.material.source_id, 'legacy', 'legacy')
    api = reader(env, state=state)
    before = (await api.list_archived(scope=SCOPE)).entries[0]
    original = api.traces.read_payload
    api.traces.read_payload = AsyncMock(side_effect=RuntimeError('temporary archive outage'))
    unavailable = await api.read_fixed(scope=SCOPE, extraction_id=before.extraction_id)
    assert unavailable.descriptor.extraction_id == before.extraction_id
    assert unavailable.descriptor.archive_status == 'unavailable' and unavailable.result is None
    api.traces.read_payload = original
    recovered = await api.read_fixed(scope=SCOPE, extraction_id=before.extraction_id)
    assert recovered.descriptor.extraction_id == before.extraction_id
    assert recovered.result == bundle.understanding.payload['interpretation']


@pytest.mark.asyncio
async def test_missing_fixed_archive_is_document_level_unavailable_without_fabricated_extraction():
    env = environment()
    bundle = await prepare(env)
    state = DocumentSourceLifecycleV1(SCOPE, bundle.material.source_id, 'legacy', 'legacy')
    api = reader(env, state=state)
    document = env.flow.index_collection.documents[bundle.material.payload['documentId']]
    document.pop('archivedRounds')
    document['payloadRefs'] = {}
    descriptor = (await api.list_archived(scope=SCOPE)).entries[0]
    assert descriptor.extraction_id is None and descriptor.archive_status == 'unavailable'
    assert descriptor.reason == 'archive_missing'
