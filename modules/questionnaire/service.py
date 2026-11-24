"""Durable whole-questionnaire lifecycle, separate from mutable Chat sessions."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from contextlib import suppress
from copy import deepcopy
from datetime import datetime
from datetime import timezone
from uuid import uuid4

from agents import AgentPort
from agents import AgentRequest
from agents import AgentRunContext
from agents.errors import AgentConfigurationError
from agents.errors import AgentProtocolError
from modules.questionnaire.agent import QuestionnaireAgent
from modules.questionnaire.agent import QuestionnaireExecutionVersionError
from modules.questionnaire.agent import bind_questionnaire_execution
from modules.questionnaire.evidence import QuestionnaireEvidenceVersionError
from modules.questionnaire.factor_tasks import MAX_PREPARATION_BYTES
from modules.questionnaire.factor_tasks import bounded_payload
from modules.questionnaire.factor_tasks import factor_assessment_intent
from modules.questionnaire.factor_tasks import unavailable_assessment
from modules.questionnaire.profile_source import acceptance_selection
from modules.questionnaire.profile_source import prepare_submission_source
from modules.questionnaire.schemas import QuestionnaireConflictError
from modules.questionnaire.schemas import QuestionnaireExpiredError
from modules.questionnaire.schemas import QuestionnaireNotFoundError
from modules.questionnaire.schemas import QuestionnaireValidationError
from modules.questionnaire.schemas import normalize_answers
from modules.questionnaire.schemas import normalize_file_list
from modules.questionnaire.schemas import required_text
from modules.questionnaire.schemas import stable_hash
from modules.questionnaire.schemas import validate_questionnaire
from utils.identifiers import create_session_lease_id

logger = logging.getLogger(__name__)
READY_TTL_SECONDS = 7200
GENERATION_TIMEOUT_SECONDS = 600
MAX_GENERATION_ATTEMPTS = 3
SUBMISSION_METADATA_RESERVE_BYTES = 64 * 1024
GENERATED_FIELDS = (
    'questionnaire',
    'reports',
    'guidance',
    'provenance',
    'artifacts',
    'mode',
    'processing',
    'factorPreparation',
)


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class QuestionnaireService:
    def __init__(
        self,
        *,
        store,
        generator,
        lease_manager,
        session_repository,
        legacy_session_cache=None,
        task_spawner=None,
        profile_delivery=None,
        factor_tasks=None,
        main_agent=None,
        manifest_provider=None,
        profile_source_guard=None,
        profile_source_delivery=None,
        clock=time.time,
    ):
        self.store = store
        self.generator = generator
        self.lease_manager = lease_manager
        self.session_repository = session_repository
        self.legacy_session_cache = legacy_session_cache
        self.task_spawner = task_spawner or asyncio.create_task
        self.profile_delivery = profile_delivery
        self.factor_tasks = factor_tasks
        self.clock = clock
        self._tasks = {}
        self.profile_source_guard = profile_source_guard
        self.profile_source_delivery = profile_source_delivery
        self._main_agent_ready = True
        self._domain_agent = QuestionnaireAgent(
            generation=self._execute_generation,
            factors=getattr(factor_tasks, '_execute_factor_task', None),
            manifest_provider=manifest_provider,
        )
        if main_agent is not None and not isinstance(main_agent, AgentPort):
            raise TypeError('main_agent must implement AgentPort')
        self._main_agent = self._domain_agent if main_agent is None else main_agent
        bind = getattr(factor_tasks, 'bind_main_agent', None)
        if bind is not None:
            bind(self._main_agent, self._domain_agent.execution_manifest)

    @property
    def agent_port(self):
        return self._main_agent

    def set_agent_readiness(self, ready):
        self._main_agent_ready = ready
        setter = getattr(self.factor_tasks, 'set_agent_readiness', None)
        if setter is not None:
            setter(ready)

    def _require_agent_ready(self):
        if not self._main_agent_ready:
            raise AgentConfigurationError('Questionnaire main Agent module is unavailable')

    @asynccontextmanager
    async def _lease(self, user_id, session_id):
        lease = await self.lease_manager.try_acquire(
            create_session_lease_id(user_id, session_id),
            uuid4().hex,
        )
        if lease is None:
            raise QuestionnaireConflictError('Session is processing another request; retry after reading its status')
        lost = asyncio.Event()

        async def guard():
            if lost.is_set() or not await self.lease_manager.renew(lease):
                lost.set()
                raise QuestionnaireConflictError('Session ownership expired; read its current status')

        async def heartbeat():
            while True:
                await asyncio.sleep(max(0.01, min(30, getattr(lease, 'ttl_seconds', 300) / 3)))
                try:
                    await guard()
                except Exception:
                    lost.set()
                    return

        renewal = asyncio.create_task(heartbeat())
        try:
            yield guard
        finally:
            renewal.cancel()
            with suppress(asyncio.CancelledError):
                await renewal
            await self.lease_manager.release(lease)

    async def _save(self, snapshot, guard):
        await guard()
        expected = snapshot['revision']
        draft = deepcopy(snapshot)
        draft['revision'] = uuid4().hex
        draft['updatedAt'] = _iso(self.clock())
        if not await self.store.save(draft, expected_revision=expected):
            raise QuestionnaireConflictError('Questionnaire changed; read its current state')
        snapshot.clear()
        snapshot.update(draft)

    async def _write_owner(self, snapshot, guard):
        """Only CAS the tiny questionnaire marker; a Chat document is immutable here."""
        await guard()
        current = await self.session_repository.get(snapshot['userId'], snapshot['sessionId'])
        expected = deepcopy(current)
        allowed = not current or (
            current.get('type') == 'questionnaire'
            and current.get('questionnaireId') in (None, snapshot['questionnaireId'])
        )
        if current and current.get('type') == 'questionnaire' and not allowed:
            latest = await self.store.get_current(snapshot['userId'], snapshot['sessionId'])
            previous = await self.store.get_by_id(snapshot['userId'], current['questionnaireId'])
            allowed = bool(
                latest
                and latest['questionnaireId'] == snapshot['questionnaireId']
                and previous
                and previous['status'] == 'expired'
            )
        if not allowed:
            if current.get('type') == 'chat' and snapshot['status'] == 'submitted':
                return
            raise QuestionnaireConflictError('Session belongs to another workflow')
        marker = {
            'userId': snapshot['userId'],
            'sessionId': snapshot['sessionId'],
            'type': 'questionnaire',
            'owner': 'questionnaire',
            'ownerRevision': uuid4().hex,
            'questionnaireId': snapshot['questionnaireId'],
            'status': 'completed' if snapshot['status'] == 'submitted' else 'active',
            'createdAt': (current or {}).get('createdAt', snapshot['createdAt']),
        }
        if snapshot['status'] == 'submitted':
            marker['questionnaireSubmittedAt'] = snapshot['submittedAt']
        # Revalidate after every awaited read; the repository CAS also protects
        # the interval between this check and the actual Mongo write.
        await guard()
        saved = await self.session_repository.save_questionnaire_owner(
            marker['userId'],
            marker['sessionId'],
            marker,
            expected=expected,
        )
        if not saved:
            latest = await self.session_repository.get(marker['userId'], marker['sessionId'])
            if latest and latest.get('type') == 'chat' and snapshot['status'] == 'submitted':
                return
            raise QuestionnaireConflictError('Session ownership changed; restore the current state')

    async def create(self, user_id, session_id, file_list=None):
        self._require_agent_ready()
        required_text(user_id, 'userId')
        required_text(session_id, 'sessionId')
        files = normalize_file_list(file_list)
        request_hash = stable_hash({'fileList': files})
        # An accepted generation owns the business lease while processing its
        # reports. Identical retries only read that durable instance, so they
        # must not compete with the worker for its long-running lease.
        existing = await self.store.get_current(user_id, session_id)
        if existing and existing['status'] != 'expired' and not self._is_due(existing):
            if existing['requestHash'] != request_hash:
                raise QuestionnaireConflictError('The existing session has different generation inputs')
            if existing['status'] in ('queued', 'generating') or (
                existing['status'] == 'failed' and existing.get('retryable')
            ):
                self._schedule(user_id, existing['questionnaireId'])
            return self._public(existing)
        async with self._lease(user_id, session_id) as guard:
            existing = await self.store.get_current(user_id, session_id)
            if existing:
                existing = await self._expire_if_due(existing, guard)
                if existing['status'] not in ('expired',):
                    if existing['requestHash'] != request_hash:
                        raise QuestionnaireConflictError('The existing session has different generation inputs')
                    if existing['status'] == 'submitted':
                        await self._write_owner(existing, guard)
                    result = self._public(existing)
                    schedule = existing['status'] in ('queued', 'generating') or (
                        existing['status'] == 'failed' and existing.get('retryable')
                    )
                    if schedule:
                        self._schedule(user_id, existing['questionnaireId'])
                    return result
            current = await self.session_repository.get(user_id, session_id)
            if current and current.get('type') != 'questionnaire':
                raise QuestionnaireConflictError('Session already belongs to Record or Chat')
            if self.legacy_session_cache is not None:
                cached = await self.legacy_session_cache.get(user_id, session_id)
                if cached and cached.get('type', 'qa') != 'questionnaire':
                    raise QuestionnaireConflictError('Session already belongs to Record')
            now = self.clock()
            snapshot = {
                'userId': user_id,
                'sessionId': session_id,
                'questionnaireId': uuid4().hex,
                'status': 'queued',
                'schemaVersion': '1.0.0',
                'revision': uuid4().hex,
                'requestHash': request_hash,
                'fileList': files,
                'createdAt': _iso(now),
                'updatedAt': _iso(now),
                'generationAttempts': 0,
                'nextAttemptAt': now,
                'cleanupPending': False,
                'questionnaireAgentManifest': self._domain_agent.execution_manifest(),
            }
            if self.profile_source_delivery is not None:
                snapshot['profileSourceSelection'] = acceptance_selection(
                    user_id, self.profile_source_delivery.execution_selection()
                )
            await guard()
            if not await self.store.insert(snapshot):
                raise QuestionnaireConflictError('Questionnaire creation raced another request')
            # Replacing an expired marker uses the same fenced writer as
            # first ownership and submission; never use an unconditional save.
            await self._write_owner(snapshot, guard)
        self._schedule(user_id, snapshot['questionnaireId'])
        return self._public(snapshot)

    def _schedule(self, user_id, questionnaire_id):
        if not self._main_agent_ready:
            return
        key = (user_id, questionnaire_id)
        if key in self._tasks and not self._tasks[key].done():
            return
        task = self.task_spawner(self._generate(user_id, questionnaire_id))
        self._tasks[key] = task

        def finished(done):
            if self._tasks.get(key) is done:
                self._tasks.pop(key, None)
            if not done.cancelled():
                error = done.exception()
                if error:
                    logger.warning('Questionnaire worker failed: %s', type(error).__name__)

        task.add_done_callback(finished)

    async def _generate(self, user_id, questionnaire_id):
        self._require_agent_ready()
        snapshot = await self.store.get_by_id(user_id, questionnaire_id)
        if snapshot is None:
            return
        context = AgentRunContext.create(user_id=user_id, session_id=snapshot['sessionId'])
        return await self._main_agent.invoke(
            AgentRequest(
                {
                    'operation': 'generate',
                    'userId': user_id,
                    'sessionId': snapshot['sessionId'],
                    'questionnaireId': questionnaire_id,
                }
            ),
            context,
            thread_id=questionnaire_id,
        )

    async def _execute_generation(self, user_id, questionnaire_id, *, session_id, execution):
        self._require_agent_ready()
        await self._generate_workflow(user_id, questionnaire_id, session_id=session_id, execution=execution)
        current = await self.store.get_by_id(user_id, questionnaire_id)
        return {'status': current['status'] if current is not None else 'missing'}

    async def _generate_workflow(self, user_id, questionnaire_id, *, session_id, execution):
        snapshot = await self.store.get_by_id(user_id, questionnaire_id)
        if snapshot is None:
            return
        if snapshot['sessionId'] != session_id:
            raise AgentProtocolError('Questionnaire stored scope differs from its Agent context')
        try:
            async with self._lease(user_id, snapshot['sessionId']) as guard:
                snapshot = await self.store.get_by_id(user_id, questionnaire_id)
                if snapshot is None or snapshot['status'] not in ('queued', 'generating', 'failed'):
                    return
                if snapshot['sessionId'] != session_id:
                    raise AgentProtocolError('Questionnaire stored scope changed during lease acquisition')
                if snapshot['status'] == 'failed' and not snapshot.get('retryable'):
                    return
                if snapshot.get('nextAttemptAt', 0) > self.clock():
                    return
                current = await self.store.get_current(user_id, snapshot['sessionId'])
                if current is None or current['questionnaireId'] != questionnaire_id:
                    return
                try:
                    bind_questionnaire_execution(snapshot, self._domain_agent.execution_manifest(), execution)
                except (QuestionnaireExecutionVersionError, QuestionnaireEvidenceVersionError):
                    snapshot.update(
                        status='failed',
                        retryable=False,
                        errorCode='QUESTIONNAIRE_GENERATION_FAILED',
                        cleanupAt=self.clock() + READY_TTL_SECONDS,
                    )
                    await self._save(snapshot, guard)
                    logger.warning(
                        'Questionnaire execution manifest is unavailable: questionnaire_id=%s', questionnaire_id
                    )
                    return
                await self._write_owner(snapshot, guard)
                if snapshot['generationAttempts'] >= MAX_GENERATION_ATTEMPTS:
                    snapshot.update(
                        status='failed',
                        retryable=False,
                        errorCode='GENERATION_RETRY_EXHAUSTED',
                        cleanupAt=self.clock() + READY_TTL_SECONDS,
                    )
                    await self._save(snapshot, guard)
                    return
                snapshot['status'] = 'generating'
                snapshot['generationAttempts'] += 1
                snapshot['nextAttemptAt'] = self.clock() + GENERATION_TIMEOUT_SECONDS
                await self._save(snapshot, guard)
                generation_base = {
                    key: deepcopy(value) for key, value in snapshot.items() if key not in GENERATED_FIELDS
                }
                generation_started = time.monotonic()
                try:
                    generated = await asyncio.wait_for(
                        self.generator.generate(
                            user_id=user_id,
                            questionnaire_id=questionnaire_id,
                            file_list=deepcopy(snapshot['fileList']),
                        ),
                        timeout=GENERATION_TIMEOUT_SECONDS,
                    )
                    questionnaire = validate_questionnaire(generated['questionnaire'])
                    stable_hash(generated)
                    if 'factorPreparation' in generated:
                        bounded_payload(generated['factorPreparation'], MAX_PREPARATION_BYTES)
                    snapshot.pop('factorPreparation', None)
                    snapshot.update(
                        {
                            key: deepcopy(generated[key])
                            for key in (
                                'reports',
                                'guidance',
                                'provenance',
                                'artifacts',
                                'mode',
                                'processing',
                                'factorPreparation',
                            )
                            if key in generated
                        }
                    )
                    snapshot['questionnaire'] = questionnaire
                    snapshot['status'] = 'ready'
                    snapshot['readyAt'] = _iso(self.clock())
                    snapshot['expiresAt'] = self.clock() + READY_TTL_SECONDS
                    self._validate_ready_capacity(snapshot)
                    ready_hook = getattr(self.generator, 'ready_artifacts', None)
                    if ready_hook is not None:
                        await ready_hook(deepcopy(snapshot), snapshot['expiresAt'])
                    snapshot.pop('errorCode', None)
                    snapshot.pop('retryable', None)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    # Preserve only a safe category; upstream errors may contain
                    # signed URLs or provider details.
                    # A large or invalid generated payload must not make the failure
                    # state itself unwritable. Artifact cleanup uses source ownership.
                    snapshot.clear()
                    snapshot.update(generation_base)
                    snapshot['status'] = 'failed'
                    snapshot['errorCode'] = 'QUESTIONNAIRE_GENERATION_FAILED'
                    snapshot['retryable'] = (
                        not isinstance(error, QuestionnaireEvidenceVersionError)
                        and snapshot['generationAttempts'] < MAX_GENERATION_ATTEMPTS
                    )
                    snapshot['nextAttemptAt'] = self.clock() + 15
                    snapshot['cleanupAt'] = self.clock() + READY_TTL_SECONDS
                    await self._save(snapshot, guard)
                    logger.warning(
                        'questionnaire_event=generation_failed user_id=%s questionnaire_id=%s error_type=%s',
                        user_id,
                        questionnaire_id,
                        type(error).__name__,
                    )
                else:
                    # A lost Mongo acknowledgement must not overwrite a ready
                    # publication with a failure state or regenerate its schema.
                    await self._save(snapshot, guard)
                    reports = snapshot.get('reports', [])
                    usable = sum(
                        isinstance(report, dict)
                        and report.get('status') in ('complete', 'partial')
                        and bool(report.get('interpretation'))
                        for report in reports
                    )
                    mode = snapshot.get('mode')
                    if mode not in ('basic', 'report_informed'):
                        mode = 'unknown'
                    reason = (snapshot.get('processing') or {}).get('reason')
                    if reason not in (
                        None,
                        'no_reports',
                        'reports_unavailable',
                        'planner_unavailable',
                        'factor_preparation_unavailable',
                    ):
                        reason = 'unknown'
                    logger.info(
                        'questionnaire_event=ready user_id=%s session_id=%s questionnaire_id=%s '
                        'generation_elapsed_ms=%.2f report_count=%s usable_report_count=%s mode=%s fallback_reason=%s',
                        user_id,
                        snapshot['sessionId'],
                        questionnaire_id,
                        (time.monotonic() - generation_started) * 1000,
                        len(reports),
                        usable,
                        mode,
                        reason or 'none',
                    )
        except QuestionnaireConflictError:
            return

    async def get(self, user_id, session_id):
        required_text(user_id, 'userId')
        required_text(session_id, 'sessionId')
        snapshot = await self.store.get_current(user_id, session_id)
        if snapshot is None:
            raise QuestionnaireNotFoundError('Questionnaire not found')
        if self._is_due(snapshot):
            # Project the public deadline immediately; the background owner
            # persists expiration and cleans artifacts without a GET trigger.
            snapshot = deepcopy(snapshot)
            snapshot['status'] = 'expired'
        return self._public(snapshot)

    async def submit(self, user_id, questionnaire_id, answers):
        required_text(user_id, 'userId')
        required_text(questionnaire_id, 'questionnaireId')
        snapshot = await self.store.get_by_id(user_id, questionnaire_id)
        if snapshot is None:
            raise QuestionnaireNotFoundError('Questionnaire not found')
        async with self._lease(user_id, snapshot['sessionId']) as guard:
            snapshot = await self.store.get_by_id(user_id, questionnaire_id)
            if snapshot['status'] == 'submitted':
                _, canonical = normalize_answers(snapshot['questionnaire'], answers)
                fingerprint = stable_hash(canonical)
                if snapshot['answerHash'] != fingerprint:
                    raise QuestionnaireConflictError('Questionnaire was submitted with different answers')
                return deepcopy(snapshot['submissionResponse'])
            current = await self.store.get_current(user_id, snapshot['sessionId'])
            if current is None or current['questionnaireId'] != questionnaire_id:
                raise QuestionnaireConflictError('Questionnaire is no longer the current instance')
            snapshot = await self._expire_if_due(snapshot, guard)
            if snapshot['status'] == 'expired':
                raise QuestionnaireExpiredError('Questionnaire expired; generate a new instance')
            if snapshot['status'] != 'ready':
                raise QuestionnaireConflictError('Questionnaire is not ready for submission')
            # Idempotency follows the frozen survey's canonical answer values.
            # Raw input is evidence from the first acceptance, never a conflict key.
            rows, canonical = normalize_answers(snapshot['questionnaire'], answers)
            fingerprint = stable_hash(canonical)
            pending = snapshot.get('pendingSubmission')
            if pending:
                if pending['answerHash'] != fingerprint:
                    raise QuestionnaireConflictError('Different answers were already accepted')
            else:
                proposed = {
                    'answerHash': fingerprint,
                    'rawAnswerHash': stable_hash(answers),
                    'rawAnswers': deepcopy(answers),
                    'answers': rows,
                    'canonicalAnswers': canonical,
                    'acceptedAt': _iso(self.clock()),
                }
                self._validate_submission_capacity(snapshot, proposed)
                snapshot['pendingSubmission'] = proposed
                await self._save(snapshot, guard)
            await self._commit_submission(snapshot, guard)
        await self._deliver(snapshot)
        return deepcopy(snapshot['submissionResponse'])

    def _submission_document(self, snapshot, pending):
        draft = deepcopy(snapshot)
        draft.update(
            {
                'status': 'submitted',
                'submittedAt': pending['acceptedAt'],
                'submissionId': snapshot['questionnaireId'],
                'sourceRevision': 1,
                'rawAnswers': pending['rawAnswers'],
                'rawAnswerHash': pending['rawAnswerHash'],
                'answers': pending['answers'],
                'canonicalAnswers': pending['canonicalAnswers'],
                'answerHash': pending['answerHash'],
                'profileIngestion': {'status': 'pending', 'jobId': None, 'updatedAt': _iso(self.clock())},
            }
        )
        intent_options = {'now': self.clock()}
        if self.factor_tasks is not None:
            intent_options['pipeline_version'] = self.factor_tasks.pipeline_version
        prepare_submission_source(draft)
        draft['factorAssessment'] = factor_assessment_intent(draft, **intent_options)
        draft.pop('pendingSubmission', None)
        draft['submissionResponse'] = self._public(draft)
        return draft

    def _validate_submission_capacity(self, snapshot, pending, *, reserve_bytes=0):
        check = getattr(self.store, 'validate_capacity', None)
        if check is None:
            return  # Existing injected stores may not have a document-size boundary.
        proposed = deepcopy(snapshot)
        proposed['pendingSubmission'] = deepcopy(pending)
        try:
            check(proposed, reserve_bytes=reserve_bytes)
            check(self._submission_document(snapshot, pending), reserve_bytes=reserve_bytes)
        except ValueError as error:
            raise QuestionnaireValidationError(
                'Questionnaire and answers exceed the submission storage budget'
            ) from error

    def _validate_ready_capacity(self, snapshot):
        check = getattr(self.store, 'validate_capacity', None)
        if check is None:
            return
        questions = [question for page in snapshot['questionnaire']['pages'] for question in page['elements']]
        # Normal nonempty text is limited to 20,000 characters by normalize_answers.
        # Raw whitespace and duplicate checkbox values have no schema length bound,
        # so the actual submitted payload is also checked before accepting it.
        text_reserve = sum(20000 * 4 * 5 for question in questions if question['type'] == 'text')
        check(snapshot, reserve_bytes=text_reserve + SUBMISSION_METADATA_RESERVE_BYTES)
        maximum_answers = {}
        for question in questions:
            if question['type'] == 'text':
                value = '\U00010000' * 20000
            elif question['type'] == 'radiogroup':
                value = max(
                    (choice['value'] for choice in question['choices']), key=lambda item: len(item.encode('utf-8'))
                )
            else:
                value = [choice['value'] for choice in question['choices']]
                if question.get('showNoneItem') and len(str(value).encode('utf-8')) < len("['none']"):
                    value = ['none']
            maximum_answers[question['name']] = value
        rows, canonical = normalize_answers(snapshot['questionnaire'], maximum_answers)
        pending = {
            'answerHash': '0' * 64,
            'rawAnswerHash': '0' * 64,
            'rawAnswers': maximum_answers,
            'answers': rows,
            'canonicalAnswers': canonical,
            'acceptedAt': _iso(self.clock()),
        }
        self._validate_submission_capacity(snapshot, pending, reserve_bytes=SUBMISSION_METADATA_RESERVE_BYTES)

    async def _commit_submission(self, snapshot, guard):
        pending = snapshot['pendingSubmission']
        self._validate_submission_capacity(snapshot, pending)
        draft = self._submission_document(snapshot, pending)
        hook = getattr(self.generator, 'pin_artifacts', None)
        if hook is not None:
            await hook(deepcopy(snapshot))
        snapshot.clear()
        snapshot.update(draft)
        await self._save(snapshot, guard)
        logger.info(
            'questionnaire_event=submitted user_id=%s session_id=%s questionnaire_id=%s source_revision=%s',
            snapshot['userId'],
            snapshot['sessionId'],
            snapshot['questionnaireId'],
            snapshot['sourceRevision'],
        )
        await self._write_owner(snapshot, guard)

    def _is_due(self, snapshot):
        return (
            snapshot['status'] == 'ready'
            and not snapshot.get('pendingSubmission')
            and snapshot['expiresAt'] <= self.clock()
        ) or (
            snapshot['status'] == 'failed'
            and not snapshot.get('retryable')
            and snapshot.get('cleanupAt', float('inf')) <= self.clock()
        )

    async def _expire_if_due(self, snapshot, guard):
        if self._is_due(snapshot):
            snapshot['status'] = 'expired'
            snapshot['cleanupPending'] = True
            await self._save(snapshot, guard)
            await self._cleanup(snapshot, guard)
        return snapshot

    async def _cleanup(self, snapshot, guard):
        artifact_count = len(snapshot.get('artifacts', []))
        hook = getattr(self.generator, 'expire_artifacts', None)
        if hook is not None:
            await hook(deepcopy(snapshot))
        for key in ('questionnaire', 'reports', 'guidance', 'provenance', 'artifacts', 'fileList', 'factorPreparation'):
            snapshot.pop(key, None)
        snapshot['cleanupPending'] = False
        await self._save(snapshot, guard)
        logger.info(
            'questionnaire_event=cleanup user_id=%s questionnaire_id=%s instances=1 artifact_refs=%s',
            snapshot['userId'],
            snapshot['questionnaireId'],
            artifact_count,
        )

    async def hasSession(self, user_id, session_id):
        return await self.store.get_current(user_id, session_id) is not None

    async def hasPendingSession(self, user_id, session_id):
        # Authoritative durable state, not Redis absence/TTL, is the gate.
        snapshot = await self.store.get_current(user_id, session_id)
        return snapshot is not None and snapshot['status'] != 'submitted'

    async def assertNoPendingSession(self, user_id, session_id):
        if await self.hasPendingSession(user_id, session_id):
            raise QuestionnaireConflictError('Submit this questionnaire before entering Chat')

    async def get_session_context(self, user_id, session_id):
        snapshot = await self.store.get_current(user_id, session_id)
        if snapshot is None or snapshot['status'] != 'submitted':
            return None
        from modules.questionnaire.context import submitted_context

        return submitted_context(snapshot)

    async def _deliver(self, snapshot):
        deliveries = []
        if (
            self.profile_delivery is not None
            or self.profile_source_guard is not None
            or self.profile_source_delivery is not None
        ):
            deliveries.append(self._deliver_profile(snapshot))
        if self.factor_tasks is not None and snapshot.get('factorAssessment'):
            deliveries.append(self.factor_tasks.ensure_queued(deepcopy(snapshot)))
        for outcome in await asyncio.gather(*deliveries, return_exceptions=True):
            if isinstance(outcome, BaseException):
                logger.warning(
                    'questionnaire_event=delivery_deferred user_id=%s questionnaire_id=%s error_type=%s',
                    snapshot['userId'],
                    snapshot['questionnaireId'],
                    type(outcome).__name__,
                )

    async def _deliver_profile(self, snapshot):
        ingestion = snapshot.get('profileIngestion', {})
        if not isinstance(ingestion, dict):
            return None
        if ingestion.get('deliveryMode') not in (None, 'legacy'):
            if self.profile_source_delivery is not None:
                # This entry point only records local work; external providers run in the worker.
                return await self.profile_source_delivery.ensure_queued(
                    user_id=snapshot['userId'], questionnaire_id=snapshot['questionnaireId']
                )
            return None
        if self.profile_source_guard is not None and await self.profile_source_guard(
            user_id=snapshot['userId'], questionnaire_id=snapshot['questionnaireId']
        ):
            return await self.store.mark_ingestion_source_managed(
                snapshot['userId'], snapshot['questionnaireId'], source_revision=snapshot['sourceRevision']
            )
        if self.profile_delivery is not None:
            return await self.profile_delivery(user_id=snapshot['userId'], questionnaire_id=snapshot['questionnaireId'])

    async def get_factor_assessment(self, user_id, questionnaire_id):
        if self.factor_tasks is not None:
            return await self.factor_tasks.get(user_id, questionnaire_id)
        required_text(user_id, 'userId')
        required_text(questionnaire_id, 'questionnaireId')
        read = getattr(self.store, 'get_for_factor_assessment', self.store.get_by_id)
        snapshot = await read(user_id, questionnaire_id)
        if snapshot is None:
            raise QuestionnaireNotFoundError('Questionnaire not found')
        if snapshot['status'] != 'submitted':
            raise QuestionnaireConflictError('Submit the questionnaire before reading its factor assessment')
        code = 'FACTOR_SERVICE_UNAVAILABLE' if snapshot.get('factorAssessment') else 'FACTOR_ASSESSMENT_NOT_REQUESTED'
        return unavailable_assessment(snapshot, code)

    async def drain_factor_assessments(self):
        return await self.factor_tasks.drain_pending() if self.factor_tasks is not None else 0

    async def recover_factor_assessments(self):
        return await self.factor_tasks.recover_pending() if self.factor_tasks is not None else 0

    async def recover_pending(self, *, limit=100):
        recovered = 0
        cleaned = 0
        generation_candidates = 0
        for snapshot in await self.store.list_recoverable(now=self.clock(), limit=limit):
            if snapshot['status'] in ('queued', 'generating', 'failed') and not self._is_due(snapshot):
                self._schedule(snapshot['userId'], snapshot['questionnaireId'])
                generation_candidates += 1
                continue
            try:
                async with self._lease(snapshot['userId'], snapshot['sessionId']) as guard:
                    snapshot = await self.store.get_by_id(snapshot['userId'], snapshot['questionnaireId'])
                    if snapshot is None:
                        continue
                    if snapshot['status'] == 'ready' and snapshot.get('pendingSubmission'):
                        await self._commit_submission(snapshot, guard)
                        recovered += 1
                    elif snapshot.get('cleanupPending'):
                        await self._cleanup(snapshot, guard)
                        cleaned += 1
                    else:
                        was_due = self._is_due(snapshot)
                        await self._expire_if_due(snapshot, guard)
                        cleaned += int(was_due and not snapshot.get('cleanupPending'))
            except QuestionnaireConflictError:
                continue
            except Exception as error:
                logger.warning('Questionnaire recovery deferred: %s', type(error).__name__)
        for snapshot in await self.store.list_pending_ingestion(limit=limit):
            await self._deliver(snapshot)
        if recovered or cleaned or generation_candidates:
            logger.info(
                'questionnaire_event=recovery submissions_completed=%s cleanups_completed=%s generation_candidates=%s',
                recovered,
                cleaned,
                generation_candidates,
            )
        return recovered

    @staticmethod
    def _public(snapshot):
        result = {
            key: deepcopy(snapshot[key])
            for key in (
                'userId',
                'sessionId',
                'questionnaireId',
                'schemaVersion',
                'status',
                'createdAt',
                'readyAt',
                'submittedAt',
                'submissionId',
                'sourceRevision',
                'questionnaire',
                'reports',
                'guidance',
                'mode',
                'processing',
                'errorCode',
                'retryable',
            )
            if key in snapshot
        }
        guidance = result.get('guidance')
        if isinstance(guidance, dict):
            for field, fields in (
                ('managerInterview', ('question', 'reason')),
                ('riskAssessment', ('priority', 'assessment')),
                ('suggestedTests', ('name', 'reason')),
            ):
                if field in guidance:
                    guidance[field] = [
                        {key: deepcopy(item[key]) for key in fields if key in item}
                        for item in guidance[field]
                        if isinstance(item, dict)
                    ]
        if snapshot['status'] not in ('ready', 'submitted'):
            for key in ('questionnaire', 'reports', 'guidance', 'mode', 'processing'):
                result.pop(key, None)
        if snapshot['status'] == 'ready' and snapshot.get('expiresAt') is not None:
            result['expiresAt'] = _iso(snapshot['expiresAt'])
        result['canChat'] = snapshot['status'] == 'submitted'
        result['submissionPending'] = snapshot['status'] == 'ready' and bool(snapshot.get('pendingSubmission'))
        if snapshot['status'] == 'submitted':
            result['answers'] = deepcopy(snapshot['canonicalAnswers'])
        if snapshot['status'] in ('queued', 'generating') or result['submissionPending']:
            result['retryAfterSeconds'] = 3
        elif snapshot['status'] == 'failed' and snapshot.get('retryable'):
            result['retryAfterSeconds'] = 15
        return result
