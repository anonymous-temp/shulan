"""Independent source recovery, exact ACK replay and attachment-before-answer delivery."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from modules.questionnaire.profile_publication import source_task_id
from modules.questionnaire.profile_source import QuestionnaireSourceConflict
from modules.questionnaire.profile_source import QuestionnaireSourcePolicy
from tests.unit.questionnaire.source_fakes import add_attachment
from tests.unit.questionnaire.source_fakes import environment
from tests.unit.questionnaire.source_fakes import save
from tests.unit.questionnaire.source_fakes import submission
from tests.unit.questionnaire.source_fakes import task


async def retry(env):
    env.now[0] += 31
    await env.service.recover_pending()
    return await task(env)


@pytest.mark.asyncio
async def test_saved_business_registration_calls_no_provider_and_restart_without_get_recovers_in_order():
    env = environment()
    await save(env)
    await env.service.ensure_queued(user_id='u', questionnaire_id='q')
    assert env.order == []
    await env.service.recover_pending()
    current = await task(env)
    assert env.order == ['scope', 'reserve', 'ready', 'attachments_prepare', 'attachments_publish', 'answers']
    assert current['status'] == 'waiting_receipt' and current['attachmentStatus'] == 'accepted'
    assert current['receipt']['watermark']['appliedPublicationRevision'] == 0
    assert len(current['material']['snapshotReference']['chunkDigests']) == 1
    assert 'snapshot' not in current['material'] and 'rawAnswers' not in current
    env.receiver.status = 'applied'
    assert (await retry(env))['status'] == 'applied'
    assert len(env.attachments.published) == len(env.receiver.events) == 1
    assert len(env.receiver.lookups) == 1


@pytest.mark.asyncio
async def test_saved_source_scan_repairs_lost_local_registration_without_get():
    env = environment()
    await save(env)
    insert = env.resources.store.insert
    env.resources.store.insert = AsyncMock(side_effect=RuntimeError('local outage'))
    assert await env.service.ensure_queued(user_id='u', questionnaire_id='q') is None
    assert env.order == []
    env.resources.store.insert = insert
    await env.service.recover_pending()
    assert (await task(env))['status'] == 'waiting_receipt'


@pytest.mark.asyncio
async def test_profile_outage_never_runs_attachments_and_keeps_saved_original():
    env = environment()
    await save(env)
    env.admission.failure = True
    await env.service.recover_pending()
    assert (await task(env))['status'] == 'retry_wait'
    assert env.order == ['scope'] and env.source.documents['q']['status'] == 'submitted'
    env.admission.failure = False
    assert (await retry(env))['status'] == 'waiting_receipt'


@pytest.mark.asyncio
async def test_attachment_failure_replays_frozen_command_and_does_not_accept_answers_early():
    env = environment()
    await save(env)
    env.attachments.failure = True
    await env.service.recover_pending()
    current = await task(env)
    assert current['status'] == 'retry_wait' and current['attachmentStatus'] == 'prepared'
    assert env.receiver.events == []
    prepared = deepcopy(env.attachments.prepared)
    env.submitted.read_submission = AsyncMock(side_effect=AssertionError('must not read latest after checkpoint'))
    env.attachments.failure = False
    assert (await retry(env))['attachmentStatus'] == 'accepted'
    assert env.attachments.prepared == prepared and len(env.attachments.published) == 2
    assert env.attachments.published[0] == env.attachments.published[1]
    assert len(env.receiver.events) == 1


@pytest.mark.asyncio
async def test_attachment_ack_loss_replays_exact_command_then_answer_ack_loss_repeats_exact_event():
    env = environment()
    await save(env)
    env.attachments.ack_lost = True
    await env.service.recover_pending()
    assert not env.receiver.events
    env.receiver.ack_lost = True
    await retry(env)
    assert len(env.receiver.events) == 1 and (await task(env)).get('receipt') is None
    assert (await retry(env))['status'] == 'waiting_receipt'
    assert env.receiver.events[0] == env.receiver.events[1]
    assert env.receiver.lookups == [] and len(env.attachments.published) == 2


@pytest.mark.asyncio
async def test_wrong_attachment_submission_proof_fails_before_document_dispatch():
    env = environment()
    await save(env)
    read = env.submitted.read_submission

    async def changed(**kwargs):
        result = await read(**kwargs)
        result['payload']['submissionProof']['rawAnswerHash'] = '0' * 64
        return result

    env.submitted.read_submission = changed
    await env.service.recover_pending()
    assert (await task(env))['status'] == 'failed'
    assert env.attachments.prepared == env.receiver.events == []


@pytest.mark.asyncio
async def test_nonready_authorization_cannot_publish_attachments_or_answers():
    env = environment()
    await save(env)
    env.admission.authorize = AsyncMock(side_effect=lambda value: value)
    await env.service.recover_pending()
    assert (await task(env))['status'] == 'failed'
    assert env.attachments.prepared == env.receiver.events == []


@pytest.mark.asyncio
async def test_raw_change_before_worker_is_rejected_before_scope_creation():
    env = environment()
    await save(env)
    await env.service.register(user_id='u', questionnaire_id='q')
    env.source.documents['q']['rawAnswers']['z_note'] = 'other'
    await env.service.recover_one(source_task_id('u', 'q'))
    assert (await task(env))['status'] == 'failed' and env.order == []


@pytest.mark.asyncio
async def test_fair_discovery_advances_invalid_first_and_wraps_to_new_lower_identity():
    env = environment()
    invalid = submission(questionnaire_id='a')
    invalid['profileIngestion']['deliveryMode'] = 'unknown'
    await env.source.insert_one({'_id': 'a', **invalid})
    await save(env)
    assert await env.service.recover_pending(limit=1) == 0
    assert await env.service.recover_pending(limit=1) == 1
    assert (await task(env))['status'] == 'waiting_receipt'
    newer = submission(questionnaire_id='b')
    await env.source.insert_one({'_id': 'b', **newer})
    env.attachments.command = None
    for _ in range(3):
        await env.service.recover_pending(limit=1)
    assert await task(env, 'b') is not None
    assert any(row.get('recordKind') == 'discovery_error' for row in env.owned.documents.values())


@pytest.mark.asyncio
async def test_process_deaths_consume_frozen_attempt_budget_and_require_explicit_redrive():
    env = environment(QuestionnaireSourcePolicy(max_attempts=2))
    await save(env)
    identity = source_task_id('u', 'q')
    await env.service.register(user_id='u', questionnaire_id='q')
    for i in range(2):
        assert await env.resources.store.claim(identity, now=env.now[0], owner=str(i))
        env.now[0] += 121
    await env.service.recover_one(identity)
    current = await task(env)
    assert current['status'] == 'failed' and current['errorCode'] == 'questionnaire_source_attempts_exhausted'
    assert env.order == []
    await env.service.redrive_transport(identity, reason='operator retry', policy=QuestionnaireSourcePolicy())
    await env.service.recover_one(identity)
    assert (await task(env))['attempts'] == 4 and (await task(env))['status'] == 'waiting_receipt'


@pytest.mark.asyncio
async def test_deadline_across_calls_stops_before_reserve():
    env = environment(QuestionnaireSourcePolicy(max_age_seconds=10))
    await save(env)
    original = env.admission.prepare_self_scope

    async def expired(user_id):
        result = await original(user_id)
        env.now[0] += 11
        return result

    env.admission.prepare_self_scope = expired
    await env.service.recover_pending()
    assert env.order == ['scope']
    assert (await task(env))['errorCode'] == 'questionnaire_source_deadline_exceeded'


@pytest.mark.asyncio
async def test_profile_governance_failure_is_not_transport_redrive():
    env = environment()
    await save(env)
    env.receiver.status = 'failed'
    await env.service.recover_pending()
    with pytest.raises(QuestionnaireSourceConflict):
        await env.service.redrive_transport(
            source_task_id('u', 'q'), reason='retry', policy=QuestionnaireSourcePolicy()
        )


@pytest.mark.asyncio
async def test_late_provider_result_cannot_overwrite_new_lease_owner():
    env = environment()
    await save(env)
    original = env.admission.prepare_self_scope

    async def stale(user_id):
        result = await original(user_id)
        env.now[0] += 121
        current = env.owned.documents[source_task_id('u', 'q')]
        current.update(leaseOwner='new-worker', leaseGeneration=999, leaseExpiresAt=env.now[0] + 120)
        return result

    env.admission.prepare_self_scope = stale
    await env.service.recover_pending()
    current = await task(env)
    assert current['leaseOwner'] == 'new-worker' and 'scope' not in current
    assert env.order == ['scope']


@pytest.mark.asyncio
@pytest.mark.parametrize('status,job_id', [('pending', None), ('queued', 'actual-job')])
async def test_real_durable_pending_attachment_is_sufficient_without_claiming_application(status, job_id):
    env = environment()
    expected_item = add_attachment(env)
    await save(env)
    prepare = env.attachments.prepare
    publish = env.attachments.publish

    async def with_item(**kwargs):
        command = await prepare(**kwargs)
        command['items'] = [{**expected_item, 'reportId': 'report'}]
        env.attachments.command = deepcopy(command)
        return command

    async def with_receipt(**kwargs):
        receipt = await publish(**kwargs)
        receipt['items'] = [
            {
                'artifactId': 'a',
                'reportId': 'report',
                'legacySourceRevision': 9000000000,
                'inputInterpretationDigest': expected_item['interpretationDigest'],
                'storedResultDigest': '2' * 64,
                'ingestionStatus': status,
                'ingestionJobId': job_id,
            }
        ]
        return receipt

    env.attachments.prepare, env.attachments.publish = with_item, with_receipt
    await env.service.recover_pending()
    current = await task(env)
    assert current['attachmentStatus'] == 'accepted' and current['status'] == 'waiting_receipt'
    assert current['attachmentReceipt']['items'][0]['ingestionStatus'] == status
    assert current['receipt']['watermark']['appliedPublicationRevision'] == 0


@pytest.mark.asyncio
async def test_boolean_attachment_revision_is_rejected_before_publish():
    env = environment()
    await save(env)
    prepare = env.attachments.prepare

    async def invalid(**kwargs):
        command = await prepare(**kwargs)
        command['questionnaireSourceRevision'] = True
        return command

    env.attachments.prepare = invalid
    await env.service.recover_pending()
    assert (await task(env))['status'] == 'failed'
    assert env.attachments.published == env.receiver.events == []


@pytest.mark.asyncio
@pytest.mark.parametrize('revision,settled,retracted', [(None, 1, 0), (1, 1, 1), (2, 1, 2), (2, 2, 0), (True, 2, 2)])
async def test_incomplete_retraction_proof_cannot_retire_questionnaire_source(revision, settled, retracted):
    env = environment()
    await save(env)
    original = env.receiver.receipt

    def invalid(event):
        result = original(event)
        result.update(
            status='superseded', applicationState='superseded_by_retraction', supersededByPublicationRevision=revision
        )
        result['watermark'].update(settledPublicationRevision=settled, appliedRetractionRevision=retracted)
        return result

    env.receiver.receipt = invalid
    await env.service.recover_pending()
    current = await task(env)
    assert current['status'] == 'failed' and current.get('receipt') is None
    assert current['errorCode'] == 'questionnaire_source_QuestionnaireSourceConflict'


@pytest.mark.asyncio
async def test_applied_later_retraction_with_complete_settlement_proof_can_retire_source():
    env = environment()
    await save(env)
    original = env.receiver.receipt

    def withdrawn(event):
        result = original(event)
        result.update(
            status='superseded', applicationState='superseded_by_retraction', supersededByPublicationRevision=3
        )
        result['watermark'].update(
            acceptedPublicationRevision=3, settledPublicationRevision=3, appliedRetractionRevision=3
        )
        return result

    env.receiver.receipt = withdrawn
    await env.service.recover_pending()
    assert (await task(env))['status'] == 'superseded'
    env.now[0] += 31
    await env.service.recover_pending()
    assert len(env.receiver.events) == 1 and env.receiver.lookups == []


@pytest.mark.asyncio
async def test_attachment_adapter_only_translates_public_scope_and_freezes_arguments():
    from types import SimpleNamespace

    from adapters.questionnaire_profile_source import QuestionnaireDocumentAttachmentAdapter
    from modules.analysis.snapshots import DocumentScope

    scope = {'userId': 'u', 'subjectId': 'self'}
    reference, event = {'snapshotId': 'fixed'}, {'eventId': 'event'}
    captured = []

    async def prepare(**kwargs):
        scope['userId'] = 'other'
        reference['snapshotId'] = 'changed'
        event['eventId'] = 'changed'
        captured.append(kwargs)
        return {'commandId': 'command'}

    provider = SimpleNamespace(prepare=prepare, publish=AsyncMock(return_value={'status': 'accepted'}))
    adapter = QuestionnaireDocumentAttachmentAdapter(provider)
    assert await adapter.prepare(
        scope=scope,
        request_id='request',
        submitted_reference=reference,
        submitted_digest='1' * 64,
        questionnaire_event=event,
        selection='fixed',
    ) == {'commandId': 'command'}
    assert captured[0]['scope'] == DocumentScope('u', 'self')
    assert captured[0]['submitted_reference'] == {'snapshotId': 'fixed'}
    assert captured[0]['questionnaire_event'] == {'eventId': 'event'}
    await adapter.publish(scope={'userId': 'u', 'subjectId': 'self'}, command_id='command')
    provider.publish.assert_awaited_once_with(scope=DocumentScope('u', 'self'), command_id='command')


@pytest.mark.asyncio
async def test_provider_cannot_silently_drop_a_public_submitted_attachment():
    env = environment()
    add_attachment(env)
    await save(env)
    # The fake's empty command is intentionally inconsistent with the actual public reader.
    await env.service.recover_pending()
    current = await task(env)
    assert current['status'] == 'failed'
    assert len(current['submittedMaterial']['items']) == 1
    assert env.attachments.published == env.receiver.events == []
