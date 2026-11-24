"""Independent, durable assessment of immutable submitted questionnaire evidence.

No profile, Chat, model SDK or database configuration is imported here. The
evaluator owns clinical validation; this module owns delivery and execution.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import suppress
from copy import deepcopy
from datetime import datetime
from datetime import timezone
from uuid import uuid4

from agents import AgentRequest
from agents import AgentRunContext
from agents.errors import AgentConfigurationError
from modules.questionnaire.agent import LEGACY_QUESTIONNAIRE_MANIFEST
from modules.questionnaire.agent import QuestionnaireAgent
from modules.questionnaire.agent import bind_questionnaire_execution
from modules.questionnaire.agent import resolve_questionnaire_manifest
from modules.questionnaire.schemas import QuestionnaireConflictError
from modules.questionnaire.schemas import QuestionnaireNotFoundError
from modules.questionnaire.schemas import required_text
from modules.questionnaire.schemas import stable_hash

logger = logging.getLogger(__name__)
PIPELINE_VERSION = 'factor-assessment-v1'
MAX_INPUT_BYTES = 6 * 1024 * 1024
MAX_RESULT_BYTES = 4 * 1024 * 1024
MAX_CHECKPOINT_BYTES = 4 * 1024 * 1024
MAX_PREPARATION_BYTES = 4 * 1024 * 1024
INPUT_FIELDS = (
    'userId',
    'sessionId',
    'questionnaireId',
    'status',
    'sourceRevision',
    'submittedAt',
    'questionnaire',
    'reports',
    'provenance',
    'factorPreparation',
    'answers',
    'canonicalAnswers',
    'rawAnswers',
    'answerHash',
    'rawAnswerHash',
)


class FactorPayloadTooLarge(ValueError):
    """A bounded source, checkpoint or result cannot be safely persisted."""


class FactorLeaseLost(RuntimeError):
    """The worker may no longer publish or checkpoint this attempt."""


def bounded_payload(value, limit):
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    if len(encoded) > limit:
        raise FactorPayloadTooLarge('Factor assessment payload exceeds its storage budget')


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def factor_assessment_intent(snapshot, *, pipeline_version=PIPELINE_VERSION, now=None):
    """Freeze task identity in the same source write that accepts the answers."""
    preparation = snapshot.get('factorPreparation') or {}
    identity = {
        'userId': snapshot['userId'],
        'questionnaireId': snapshot['questionnaireId'],
        'answerHash': snapshot['answerHash'],
        'preparationVersion': preparation.get('version') or 'missing',
        'pipelineVersion': pipeline_version,
    }
    return {
        **identity,
        'assessmentId': stable_hash(identity),
        'inputFingerprint': stable_hash({key: snapshot[key] for key in INPUT_FIELDS if key in snapshot}),
        'sourceRevision': snapshot['sourceRevision'],
        'status': 'pending',
        'createdAt': _iso(time.time() if now is None else now),
        'agentManifest': deepcopy(snapshot.get('questionnaireAgentManifest', LEGACY_QUESTIONNAIRE_MANIFEST)),
    }


def unavailable_assessment(snapshot, error_code='FACTOR_ASSESSMENT_NOT_REQUESTED'):
    return {
        'questionnaireId': snapshot['questionnaireId'],
        'sessionId': snapshot['sessionId'],
        'status': 'unavailable',
        'result': None,
        'errorCode': error_code,
        'retryable': False,
    }


class QuestionnaireFactorTasks:
    def __init__(
        self,
        *,
        store,
        questionnaire_store,
        evaluator,
        pipeline_version=PIPELINE_VERSION,
        clock=time.time,
        lease_seconds=120,
        heartbeat_seconds=30,
        max_attempts=3,
        batch_size=4,
        evaluation_timeout_seconds=600,
        retry_base_seconds=15,
    ):
        if min(lease_seconds, heartbeat_seconds, max_attempts, batch_size, evaluation_timeout_seconds) <= 0:
            raise ValueError('Factor task execution limits must be positive')
        self.store = store
        self.questionnaire_store = questionnaire_store
        self.evaluator = evaluator
        self.pipeline_version = pipeline_version
        self.clock = clock
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = min(heartbeat_seconds, lease_seconds / 3)
        self.max_attempts = max_attempts
        self.batch_size = batch_size
        self.evaluation_timeout_seconds = evaluation_timeout_seconds
        self.retry_base_seconds = max(1, retry_base_seconds)
        self.owner = uuid4().hex
        self._draining = asyncio.Lock()
        self._recovering = asyncio.Lock()
        self._accepting = True
        self._main_agent_ready = True
        self._domain_agent = QuestionnaireAgent(factors=self._execute_factor_task)
        self._main_agent = self._domain_agent
        self._manifest_provider = self._domain_agent.execution_manifest

    def bind_main_agent(self, port, manifest_provider):
        self._main_agent = port
        self._manifest_provider = manifest_provider

    def set_agent_readiness(self, ready):
        self._main_agent_ready = ready

    def _require_agent_ready(self):
        if not self._main_agent_ready:
            raise AgentConfigurationError('Questionnaire main Agent module is unavailable')

    def stop_accepting(self):
        self._accepting = False

    async def ensure_queued(self, snapshot):
        source = await self.questionnaire_store.get_submitted(snapshot['userId'], snapshot['questionnaireId'])
        if source is None or not source.get('factorAssessment'):
            return None  # Older submissions are never implicitly backfilled.
        intent = source['factorAssessment']
        expected = factor_assessment_intent(source, pipeline_version=intent['pipelineVersion'], now=self.clock())
        if (
            intent['assessmentId'] != expected['assessmentId']
            or intent['answerHash'] != source['answerHash']
            or intent.get('inputFingerprint') != expected['inputFingerprint']
        ):
            raise QuestionnaireConflictError('Factor assessment source identity changed')
        frozen = {key: deepcopy(source[key]) for key in INPUT_FIELDS if key in source}
        fingerprint = stable_hash(frozen)
        terminal_error = None
        try:
            bounded_payload(frozen, MAX_INPUT_BYTES)
        except FactorPayloadTooLarge:
            frozen = None
            terminal_error = 'FACTOR_INPUT_TOO_LARGE'
        job = await self.store.admit(
            {
                **deepcopy(intent),
                'sessionId': source['sessionId'],
                'inputFingerprint': fingerprint,
                'inputSnapshot': frozen,
                'questionnaireAgentManifest': deepcopy(intent.get('agentManifest', LEGACY_QUESTIONNAIRE_MANIFEST)),
            },
            terminal_error=terminal_error,
        )
        mark = getattr(self.questionnaire_store, 'mark_factor_assessment_queued', None)
        if mark is not None and not await mark(source['userId'], source['questionnaireId'], intent['assessmentId']):
            raise RuntimeError('Factor task was admitted but its source outbox was not acknowledged')
        return job

    async def recover_pending(self, *, limit=100):
        if not self._accepting or self._recovering.locked():
            return 0
        async with self._recovering:
            recovered = await self.store.recover_exhausted(max_attempts=self.max_attempts, limit=limit)
            pending = getattr(self.questionnaire_store, 'list_pending_factor_assessments', None)
            if pending is None:
                return recovered
            for source in await pending(limit=limit):
                try:
                    await self.ensure_queued(source)
                    recovered += 1
                except Exception as error:
                    # The source intent stays pending. Never log evidence or provider errors.
                    logger.warning('questionnaire_factor_event=delivery_deferred error_type=%s', type(error).__name__)
            return recovered

    async def drain_pending(self):
        if not self._accepting or not self._main_agent_ready or self._draining.locked():
            return 0
        async with self._draining:
            await self.store.recover_exhausted(max_attempts=self.max_attempts, limit=self.batch_size)
            count = 0
            for _ in range(self.batch_size):
                if not self._accepting:
                    break
                job = await self.store.claim(
                    self.owner,
                    lease_seconds=self.lease_seconds,
                    max_attempts=self.max_attempts,
                )
                if job is None:
                    break
                await self._run(job)
                count += 1
            return count

    async def _run(self, job):
        self._require_agent_ready()
        context = AgentRunContext.create(user_id=job['userId'], session_id=job['sessionId'])
        return await self._main_agent.invoke(
            AgentRequest(
                {
                    'operation': 'factor_assessment',
                    'userId': job['userId'],
                    'sessionId': job['sessionId'],
                    'job': deepcopy(job),
                }
            ),
            context,
            thread_id=job['assessmentId'],
        )

    async def _execute_factor_task(self, job, *, execution):
        self._require_agent_ready()
        await self._run_workflow(job, execution=execution)
        current = await self.store.get(job['userId'], job['questionnaireId'], job['assessmentId'])
        return {'status': current['status'] if current is not None else 'missing'}

    async def _run_workflow(self, job, *, execution):
        lost = asyncio.Event()

        async def checkpoint(value):
            if not isinstance(value, dict):
                raise ValueError('Factor checkpoint must be an object')
            bounded_payload(value, MAX_CHECKPOINT_BYTES)
            if lost.is_set() or not await self.store.save_checkpoint(job, deepcopy(value)):
                raise FactorLeaseLost('Factor assessment lease expired')

        async def evaluate():
            accepted_manifest = deepcopy(job.get('questionnaireAgentManifest'))
            bind_questionnaire_execution(job, self._manifest_provider(), execution)
            if not await self.store.bind_execution(job, expected_manifest=accepted_manifest):
                raise FactorLeaseLost('Factor assessment execution no longer owns its lease')
            if job['pipelineVersion'] != self.pipeline_version:
                raise ValueError('Unsupported factor assessment pipeline version')
            snapshot = deepcopy(job['inputSnapshot'])
            if stable_hash(snapshot) != job['inputFingerprint']:
                raise ValueError('Factor assessment input fingerprint changed')
            snapshot['_factorEvidenceRuleSelection'] = resolve_questionnaire_manifest(
                job['questionnaireAgentManifest']
            )['evidenceRuleSelection']
            if job.get('checkpoint') is not None:
                snapshot['_factorCheckpoint'] = deepcopy(job['checkpoint'])
            result = await self.evaluator.evaluate(snapshot, checkpoint=checkpoint)
            if not isinstance(result, dict):
                raise ValueError('Factor assessment result must be an object')
            bounded_payload(result, MAX_RESULT_BYTES)
            return result

        work = asyncio.create_task(evaluate())

        async def heartbeat():
            while True:
                await asyncio.sleep(self.heartbeat_seconds)
                try:
                    renewed = await self.store.renew(job, lease_seconds=self.lease_seconds)
                except Exception:
                    renewed = False
                if not renewed:
                    lost.set()
                    work.cancel()
                    return

        renewal = asyncio.create_task(heartbeat())
        try:
            try:
                result = await asyncio.wait_for(work, self.evaluation_timeout_seconds)
            except asyncio.CancelledError:
                if not lost.is_set():
                    raise
                return
            except FactorLeaseLost:
                return
            except Exception as error:
                if lost.is_set():
                    return
                code = 'FACTOR_EVALUATION_FAILED'
                if isinstance(error, FactorPayloadTooLarge):
                    code = 'FACTOR_OUTPUT_TOO_LARGE'
                elif isinstance(error, TimeoutError):
                    code = 'FACTOR_EVALUATION_TIMEOUT'
                elif isinstance(error, ValueError):
                    code = 'FACTOR_EVALUATION_INVALID'
                await self.store.fail(
                    job,
                    error_code=code,
                    retryable=not isinstance(error, ValueError),
                    max_attempts=self.max_attempts,
                    delay_seconds=min(300, self.retry_base_seconds * 2 ** (job['attempts'] - 1)),
                )
            else:
                if not lost.is_set():
                    # Save failures propagate, leaving a leased recoverable job. A
                    # missing acknowledgement must not masquerade as a saved result.
                    try:
                        await self.store.complete(job, result)
                    except FactorPayloadTooLarge:
                        await self.store.fail(
                            job,
                            error_code='FACTOR_OUTPUT_TOO_LARGE',
                            retryable=False,
                            max_attempts=self.max_attempts,
                            delay_seconds=0,
                        )
        finally:
            renewal.cancel()
            with suppress(asyncio.CancelledError):
                await renewal

    async def get(self, user_id, questionnaire_id):
        required_text(user_id, 'userId')
        required_text(questionnaire_id, 'questionnaireId')
        read = getattr(self.questionnaire_store, 'get_for_factor_assessment', self.questionnaire_store.get_by_id)
        source = await read(user_id, questionnaire_id)
        if source is None:
            raise QuestionnaireNotFoundError('Questionnaire not found')
        if source['status'] != 'submitted':
            raise QuestionnaireConflictError('Submit the questionnaire before reading its factor assessment')
        intent = source.get('factorAssessment')
        if not intent:
            return unavailable_assessment(source)
        job = await self.store.get(user_id, questionnaire_id, intent['assessmentId'])
        result = {
            'questionnaireId': questionnaire_id,
            'sessionId': source['sessionId'],
            'assessmentId': intent['assessmentId'],
            'sourceRevision': source['sourceRevision'],
            'pipelineVersion': intent['pipelineVersion'],
            'preparationVersion': intent['preparationVersion'],
            'status': job['status'] if job else 'queued',
            'result': deepcopy(job.get('result')) if job and job['status'] == 'completed' else None,
            'retryable': bool(job.get('retryable')) if job else True,
        }
        if job:
            result.update(attempts=job['attempts'], updatedAt=_iso(job['updatedAt']))
            if job.get('errorCode'):
                result['errorCode'] = job['errorCode']
        if result['status'] in ('queued', 'running'):
            result['retryAfterSeconds'] = 3
        return result
