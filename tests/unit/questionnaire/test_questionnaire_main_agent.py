"""Whole workflow, explicit legacy versions and background-only recovery."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest
from pymongo import ReturnDocument

from adapters.persistence.questionnaires import MongoQuestionnaireStore
from agents import AgentRequest
from agents import AgentRunContext
from agents.errors import AgentConfigurationError
from agents.errors import AgentProtocolError
from modules.questionnaire.agent import LEGACY_QUESTIONNAIRE_MANIFEST
from modules.questionnaire.agent import QUESTIONNAIRE_MAIN_SELECTION
from modules.questionnaire.agent import install_questionnaire_agent
from modules.questionnaire.agent import questionnaire_agent_manifest
from tests.unit.health_profile.test_profile_read_and_subjects import FakeCollection
from tests.unit.questionnaire.test_factor_composition import composed as composed
from tests.unit.questionnaire.test_factor_tasks import factor_harness as factor_harness
from tests.unit.questionnaire.test_lifecycle import harness as harness


class RecordingMain:
    def __init__(self, delegate):
        self.delegate, self.calls = delegate, []

    async def invoke(self, request, context, *, thread_id=None):
        self.calls.append((deepcopy(dict(request.payload)), context, thread_id))
        return await self.delegate.invoke(request, context, thread_id=thread_id)

    async def stream(self, *args, **kwargs):
        raise AssertionError('Questionnaire uses result queries')
        yield


async def generated(h):
    created = await h.service.create('user', 'session')
    await asyncio.gather(*list(h.service._tasks.values()))
    return await h.store.get_by_id('user', created['questionnaireId'])


@pytest.mark.asyncio
async def test_generation_and_expiration_complete_without_any_get(harness):
    h = harness
    h.service.get = AsyncMock(side_effect=AssertionError('GET must not drive recovery'))
    main = RecordingMain(h.service.agent_port)
    h.service._main_agent = main
    source = await generated(h)
    assert source['status'] == 'ready' and len(main.calls) == 1
    assert source['questionnaireAgentManifest'] == questionnaire_agent_manifest()
    assert source['questionnaireAgentExecution']['runId'] == main.calls[0][1].run_id
    assert source['generationAttempts'] == 1 and not h.leases.owned
    h.now[0] += 7200
    await h.service.recover_pending()
    expired = await h.store.get_by_id('user', source['questionnaireId'])
    assert expired['status'] == 'expired' and not expired['cleanupPending']
    assert 'questionnaire' not in expired
    h.generator.expire_artifacts.assert_awaited_once()
    assert len(main.calls) == 1


@pytest.mark.asyncio
async def test_background_recovers_accepted_legacy_generation_without_get(harness):
    h = harness
    schedule = h.service._schedule
    h.service._schedule = Mock()
    created = await h.service.create('user', 'session')
    h.store.docs[('user', created['questionnaireId'])].pop('questionnaireAgentManifest')
    h.store.docs[('user', created['questionnaireId'])]['generationAttempts'] = 2
    h.service._schedule = schedule
    h.service.get = AsyncMock(side_effect=AssertionError('no polling client'))
    await h.service.recover_pending()
    await asyncio.gather(*list(h.service._tasks.values()))
    source = await h.store.get_by_id('user', created['questionnaireId'])
    assert source['status'] == 'ready' and source['generationAttempts'] == 3
    assert source['questionnaireAgentManifest'] == LEGACY_QUESTIONNAIRE_MANIFEST


@pytest.mark.asyncio
async def test_pending_submission_recovers_after_deadline_without_get_or_another_generation(harness):
    h = harness
    h.service.get = AsyncMock(side_effect=AssertionError('GET must not drive submission'))
    source = await generated(h)
    h.store.fail_submission_once = True
    with pytest.raises(RuntimeError):
        await h.service.submit('user', source['questionnaireId'], {'note': 'original user evidence'})
    h.now[0] += 7201
    assert await h.service.recover_pending() == 1
    submitted = await h.store.get_by_id('user', source['questionnaireId'])
    assert submitted['status'] == 'submitted'
    assert submitted['canonicalAnswers']['note'] == 'original user evidence'
    h.generator.generate.assert_awaited_once()
    h.generator.expire_artifacts.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['queued', 'generating', 'failed', 'ready'])
async def test_get_only_projects_state_without_scheduling_leases_or_saves(harness, status):
    h = harness
    h.service._schedule = Mock()
    created = await h.service.create('user', 'session')
    source = h.store.docs[('user', created['questionnaireId'])]
    source.update(status=status, retryable=True, expiresAt=h.now[0] - 1)
    h.service._schedule.reset_mock()
    h.store.save = AsyncMock(side_effect=AssertionError('GET cannot save'))
    h.leases.try_acquire = AsyncMock(side_effect=AssertionError('GET cannot acquire a lease'))
    result = await h.service.get('user', 'session')
    assert result['status'] == ('expired' if status == 'ready' else status)
    assert source['status'] == status
    h.service._schedule.assert_not_called()
    h.store.save.assert_not_awaited()
    h.generator.generate.assert_not_awaited()
    h.generator.expire_artifacts.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy', [False, True])
async def test_generation_refuses_changed_fixed_dependency_without_a_model_or_budget_reset(harness, legacy):
    h = harness
    h.service._schedule = Mock()
    created = await h.service.create('user', 'session')
    source = h.store.docs[('user', created['questionnaireId'])]
    source['generationAttempts'] = 2
    if legacy:
        source.pop('questionnaireAgentManifest')
    newer = {**deepcopy(LEGACY_QUESTIONNAIRE_MANIFEST), 'factorModel': 'future'}
    h.service._domain_agent.manifest_provider = lambda: newer
    await h.service._generate('user', created['questionnaireId'])
    saved = await h.store.get_by_id('user', created['questionnaireId'])
    assert saved['status'] == 'failed' and not saved['retryable']
    assert saved['generationAttempts'] == 2
    h.generator.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_factor_task_runs_whole_workflow_through_same_main_and_keeps_result_private(factor_harness):
    h = factor_harness
    main = RecordingMain(h.service.agent_port)
    h.service._domain_agent.factors = h.factor_tasks._execute_factor_task
    h.service._main_agent = main
    h.factor_tasks.bind_main_agent(main, h.service._domain_agent.execution_manifest)
    source = await generated(h)
    await h.service.submit('user', source['questionnaireId'], {'note': 'user evidence'})
    assert await h.service.drain_factor_assessments() == 1
    assert [call[0]['operation'] for call in main.calls] == ['generate', 'factor_assessment']
    job = next(iter(h.collection.documents.values()))
    assert job['status'] == 'completed' and job['attempts'] == 1
    assert job['questionnaireAgentManifest'] == questionnaire_agent_manifest()
    assert job['questionnaireAgentExecution']['runId'] == main.calls[1][1].run_id
    before = len(main.calls)
    result = await h.service.get_factor_assessment('user', source['questionnaireId'])
    assert len(main.calls) == before and result['status'] == 'completed'
    assert 'questionnaireAgentExecution' not in result


@pytest.mark.asyncio
async def test_factor_unknown_manifest_is_terminal_before_evaluation(factor_harness):
    h = factor_harness
    source = await generated(h)
    await h.service.submit('user', source['questionnaireId'], {'note': 'user evidence'})
    job = next(iter(h.collection.documents.values()))
    job['questionnaireAgentManifest']['factorModel'] = 'unsupported'
    await h.service.drain_factor_assessments()
    assert job['status'] == 'failed' and not job['retryable'] and job['attempts'] == 1
    h.evaluator.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_production_manifest_tracks_the_actual_planner_adapter(composed, monkeypatch):
    h = composed
    h.service._schedule = Mock()
    created = await h.service.create('user', 'session')
    source = await h.source_store.get_by_id('user', created['questionnaireId'])
    assert source['questionnaireAgentManifest']['reportPlan'] == h.service.generator.planner.VERSION
    monkeypatch.setattr(h.service.generator.planner, 'VERSION', 'unsupported-new-planner')
    h.service.generator.generate = AsyncMock(side_effect=AssertionError('wrong provider must not run'))
    await h.service._generate('user', created['questionnaireId'])
    source = await h.source_store.get_by_id('user', created['questionnaireId'])
    assert source['status'] == 'failed' and not source['retryable']
    h.service.generator.generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_factor_job_without_manifest_uses_fixed_legacy_mapping(factor_harness):
    h = factor_harness
    source = await generated(h)
    await h.service.submit('user', source['questionnaireId'], {'note': 'original user evidence'})
    job = next(iter(h.collection.documents.values()))
    job.pop('questionnaireAgentManifest')
    assert await h.service.drain_factor_assessments() == 1
    assert job['status'] == 'completed' and job['questionnaireAgentManifest'] == LEGACY_QUESTIONNAIRE_MANIFEST
    h.evaluator.evaluate.assert_awaited_once()


@pytest.mark.asyncio
async def test_public_main_rejects_questionnaire_from_another_session_before_any_work(harness):
    h = harness
    h.service._schedule = Mock()
    created = await h.service.create('user', 'actual-session')
    before = deepcopy(h.store.docs)
    h.store.save = AsyncMock(side_effect=AssertionError('foreign session cannot be saved'))
    h.leases.try_acquire = AsyncMock(side_effect=AssertionError('foreign session cannot be leased'))
    with pytest.raises(AgentProtocolError, match='scope'):
        await h.service.agent_port.invoke(
            AgentRequest(
                {
                    'operation': 'generate',
                    'userId': 'user',
                    'sessionId': 'different-session',
                    'questionnaireId': created['questionnaireId'],
                }
            ),
            AgentRunContext.create(user_id='user', session_id='different-session'),
        )
    assert h.store.docs == before
    h.generator.generate.assert_not_awaited()
    h.leases.try_acquire.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['userId', 'sessionId', 'questionnaireId', 'assessmentId', 'inputFingerprint'])
@pytest.mark.parametrize('invalid_manifest', [False, True])
async def test_factor_scope_and_fingerprint_are_bound_to_the_persisted_claim(factor_harness, field, invalid_manifest):
    h = factor_harness
    source = await generated(h)
    await h.service.submit('user', source['questionnaireId'], {'note': 'original evidence'})
    job = await h.task_store.claim('worker', lease_seconds=120, max_attempts=3)
    before = deepcopy(h.collection.documents)
    job[field] = 'forged'
    if invalid_manifest:
        job['questionnaireAgentManifest'] = {'contractVersion': 'unsupported'}
    await h.factor_tasks._run(job)
    assert h.collection.documents == before
    h.evaluator.evaluate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['pipelineVersion', 'questionnaireAgentManifest'])
async def test_factor_cannot_replace_the_stored_execution_version_using_a_valid_lease(factor_harness, field):
    h = factor_harness
    source = await generated(h)
    await h.service.submit('user', source['questionnaireId'], {'note': 'original evidence'})
    job = await h.task_store.claim('worker', lease_seconds=120, max_attempts=3)
    # The caller supplies today's valid version while the accepted row is different.
    h.collection.documents[job['_id']][field] = 'unsupported-persisted-version'
    before = deepcopy(h.collection.documents)
    await h.factor_tasks._run(job)
    assert h.collection.documents == before
    h.evaluator.evaluate.assert_not_awaited()


class RetirementCollection(FakeCollection):
    async def update_one(self, query, update):
        changed = await self.find_one_and_update(query, update, return_document=ReturnDocument.AFTER)
        return SimpleNamespace(acknowledged=True, matched_count=int(changed is not None))


@pytest.mark.asyncio
async def test_managed_legacy_intents_release_fixed_pending_scan_without_fake_jobs(harness):
    h = harness
    collection = RetirementCollection()
    collection.documents = [
        {
            '_id': f'q-{index}',
            'userId': 'user',
            'sessionId': f's-{index}',
            'questionnaireId': f'q-{index}',
            'sourceRevision': 1,
            'status': 'submitted',
            'submittedAt': str(index),
            'profileIngestion': {'status': 'pending', 'jobId': None},
        }
        for index in range(3)
    ]
    store = MongoQuestionnaireStore(collection)
    h.service.store = store
    h.service.profile_source_guard = AsyncMock(side_effect=lambda **kw: kw['questionnaire_id'] in {'q-0', 'q-1'})

    async def legacy_delivery(*, user_id, questionnaire_id):
        await store.mark_ingestion_queued(user_id, questionnaire_id, 'actual-legacy-job')

    delivery = AsyncMock(side_effect=legacy_delivery)
    h.service.profile_delivery = delivery
    await h.service.recover_pending(limit=2)
    delivery.assert_not_awaited()
    assert all(row['profileIngestion']['status'] == 'source_managed' for row in collection.documents[:2])
    assert all(row['profileIngestion']['jobId'] is None for row in collection.documents[:2])
    await h.service.recover_pending(limit=2)
    delivery.assert_awaited_once_with(user_id='user', questionnaire_id='q-2')
    collection.documents[0].update(sourceRevision=2, profileIngestion={'status': 'pending'})
    assert not await store.mark_ingestion_source_managed('user', 'q-0', source_revision=1)
    assert collection.documents[0]['profileIngestion']['status'] == 'pending'


@pytest.mark.asyncio
async def test_install_failure_preserves_queries_but_blocks_ai_including_old_port(factor_harness):
    from tests.unit.agents.core.test_versioned_agent_ports import runtime

    h = factor_harness
    source = await generated(h)
    instance, _ = runtime()
    stale = h.service.agent_port
    instance.ports.register(QUESTIONNAIRE_MAIN_SELECTION, stale)
    assert not install_questionnaire_agent(instance, h.service)
    assert (await h.service.get('user', 'session'))['status'] == 'ready'
    with pytest.raises(AgentConfigurationError, match='unavailable'):
        await h.service.create('user', 'new')
    with pytest.raises(AgentConfigurationError, match='unavailable'):
        await stale.invoke(
            AgentRequest(
                {
                    'operation': 'generate',
                    'userId': 'user',
                    'sessionId': 'session',
                    'questionnaireId': source['questionnaireId'],
                }
            ),
            AgentRunContext.create(user_id='user', session_id='session'),
        )
    await h.service.submit('user', source['questionnaireId'], {'note': 'submit already generated questionnaire'})
    assert await h.service.drain_factor_assessments() == 0
    h.evaluator.evaluate.assert_not_awaited()
