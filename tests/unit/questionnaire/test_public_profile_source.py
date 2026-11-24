"""Real Profile V2 admission, immutable resolver and governance with offline storage/model."""

from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from adapters.questionnaire_profile_source import create_questionnaire_profile_source
from health_profile.ingestion import HealthFactPolicy
from health_profile.models import FactStatus
from modules.health_profile.application import DurableProfileSourceExtractor
from modules.health_profile.application import MongoProfileSourcePresenceReader
from modules.health_profile.application import ProfileFreshSourceAdmission
from modules.health_profile.application import ProfileUserSourceResolver
from modules.health_profile.contracts import ProfileQuestionnaireSourceSnapshotV2
from modules.health_profile.contracts import ProfileScope
from modules.health_profile.contracts import ProfileSourceSnapshotRefV1
from modules.questionnaire.profile_source import execution_selection
from modules.questionnaire.profile_source import prepare_submission_source
from modules.questionnaire.profile_source import saved_binding
from tests.unit.health_profile.test_multi_source_publications import SELF_TEXT
from tests.unit.health_profile.test_multi_source_publications import candidate
from tests.unit.health_profile.test_public_contracts import setup_flow
from tests.unit.health_profile.test_questionnaire_source_v2 import submitted
from tests.unit.health_profile.test_record_ingestion import _runtime
from tests.unit.health_profile.test_source_admission import Collection as PresenceCollection
from tests.unit.questionnaire.source_fakes import environment
from tests.unit.questionnaire.source_fakes import save
from tests.unit.questionnaire.source_fakes import submission
from tests.unit.questionnaire.source_fakes import task


async def composition():
    env = environment()
    profile = await setup_flow(authorize=False)
    profile.now[0] += timedelta(days=1)
    env.now[0] = profile.now[0].timestamp()
    env.raw = submission(label=SELF_TEXT)
    collections = {
        name: PresenceCollection() for name in ('subjects', 'reports', 'revisions', 'jobs', 'facts', 'publications')
    }
    for name in ('subjects', 'jobs', 'facts', 'publications'):
        collections[name].documents = profile.collections[name].documents
    presence = MongoProfileSourcePresenceReader(**collections, clock=lambda: profile.now[0])
    fresh = ProfileFreshSourceAdmission(
        inbox=profile.inbox, presence_reader=presence, owner_fence_readers={'questionnaire': env.resources.fence_reader}
    )
    profile.applier.resolvers[('questionnaire', 'profile.questionnaire-source.v2')] = ProfileUserSourceResolver(
        env.resources.snapshot_reader
    )
    runtime, gateway = _runtime({'candidates': [candidate(answer_index=1)]})
    profile.applier.extractor = DurableProfileSourceExtractor(runtime.hub, policy_version=HealthFactPolicy.VERSION)
    env.router = create_questionnaire_profile_source(
        resources=env.resources,
        inbox=profile.inbox,
        presence=presence,
        fresh_admission=fresh,
        submitted=env.submitted,
        attachments=env.attachments,
        clock=lambda: env.now[0],
    )
    env.service = env.router.publication
    env.profile, env.fresh, env.gateway, env.presence = profile, fresh, gateway, presence
    return env


async def retry(env):
    env.now[0] += 31
    env.profile.now[0] += timedelta(seconds=31)
    await env.service.recover_pending()
    return await task(env)


@pytest.mark.asyncio
async def test_actual_fresh_v2_resolver_and_apply_keep_one_original_model_call():
    env = await composition()
    await save(env)
    await env.service.ensure_queued(user_id='u', questionnaire_id='q')
    assert not env.profile.collections['publications'].documents
    await env.service.recover_pending()
    current = await task(env)
    assert current['status'] == 'waiting_receipt' and current['authorization']['status'] == 'ready'
    assert current['attachmentStatus'] == 'accepted'
    assert await env.profile.worker.recover_pending() == 1
    assert (await retry(env))['status'] == 'applied'
    assert len(env.gateway.complete_requests) == 1
    receipt = (await task(env))['receipt']
    assert receipt['processingRevision'] == 'profile-questionnaire-source-v2'
    facts = await env.profile.repository.list_facts(
        'u', env.profile.scope.subject_id, statuses=(FactStatus.ACTIVE, FactStatus.PENDING)
    )
    assert len(facts) == 1
    assert not env.profile.collections['jobs'].documents
    assert await env.service.recover_pending() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['reserve_fresh_source', 'authorize_fresh_source', 'receive'])
async def test_actual_provider_ack_loss_replays_same_frozen_authorization_or_event(phase):
    env = await composition()
    await save(env)
    target = env.profile.inbox if phase == 'receive' else env.fresh
    original = getattr(target, phase)
    calls = []

    async def lost_once(*args, **kwargs):
        calls.append(args[0].model_dump(mode='json', by_alias=True))
        result = await original(*args, **kwargs)
        if len(calls) == 1:
            raise RuntimeError('actual provider persisted but acknowledgement was lost')
        return result

    setattr(target, phase, lost_once)
    await env.service.recover_pending()
    assert (await task(env))['status'] == 'retry_wait'
    assert (await retry(env))['status'] == 'waiting_receipt'
    assert calls[0] == calls[1]
    assert await env.profile.worker.recover_pending() == 1
    assert (await retry(env))['status'] == 'applied'
    assert len(env.gateway.complete_requests) == 1


@pytest.mark.asyncio
async def test_maximum_pointer_and_session_are_accepted_by_real_public_v2_after_chunk_roundtrip():
    env = environment()
    source = submitted(name='~' * 512, session_id='s' * 512)
    source['profileSourceSelection'] = execution_selection()
    prepare_submission_source(source)
    env.raw = source
    await save(env)
    material = await env.service.materials.prepare(saved_binding(source), scope={'userId': 'u', 'subjectId': 'self'})
    snapshot = await env.resources.snapshot_reader.read_source_snapshot(
        ProfileSourceSnapshotRefV1.model_validate(material['event']['snapshotRef']),
        scope=ProfileScope(user_id='u', subject_id='self'),
    )
    parsed = ProfileQuestionnaireSourceSnapshotV2.model_validate(snapshot)
    assert len(parsed.answers[0].source_pointer) == 1039 and parsed.session_id == 's' * 512
    assert parsed.model_dump(mode='json', by_alias=True) == snapshot


@pytest.mark.asyncio
async def test_actual_q_birth_mode_blocks_legacy_sender_before_and_after_fresh_marker():
    from unittest.mock import AsyncMock

    from health_profile.questionnaire_source import QuestionnaireProfileSourceService

    env = await composition()
    await save(env)
    queue = AsyncMock()
    legacy = QuestionnaireProfileSourceService(store=env.submitted.store, job_queue=queue)
    assert await legacy.ensure_queued(user_id='u', questionnaire_id='q') is None
    await env.service.recover_pending()
    assert (await task(env))['authorization']['status'] == 'ready'
    assert await legacy.ensure_queued(user_id='u', questionnaire_id='q') is None
    queue.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_or_changed_fixed_public_material_never_uses_latest_source():
    env = await composition()
    await save(env)
    await env.service.recover_pending()
    current = await task(env)
    snapshot = await env.resources.store.read_blob(current['material']['snapshotReference'])
    before = deepcopy(snapshot)
    env.owned.documents[current['material']['snapshotReference']['blobId'] + ':0']['content'] = b'broken'
    await env.profile.worker.recover_pending()
    receipt = await env.profile.inbox.get_receipt(current['material']['event']['eventId'], scope=env.profile.scope)
    assert receipt.status in {'failed', 'retry_wait'} and not env.gateway.complete_requests
    assert env.source.documents['q']['rawAnswers'] == env.raw['rawAnswers']
    assert before['answers'][1]['answer'] == SELF_TEXT


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['queued', 'processing', 'completed', 'failed', 'dead_letter'])
async def test_actual_all_history_presence_rejects_old_null_subject_questionnaire_jobs(status):
    env = await composition()
    await save(env)
    env.profile.collections['jobs'].documents.append(
        {
            '_id': 'old',
            'userId': 'u',
            'subjectId': None,
            'sourceType': 'questionnaire',
            'sourceId': 'q',
            'status': status,
        }
    )
    await env.service.recover_pending()
    assert (await task(env))['status'] == 'failed'
    assert not env.profile.collections['publications'].documents
    assert env.attachments.prepared == [] and env.gateway.complete_requests == []
    assert env.source.documents['q']['status'] == 'submitted'


@pytest.mark.asyncio
async def test_actual_relative_fact_contribution_also_blocks_fresh_admission():
    env = await composition()
    await save(env)
    env.profile.collections['facts'].documents.append(
        {
            '_id': 'relative',
            'userId': 'u',
            'subjectId': 'father',
            'status': 'deleted',
            'sources': [{'sourceType': 'questionnaire', 'sourceId': 'q'}],
        }
    )
    await env.service.recover_pending()
    assert (await task(env))['status'] == 'failed' and env.attachments.prepared == []
    assert not env.profile.collections['publications'].documents


@pytest.mark.asyncio
async def test_ready_reservation_replay_reuses_original_proof_after_own_publication_exists():
    env = await composition()
    await save(env)
    await env.service.recover_pending()
    current = await task(env)
    row = env.owned.documents[current['_id']]
    for field in ('reservation', 'authorization', 'receipt'):
        row.pop(field)
    env.presence.read_legacy_presence = AsyncMock(
        side_effect=AssertionError('ready replay cannot require current absence')
    )
    result = await retry(env)
    assert result['reservation']['status'] == 'ready' and result['status'] == 'waiting_receipt'
    assert (
        len([item for item in env.profile.collections['publications'].documents if item['recordKind'] == 'publication'])
        == 1
    )
    env.presence.read_legacy_presence.assert_not_awaited()
