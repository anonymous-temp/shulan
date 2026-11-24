"""Real Questionnaire owner, Profile admission/governance and Document attachment command."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.analysis.questionnaire_publication import create_document_questionnaire_attachment_publisher
from adapters.questionnaire_profile_source import QuestionnaireDocumentAttachmentAdapter
from adapters.questionnaire_profile_source import create_questionnaire_profile_source
from modules.questionnaire.profile_source import prepare_submission_source
from tests.unit.questionnaire.source_fakes import Collection
from tests.unit.questionnaire.source_fakes import save
from tests.unit.questionnaire.source_fakes import task
from tests.unit.questionnaire.test_public_profile_source import composition
from tests.unit.questionnaire.test_public_profile_source import retry
from tests.unit.services.analysis.test_document_compatible_publication import environment as legacy_environment


def restart(env):
    env.publisher = create_document_questionnaire_attachment_publisher(
        submitted_reader=env.submitted,
        source_reader=env.resources.snapshot_reader,
        profile_inbox=env.profile.inbox,
        health_report_source=env.documents.flow.source,
        publication_ledger=env.documents.ledger,
        command_records=env.commands,
    )
    env.router = create_questionnaire_profile_source(
        resources=env.resources,
        inbox=env.profile.inbox,
        presence=env.presence,
        fresh_admission=env.fresh,
        submitted=env.submitted,
        attachments=QuestionnaireDocumentAttachmentAdapter(env.publisher),
        clock=lambda: env.now[0],
    )
    env.service = env.router.publication


async def environment():
    env = await composition()
    env.documents, env.commands = legacy_environment(), Collection()
    env.interpretations = {
        'a0': {
            'reportType': 'blood',
            'processing': {'status': 'partial'},
            'observations': [{'itemName': 'glucose', 'resultValue': '6.4', 'unit': 'mmol/L'}],
        },
        'a1': {
            'reportType': 'blood',
            'processing': {'status': 'complete'},
            'observations': [{'itemName': 'glucose', 'resultValue': '5.7', 'unit': 'mmol/L'}],
        },
    }
    env.raw['reports'] = [
        {'artifactId': artifact_id, 'interpretation': deepcopy(result)}
        for artifact_id, result in env.interpretations.items()
    ]
    env.raw['artifacts'] = [
        {
            'artifactId': artifact_id,
            'questionnaireId': 'q',
            'kind': 'questionnaire_document',
            'fileIndex': index,
            **({'contentSha256': 'original-sha'} if index == 0 else {}),
        }
        for index, artifact_id in enumerate(env.interpretations)
    ]
    # A duplicate retains its original position and identity, but is explicitly excluded.
    env.raw['artifacts'].insert(1, {**env.raw['artifacts'][0], 'artifactId': 'duplicate'})
    env.raw['reports'].append({'artifactId': 'duplicate', 'interpretation': deepcopy(env.interpretations['a0'])})
    env.raw['submissionResponse']['reports'] = deepcopy(env.raw['reports'])
    prepare_submission_source(env.raw)

    async def archived(*, user_id, artifact_id):
        return {
            'userId': user_id,
            'questionnaireId': 'q',
            'retained': True,
            'contentSha256': 'original-sha' if artifact_id == 'a0' else 'later-archive-sha',
        }

    env.submitted.archive = SimpleNamespace(
        get=AsyncMock(side_effect=archived),
        read_payload=AsyncMock(side_effect=lambda artifact_id, *args: deepcopy(env.interpretations[artifact_id])),
    )
    restart(env)
    return env


@pytest.mark.asyncio
@pytest.mark.parametrize('queue_available', [False, True])
async def test_actual_ready_attachment_durable_acceptance_and_answer_apply_are_separate(monkeypatch, queue_available):
    env = await environment()
    await save(env)
    if not queue_available:
        monkeypatch.setattr(
            env.documents.flow.source._job_queue,
            'enqueue',
            AsyncMock(side_effect=RuntimeError('offline attachment queue')),
        )
    actual_save = env.publisher.legacy_publication.save
    actual_receive = env.profile.inbox.receive
    order = []

    async def checked_save(**kwargs):
        current = await task(env)
        ready = await env.profile.inbox.get_cutover(env.profile.scope, 'questionnaire', 'q')
        assert ready.status == 'ready' and current.get('receipt') is None
        order.append('attachment')
        return await actual_save(**kwargs)

    async def checked_receive(*args, **kwargs):
        current = await task(env)
        assert current['attachmentStatus'] == 'accepted'
        assert len(current['attachmentReceipt']['items']) == 2
        assert len(env.documents.flow.collection.documents) == 2
        order.append('answer')
        return await actual_receive(*args, **kwargs)

    monkeypatch.setattr(env.publisher.legacy_publication, 'save', checked_save)
    monkeypatch.setattr(env.profile.inbox, 'receive', checked_receive)
    await env.service.ensure_queued(user_id='u', questionnaire_id='q')
    assert not env.documents.flow.collection.documents
    assert not env.profile.collections['publications'].documents
    await env.service.recover_pending()
    current = await task(env)
    assert current['status'] == 'waiting_receipt'
    assert order == ['attachment', 'attachment', 'answer']
    receipt = current['attachmentReceipt']
    assert [item['artifactId'] for item in receipt['items']] == ['a0', 'a1']
    assert [item['contentKey'] for item in current['attachmentCommand']['items']] == ['original-sha', 'a1']
    assert [item['reason'] for item in receipt['excluded']] == ['duplicate_content']
    assert {item['ingestionStatus'] for item in receipt['items']} == ({'queued'} if queue_available else {'pending'})
    assert len(env.documents.flow.jobs.jobs) == (2 if queue_available else 0)
    first = await env.documents.flow.reports.get_report(receipt['items'][0]['reportId'])
    assert first.result == env.interpretations['a0'] and first.result['processing']['status'] == 'partial'
    assert not env.documents.flow.client.submit_calls and not env.documents.flow.graph.calls
    assert current['receipt']['watermark']['appliedPublicationRevision'] == 0
    assert not env.gateway.complete_requests
    assert await env.profile.worker.recover_pending() == 1
    assert (await retry(env))['status'] == 'applied'
    assert len(env.gateway.complete_requests) == 1
    assert (await task(env))['attachmentReceipt'] == receipt
    assert env.source.documents['q']['rawAnswers'] == env.raw['rawAnswers']


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['prepare', 'publish', 'receive'])
async def test_real_cross_domain_ack_loss_restarts_same_fixed_command_and_event(monkeypatch, phase):
    env = await environment()
    await save(env)
    target = env.profile.inbox if phase == 'receive' else env.publisher
    actual = getattr(target, phase)
    calls = []

    async def lost(*args, **kwargs):
        calls.append(deepcopy((args, kwargs)))
        result = await actual(*args, **kwargs)
        if len(calls) == 1:
            raise RuntimeError('persisted acknowledgement was lost')
        return result

    monkeypatch.setattr(target, phase, lost)
    await env.service.recover_pending()
    current = await task(env)
    assert current['status'] == 'retry_wait'
    original = deepcopy(current['material']['event'])
    if phase != 'receive':
        assert not [
            row for row in env.profile.collections['publications'].documents if row['recordKind'] == 'publication'
        ]
    env.submitted = SimpleNamespace(
        read_submission=AsyncMock(side_effect=AssertionError('resume must not capture a new submission')),
        read_snapshot=env.submitted.read_snapshot,
    )
    # New service/provider instances, the same persistent namespaces and exact refs.
    restart(env)
    result = await retry(env)
    assert result['status'] == 'waiting_receipt' and result['material']['event'] == original
    assert len(env.documents.flow.jobs.jobs) == len(env.documents.flow.collection.documents) == 2
    assert (
        len([row for row in env.profile.collections['publications'].documents if row['recordKind'] == 'publication'])
        == 1
    )
    assert await env.profile.worker.recover_pending() == 1
    assert (await retry(env))['status'] == 'applied'
    assert len(env.gateway.complete_requests) == 1
    env.submitted.read_submission.assert_not_awaited()
    assert not env.documents.flow.client.submit_calls and not env.documents.flow.graph.calls
    if phase == 'receive':
        assert calls[0] == calls[1]


@pytest.mark.asyncio
async def test_lost_fixed_attachment_after_prepare_prevents_all_formal_writes(monkeypatch):
    env = await environment()
    await save(env)
    monkeypatch.setattr(env.publisher, 'publish', AsyncMock(side_effect=RuntimeError('not yet dispatched')))
    await env.service.recover_pending()
    current = await task(env)
    assert current['attachmentStatus'] == 'prepared'
    env.interpretations['a1'] = {'different': 'current interpretation is not the fixed source'}
    restart(env)
    result = await retry(env)
    assert result['status'] == 'failed'
    assert not env.documents.flow.collection.documents and not env.documents.flow.jobs.jobs
    assert not [row for row in env.profile.collections['publications'].documents if row['recordKind'] == 'publication']
    assert env.source.documents['q']['status'] == 'submitted'
    assert result['material'] == current['material']
