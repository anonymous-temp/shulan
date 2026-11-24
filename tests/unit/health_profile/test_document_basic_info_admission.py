"""Current Document intake, with real durable Profile state and synthetic readers."""

from copy import deepcopy

import pytest

from health_profile.models import FactStatus
from health_profile.models import SubjectStatus
from health_profile.source_material import ProfileSourceApplicationError
from health_profile.source_publications import ProfileSourceConflictError
from health_profile.source_publications import publication_id
from health_profile.source_publications import source_key

from .test_document_source_v2_resolver import event_for
from .test_document_source_v2_resolver import rebuild
from .test_document_source_v2_resolver import setup_document_current


async def binding(flow):
    return await flow.inbox.store.get(
        'document_binding_' + source_key(flow.scope, 'health_report', flow.event.source_id)
    )


async def cycle(flow):
    for status in (SubjectStatus.ARCHIVED, SubjectStatus.ACTIVE):
        subject = await flow.repository.get_subject(flow.scope.user_id, flow.scope.subject_id)
        await flow.repository.update_subject(
            subject.model_copy(update={'status': status, 'version': subject.version + 1, 'updated_at': flow.now[0]}),
            expected_version=subject.version,
        )


@pytest.mark.asyncio
async def test_current_receive_validates_then_binds_and_freezes_original_epoch_without_migration_markers():
    flow = await setup_document_current()
    receipt = await flow.inbox.receive(flow.event, scope=flow.scope)
    assert receipt.processing_revision == 'profile-document-source-v2'
    saved = await binding(flow)
    assert saved['lifecycleEpoch'] == 1 and saved['materialContract'] == 'document.source-observations.v2'
    assert saved['firstSnapshotRef'] == flow.event.snapshot_ref.model_dump(by_alias=True)
    alias = await flow.inbox.store.get(flow.inbox.alias_id(flow.scope, flow.event.event_id))
    publication = await flow.inbox.store.get(publication_id(flow.event))
    assert alias['lifecycleEpoch'] == publication['lifecycleEpoch'] == saved['lifecycleEpoch']
    assert alias['processingSelection'] == publication['processingSelection']
    assert alias['currentDocument'] and publication['currentDocument']
    assert not any(row['recordKind'] == 'cutover' for row in flow.collections['publications'].documents)
    assert not flow.collections['facts'].documents


@pytest.mark.asyncio
async def test_accepted_ack_replays_fixed_binding_without_a_live_reader():
    flow = await setup_document_current()
    first = await flow.inbox.receive(flow.event, scope=flow.scope)
    calls = flow.public_reader.read_source_observations.await_count
    flow.inbox.current_document_source = None
    second = await flow.inbox.receive(flow.event, scope=flow.scope)
    assert first.model_dump(mode='json') == second.model_dump(mode='json')
    assert flow.public_reader.read_source_observations.await_count == calls
    later = flow.event.model_copy(
        update={
            'event_id': 'test-next',
            'publication_revision': 2,
            'prior_revision': 1,
            'event_kind': 'correct',
        }
    )
    with pytest.raises(ProfileSourceConflictError, match='receiver_not_ready'):
        await flow.inbox.receive(later, scope=flow.scope)


@pytest.mark.asyncio
async def test_durable_alias_recovers_the_exact_binding_without_a_reader(monkeypatch):
    flow = await setup_document_current()
    insert = flow.inbox.store.insert

    async def interrupt(document):
        if document['recordKind'] == 'publication':
            raise OSError('synthetic interruption after alias')
        return await insert(document)

    monkeypatch.setattr(flow.inbox.store, 'insert', interrupt)
    with pytest.raises(OSError):
        await flow.inbox.receive(flow.event, scope=flow.scope)
    alias = await flow.inbox.store.get(flow.inbox.alias_id(flow.scope, flow.event.event_id))
    monkeypatch.setattr(flow.inbox.store, 'insert', insert)
    flow.inbox.current_document_source = None
    await flow.inbox.receive(flow.event, scope=flow.scope)
    saved = await flow.inbox.store.get(publication_id(flow.event))
    assert saved['processingSelection'] == alias['processingSelection']
    assert saved['lifecycleEpoch'] == alias['lifecycleEpoch']


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'changed',
    [
        'missing_epoch',
        'zero_epoch',
        'bool_epoch',
        'inventory_epoch',
        'foreign_epoch',
        'scope',
        'plan',
        'digest',
        'provider',
        'contract',
    ],
)
async def test_invalid_current_material_leaves_no_binding_and_a_correct_retry_can_succeed(changed):
    flow = await setup_document_current()
    raw, inventory, event = deepcopy(flow.raw), deepcopy(flow.inventory), flow.event
    coverage = flow.raw['payload']['coverage']
    if changed == 'missing_epoch':
        del coverage['lifecycleEpoch']
    elif changed == 'zero_epoch':
        coverage['lifecycleEpoch'] = 0
    elif changed == 'bool_epoch':
        coverage['lifecycleEpoch'] = True
    elif changed == 'inventory_epoch':
        flow.inventory['payload']['lifecycleEpoch'] += 1
    elif changed == 'foreign_epoch':
        coverage['lifecycleEpoch'] += 2
        flow.inventory['payload']['lifecycleEpoch'] += 2
    elif changed == 'scope':
        flow.raw['scope']['subjectId'] = 'test-other-subject'
    elif changed == 'plan':
        flow.inventory['payload']['fence']['evidenceRef'] = 'test-another-plan'
    rebuild(flow)
    bad = flow.event
    if changed == 'digest':
        flow.materials[bad.snapshot_ref.snapshot_id]['payload']['quality']['details'] = {'tampered': True}
    elif changed in {'provider', 'contract'}:
        ref = bad.snapshot_ref.model_copy(update={changed if changed == 'provider' else 'contract_version': 'invalid'})
        bad = bad.model_copy(update={'snapshot_ref': ref})
    with pytest.raises((ProfileSourceConflictError, ProfileSourceApplicationError)):
        await flow.inbox.receive(bad, scope=flow.scope)
    assert not flow.collections['publications'].documents
    assert not flow.collections['facts'].documents
    flow.raw, flow.inventory = raw, inventory
    rebuild(flow)
    assert flow.event == event
    await flow.inbox.receive(flow.event, scope=flow.scope)
    assert (await binding(flow))['lifecycleEpoch'] == 1


@pytest.mark.asyncio
async def test_source_binding_is_not_a_grant_for_one_snapshot_or_plan():
    flow = await setup_document_current()
    await flow.inbox.receive(flow.event, scope=flow.scope)
    original = await binding(flow)
    flow.inventory['payload']['activeSetEvidenceRef'] = 'test-next-managed-plan'
    flow.inventory['payload']['fence']['evidenceRef'] = 'test-next-managed-plan'
    rebuild(flow, revision=2)
    receipt = await flow.inbox.receive(flow.event, scope=flow.scope)
    assert receipt.watermark.accepted_publication_revision == 2
    assert await binding(flow) == original
    assert flow.event.content_digest != original['firstContentDigest']


@pytest.mark.asyncio
async def test_out_of_order_first_seen_publication_does_not_lock_out_its_predecessor():
    flow = await setup_document_current()
    first = flow.event
    second = event_for(flow, flow.raw, revision=2)
    await flow.inbox.receive(second, scope=flow.scope)
    assert (await flow.inbox.receive(first, scope=flow.scope)).watermark.accepted_publication_revision == 2
    assert await flow.worker.recover_pending() == 1
    assert await flow.worker.recover_pending() == 1
    assert (await flow.inbox.get_receipt(second.event_id, scope=flow.scope)).status == 'applied'


@pytest.mark.asyncio
async def test_retract_first_reads_original_epoch_and_late_publish_cannot_revive_the_source():
    flow = await setup_document_current()
    first = flow.event
    raw = {
        'scope': flow.scope.model_dump(by_alias=True),
        'contractVersion': 'document.retraction.v1',
        'sourceType': 'health_report',
        'sourceId': first.source_id,
        'sourceRevision': 1,
        'publicationRevision': 2,
        'priorRevision': 1,
        'eventKind': 'retract',
        'lifecycleEpoch': 1,
    }
    retract = event_for(flow, raw, retraction=True, revision=2)
    await flow.inbox.receive(retract, scope=flow.scope)
    assert (await binding(flow))['lifecycleEpoch'] == 1
    await flow.inbox.receive(first, scope=flow.scope)
    assert await flow.worker.recover_pending() == 1
    assert await flow.worker.recover_pending() == 1
    assert not await flow.repository.list_facts(
        flow.scope.user_id, flow.scope.subject_id, statuses=(FactStatus.ACTIVE,)
    )
    current = await flow.repository.basic_info.read(flow.scope.user_id, subject_id=flow.scope.subject_id)
    assert current.fields['name'].value is None
    assert (await flow.inbox.get_receipt(retract.event_id, scope=flow.scope)).status == 'applied'


@pytest.mark.asyncio
async def test_retraction_without_original_epoch_does_not_bind_unknown_source_or_block_correct_retry():
    flow = await setup_document_current()
    raw = {
        'scope': flow.scope.model_dump(by_alias=True),
        'contractVersion': 'document.retraction.v1',
        'sourceType': 'health_report',
        'sourceId': flow.event.source_id,
        'sourceRevision': 1,
        'publicationRevision': 2,
        'priorRevision': 1,
        'eventKind': 'retract',
    }
    bad = event_for(flow, raw, retraction=True, revision=2)
    with pytest.raises(ProfileSourceApplicationError, match='lifecycle_epoch_required'):
        await flow.inbox.receive(bad, scope=flow.scope)
    assert not flow.collections['publications'].documents
    raw['lifecycleEpoch'] = 1
    await flow.inbox.receive(event_for(flow, raw, retraction=True, revision=2), scope=flow.scope)
    assert (await binding(flow))['lifecycleEpoch'] == 1


@pytest.mark.asyncio
async def test_bound_source_cannot_rebind_after_archive_and_reactivation():
    flow = await setup_document_current()
    await flow.inbox.receive(flow.event, scope=flow.scope)
    original = await binding(flow)
    await cycle(flow)
    with pytest.raises(ProfileSourceConflictError, match='lifecycle_mismatch'):
        await flow.inbox.receive(flow.event, scope=flow.scope)
    assert await binding(flow) == original
    coverage = flow.raw['payload']['coverage']
    coverage['lifecycleEpoch'] = flow.inventory['payload']['lifecycleEpoch'] = 3
    rebuild(flow, revision=2)
    with pytest.raises(ProfileSourceConflictError, match='lifecycle_mismatch'):
        await flow.inbox.receive(flow.event, scope=flow.scope)
    assert await binding(flow) == original
