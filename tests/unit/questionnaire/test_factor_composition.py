"""Production composition with synthetic ports; never opens external services."""

from __future__ import annotations

import asyncio
import sys
from copy import deepcopy
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest

from adapters.persistence.questionnaire_factor_tasks import MongoQuestionnaireFactorTaskStore
from adapters.persistence.questionnaires import MongoQuestionnaireStore
from adapters.questionnaire_factory import build_questionnaire_service
from agents.errors import AionResponseError
from agents.llm.aion_gateway import AionLLMGateway
from modules.questionnaire.factor_tasks import QuestionnaireFactorTasks
from modules.questionnaire.factors import QuestionnaireFactorPipeline
from modules.questionnaire.schemas import QuestionnaireValidationError
from runtime.application import ApplicationBundle
from runtime.application import drain_application
from runtime.application import run_application
from runtime.application import schedule_supervised_task
from runtime.supervisor import TaskSupervisor
from tests.unit.questionnaire.test_factor_tasks import FactorSourceStore
from tests.unit.questionnaire.test_factor_tasks import TaskCollection
from tests.unit.questionnaire.test_factor_tasks import factor_harness as factor_harness
from tests.unit.questionnaire.test_lifecycle import Leases
from tests.unit.questionnaire.test_lifecycle import Sessions
from tests.unit.questionnaire.test_lifecycle import harness as harness
from tests.unit.questionnaire.test_lifecycle import ready
from web.lifecycle import ApplicationLifecycle


@pytest.fixture
def composed(monkeypatch):
    import adapters.analysis.document_trace as document_trace
    import adapters.questionnaire_factory as factory

    source_store = FactorSourceStore()
    collection = TaskCollection()
    database = ModuleType('db.mongodb')
    database.collection_document_index = object()
    database.collection_health_subjects = object()
    database.collection_questionnaire_factor_tasks = collection
    profile = SimpleNamespace(ensure_queued=AsyncMock())
    profile_module = ModuleType('health_profile.application')
    profile_module.create_application_questionnaire_profile_source = Mock(return_value=profile)
    monkeypatch.setitem(sys.modules, 'db.mongodb', database)
    monkeypatch.setitem(sys.modules, 'health_profile.application', profile_module)
    monkeypatch.setattr(factory, 'create_questionnaire_store', lambda: source_store)
    monkeypatch.setattr(factory, 'create_questionnaire_archive', lambda: object())
    monkeypatch.setattr(document_trace, 'create_application_document_trace_store', lambda: object())
    runtime = SimpleNamespace(
        llm_gateway=SimpleNamespace(complete=AsyncMock()),
        knowledge_gateway=SimpleNamespace(enabled=True, search=AsyncMock()),
        lease_manager=Leases(),
    )
    supervisor = TaskSupervisor()
    service = build_questionnaire_service(
        runtime=runtime,
        interpreter=object(),
        session_repository=Sessions(),
        session_cache=None,
        task_supervisor=supervisor,
    )
    documents = service.generator.documents
    documents.stage = AsyncMock(return_value={'reports': [], 'artifacts': []})
    documents.ready_artifacts = AsyncMock()
    documents.pin_artifacts = AsyncMock()
    documents.expire_artifacts = AsyncMock()
    return SimpleNamespace(
        service=service,
        runtime=runtime,
        supervisor=supervisor,
        profile=profile,
        source_store=source_store,
        collection=collection,
    )


def test_factory_builds_real_pipeline_and_independent_task_store_without_io(composed):
    h = composed
    pipeline = h.service.generator.factor_pipeline
    assert isinstance(pipeline, QuestionnaireFactorPipeline)
    assert isinstance(h.service.factor_tasks, QuestionnaireFactorTasks)
    assert isinstance(h.service.factor_tasks.store, MongoQuestionnaireFactorTaskStore)
    assert pipeline is h.service.factor_tasks.evaluator
    assert pipeline.knowledge is h.runtime.knowledge_gateway
    assert pipeline.model.gateway is h.runtime.llm_gateway
    assert h.service.factor_tasks.questionnaire_store is h.service.store is h.source_store
    assert h.service.factor_tasks.store.collection is h.collection
    assert h.service.lease_manager is h.runtime.lease_manager
    assert not h.collection.documents and not h.collection.indexes
    h.runtime.llm_gateway.complete.assert_not_awaited()
    h.runtime.knowledge_gateway.search.assert_not_awaited()
    h.profile.ensure_queued.assert_not_awaited()


@pytest.mark.asyncio
async def test_factory_no_report_unknown_answer_path_is_complete_without_fabricated_evidence(composed):
    h = composed
    created = await h.service.create('user', 'session')
    await h.supervisor.wait_idle()
    source = await h.source_store.get_by_id('user', created['questionnaireId'])
    assert source['status'] == 'ready' and source['factorPreparation']['phase'] == 'pre_answer'
    assert source['factorPreparation']['targets'] == []
    receipt = await h.service.submit('user', created['questionnaireId'], {})
    assert receipt['canChat'] and 'factorPreparation' not in receipt and 'factorAssessment' not in receipt
    await h.service.drain_factor_assessments()
    result = await h.service.get_factor_assessment('user', created['questionnaireId'])
    assert result['status'] == 'completed'
    assert result['result']['targets'] == [] and result['result']['sources'] == []
    assert await h.service.submit('user', created['questionnaireId'], {}) == receipt
    h.runtime.llm_gateway.complete.assert_not_awaited()
    h.runtime.knowledge_gateway.search.assert_not_awaited()
    h.profile.ensure_queued.assert_awaited_once()


def _runtime_bundle(questionnaire):
    lifecycle = ApplicationLifecycle()
    lifecycle.mark_ready()
    container = SimpleNamespace(
        lifecycle=lifecycle,
        task_supervisor=TaskSupervisor(),
        questionnaire_service=questionnaire,
        report_service=None,
        report_callback_worker=None,
        gut_barrier_service=None,
        health_profile_ingestion_worker=SimpleNamespace(drain_pending=AsyncMock(), stop_accepting=Mock()),
        chat_service=SimpleNamespace(drainPendingCustomMessages=AsyncMock()),
        record_service=SimpleNamespace(recoverPendingCreations=AsyncMock()),
    )
    return ApplicationBundle(
        web_application=SimpleNamespace(settings={}),
        container=container,
        resources=SimpleNamespace(closed=True),
    )


@pytest.mark.asyncio
async def test_runtime_registers_independent_pollers_and_a_failure_does_not_disable_next_tick(monkeypatch):
    questionnaire = SimpleNamespace(
        recover_pending=AsyncMock(),
        drain_factor_assessments=AsyncMock(side_effect=[RuntimeError('synthetic worker failure'), 1]),
        recover_factor_assessments=AsyncMock(return_value=1),
    )
    bundle = _runtime_bundle(questionnaire)
    callbacks = []

    def periodic(callback, interval):
        callbacks.append((callback, interval))
        return SimpleNamespace(start=Mock(), stop=Mock())

    monkeypatch.setattr('runtime.application.configure_tornado_logging', Mock())
    monkeypatch.setattr('runtime.application.shutdown_logging', Mock())
    monkeypatch.setattr('runtime.application.tornado.httpserver.HTTPServer', Mock(return_value=Mock()))
    monkeypatch.setattr('runtime.application.tornado.ioloop.IOLoop.current', Mock(return_value=Mock()))
    monkeypatch.setattr('runtime.application.tornado.ioloop.PeriodicCallback', periodic)
    monkeypatch.setattr('runtime.application.signal.signal', Mock())
    run_application(bundle)
    registered = {
        callback.__defaults__[2]: (callback, interval)
        for callback, interval in callbacks
        if getattr(callback, '__defaults__', None)
    }
    poll, interval = registered['worker:questionnaire-factor-assessment']
    recover, recovery_interval = registered['worker:questionnaire-factor-recovery']
    assert (interval, recovery_interval) == (5000, 15000)
    with pytest.raises(RuntimeError, match='synthetic worker failure'):
        await poll()
    await bundle.container.task_supervisor.wait_idle()
    assert await poll() == 1
    assert await recover() == 1
    await bundle.container.task_supervisor.wait_idle()
    assert questionnaire.drain_factor_assessments.await_count == 2


@pytest.mark.asyncio
async def test_shutdown_finishes_active_assessment_but_does_not_claim_remaining_batch(factor_harness):
    h = factor_harness
    first = await ready(h)
    await h.service.submit('user', first['questionnaireId'], {})
    second = await h.service.create('user', 'second-session')
    await asyncio.gather(*list(h.service._tasks.values()))
    await h.service.submit('user', second['questionnaireId'], {})
    started, release = asyncio.Event(), asyncio.Event()

    async def evaluate(snapshot, *, checkpoint):
        started.set()
        await release.wait()
        return {'finished': True}

    h.evaluator.evaluate = AsyncMock(side_effect=evaluate)
    bundle = _runtime_bundle(h.service)
    active = schedule_supervised_task(bundle, h.service.drain_factor_assessments, name='worker:questionnaire-factor')
    await started.wait()
    server = SimpleNamespace(stop=Mock(), close_all_connections=AsyncMock())
    loop = SimpleNamespace(stop=Mock())
    draining = asyncio.create_task(drain_application(bundle, server, (), loop, 'synthetic-stop'))
    await asyncio.sleep(0)
    assert not h.factor_tasks._accepting
    assert not active.done()
    release.set()
    await draining
    assert active.done() and h.evaluator.evaluate.await_count == 1
    assert (await h.factor_tasks.get('user', first['questionnaireId']))['status'] == 'completed'
    assert (await h.factor_tasks.get('user', second['questionnaireId']))['status'] == 'queued'


def test_gateway_old_sdk_sources_are_absent_and_new_sources_are_independent_of_prose():
    gateway = object.__new__(AionLLMGateway)
    gateway._provider = 'synthetic'
    client = SimpleNamespace(provider='synthetic', model='model')
    result = gateway._normalize_result(client, SimpleNamespace(content='{"sources":["invented"]}'))
    assert result.search_sources == ()
    sources = [{'title': 'synthetic source', 'originalText': 'Original excerpt'}]
    result = gateway._normalize_result(client, SimpleNamespace(content='generated content', search_sources=sources))
    sources[0]['originalText'] = 'changed'
    assert result.search_sources[0]['originalText'] == 'Original excerpt'
    with pytest.raises(AionResponseError):
        gateway._normalize_result(client, SimpleNamespace(content='content', search_sources='not structured'))


@pytest.mark.asyncio
async def test_ready_capacity_accounts_for_receipt_and_cleans_large_generated_failure(harness):
    h = harness
    h.store.validate_capacity = MongoQuestionnaireStore.validate_capacity
    h.generator.generate.return_value['reports'] = [{'interpretation': {'synthetic': 'x' * (6 * 1024 * 1024)}}]
    h.generator.generate.return_value['factorPreparation'] = {
        'version': 'synthetic',
        'evidence': ['y' * (3 * 1024 * 1024)],
    }
    created = await h.service.create('user', 'session')
    await asyncio.gather(*list(h.service._tasks.values()))
    source = await h.store.get_by_id('user', created['questionnaireId'])
    assert source['status'] == 'failed'
    assert source['errorCode'] == 'QUESTIONNAIRE_GENERATION_FAILED'
    assert 'reports' not in source and 'factorPreparation' not in source and 'questionnaire' not in source
    MongoQuestionnaireStore.validate_capacity(source)
    h.generator.ready_artifacts.assert_not_awaited()


@pytest.mark.asyncio
async def test_actual_oversized_raw_answer_is_rejected_before_pending_acceptance(harness):
    h = harness
    h.store.validate_capacity = MongoQuestionnaireStore.validate_capacity
    generated = await ready(h)
    before = await h.store.get_by_id('user', generated['questionnaireId'])
    # Whitespace is a valid unknown answer and is deliberately not truncated.
    # It has no schema length bound, making the real payload preflight necessary.
    with pytest.raises(QuestionnaireValidationError, match='storage budget'):
        await h.service.submit('user', generated['questionnaireId'], {'note': ' ' * (8 * 1024 * 1024)})
    after = await h.store.get_by_id('user', generated['questionnaireId'])
    assert after == before and 'pendingSubmission' not in after
    h.generator.pin_artifacts.assert_not_awaited()
    assert (await h.service.submit('user', generated['questionnaireId'], {'note': 'normal answer'}))[
        'status'
    ] == 'submitted'


@pytest.mark.asyncio
async def test_ready_budget_reserves_full_ordinary_text_capacity(harness):
    h = harness
    h.store.validate_capacity = MongoQuestionnaireStore.validate_capacity
    generated = await ready(h)
    largest = '\U00010000' * 20000
    result = await h.service.submit('user', generated['questionnaireId'], {'note': largest})
    assert result['answers']['note'] == largest
    stored = await h.store.get_submitted('user', generated['questionnaireId'])
    assert stored['rawAnswers']['note'] == largest
    assert stored['submissionResponse'] == deepcopy(result)
