"""Actual Q submitted reader and legacy sink; Q v2/ready ports are explicit fakes."""

import asyncio
from copy import deepcopy
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.analysis.questionnaire_publication import create_document_questionnaire_attachment_publisher
from modules.analysis.publication import PublicationModeError
from modules.analysis.questionnaire_publication_contracts import QUESTIONNAIRE_PUBLICATION_SELECTION
from modules.analysis.questionnaire_publication_contracts import QuestionnairePublicationConflict
from modules.analysis.questionnaire_publication_contracts import QuestionnairePublicationUnavailable
from modules.analysis.snapshots import DocumentScope
from modules.analysis.snapshots import digest
from modules.health_profile.contracts import ProfileFreshSourceAuthorizationV1
from modules.health_profile.contracts import ProfileFreshSourceStateV1
from modules.health_profile.contracts import ProfileScope
from modules.health_profile.contracts import ProfileSourceEventV1
from modules.health_profile.contracts import ProfileSourceSnapshotRefV1
from modules.questionnaire.application import SUBMITTED_MATERIAL_SELECTION_V1
from modules.questionnaire.application import QuestionnaireSubmittedMaterialReader
from tests.unit.services.analysis.test_document_compatible_publication import environment as legacy_environment
from tests.unit.services.analysis.test_document_fresh_profile_composition import options
from tests.unit.services.analysis.test_document_material_reader import BsonCollection
from tests.unit.services.analysis.test_document_publication import evidence

SCOPE = DocumentScope('u', 'self')
NOW = datetime(2026, 11, 10, tzinfo=UTC)


async def environment():
    env = legacy_environment()
    env.source = {
        'userId': 'u',
        'questionnaireId': 'q',
        'submissionId': 'q',
        'sessionId': 's',
        'status': 'submitted',
        'sourceRevision': 1,
        'submittedAt': NOW.isoformat(),
        'answerHash': digest({'answer': 'raw'}),
        'rawAnswerHash': digest({'answer': 'raw'}),
        'profileIngestion': {'status': 'pending', 'deliveryMode': 'profile-source-event.v1'},
        'reports': [],
        'artifacts': [],
    }
    env.interpretations = {}
    for index, status in enumerate(('partial', 'complete')):
        artifact_id = f'a{index}'
        result = {
            'reportType': 'blood',
            'processing': {'status': status},
            'observations': [{'itemName': 'glucose', 'resultValue': str(6 + index)}],
        }
        env.interpretations[artifact_id] = result
        env.source['reports'].append({'artifactId': artifact_id, 'interpretation': result})
        env.source['artifacts'].append(
            {
                'artifactId': artifact_id,
                'questionnaireId': 'q',
                'fileIndex': index,
                'kind': 'questionnaire_document',
                **({'contentSha256': 'sha'} if index == 0 else {}),
            }
        )

    async def archived(*, user_id, artifact_id):
        return {
            'userId': user_id,
            'questionnaireId': 'q',
            'retained': True,
            'contentSha256': 'sha' if artifact_id == 'a0' else 'later-sha',
        }

    env.archive = SimpleNamespace(
        get=AsyncMock(side_effect=archived),
        read_payload=AsyncMock(side_effect=lambda artifact_id, *args: deepcopy(env.interpretations[artifact_id])),
    )
    env.submitted = QuestionnaireSubmittedMaterialReader(
        store=SimpleNamespace(get_submitted=AsyncMock(side_effect=lambda *args: deepcopy(env.source))),
        archive=env.archive,
    )
    env.submission = await env.submitted.read_submission(
        user_id='u', questionnaire_id='q', source_revision=1, selection=SUBMITTED_MATERIAL_SELECTION_V1
    )
    env.raw = {
        'contractVersion': 'profile.questionnaire-source.v2',
        'scope': SCOPE.as_dict(),
        'sourceType': 'questionnaire',
        'sourceId': 'q',
        'sourceRevision': 1,
        'sessionId': 's',
        'status': 'submitted',
        'submittedAt': NOW.isoformat(),
        'answers': [],
        'submissionProof': env.submission['payload']['submissionProof'],
    }
    env.event = ProfileSourceEventV1(
        event_id='qevent',
        scope=ProfileScope(user_id='u', subject_id='self'),
        source_type='questionnaire',
        source_id='q',
        source_revision=1,
        publication_revision=1,
        event_kind='publish',
        occurred_at=NOW,
        content_digest=digest(env.raw),
        snapshot_ref=ProfileSourceSnapshotRefV1(
            provider='questionnaire', contract_version='profile.questionnaire-source.v2', snapshot_id='qraw'
        ),
    )
    env.ready = ProfileFreshSourceStateV1(
        authorization=ProfileFreshSourceAuthorizationV1(
            scope=env.event.scope,
            source_type='questionnaire',
            source_id='q',
            authorization_id='q-auth',
            owner_fence_ref=ProfileSourceSnapshotRefV1(
                provider='questionnaire', contract_version='profile-owner-admission-fence.v1', snapshot_id='q-fence'
            ),
            owner_fence_digest='a' * 64,
            presence_proof_ref='presence',
            presence_proof_digest='b' * 64,
            material_contract_version=env.event.snapshot_ref.contract_version,
            material_snapshot_ref=env.event.snapshot_ref,
            material_content_digest=env.event.content_digest,
            authorized_at=NOW,
        ),
        status='ready',
        updated_at=NOW,
    )
    env.raw_reader = SimpleNamespace(read_source_snapshot=AsyncMock(side_effect=lambda *a, **k: deepcopy(env.raw)))
    env.inbox = SimpleNamespace(get_cutover=AsyncMock(side_effect=lambda *a: env.ready))
    env.commands = options(BsonCollection())
    env.publisher = restart(env)
    return env


def restart(env):
    return create_document_questionnaire_attachment_publisher(
        submitted_reader=env.submitted,
        source_reader=env.raw_reader,
        profile_inbox=env.inbox,
        health_report_source=env.flow.source,
        publication_ledger=env.ledger,
        command_records=env.commands,
    )


async def prepare(env, **overrides):
    return await env.publisher.prepare(
        **{
            'scope': SCOPE,
            'request_id': 'q-request',
            'submitted_reference': env.submission['reference'],
            'submitted_digest': env.submission['contentDigest'],
            'questionnaire_event': env.event.model_dump(mode='json', by_alias=True),
            'selection': QUESTIONNAIRE_PUBLICATION_SELECTION,
            **overrides,
        }
    )


@pytest.mark.asyncio
async def test_real_old_processor_and_new_command_submit_identical_ordered_legacy_arguments(monkeypatch):
    from health_profile.models import ProfileIngestionSourceType
    from health_profile.questionnaire_ingestion import QuestionnaireProfileProcessor
    from tests.unit.health_profile.test_record_ingestion import FakeRecordProfileService
    from tests.unit.health_profile.test_record_ingestion import RecordFixture
    from tests.unit.health_profile.test_report_ingestion import FakeProfileRepository
    from tests.unit.health_profile.test_report_ingestion import ReportIngestionFixture

    env = await environment()
    env.source['artifacts'].insert(1, {**env.source['artifacts'][0], 'artifactId': 'duplicate'})
    env.source['reports'].append({'artifactId': 'duplicate', 'interpretation': env.interpretations['a0']})
    await refresh_materials(env)
    subject = ReportIngestionFixture.subject().model_copy(update={'user_id': 'u', 'subject_id': 'self'})
    repository = FakeProfileRepository(subject)
    old_sink = SimpleNamespace(save=AsyncMock())
    processor = QuestionnaireProfileProcessor(
        store=SimpleNamespace(get_submitted=AsyncMock(return_value=deepcopy(env.source))),
        repository=repository,
        profile_service=FakeRecordProfileService(subject, repository=repository),
        agent_hub=None,
        documents=SimpleNamespace(
            read_artifact=AsyncMock(
                side_effect=lambda **kwargs: deepcopy(env.interpretations[kwargs['artifact']['artifactId']])
            )
        ),
        report_source=old_sink,
        answer_extractor=SimpleNamespace(VERSION='offline', extract_answer_rows=AsyncMock(return_value=())),
    )
    job = RecordFixture.job().model_copy(
        update={
            'source_type': ProfileIngestionSourceType.QUESTIONNAIRE,
            'source_id': 'q',
            'source_revision': 1,
            'user_id': 'u',
            'subject_id': 'self',
        }
    )
    await processor.process(job)
    actual_sink = AsyncMock(wraps=env.publisher.legacy_publication.save)
    monkeypatch.setattr(env.publisher.legacy_publication, 'save', actual_sink)
    prepared = await prepare(env)
    await env.publisher.publish(scope=SCOPE, command_id=prepared['commandId'])
    assert [call.kwargs for call in actual_sink.await_args_list] == [
        call.kwargs for call in old_sink.save.await_args_list
    ]
    assert len(actual_sink.await_args_list) == 2
    assert actual_sink.await_args_list[0].kwargs['result']['processing']['status'] == 'partial'


@pytest.mark.asyncio
async def test_actual_legacy_identity_partial_content_queue_and_pure_replay(monkeypatch):
    env = await environment()
    prepared = await prepare(env)
    assert [x['contentKey'] for x in prepared['items']] == ['sha', 'a1']
    assert not env.flow.collection.documents and not env.flow.jobs.jobs
    receipt = await env.publisher.publish(scope=SCOPE, command_id=prepared['commandId'])
    assert receipt['status'] == 'accepted' and len(env.flow.jobs.jobs) == 2
    assert all(x['ingestionStatus'] == 'queued' and x['ingestionJobId'] for x in receipt['items'])
    assert all(x['legacySourceRevision'] == 1 for x in receipt['items'])
    first = await env.flow.reports.get_report(prepared['items'][0]['reportId'])
    assert first.result['processing']['status'] == 'partial'
    assert first.document_id is None and first.canonical_report_id is None
    assert not env.flow.client.submit_calls and not env.flow.graph.calls
    for target, name in (
        (env.submitted, 'read_snapshot'),
        (env.raw_reader, 'read_source_snapshot'),
        (env.inbox, 'get_cutover'),
        (env.flow.source, 'save'),
    ):
        monkeypatch.setattr(target, name, AsyncMock(side_effect=AssertionError('receipt replay must be pure')))
    env.publisher = restart(env)
    assert await env.publisher.publish(scope=SCOPE, command_id=prepared['commandId']) == receipt
    assert await env.publisher.read_receipt(scope=SCOPE, command_id=prepared['commandId']) == receipt


@pytest.mark.asyncio
@pytest.mark.parametrize('which', ['raw_proof', 'raw_digest', 'submitted_digest', 'old_mode', 'version'])
async def test_mismatched_fixed_sources_fail_before_formal_save(which):
    env = await environment()
    kwargs = {}
    if which == 'raw_proof':
        env.raw['submissionProof'] = {**env.raw['submissionProof'], 'answerHash': 'c' * 64}
        env.event = env.event.model_copy(update={'content_digest': digest(env.raw)})
    elif which == 'raw_digest':
        env.raw['answers'] = ['unexpected']
    elif which == 'submitted_digest':
        kwargs['submitted_digest'] = 'c' * 64
    elif which == 'old_mode':
        env.source['profileIngestion']['deliveryMode'] = 'legacy'
    else:
        kwargs['questionnaire_event'] = {
            **env.event.model_dump(mode='json', by_alias=True),
            'snapshotRef': {
                'provider': 'questionnaire',
                'contractVersion': 'profile.questionnaire-source.v1',
                'snapshotId': 'qraw',
            },
        }
    with pytest.raises(ValueError):
        await prepare(env, **kwargs)
    assert not env.flow.collection.documents and not env.commands.documents


@pytest.mark.asyncio
@pytest.mark.parametrize('state', ['missing', 'blocked', 'wrong_digest', 'wrong_source'])
async def test_real_ready_lookup_is_required_before_attachment_save(state):
    env = await environment()
    prepared = await prepare(env)
    if state == 'missing':
        env.ready = None
    elif state == 'blocked':
        env.ready = env.ready.model_copy(update={'status': 'blocked'})
    else:
        field, value = ('material_content_digest', 'c' * 64) if state == 'wrong_digest' else ('source_id', 'other')
        env.ready = env.ready.model_copy(
            update={'authorization': env.ready.authorization.model_copy(update={field: value})}
        )
    with pytest.raises((QuestionnairePublicationConflict, QuestionnairePublicationUnavailable)):
        await env.publisher.publish(scope=SCOPE, command_id=prepared['commandId'])
    assert not env.flow.collection.documents


@pytest.mark.asyncio
async def test_all_materials_are_revalidated_before_the_first_save():
    env = await environment()
    prepared = await prepare(env)
    env.interpretations['a1'] = {'changed': 'after preparation'}
    with pytest.raises(ValueError):
        await env.publisher.publish(scope=SCOPE, command_id=prepared['commandId'])
    assert not env.flow.collection.documents and not env.flow.jobs.jobs


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['save', 'item_ack', 'final_ack'])
async def test_lost_ack_recovers_same_sources_without_duplicate_queue(phase, monkeypatch):
    env = await environment()
    prepared = await prepare(env)
    failed = [False]
    if phase == 'save':
        target, name = env.flow.source, 'save'
    else:
        target, name = env.publisher.records, 'put_acknowledgement' if phase == 'item_ack' else 'put'
    actual = getattr(target, name)

    async def lost(*args, **kwargs):
        result = await actual(*args, **kwargs)
        eligible = phase != 'final_ack' or args[0] == 'questionnaire_receipt'
        if eligible and not failed[0]:
            failed[0] = True
            raise RuntimeError('ACK lost')
        return result

    monkeypatch.setattr(target, name, lost)
    with pytest.raises(RuntimeError, match='ACK lost'):
        await env.publisher.publish(scope=SCOPE, command_id=prepared['commandId'])
    env.publisher = restart(env)
    receipt = await env.publisher.publish(scope=SCOPE, command_id=prepared['commandId'])
    assert receipt['status'] == 'accepted' and len(env.flow.jobs.jobs) == 2
    assert len(env.flow.collection.documents) == 2


@pytest.mark.asyncio
async def test_pending_is_durably_accepted_but_never_claimed_queued(monkeypatch):
    env = await environment()
    prepared = await prepare(env)
    monkeypatch.setattr(env.flow.source._job_queue, 'enqueue', AsyncMock(side_effect=RuntimeError('offline queue')))
    receipt = await env.publisher.publish(scope=SCOPE, command_id=prepared['commandId'])
    assert receipt['status'] == 'accepted'
    assert all(x['ingestionStatus'] == 'pending' and x['ingestionJobId'] is None for x in receipt['items'])
    assert len(env.flow.collection.documents) == 2 and not env.flow.jobs.jobs


@pytest.mark.asyncio
async def test_existing_legacy_first_result_is_preserved_instead_of_creating_revision():
    env = await environment()
    prepared = await prepare(env)
    item = prepared['items'][0]
    old = await env.flow.source.save(
        report_id=item['reportId'],
        user_id='u',
        subject_id='self',
        source_fingerprint=item['sourceFingerprint'],
        result={'legacy': 'first'},
    )
    receipt = await env.publisher.publish(scope=SCOPE, command_id=prepared['commandId'])
    assert receipt['items'][0]['storedResultDigest'] == digest(old.record.result)
    assert receipt['items'][0]['inputInterpretationDigest'] != digest(old.record.result)
    assert not env.flow.reports._revisions.documents and len(env.flow.jobs.jobs) == 2


@pytest.mark.asyncio
async def test_concurrent_commands_and_changed_request_are_idempotent():
    env = await environment()
    prepared = await prepare(env)
    results = await asyncio.gather(
        *(env.publisher.publish(scope=SCOPE, command_id=prepared['commandId']) for _ in range(3))
    )
    assert results[0] == results[1] == results[2] and len(env.flow.jobs.jobs) == 2
    with pytest.raises(QuestionnairePublicationConflict, match='different inputs'):
        await prepare(env, questionnaire_event={**env.event.model_dump(mode='json', by_alias=True), 'eventId': 'other'})


async def refresh_materials(env):
    env.submission = await env.submitted.read_submission(
        user_id='u', questionnaire_id='q', source_revision=1, selection=SUBMITTED_MATERIAL_SELECTION_V1
    )
    env.raw['submissionProof'] = env.submission['payload']['submissionProof']
    env.event = env.event.model_copy(update={'content_digest': digest(env.raw)})
    env.ready = env.ready.model_copy(
        update={
            'authorization': env.ready.authorization.model_copy(
                update={'material_content_digest': env.event.content_digest}
            )
        }
    )


@pytest.mark.asyncio
async def test_excluded_duplicates_and_document_references_keep_first_attachment_order():
    env = await environment()
    original = env.source['artifacts'][0]
    env.source['artifacts'] += [
        {**original, 'artifactId': 'duplicate', 'fileIndex': 2},
        {**original, 'artifactId': 'reference', 'fileIndex': 3, 'kind': 'document_reference'},
        {**original, 'artifactId': 'failed', 'fileIndex': 4},
    ]
    env.source['reports'] += [
        {'artifactId': 'duplicate', 'interpretation': env.interpretations['a0']},
        {'artifactId': 'reference', 'interpretation': env.interpretations['a0']},
        {'artifactId': 'failed', 'status': 'unavailable'},
    ]
    await refresh_materials(env)
    prepared = await prepare(env)
    receipt = await env.publisher.publish(scope=SCOPE, command_id=prepared['commandId'])
    assert [x['artifactId'] for x in receipt['items']] == ['a0', 'a1']
    assert [x['reason'] for x in receipt['excluded']] == [
        'duplicate_content',
        'document_reference',
        'no_interpretation',
    ]
    assert len(env.flow.jobs.jobs) == 2


@pytest.mark.asyncio
async def test_empty_attachments_still_require_real_questionnaire_ready():
    env = await environment()
    env.source['artifacts'] = env.source['reports'] = []
    await refresh_materials(env)
    prepared = await prepare(env)
    env.ready = env.ready.model_copy(update={'status': 'blocked'})
    with pytest.raises(QuestionnairePublicationUnavailable):
        await env.publisher.publish(scope=SCOPE, command_id=prepared['commandId'])
    env.ready = env.ready.model_copy(update={'status': 'ready'})
    assert (await env.publisher.publish(scope=SCOPE, command_id=prepared['commandId']))['items'] == []
    assert not env.flow.jobs.jobs


@pytest.mark.asyncio
@pytest.mark.parametrize('gate', ['document', 'profile'])
async def test_managed_attachment_gate_never_becomes_a_fake_legacy_acceptance(monkeypatch, gate):
    env = await environment()
    prepared = await prepare(env)
    if gate == 'document':
        await env.ledger.switch_mode(SCOPE, prepared['items'][0]['reportId'], target='events', evidence=evidence())
        expected = PublicationModeError
    else:
        monkeypatch.setattr(env.flow.source._job_queue, 'is_source_publication_managed', AsyncMock(return_value=True))
        expected = QuestionnairePublicationUnavailable
    with pytest.raises(expected):
        await env.publisher.publish(scope=SCOPE, command_id=prepared['commandId'])
    assert await env.publisher.read_receipt(scope=SCOPE, command_id=prepared['commandId']) is None
    assert not env.flow.jobs.jobs


@pytest.mark.asyncio
async def test_cancelled_save_retains_uncertain_ticket_and_resumes_same_command(monkeypatch):
    env = await environment()
    prepared = await prepare(env)
    original = env.flow.source.save
    first = [True]

    async def cancelled(**kwargs):
        result = await original(**kwargs)
        if first[0]:
            first[0] = False
            raise asyncio.CancelledError()
        return result

    monkeypatch.setattr(env.flow.source, 'save', cancelled)
    with pytest.raises(asyncio.CancelledError):
        await env.publisher.publish(scope=SCOPE, command_id=prepared['commandId'])
    source = await env.ledger.read_source(SCOPE, prepared['items'][0]['reportId'])
    assert source['legacyTickets']
    assert (await env.publisher.publish(scope=SCOPE, command_id=prepared['commandId']))['status'] == 'accepted'
    assert len(env.flow.jobs.jobs) == 2
    assert (await env.ledger.read_source(SCOPE, prepared['items'][0]['reportId']))['legacyTickets']


@pytest.mark.asyncio
async def test_factory_is_pure_and_scope_cannot_read_other_commands(monkeypatch):
    env = await environment()
    prepared = await prepare(env)
    for target, method in (
        (env.submitted, 'read_snapshot'),
        (env.raw_reader, 'read_source_snapshot'),
        (env.inbox, 'get_cutover'),
        (env.flow.source, 'save'),
        (env.commands, 'insert_one'),
    ):
        monkeypatch.setattr(target, method, AsyncMock(side_effect=AssertionError('construction must perform no IO')))
    env.publisher = restart(env)
    with pytest.raises(LookupError):
        await env.publisher.read_receipt(scope=DocumentScope('other', 'self'), command_id=prepared['commandId'])
