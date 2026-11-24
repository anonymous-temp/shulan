"""Actual Profile V2 resolver/admission/inbox and Document command; Q owner ports are fakes."""

from unittest.mock import AsyncMock

import pytest

from health_profile.source_extraction import ProfileUserSourceResolver
from health_profile.source_publications import ProfileSourceConflictError
from modules.analysis.questionnaire_publication_contracts import QuestionnairePublicationUnavailable
from modules.analysis.snapshots import digest
from tests.unit.health_profile.test_source_admission import admission_setup
from tests.unit.services.analysis.test_document_questionnaire_publication import SCOPE
from tests.unit.services.analysis.test_document_questionnaire_publication import environment
from tests.unit.services.analysis.test_document_questionnaire_publication import prepare
from tests.unit.services.analysis.test_document_questionnaire_publication import restart


async def compose():
    env = await environment()
    profile = await admission_setup('questionnaire')
    # This is the explicit fake Q owner fence. Profile presence, reserve/authorize,
    # immutable authorization and get_cutover are the actual public implementations.
    profile.owner.proof['sourceId'] = env.event.source_id
    presence = await profile.reader.read_legacy_presence(
        env.event.scope, source_type='questionnaire', source_id=env.event.source_id
    )
    profile.auth = profile.auth.model_copy(
        update={
            'source_id': env.event.source_id,
            'owner_fence_digest': digest(profile.owner.proof),
            'presence_proof_ref': presence.proof_ref,
            'presence_proof_digest': presence.content_digest,
            'material_contract_version': env.event.snapshot_ref.contract_version,
            'material_snapshot_ref': env.event.snapshot_ref,
            'material_content_digest': env.event.content_digest,
        }
    )
    material = await ProfileUserSourceResolver(env.raw_reader).resolve(env.event)
    assert material.snapshot == env.raw and digest(material.snapshot) == env.event.content_digest
    assert material.snapshot['submissionProof'] == env.submission['payload']['submissionProof']
    env.profile = profile
    env.inbox = profile.inbox
    env.publisher = restart(env)
    return env


@pytest.mark.asyncio
@pytest.mark.parametrize('queue_available', [False, True])
async def test_actual_blocked_then_ready_allows_durable_attachments_before_answer_acceptance(
    monkeypatch, queue_available
):
    env = await compose()
    prepared = await prepare(env)
    state = await env.profile.admission.reserve_fresh_source(env.profile.auth, scope=env.event.scope)
    assert state.status == 'blocked'
    with pytest.raises(QuestionnairePublicationUnavailable, match='not ready'):
        await env.publisher.publish(scope=SCOPE, command_id=prepared['commandId'])
    assert not env.flow.collection.documents and not env.flow.jobs.jobs
    with pytest.raises(ProfileSourceConflictError, match='ready'):
        await env.inbox.receive(env.event, scope=env.event.scope)

    state = await env.profile.admission.authorize_fresh_source(env.profile.auth, scope=env.event.scope)
    assert state.status == 'ready'
    if not queue_available:
        monkeypatch.setattr(env.flow.source._job_queue, 'enqueue', AsyncMock(side_effect=RuntimeError('offline queue')))
    accepted = await env.publisher.publish(scope=SCOPE, command_id=prepared['commandId'])
    assert accepted['status'] == 'accepted' and len(env.flow.collection.documents) == 2
    assert {item['ingestionStatus'] for item in accepted['items']} == ({'queued'} if queue_available else {'pending'})
    assert all(type(item['legacySourceRevision']) is int for item in accepted['items'])
    # The independent answer event is only accepted here; neither contract claims
    # that pending/queued attachment records or the answer have been applied.
    receipt = await env.inbox.receive(env.event, scope=env.event.scope)
    assert receipt.status == 'received' and receipt.watermark.applied_publication_revision == 0
    assert not env.profile.collections['facts'].documents

    # Ready replays the original proof after a formal answer event now exists.
    env.profile.owner.held = False
    env.profile.reader._collections['jobs'].fail = True
    replayed = await env.profile.admission.authorize_fresh_source(env.profile.auth, scope=env.event.scope)
    assert replayed == state
    monkeypatch.setattr(env.inbox, 'get_cutover', AsyncMock(side_effect=AssertionError('receipt is a pure replay')))
    monkeypatch.setattr(env.raw_reader, 'read_source_snapshot', AsyncMock(side_effect=AssertionError('no raw re-read')))
    assert await restart(env).publish(scope=SCOPE, command_id=prepared['commandId']) == accepted


@pytest.mark.asyncio
async def test_actual_old_questionnaire_history_prevents_ready_and_attachment_writes():
    env = await compose()
    env.profile.collections['jobs'].documents.append(
        {'_id': 'old', 'userId': 'u', 'sourceType': 'questionnaire', 'sourceId': 'q', 'status': 'completed'}
    )
    with pytest.raises(ProfileSourceConflictError):
        await env.profile.admission.reserve_fresh_source(env.profile.auth, scope=env.event.scope)
    assert await env.inbox.get_cutover(env.event.scope, 'questionnaire', 'q') is None
    prepared = await prepare(env)
    with pytest.raises(QuestionnairePublicationUnavailable):
        await env.publisher.publish(scope=SCOPE, command_id=prepared['commandId'])
    assert not env.flow.collection.documents and not env.flow.jobs.jobs


@pytest.mark.asyncio
async def test_actual_ready_and_attachment_ack_loss_recover_fixed_authorization_and_command(monkeypatch):
    env = await compose()
    await env.profile.admission.reserve_fresh_source(env.profile.auth, scope=env.event.scope)

    async def ready_lost():
        await env.profile.admission.authorize_fresh_source(env.profile.auth, scope=env.event.scope)
        raise RuntimeError('ready ACK lost')

    with pytest.raises(RuntimeError, match='ready ACK'):
        await ready_lost()
    prepared = await prepare(env)
    save = env.publisher.legacy_publication.save
    once = [True]

    async def save_lost(**kwargs):
        result = await save(**kwargs)
        if once[0]:
            once[0] = False
            raise RuntimeError('attachment ACK lost')
        return result

    monkeypatch.setattr(env.publisher.legacy_publication, 'save', save_lost)
    with pytest.raises(RuntimeError, match='attachment ACK'):
        await env.publisher.publish(scope=SCOPE, command_id=prepared['commandId'])
    assert len(env.flow.jobs.jobs) == 1
    assert await env.publisher.read_receipt(scope=SCOPE, command_id=prepared['commandId']) is None
    assert (
        await env.profile.admission.authorize_fresh_source(env.profile.auth, scope=env.event.scope)
    ).status == 'ready'
    result = await restart(env).publish(scope=SCOPE, command_id=prepared['commandId'])
    assert result['status'] == 'accepted' and len(env.flow.jobs.jobs) == 2
    assert (await env.inbox.receive(env.event, scope=env.event.scope)).status == 'received'
