"""Real formal Document orchestration with Profile admission/application and BSON stores."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from modules.analysis.application.documents import DocumentNotFoundError
from modules.analysis.formal_source_contracts import DocumentFormalSourceConflict
from modules.analysis.formal_source_contracts import DocumentFormalSourcePolicy
from modules.analysis.snapshots import DocumentScope
from tests.unit._support.document_profile_composition.analysis.document_formal_source import advance as advance
from tests.unit._support.document_profile_composition.analysis.document_formal_source import apply as apply
from tests.unit._support.document_profile_composition.analysis.document_formal_source import build as build
from tests.unit._support.document_profile_composition.analysis.document_formal_source import flow as flow
from tests.unit._support.document_profile_composition.analysis.document_formal_source import request as request
from tests.unit.services.analysis.test_document_complete_source import change_interpretation
from tests.unit.services.analysis.test_document_managed_source import item


@pytest.mark.asyncio
async def test_submission_and_query_do_not_touch_profile_then_background_finishes_real_fresh_source():
    current = await flow()
    command = request(current)
    original = current.fresh.reserve_fresh_source
    current.fresh.reserve_fresh_source = AsyncMock(side_effect=AssertionError('submit/read must not call Profile'))
    receipt = await current.formal.submit(command)
    assert receipt['status'] == 'pending'
    assert await current.formal.read_receipt(scope=current.scope, command_id=command.command_id) == receipt
    assert not current.env.ledger.sources.documents
    assert not current.env.flow.collection.documents and not current.profile.collections['jobs'].documents
    current.fresh.reserve_fresh_source = original
    completed = await advance(current, command)
    assert completed['status'] == 'completed', completed
    base = completed['result']['basePublications'][0]
    assert base['kind'] == 'archive' and base['publicationRevision'] == 1
    assert base['publication']['state'] != 'applied'
    assert current.env.flow.collection.documents and not current.env.flow.jobs.jobs
    await apply(current)
    lifecycle = await current.runtime.lifecycle.read(scope=current.scope, source_id=current.bundle.material.source_id)
    assert lifecycle.status == 'active'
    current.formal = build(current)
    current.formal.steps = AsyncMock(side_effect=AssertionError('completed reads never resume'))
    assert await current.formal.submit(command) == completed
    assert await current.formal.recover_pending() == 0
    with pytest.raises(PermissionError):
        await current.formal.read_receipt(
            scope=DocumentScope('other', current.scope.subject_id), command_id=command.command_id
        )
    with pytest.raises(DocumentFormalSourceConflict):
        await current.formal.submit(replace(command, operation='report'))


@pytest.mark.asyncio
async def test_existing_legacy_source_keeps_real_receipt_and_never_enters_fresh():
    current = await flow()
    old = await current.env.api.publish_prepared(
        scope=current.scope,
        understanding_snapshot_id=current.item.understanding_snapshot_ref,
        selection=current.item.selection,
        preparation_receipt=current.item.preparation_receipt,
    )
    current.admission.prepare_fresh_plan = AsyncMock(side_effect=AssertionError('existing history must remain legacy'))
    command = request(current)
    await current.formal.submit(command)
    receipt = await advance(current, command)
    assert receipt['status'] == 'completed', receipt
    base = receipt['result']['basePublications'][0]
    assert base['kind'] == 'legacy' and base['receipt']['legacy_source_revision'] == old.legacy_source_revision
    assert len(current.env.flow.jobs.jobs) == 1
    assert (
        await current.runtime.lifecycle.read(scope=current.scope, source_id=current.bundle.material.source_id)
    ).mode == 'legacy'


@pytest.mark.asyncio
async def test_managed_base_correction_uses_fixed_prior_and_real_archive():
    current = await flow()
    first = request(current)
    await current.formal.submit(first)
    assert (await advance(current, first))['status'] == 'completed'
    await apply(current)
    changed = await change_interpretation(current.env, current.bundle, scope=current.scope, value='477')
    command = request(current, identity='correct', inputs=(item(changed),))
    await current.formal.submit(command)
    result = await advance(current, command)
    assert result['status'] == 'completed', result
    base = result['result']['basePublications'][0]
    assert base['kind'] == 'archive' and base['publicationRevision'] == 2
    assert base['managedCommandId'] and not current.env.flow.jobs.jobs


@pytest.mark.asyncio
async def test_report_business_projection_does_not_wait_for_profile_but_supplement_waits_for_own_applied_base():
    current = await flow()
    command = request(current, operation='report')
    projected = await current.formal.prepare_report(command)
    assert projected.payload['processingStatus'] == 'complete'
    assert not current.env.ledger.sources.documents
    await current.formal.submit(command)
    pending = await advance(current, command)
    assert pending['status'] == 'pending' and pending['phase'] == 'report', pending
    assert (await current.formal.store.get(command.command_id))['progress']['projectionRef'] == projected.snapshot_id
    await apply(current)
    completed = await advance(current, command)
    assert completed['status'] == 'completed', completed
    assert completed['result']['reportPublication']['projectionRef'] == projected.snapshot_id
    assert completed['result']['reportPublication']['publicationRevision'] == 2
    assert not current.env.flow.jobs.jobs


@pytest.mark.asyncio
async def test_lost_fresh_authorization_ack_replays_same_durable_binding():
    current = await flow()
    command = request(current)
    original = current.fresh.authorize_fresh_source
    calls = []

    async def unknown(authorization, *, scope):
        calls.append(authorization.model_dump(mode='json', by_alias=True))
        result = await original(authorization, scope=scope)
        if len(calls) == 1:
            raise TimeoutError('ack lost after ready')
        return result

    current.fresh.authorize_fresh_source = unknown
    await current.formal.submit(command)
    await current.formal.recover_pending()
    saved = deepcopy(await current.formal.store.get(command.command_id))
    assert saved['progress']['sources'][0]['phase'] == 'fresh_authorize'
    assert saved['transportFailures'] == 1
    current.profile.now[0] += timedelta(minutes=1)
    current.formal = build(current)
    completed = await advance(current, command)
    assert completed['status'] == 'completed', completed
    assert len(calls) == 2 and calls[0] == calls[1]
    assert len(current.env.ledger.intents.documents) == 1


@pytest.mark.asyncio
async def test_profile_unavailability_does_not_turn_new_accepted_source_into_legacy():
    current = await flow()
    current.formal = build(current, policy=DocumentFormalSourcePolicy(max_transport_failures=1))
    current.fresh.reserve_fresh_source = AsyncMock(side_effect=RuntimeError('Profile offline'))
    command = request(current)
    await current.formal.submit(command)
    result = await advance(current, command)
    assert result['status'] == 'failed' and result['errorCode'] == 'RuntimeError'
    state = await current.admission.read_status(scope=current.scope, source_id=current.bundle.material.source_id)
    assert state['mode'] == 'admission' and state['ownerProofIssued']
    assert not current.env.flow.jobs.jobs


@pytest.mark.asyncio
async def test_withdrawn_source_is_not_republished_by_a_new_business_command():
    current = await flow()
    command = request(current)
    await current.formal.submit(command)
    assert (await advance(current, command))['status'] == 'completed'
    await apply(current)
    await current.runtime.publisher.retract(
        scope=current.scope,
        source_id=current.bundle.material.source_id,
        source_revision=1,
        request_id='withdraw',
        expected_prior_revision=1,
    )
    later = request(current, identity='after-withdraw')
    await current.formal.submit(later)
    receipt = await advance(current, later)
    assert receipt['status'] == 'not_published', receipt
    assert receipt['result']['basePublications'][0]['reason'] == 'retracting'
    assert len(current.env.ledger.intents.documents) == 2


@pytest.mark.asyncio
async def test_mixed_legacy_first_and_managed_secondary_publish_without_sending_secondary_to_old_sink():
    current = await flow()
    initial = request(current)
    await current.formal.submit(initial)
    assert (await advance(current, initial))['status'] == 'completed'
    await apply(current)
    legacy = await current.env.api.prepare_bundle(
        scope=current.scope,
        selection=current.item.selection,
        content=b'%PDF-1.7 another report',
        file_name='legacy.pdf',
        file_type='pdf',
    )
    assert legacy.material.source_id != current.bundle.material.source_id
    legacy_item = item(legacy)
    await current.env.api.publish_prepared(
        scope=current.scope,
        understanding_snapshot_id=legacy_item.understanding_snapshot_ref,
        selection=legacy_item.selection,
        preparation_receipt=legacy_item.preparation_receipt,
    )
    calls = []
    old_publish = current.env.api.publish_prepared

    async def checked(**kwargs):
        calls.append(kwargs['understanding_snapshot_id'])
        assert kwargs['understanding_snapshot_id'] != current.item.understanding_snapshot_ref
        return await old_publish(**kwargs)

    current.env.api.publish_prepared = checked
    command = request(current, identity='mixed', operation='report', inputs=(legacy_item, current.item))
    await current.formal.submit(command)
    result = await advance(current, command)
    assert result['status'] == 'completed', result
    assert [base['kind'] for base in result['result']['basePublications']] == ['legacy', 'archive']
    assert result['result']['reportPublication']['kind'] == 'legacy_report'
    assert result['result']['reportPublication']['sourceId'] == legacy.material.source_id
    assert result['result']['reportPublication']['legacySourceRevision'] > 1
    assert calls and set(calls) == {legacy_item.understanding_snapshot_ref}
    assert (
        await current.runtime.lifecycle.read(scope=current.scope, source_id=current.bundle.material.source_id)
    ).mode == 'events'
    assert len(current.env.flow.jobs.jobs) == 2


@pytest.mark.asyncio
async def test_partial_managed_input_keeps_business_projection_without_formal_downgrade():
    current = await flow()
    initial = request(current)
    await current.formal.submit(initial)
    assert (await advance(current, initial))['status'] == 'completed'
    await apply(current)
    partial = await change_interpretation(current.env, current.bundle, scope=current.scope, status='partial')
    command = request(current, identity='partial', operation='report', inputs=(item(partial),))
    projection = await current.formal.prepare_report(command)
    assert projection.payload['processingStatus'] == 'partial'
    await current.formal.submit(command)
    result = await advance(current, command)
    assert result['status'] == 'not_published', result
    assert result['result']['projectionRef'] == projection.snapshot_id
    assert len(current.env.ledger.intents.documents) == 1 and not current.env.flow.jobs.jobs


@pytest.mark.asyncio
async def test_deadline_of_formal_report_does_not_stop_already_published_base_application():
    current = await flow()
    current.formal = build(current, policy=DocumentFormalSourcePolicy(deadline_seconds=240))
    command = request(current, operation='report')
    await current.formal.submit(command)
    pending = await advance(current, command, count=4)
    assert pending['status'] == 'pending'
    current.profile.now[0] += timedelta(minutes=10)
    failed = await advance(current, command)
    assert failed['status'] == 'failed' and failed['errorCode'] == 'formal_deadline_exhausted'
    await apply(current)
    assert (
        await current.runtime.lifecycle.read(scope=current.scope, source_id=current.bundle.material.source_id)
    ).status == 'active'
    assert len(current.env.ledger.intents.documents) == 1


@pytest.mark.asyncio
async def test_cancellation_keeps_fixed_unknown_stage_and_releases_only_own_claim():
    current = await flow()
    command = request(current)
    entered = asyncio.Event()
    original = current.formal.steps.step

    async def blocked(*args):
        entered.set()
        await asyncio.Event().wait()

    current.formal.steps.step = blocked
    await current.formal.submit(command)
    task = asyncio.create_task(current.formal.advance(command.command_id))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = await current.formal.store.get(command.command_id)
    assert row['phase'] == 'prepare' and row['status'] == 'pending' and row['leaseOwner'] is None
    current.formal.steps.step = original
    assert (await advance(current, command))['status'] == 'completed'


@pytest.mark.asyncio
async def test_unknown_persisted_command_fails_before_any_provider_dispatch_and_does_not_starve_next():
    from modules.analysis.snapshots import digest

    current = await flow()
    command = request(current)
    await current.formal.submit(command)
    row = current.formal_records.documents[command.command_id]
    row['command']['commandRevision'] = 'unavailable-version'
    row['commandDigest'] = digest(row['command'])
    current.formal.steps.step = AsyncMock(side_effect=AssertionError('unknown command must stop'))
    await current.formal.recover_pending()
    saved = await current.formal.store.get(command.command_id)
    assert saved['status'] == 'failed' and saved['errorCode'] == 'UnsupportedOutputVersionError'
    assert not current.env.ledger.sources.documents
    assert await current.formal.store.pending(now=current.profile.now[0].timestamp(), limit=1) == []


@pytest.mark.asyncio
async def test_long_legacy_compatible_user_is_selected_before_public_profile_scope_calls():
    current = await flow()
    scope = DocumentScope('u' * 512, current.scope.subject_id)
    bundle = await current.env.api.prepare_bundle(
        scope=scope,
        selection=current.item.selection,
        content=b'%PDF-1.7 long user',
        file_name='long.pdf',
        file_type='pdf',
    )
    current.scope, current.item, current.bundle = scope, item(bundle), bundle
    current.admission.profile_presence.read_absence = AsyncMock(
        side_effect=AssertionError('unsupported scope must stay legacy')
    )
    command = request(current)
    await current.formal.submit(command)
    result = await advance(current, command)
    assert result['status'] == 'completed', result
    assert result['result']['basePublications'][0]['kind'] == 'legacy'
    assert len(current.env.flow.jobs.jobs) == 1


@pytest.mark.asyncio
async def test_duplicate_preparations_preserve_order_without_republishing_the_same_source():
    current = await flow()
    await current.env.api.publish_prepared(
        scope=current.scope,
        understanding_snapshot_id=current.item.understanding_snapshot_ref,
        selection=current.item.selection,
        preparation_receipt=current.item.preparation_receipt,
    )
    repeated = await current.env.api.prepare_bundle(
        scope=current.scope,
        selection=current.item.selection,
        document_id=current.item.preparation_receipt.document_id,
    )
    assert repeated.receipt.trace_id != current.item.preparation_receipt.trace_id
    command = request(current, operation='report', inputs=(current.item, item(repeated)))
    await current.formal.submit(command)
    result = await advance(current, command)
    assert result['status'] == 'completed', result
    assert len(result['result']['basePublications']) == 1
    assert len(current.env.flow.jobs.jobs) == 2
    assert len((await current.formal.store.get(command.command_id))['command']['inputs']) == 2


@pytest.mark.asyncio
async def test_every_fixed_input_is_checked_before_the_first_source_write():
    current = await flow()
    invalid = replace(current.item, material_snapshot_ref='another-fixed-material')
    command = request(current, operation='report', inputs=(current.item, invalid))
    await current.formal.submit(command)
    result = await advance(current, command)
    assert result['status'] == 'failed' and result['errorCode'] == 'DocumentFormalSourceConflict'
    assert not current.env.ledger.sources.documents and not current.env.flow.jobs.jobs


@pytest.mark.asyncio
async def test_later_source_correction_cannot_be_overwritten_by_a_waiting_report_supplement():
    from modules.analysis.managed_source_contracts import MANAGED_SOURCE_REVISION
    from modules.analysis.managed_source_contracts import DocumentManagedSourceCommand

    current = await flow()
    command = request(current, operation='report')
    await current.formal.submit(command)
    assert (await advance(current, command))['status'] == 'pending'
    await apply(current)
    lifecycle = await current.runtime.lifecycle.read(scope=current.scope, source_id=current.bundle.material.source_id)
    changed = await change_interpretation(current.env, current.bundle, scope=current.scope, value='481')
    corrected = await current.runtime.commands.execute(
        DocumentManagedSourceCommand(
            current.scope,
            current.bundle.material.source_id,
            'newer-correction',
            1,
            lifecycle.active_snapshot_ref,
            'replace_base',
            (item(changed),),
            MANAGED_SOURCE_REVISION,
        )
    )
    assert corrected['publicationRevision'] == 2
    result = await advance(current, command)
    assert result['status'] == 'failed' and result['errorCode'] == 'DocumentFormalSourceConflict'
    assert len(current.env.ledger.intents.documents) == 2


@pytest.mark.asyncio
async def test_execute_finishes_base_with_actual_archive_receipt_without_waiting_for_application():
    current = await flow()
    command = request(current)
    completed = await current.formal.execute(command)
    assert completed['status'] == 'completed', completed
    assert completed['completedBaseCount'] == completed['totalUniqueSources'] == 1
    base = completed['result']['basePublications'][0]
    archived = await current.runtime.archive.read(scope=current.scope, archive_ref=base['archiveRef'])
    assert base['canonicalReportId'] == archived['receipt']['canonicalReportId']
    assert base['legacySourceRevision'] == archived['receipt']['legacySourceRevision'] == 1
    assert base['ingestionStatus'] == 'source_managed' and base['jobId'] is None
    assert base['publication']['state'] != 'applied'
    assert not current.env.flow.jobs.jobs
    assert await current.formal.execute(command) == completed


@pytest.mark.asyncio
async def test_execute_returns_pending_report_with_durable_completed_progress_and_projection():
    current = await flow()
    command = request(current, operation='report')
    prepared = await current.formal.prepare_report(command)
    pending = await current.formal.execute(command)
    assert pending['status'] == 'pending' and pending['phase'] == 'report', pending
    assert pending['completedBaseCount'] == pending['totalUniqueSources'] == 1
    assert pending['result']['projectionRef'] == prepared.snapshot_id
    assert pending['result']['processingStatus'] == 'complete'
    assert pending['result']['reportPublication'] is None
    base = pending['result']['basePublications'][0]
    assert base['legacySourceRevision'] == 1 and base['ingestionStatus'] == 'source_managed'
    before = await current.formal.store.get(command.command_id)
    assert await current.formal.execute(command) == pending
    assert await current.formal.store.get(command.command_id) == before
    current.profile.now[0] += timedelta(days=2)
    failed = await current.formal.execute(command)
    assert failed['status'] == 'failed' and failed['errorCode'] == 'formal_deadline_exhausted'
    assert failed['result'] == pending['result'] and failed['completedBaseCount'] == 1
    await apply(current)
    assert (
        await current.runtime.lifecycle.read(scope=current.scope, source_id=current.bundle.material.source_id)
    ).status == 'active'


@pytest.mark.asyncio
async def test_execute_returns_pending_without_mutating_another_workers_lease():
    current = await flow()
    command = request(current)
    submitted = await current.formal.submit(command)
    claimed = await current.formal.store.claim(
        command.command_id, owner='another-worker', now=current.profile.now[0].timestamp()
    )
    current.formal.steps.step = AsyncMock(side_effect=AssertionError('another worker owns command'))
    assert await current.formal.execute(command) == submitted
    assert await current.formal.store.get(command.command_id) == claimed
    assert submitted['completedBaseCount'] == 0 and submitted['totalUniqueSources'] is None
    assert submitted['result']['basePublications'] == []


@pytest.mark.asyncio
async def test_execute_has_total_stage_bound_without_sleeping_or_resetting_unknown_work():
    current = await flow()
    command = request(current)
    current.formal.steps.step = AsyncMock(return_value={'phase': 'prepare'})
    pending = await current.formal.execute(command)
    assert pending['status'] == 'pending'
    assert current.formal.steps.step.await_count == 16
    row = await current.formal.store.get(command.command_id)
    assert row['leaseGeneration'] == 2 and row['leaseOwner'] is None
    assert row['transportFailures'] == 0 and row['progress'] == {}


@pytest.mark.asyncio
async def test_public_projection_read_is_pure_scoped_and_never_regenerates_missing_material():
    current = await flow()
    command = request(current, operation='report')
    prepared = await current.formal.prepare_report(command)
    current.runtime.reports.prepare = AsyncMock(side_effect=AssertionError('a read must not extract'))
    current.formal.steps.step = AsyncMock(side_effect=AssertionError('a read must not publish'))
    result = await current.formal.read_report_projection(scope=current.scope, projection_ref=prepared.snapshot_id)
    assert result == prepared
    assert not current.formal_records.documents and not current.env.ledger.sources.documents
    with pytest.raises(DocumentNotFoundError):
        await current.formal.read_report_projection(
            scope=DocumentScope('another-user', current.scope.subject_id), projection_ref=prepared.snapshot_id
        )
    with pytest.raises(DocumentNotFoundError):
        await current.formal.read_report_projection(scope=current.scope, projection_ref='missing-projection')
    current.runtime.reports.prepare.assert_not_awaited()
    current.formal.steps.step.assert_not_awaited()
