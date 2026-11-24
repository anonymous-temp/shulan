"""Synthetic durable factor tasks; no private environment, models or databases."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pymongo.errors import DuplicateKeyError

from adapters.persistence.questionnaire_factor_tasks import MongoQuestionnaireFactorTaskStore
from modules.questionnaire.factor_tasks import FactorPayloadTooLarge
from modules.questionnaire.factor_tasks import QuestionnaireFactorTasks
from modules.questionnaire.schemas import QuestionnaireConflictError
from modules.questionnaire.schemas import QuestionnaireNotFoundError
from tests.unit.questionnaire.test_lifecycle import MemoryStore
from tests.unit.questionnaire.test_lifecycle import harness as harness
from tests.unit.questionnaire.test_lifecycle import ready


class TaskCollection:
    """Small collection double exercising the adapter's actual atomic filters."""

    def __init__(self):
        self.documents = {}
        self.indexes = []

    async def create_index(self, fields, **kwargs):
        self.indexes.append((fields, kwargs))

    @staticmethod
    def matches(document, query):
        for field, value in query.items():
            if field == '$or':
                if not any(TaskCollection.matches(document, alternative) for alternative in value):
                    return False
                continue
            actual = document.get(field)
            if not isinstance(value, dict):
                if actual != value:
                    return False
                continue
            if not any(key.startswith('$') for key in value):
                if actual != value:
                    return False
                continue
            for operator, operand in value.items():
                if actual is None:
                    return False
                if operator == '$lt' and not actual < operand:
                    return False
                if operator == '$lte' and not actual <= operand:
                    return False
                if operator == '$gt' and not actual > operand:
                    return False
                if operator == '$gte' and not actual >= operand:
                    return False
        return True

    async def find_one(self, query):
        return next((deepcopy(row) for row in self.documents.values() if self.matches(row, query)), None)

    async def insert_one(self, document):
        if document['_id'] in self.documents:
            raise DuplicateKeyError('duplicate identity')
        self.documents[document['_id']] = deepcopy(document)
        return SimpleNamespace(acknowledged=True)

    async def find_one_and_update(self, query, update, **kwargs):
        for document in self.documents.values():
            if not self.matches(document, query):
                continue
            document.update(deepcopy(update.get('$set', {})))
            for field, value in update.get('$inc', {}).items():
                document[field] = document.get(field, 0) + value
            for field in update.get('$unset', {}):
                document.pop(field, None)
            return deepcopy(document)
        return None

    async def update_one(self, query, update):
        changed = await self.find_one_and_update(query, update)
        return SimpleNamespace(acknowledged=True, matched_count=int(changed is not None))


class FactorSourceStore(MemoryStore):
    async def get_for_factor_assessment(self, user_id, questionnaire_id):
        return await self.get_by_id(user_id, questionnaire_id)

    async def list_pending_factor_assessments(self, *, limit=100):
        return [
            deepcopy(row)
            for row in self.docs.values()
            if row['status'] == 'submitted' and row.get('factorAssessment', {}).get('status') == 'pending'
        ][:limit]

    async def mark_factor_assessment_queued(self, user_id, questionnaire_id, assessment_id):
        source = self.docs[(user_id, questionnaire_id)]
        assert source['factorAssessment']['assessmentId'] == assessment_id
        source['factorAssessment']['status'] = 'queued'
        return True


@pytest.fixture
def factor_harness(harness):
    h = harness
    h.store = FactorSourceStore()
    h.service.store = h.store
    h.collection = TaskCollection()
    h.task_store = MongoQuestionnaireFactorTaskStore(h.collection, clock=lambda: h.now[0])
    h.evaluator = SimpleNamespace(evaluate=AsyncMock(return_value={'factors': [], 'coverage': 'no_candidate'}))
    h.factor_tasks = QuestionnaireFactorTasks(
        store=h.task_store,
        questionnaire_store=h.store,
        evaluator=h.evaluator,
        clock=lambda: h.now[0],
    )
    h.service.factor_tasks = h.factor_tasks
    return h


async def submit(h):
    generated = await ready(h)
    receipt = await h.service.submit('user', generated['questionnaireId'], {'note': 'first original answer'})
    return receipt, h.store.docs[('user', generated['questionnaireId'])]


@pytest.mark.asyncio
async def test_preparation_is_published_with_ready_then_cleaned_without_public_leak(factor_harness):
    h = factor_harness
    prep = {'version': 'prep-1', 'candidates': [{'id': 'candidate'}]}
    h.generator.generate.return_value['factorPreparation'] = prep
    generated = await ready(h)
    stored = await h.store.get_by_id('user', generated['questionnaireId'])
    assert stored['factorPreparation'] == prep
    assert h.generator.ready_artifacts.call_args.args[0]['factorPreparation'] == prep
    assert 'factorPreparation' not in generated
    h.now[0] += 7200
    await h.service.get('user', 'session')
    assert 'factorPreparation' in h.store.docs[('user', generated['questionnaireId'])]
    await h.service.recover_pending()
    assert 'factorPreparation' not in h.store.docs[('user', generated['questionnaireId'])]


@pytest.mark.asyncio
async def test_submission_atomically_saves_intent_and_keeps_immutable_receipt(factor_harness):
    h = factor_harness
    h.service.profile_delivery = None
    receipt, source = await submit(h)
    assert source['factorAssessment']['status'] == 'queued'
    assert source['factorAssessment']['preparationVersion'] == 'missing'
    assert source['factorAssessment']['answerHash'] == source['answerHash']
    assert len(h.collection.documents) == 1
    await h.service.drain_factor_assessments()
    assert (await h.service.get_factor_assessment('user', receipt['questionnaireId']))['status'] == 'completed'
    assert await h.service.submit('user', receipt['questionnaireId'], {'note': 'first original answer'}) == receipt
    assert 'factorAssessment' not in receipt and receipt['canChat']


@pytest.mark.asyncio
async def test_failed_submission_does_not_publish_task_and_recovery_preserves_source(factor_harness):
    h = factor_harness
    generated = await ready(h)
    h.store.fail_submission_once = True
    with pytest.raises(RuntimeError):
        await h.service.submit('user', generated['questionnaireId'], {'note': 'retained answer'})
    assert not h.collection.documents
    source = h.store.docs[('user', generated['questionnaireId'])]
    assert 'factorAssessment' not in source and source['pendingSubmission']['rawAnswers']['note'] == 'retained answer'
    h.now[0] += 8000
    await h.service.recover_pending()
    assert len(h.collection.documents) == 1
    job = next(iter(h.collection.documents.values()))
    assert job['inputSnapshot']['answers']['note']['value'] == 'retained answer'
    h.generator.expire_artifacts.assert_not_awaited()


@pytest.mark.asyncio
async def test_profile_failure_cannot_block_factor_delivery(factor_harness):
    h = factor_harness
    h.delivery.side_effect = RuntimeError('synthetic profile failure')
    receipt, _ = await submit(h)
    assert receipt['status'] == 'submitted' and len(h.collection.documents) == 1


@pytest.mark.asyncio
async def test_delivery_failure_keeps_pending_intent_for_independent_compensation(factor_harness):
    h = factor_harness
    original = h.task_store.admit
    h.task_store.admit = AsyncMock(side_effect=RuntimeError('storage unavailable'))
    receipt, source = await submit(h)
    assert source['factorAssessment']['status'] == 'pending'
    h.delivery.assert_awaited_once()
    assert (await h.service.get_factor_assessment('user', receipt['questionnaireId']))['status'] == 'queued'
    h.task_store.admit = original
    assert await h.service.recover_factor_assessments() == 1
    assert len(h.collection.documents) == 1
    assert await h.service.submit('user', receipt['questionnaireId'], {'note': 'first original answer'}) == receipt


@pytest.mark.asyncio
async def test_lost_outbox_ack_does_not_duplicate_admitted_job(factor_harness):
    h = factor_harness
    original = h.store.mark_factor_assessment_queued
    h.store.mark_factor_assessment_queued = AsyncMock(return_value=False)
    _, source = await submit(h)
    assert source['factorAssessment']['status'] == 'pending' and len(h.collection.documents) == 1
    h.store.mark_factor_assessment_queued = original
    await h.factor_tasks.recover_pending()
    assert len(h.collection.documents) == 1 and source['factorAssessment']['status'] == 'queued'


@pytest.mark.asyncio
async def test_retry_uses_frozen_input_and_persisted_checkpoint(factor_harness):
    h = factor_harness
    _, source = await submit(h)
    calls = []

    async def evaluate(snapshot, *, checkpoint):
        calls.append(deepcopy(snapshot))
        if len(calls) == 1:
            await checkpoint({'inputHash': 'stable', 'evaluations': {'candidate': {'judgment': 'unknown'}}})
            raise RuntimeError('transient failure with sensitive upstream detail')
        return {'confirmed': snapshot['answers']['note']['value']}

    h.evaluator.evaluate = evaluate
    await h.factor_tasks.drain_pending()
    assert await h.factor_tasks.drain_pending() == 0
    source['answers']['note']['value'] = 'subsequent mutation must not enter retry'
    h.now[0] += 15
    await h.factor_tasks.drain_pending()
    assert calls[1]['answers']['note']['value'] == 'first original answer'
    assert calls[1]['_factorCheckpoint']['evaluations']['candidate']['judgment'] == 'unknown'
    result = await h.factor_tasks.get('user', source['questionnaireId'])
    assert result['status'] == 'completed' and result['attempts'] == 2
    assert result['result']['confirmed'] == 'first original answer'
    assert 'checkpoint' not in next(iter(h.collection.documents.values()))
    assert 'sensitive' not in repr(result)


@pytest.mark.asyncio
async def test_concurrent_admission_is_idempotent_and_rejects_changed_input(factor_harness):
    h = factor_harness
    _, source = await submit(h)
    await asyncio.gather(h.factor_tasks.ensure_queued(source), h.factor_tasks.ensure_queued(source))
    assert len(h.collection.documents) == 1
    source['reports'] = [{'observation': 'different input'}]
    with pytest.raises(QuestionnaireConflictError, match='source identity changed'):
        await h.factor_tasks.ensure_queued(source)


@pytest.mark.asyncio
async def test_source_fingerprint_rejects_pre_admission_mutation(factor_harness):
    h = factor_harness
    h.task_store.admit = AsyncMock(side_effect=RuntimeError('temporary outage'))
    _, source = await submit(h)
    source['answers']['note']['value'] = 'not the accepted answer'
    with pytest.raises(QuestionnaireConflictError, match='source identity changed'):
        await h.factor_tasks.ensure_queued(source)
    assert not h.collection.documents


@pytest.mark.asyncio
async def test_only_one_owner_claims_and_expired_attempt_cannot_write(factor_harness):
    h = factor_harness
    await submit(h)
    first, blocked = await asyncio.gather(
        h.task_store.claim('first', lease_seconds=10, max_attempts=3),
        h.task_store.claim('second', lease_seconds=10, max_attempts=3),
    )
    assert first and blocked is None
    h.now[0] += 10
    second = await h.task_store.claim('second', lease_seconds=10, max_attempts=3)
    assert second['attempts'] == 2 and second['leaseToken'] != first['leaseToken']
    assert not await h.task_store.complete(first, {'stale': True})
    assert not await h.task_store.save_checkpoint(first, {'stale': True})
    assert not await h.task_store.renew(first, lease_seconds=10)
    assert not await h.task_store.fail(first, error_code='OLD', retryable=False, max_attempts=3, delay_seconds=1)
    assert await h.task_store.complete(second, {'current': True})


@pytest.mark.asyncio
async def test_crashed_last_attempt_becomes_terminal_failure(factor_harness):
    h = factor_harness
    _, source = await submit(h)
    for attempt in range(3):
        job = await h.task_store.claim(str(attempt), lease_seconds=10, max_attempts=3)
        assert job['attempts'] == attempt + 1
        h.now[0] += 10
    assert await h.factor_tasks.recover_pending() == 1
    assert await h.factor_tasks.drain_pending() == 0
    result = await h.factor_tasks.get('user', source['questionnaireId'])
    assert result['status'] == 'failed' and not result['retryable']
    assert result['errorCode'] == 'FACTOR_ATTEMPTS_EXHAUSTED'


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [ValueError('invalid medical structure'), FactorPayloadTooLarge('too large')])
async def test_invalid_outputs_are_terminal_without_model_retry(factor_harness, error):
    h = factor_harness
    _, source = await submit(h)
    h.evaluator.evaluate.side_effect = error
    assert await h.factor_tasks.drain_pending() == 1
    assert await h.factor_tasks.drain_pending() == 0
    response = await h.factor_tasks.get('user', source['questionnaireId'])
    assert response['status'] == 'failed' and response['attempts'] == 1
    assert response['result'] is None and not response['retryable']


@pytest.mark.asyncio
async def test_timeout_retries_are_bounded_and_return_safe_error(factor_harness):
    h = factor_harness
    _, source = await submit(h)
    h.factor_tasks.evaluation_timeout_seconds = 0.01

    async def evaluate(snapshot, *, checkpoint):
        await asyncio.Event().wait()

    h.evaluator.evaluate = evaluate
    for _ in range(3):
        await h.factor_tasks.drain_pending()
        h.now[0] += 300
    result = await h.factor_tasks.get('user', source['questionnaireId'])
    assert result['status'] == 'failed' and result['errorCode'] == 'FACTOR_EVALUATION_TIMEOUT'
    assert await h.factor_tasks.drain_pending() == 0


@pytest.mark.asyncio
async def test_lease_renewal_and_loss_cancel_evaluation_without_publishing(factor_harness):
    h = factor_harness
    _, source = await submit(h)
    h.factor_tasks.heartbeat_seconds = 0.001
    h.task_store.renew = AsyncMock(side_effect=[True, False])
    cancelled = asyncio.Event()

    async def evaluate(snapshot, *, checkpoint):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    h.evaluator.evaluate = evaluate
    await h.factor_tasks.drain_pending()
    assert cancelled.is_set() and h.task_store.renew.await_count == 2
    result = await h.factor_tasks.get('user', source['questionnaireId'])
    assert result['status'] == 'running' and result['result'] is None


@pytest.mark.asyncio
async def test_worker_cancellation_leaves_recoverable_checkpoint(factor_harness):
    h = factor_harness
    _, source = await submit(h)
    started = asyncio.Event()

    async def evaluate(snapshot, *, checkpoint):
        await checkpoint({'evaluations': {'already-done': True}})
        started.set()
        await asyncio.Event().wait()

    h.evaluator.evaluate = evaluate
    worker = asyncio.create_task(h.factor_tasks.drain_pending())
    await started.wait()
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    h.now[0] += 121
    h.evaluator.evaluate = AsyncMock(return_value={'resumed': True})
    await h.factor_tasks.drain_pending()
    assert h.evaluator.evaluate.call_args.args[0]['_factorCheckpoint']['evaluations']['already-done']
    assert (await h.factor_tasks.get('user', source['questionnaireId']))['status'] == 'completed'


@pytest.mark.asyncio
async def test_result_save_failure_propagates_without_false_completion(factor_harness):
    h = factor_harness
    _, source = await submit(h)
    h.task_store.complete = AsyncMock(side_effect=RuntimeError('Mongo write unavailable'))
    with pytest.raises(RuntimeError, match='Mongo write'):
        await h.factor_tasks.drain_pending()
    result = await h.factor_tasks.get('user', source['questionnaireId'])
    assert result['status'] == 'running' and result['result'] is None


@pytest.mark.asyncio
async def test_oversized_input_has_durable_terminal_error_without_model_call(factor_harness, monkeypatch):
    h = factor_harness
    monkeypatch.setattr('modules.questionnaire.factor_tasks.MAX_INPUT_BYTES', 1)
    _, source = await submit(h)
    result = await h.factor_tasks.get('user', source['questionnaireId'])
    assert result['status'] == 'failed' and result['errorCode'] == 'FACTOR_INPUT_TOO_LARGE'
    assert next(iter(h.collection.documents.values()))['inputSnapshot'] is None
    assert source['factorAssessment']['status'] == 'queued'
    assert await h.factor_tasks.drain_pending() == 0
    h.evaluator.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_result_size_budget_is_enforced_before_completion(factor_harness, monkeypatch):
    h = factor_harness
    _, source = await submit(h)
    monkeypatch.setattr('modules.questionnaire.factor_tasks.MAX_RESULT_BYTES', 1)
    await h.factor_tasks.drain_pending()
    result = await h.factor_tasks.get('user', source['questionnaireId'])
    assert result['status'] == 'failed' and result['errorCode'] == 'FACTOR_OUTPUT_TOO_LARGE'


@pytest.mark.asyncio
async def test_read_does_not_enqueue_or_backfill_old_submissions(factor_harness):
    h = factor_harness
    receipt, source = await submit(h)
    source.pop('factorAssessment')
    h.collection.documents.clear()
    h.collection.indexes.clear()
    assert await h.factor_tasks.ensure_queued(source) is None
    assert (await h.service.get_factor_assessment('user', receipt['questionnaireId']))['status'] == 'unavailable'
    assert await h.factor_tasks.recover_pending() == 0
    assert not h.collection.documents
    h.evaluator.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_enforces_ownership_and_submitted_state(factor_harness):
    h = factor_harness
    generated = await ready(h)
    with pytest.raises(QuestionnaireNotFoundError):
        await h.factor_tasks.get('someone-else', generated['questionnaireId'])
    with pytest.raises(QuestionnaireConflictError):
        await h.factor_tasks.get('user', generated['questionnaireId'])
    assert not h.collection.documents and not h.collection.indexes


@pytest.mark.asyncio
async def test_mongo_result_read_has_no_index_write_and_no_input_leak(factor_harness):
    h = factor_harness
    _, source = await submit(h)
    h.collection.indexes.clear()
    h.task_store._indexes_ready = False
    h.task_store.claim = AsyncMock(side_effect=AssertionError('GET must not claim work'))
    result = await h.factor_tasks.get('user', source['questionnaireId'])
    assert result['status'] == 'queued' and not h.collection.indexes
    assert 'inputSnapshot' not in result and 'first original answer' not in repr(result)


@pytest.mark.asyncio
async def test_mongo_document_guard_precedes_write(factor_harness, monkeypatch):
    h = factor_harness
    _, source = await submit(h)
    job = await h.task_store.claim('worker', lease_seconds=10, max_attempts=3)
    monkeypatch.setattr('adapters.persistence.questionnaire_factor_tasks.MAX_DOCUMENT_BYTES', 1)
    with pytest.raises(FactorPayloadTooLarge):
        await h.task_store.complete(job, {'large': True})
    assert (await h.factor_tasks.get('user', source['questionnaireId']))['status'] == 'running'


@pytest.mark.asyncio
async def test_bson_result_limit_becomes_terminal_failure(factor_harness):
    h = factor_harness
    _, source = await submit(h)
    h.task_store.complete = AsyncMock(side_effect=FactorPayloadTooLarge('BSON limit'))
    await h.factor_tasks.drain_pending()
    result = await h.factor_tasks.get('user', source['questionnaireId'])
    assert result['status'] == 'failed' and result['errorCode'] == 'FACTOR_OUTPUT_TOO_LARGE'


@pytest.mark.asyncio
async def test_unacknowledged_write_is_real_failure(factor_harness):
    h = factor_harness
    await submit(h)
    job = await h.task_store.claim('worker', lease_seconds=10, max_attempts=3)
    h.collection.update_one = AsyncMock(return_value=SimpleNamespace(acknowledged=False))
    with pytest.raises(RuntimeError, match='not acknowledged'):
        await h.task_store.complete(job, {'complete': True})
