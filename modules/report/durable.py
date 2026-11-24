"""Durable report generation/recovery and independent callback delivery."""

from __future__ import annotations

import asyncio
import random
from contextlib import suppress
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import urlsplit
from uuid import uuid4

from modules.report.artifacts import EVALUATE_REPORT_TYPES
from modules.report.artifacts import REPORT_TYPES
from modules.report.artifacts import bind_report_batch
from modules.report.artifacts import note_report_documents
from modules.report.artifacts import reset_report_batch
from modules.report.artifacts import save_report_artifact
from modules.report.durable_ports import ACTIVE_GENERATION
from modules.report.durable_ports import CallbackTransport
from modules.report.durable_ports import ReportWorkflowStore
from modules.report.fallback_policy import REPORT_GENERATION_CONCURRENCY
from modules.report.schemas import ReportRequest
from modules.report.task_coordinator import report_request_fingerprint
from modules.report.terminal import failure_payload
from utils.log import getLogger
from utils.log.context import LogContext
from utils.log.context import bind_log_context
from utils.log.context import task_log_context

logger = getLogger(__name__)
DEFAULT_RETRY_DELAYS_SECONDS = (30, 120, 600, 3600)
_GENERATION_RETRY_INITIAL_SECONDS = 0.1
_GENERATION_RETRY_MAX_SECONDS = 5.0


def utcnow() -> datetime:
    return datetime.now(UTC)


def validate_callback_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('callback URL must be an HTTP(S) endpoint without embedded credentials')
    _ = parsed.port  # Validate a malformed port before accepting a durable job.
    return value


def next_delivery_time(
    attempts: int,
    now: datetime,
    *,
    retry_after: str | None = None,
    jitter: Callable[[], float] = random.random,
    retry_delays_seconds: tuple[int, ...] = DEFAULT_RETRY_DELAYS_SECONDS,
    jitter_ratio: float = 0.1,
) -> datetime:
    seconds = retry_delays_seconds[min(max(0, attempts - 1), len(retry_delays_seconds) - 1)]
    # Negative jitter staggers jobs without exceeding the configured delay.
    due = now + timedelta(seconds=seconds * (1 - jitter_ratio + jitter_ratio * jitter()))
    if retry_after:
        try:
            if retry_after.strip().isdigit():
                requested = now + timedelta(seconds=int(retry_after.strip()))
            else:
                requested = parsedate_to_datetime(retry_after)
                if requested.tzinfo is None:
                    requested = requested.replace(tzinfo=UTC)
            due = max(due, requested)
        except (ValueError, TypeError, OverflowError):
            pass
    return due


class DurableReportService:
    def __init__(
        self,
        *,
        store: ReportWorkflowStore,
        artifact_store,
        pipeline,
        callback_url: str,
        heartbeat_seconds: float = 30,
        concurrency: int = REPORT_GENERATION_CONCURRENCY,
        main_agent=None,
    ) -> None:
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
            raise ValueError('report generation concurrency must be a positive integer')
        self.store = store
        self.artifact_store = artifact_store
        self.pipeline = pipeline
        self.main_agent = main_agent
        self.callback_url = callback_url
        self.heartbeat_seconds = heartbeat_seconds
        self.concurrency = concurrency
        self.owner = f'report_generation_{uuid4().hex}'
        self._generation_lock = asyncio.Lock()
        self._generation_tasks: set[asyncio.Task] = set()
        self._recovery_lock = asyncio.Lock()
        self._accepting = True
        self._generation_retry_at: float | None = None
        self._generation_retry_failures = 0

    def stop_accepting(self) -> None:
        self._accepting = False

    async def submit(self, request: ReportRequest) -> dict:
        return await self.store.admit(
            request.model_dump(mode='json', by_alias=True, exclude_none=False),
            report_request_fingerprint(request),
            validate_callback_url(self.callback_url),
            **({'execution_manifest': self.main_agent.execution_manifest()} if self.main_agent is not None else {}),
        )

    @task_log_context(clear=True)
    async def drain_generation(self) -> int:
        """Fill free slots continuously, never wait for a whole batch to finish.

        All entry points share the same per-service running-task set. The lock
        covers only claims, so later requests can use remaining capacity while
        earlier reports run. Each invocation owns and awaits its child tasks;
        runtime cancellation cannot leave detached generators behind.
        """
        if not self._accepting:
            return 0
        pending: set[asyncio.Task] = set()
        claimed = 0
        pending_job = None
        loop = asyncio.get_running_loop()
        try:
            while True:
                async with self._generation_lock:
                    while self._accepting and len(self._generation_tasks) < self.concurrency:
                        if self._generation_retry_at is not None and self._generation_retry_at > loop.time():
                            break
                        self._generation_retry_at = None
                        try:
                            pending_job = None
                            job = await self.store.claim_generation(self.owner)
                        except Exception as error:
                            # A queue read failure must not cancel reports which
                            # already own valid leases. Retry the free capacity
                            # using shared bounded backoff, without waiting for siblings.
                            logger.warning('报告任务领取失败: error_type=%s', type(error).__name__)
                            self._generation_retry_failures += 1
                            self._generation_retry_at = loop.time() + min(
                                _GENERATION_RETRY_MAX_SECONDS,
                                _GENERATION_RETRY_INITIAL_SECONDS * (2 ** min(self._generation_retry_failures - 1, 16)),
                            )
                            break
                        self._generation_retry_failures = 0
                        if job is None:
                            break
                        pending_job = job
                        if not self._accepting:
                            await self._release_unstarted(job)
                            pending_job = None
                            break
                        generation = self._execute(job)
                        try:
                            task = asyncio.create_task(generation, name=f'report-job:{job["_id"]}')
                        except BaseException:
                            generation.close()
                            raise
                        pending_job = None
                        self._generation_tasks.add(task)
                        pending.add(task)
                        task.add_done_callback(self._generation_tasks.discard)
                        claimed += 1
                    if len(self._generation_tasks) >= self.concurrency:
                        self._generation_retry_at = None
                if not pending and (self._generation_retry_at is None or not self._accepting):
                    return claimed
                # A queue interruption must not leave free slots waiting for
                # slow siblings or the next periodic worker tick.
                retry = None
                waiters = set(pending)
                if self._generation_retry_at is not None and self._accepting:
                    retry = asyncio.create_task(asyncio.sleep(max(0, self._generation_retry_at - loop.time())))
                    waiters.add(retry)
                try:
                    done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    if retry is not None:
                        retry.cancel()
                        await asyncio.gather(retry, return_exceptions=True)
                done = done.intersection(pending)
                pending.difference_update(done)
                for task in done:
                    # Lease loss cancels only this job, not its healthy peers.
                    if not task.cancelled() and (error := task.exception()) is not None:
                        logger.error('报告任务执行异常: error_type=%s', type(error).__name__)
                # Immediately refill any vacated slots, without awaiting peers
                # or requiring another request / periodic dispatch tick.
        finally:
            for task in pending:
                if not task.done():
                    task.cancel()
            cleanup = asyncio.gather(*pending, return_exceptions=True)
            while not cleanup.done():
                with suppress(asyncio.CancelledError):
                    await asyncio.shield(cleanup)
            if pending_job is not None:
                await self._release_unstarted(pending_job)

    async def _release_unstarted(self, job: dict) -> bool:
        try:
            async with asyncio.timeout(5):
                return await self.store.release_generation(job, known_not_started=True)
        except Exception as error:
            logger.warning('报告未启动任务释放失败: error_type=%s', type(error).__name__)
            return False

    async def _heartbeat(self, job: dict, owner_task: asyncio.Task) -> None:
        while True:
            await asyncio.sleep(min(self.heartbeat_seconds, max(1, self.store.lease_seconds / 3)))
            try:
                renewed = await self.store.renew_generation(job)
            except Exception as error:
                logger.warning('报告生成续租失败: final_report_id=%s error_type=%s', job['_id'], type(error).__name__)
                renewed = False
            if not renewed:
                # A stale process cannot keep producing/publishing results.
                owner_task.cancel()
                return

    @task_log_context(job='job', task='report-generation')
    async def _execute(self, job: dict, *, recovery: bool = False) -> None:
        lease_token = ACTIVE_GENERATION.set((job['_id'], job['leaseToken']))
        heartbeat = asyncio.create_task(self._heartbeat(job, asyncio.current_task()))
        result = None
        error_code = 'REPORT_GENERATION_INTERRUPTED' if recovery else 'REPORT_GENERATION_FAILED'
        try:
            if not recovery:
                try:
                    if not await self.store.mark_generation_started(job):
                        return
                    result = await self._generate_result(job)
                except Exception as error:
                    from modules.report.agent import ReportExecutionVersionError

                    if isinstance(error, ReportExecutionVersionError):
                        error_code = error.code
                    logger.error('报告生成失败: final_report_id=%s error_type=%s', job['_id'], type(error).__name__)
            if await self.store.finalize_generation(job, error_code):
                await self._complete_results(job, error_code=error_code, result=result)
        except asyncio.CancelledError:
            # The durable lease expires and recovery emits terminal failures.
            raise
        except Exception as error:
            logger.error('报告结果持久化待恢复: final_report_id=%s error_type=%s', job['_id'], type(error).__name__)
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            ACTIVE_GENERATION.reset(lease_token)

    async def _generate_result(self, job: dict):
        if self.main_agent is not None:
            return await self.main_agent.execute(job)
        return await self.pipeline.execute_and_callback(
            ReportRequest.model_validate(job['request']),
            final_report_id=job['_id'],
            defer_callbacks=True,
        )

    async def _complete_results(self, job: dict, *, error_code: str, result=None) -> None:
        token = bind_report_batch(
            store=self.artifact_store,
            final_report_id=job['_id'],
            user_id=job['userId'],
            session_id=job['sessionId'],
            defer_callbacks=True,
        )
        try:
            artifacts = await self.store.artifacts_for(job['_id'])
            for artifact in artifacts:
                note_report_documents(artifact.get('documents') or [])
            existing = {item['reportType']: item['body'] for item in artifacts}
            # A transient storage failure during generation may have recovered
            # by now. Persist the original fixed result, without another model call.
            if result is not None:
                payloads = [result.evaluate_payload, *result.followup_payloads]
                for payload in payloads:
                    report_type = payload.get('type')
                    if report_type in REPORT_TYPES and report_type not in existing:
                        await save_report_artifact(
                            report_type,
                            payload,
                            generation_status=payload.get('status', 'success'),
                        )
                existing = {item['reportType']: item['body'] for item in await self.store.artifacts_for(job['_id'])}
            for report_type in EVALUATE_REPORT_TYPES:
                if report_type not in existing:
                    reason = error_code
                    if (
                        not reason.endswith('INTERRUPTED')
                        and report_type != 'evaluate'
                        and existing.get('evaluate', {}).get('status') == 'failed'
                    ):
                        reason = 'REPORT_DEPENDENCY_FAILED'
                    body = failure_payload(report_type, job, error_code=reason, evaluate=existing.get('evaluate'))
                    await save_report_artifact(report_type, body, generation_status='failed')
                    # Re-read the winning immutable result if a writer raced us.
                    existing = {item['reportType']: item['body'] for item in await self.store.artifacts_for(job['_id'])}
                await self.store.ensure_delivery(job['_id'], report_type)
            # Historical batches may already contain other frozen report kinds.
            # Preserve their original bodies/idempotency keys and delivery state;
            # only refrain from inventing new unrelated report placeholders.
            for report_type in existing.keys() - set(EVALUATE_REPORT_TYPES):
                await self.store.ensure_delivery(job['_id'], report_type)
            await self.store.apply_redrive(job)
            await self.store.finish_reconciliation(job)
        finally:
            reset_report_batch(token)

    @task_log_context(clear=True)
    async def recover_pending(self) -> int:
        if not self._accepting or self._recovery_lock.locked():
            return 0
        async with self._recovery_lock:
            recovered = 0
            for _ in range(100):
                if not self._accepting:
                    break
                job = await self.store.claim_generation(self.owner, expired=True)
                if job is None:
                    break
                await self._execute(job, recovery=True)
                recovered += 1
            for job in await self.store.reconciliation_jobs():
                with bind_log_context(
                    LogContext(
                        user_id=job.get('userId'),
                        session_id=job.get('sessionId'),
                        task_id=job.get('_id'),
                    )
                ):
                    await self.store.apply_redrive(job)
                    for artifact in await self.store.artifacts_for(job['_id']):
                        await self.store.ensure_delivery(job['_id'], artifact['reportType'])
                    if job.get('generationState') == 'completed' or job.get('durableVersion') != 1:
                        await self.store.finish_reconciliation(job)
            documents = getattr(self.pipeline, 'document_source_factory', None)
            recover_documents = getattr(documents, 'recover_document_submissions', None)
            if self._accepting and callable(recover_documents):
                recovered += await recover_documents()
            return recovered


class ReportCallbackWorker:
    def __init__(
        self,
        *,
        store: ReportWorkflowStore,
        transport: CallbackTransport,
        clock: Callable = utcnow,
        jitter: Callable = random.random,
        concurrency: int = 4,
        timeout_seconds: float = 15,
        batch_size: int = 100,
        retry_delays_seconds: tuple[int, ...] = DEFAULT_RETRY_DELAYS_SECONDS,
        jitter_ratio: float = 0.1,
        backlog_log_interval_seconds: float = 300,
        callback_scope: str = 'report',
    ) -> None:
        self.store = store
        self.transport = transport
        self.clock = clock
        self.jitter = jitter
        self.concurrency = concurrency
        self.timeout_seconds = timeout_seconds
        self.batch_size = batch_size
        self.retry_delays_seconds = retry_delays_seconds
        self.jitter_ratio = jitter_ratio
        self.backlog_log_interval_seconds = backlog_log_interval_seconds
        self.callback_scope = callback_scope
        self.owner = f'report_delivery_{uuid4().hex}'
        self._lock = asyncio.Lock()
        # A slot covers claim I/O as well as delivery/acknowledgement I/O.
        # Concurrent ready/timer drains therefore cannot over-reserve leases.
        self._delivery_tasks: set[asyncio.Task] = set()
        self._accepting = True
        self._next_backlog_log = datetime.min.replace(tzinfo=UTC)

    def stop_accepting(self) -> None:
        self._accepting = False

    @task_log_context(clear=True)
    async def drain_pending(self) -> int:
        if not self._accepting:
            return 0
        pending: set[asyncio.Task] = set()
        processed = launched = 0
        can_claim = True

        async def deliver_next() -> bool:
            nonlocal processed
            if not self._accepting:
                return False
            try:
                job = await self.store.claim_delivery(self.owner)
            except Exception as error:
                logger.warning(
                    '报告回调领取失败: error_type=%s callback_scope=%s', type(error).__name__, self.callback_scope
                )
                return False
            if job is None:
                return False
            # Start each claimed item immediately; no later claim can strand
            # it in an unstarted batch. A stop during claim still finishes it.
            processed += 1
            await self._deliver(job)
            return True

        try:
            while True:
                async with self._lock:
                    while (
                        self._accepting
                        and can_claim
                        and launched < self.batch_size
                        and len(self._delivery_tasks) < self.concurrency
                    ):
                        task = asyncio.create_task(deliver_next(), name='report-delivery-slot')
                        self._delivery_tasks.add(task)
                        pending.add(task)
                        task.add_done_callback(self._delivery_tasks.discard)
                        launched += 1
                if not pending:
                    break
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if task.cancelled():
                        # An item cancellation is isolated. Its durable lease
                        # expires and retries the unchanged body/idempotency key.
                        continue
                    error = task.exception()
                    if error is not None:
                        logger.error(
                            '报告回调任务异常: error_type=%s callback_scope=%s',
                            type(error).__name__,
                            self.callback_scope,
                        )
                    elif not task.result():
                        # No work / transient claim failure: avoid busy polling.
                        # A later ready/timer drain can fill the shared free slots
                        # even while this call is still waiting on slow siblings.
                        can_claim = False
        finally:
            # Each invocation owns only its tasks; cancelling one drain must
            # neither detach them nor cancel another drain's in-flight items.
            for task in pending:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        await self._log_backlog()
        return processed

    async def _log_backlog(self) -> None:
        if self.clock() < self._next_backlog_log:
            return
        # Reserve the logging interval before I/O so simultaneous drains do not
        # duplicate backlog queries. Logging failure cannot fail delivered work.
        self._next_backlog_log = self.clock() + timedelta(seconds=self.backlog_log_interval_seconds)
        try:
            summary = await self.store.backlog()
            logger.info(
                '报告回调积压: pending=%s oldest_pending_at=%s oldest_wait_seconds=%s callback_scope=%s',
                summary['pending'],
                summary['oldestPendingAt'],
                summary['oldestWaitSeconds'],
                self.callback_scope,
            )
        except Exception as error:
            logger.warning(
                '报告回调积压查询失败: error_type=%s callback_scope=%s', type(error).__name__, self.callback_scope
            )

    @task_log_context(job='job', task='report-callback')
    async def _deliver(self, job: dict) -> None:
        retry_after = None
        try:
            body = await self.store.delivery_body(job)
            async with asyncio.timeout(self.timeout_seconds):
                response = await self.transport.post_once(
                    job['targetUrl'],
                    body,
                    idempotency_key=job['_id'],
                )
            if 200 <= response.status < 300:
                # If this write fails, the expired lease will retry the same
                # body/key. The receiver may already have committed it.
                acknowledged = await self.store.acknowledge_delivery(job)
                if acknowledged:
                    logger.info(
                        '报告回调已确认: final_report_id=%s type=%s attempts=%s callback_scope=%s',
                        job['finalReportId'],
                        job['reportType'],
                        job['attempts'],
                        self.callback_scope,
                    )
                return
            retry_after = response.retry_after
            error = {'code': f'HTTP_{response.status}', 'message': '回调接收方未确认接收'}
        except Exception as failure:
            # No payload, signed URL, exception stack or response body in logs.
            error = {'code': type(failure).__name__, 'message': '回调投递或确认保存失败'}
        due = next_delivery_time(
            job['attempts'],
            self.clock(),
            retry_after=retry_after,
            jitter=self.jitter,
            retry_delays_seconds=self.retry_delays_seconds,
            jitter_ratio=self.jitter_ratio,
        )
        try:
            saved = await self.store.retry_delivery(job, next_retry_at=due, error=error)
            if saved:
                logger.warning(
                    '报告回调待重试: final_report_id=%s type=%s target_host=%s attempts=%s '
                    'error_code=%s next_retry_at=%s callback_scope=%s',
                    job['finalReportId'],
                    job['reportType'],
                    urlsplit(job['targetUrl']).hostname,
                    job['attempts'],
                    error['code'],
                    due.isoformat(),
                    self.callback_scope,
                )
        except Exception as failure:
            logger.error(
                '报告回调状态待租约恢复: delivery_id=%s error_type=%s callback_scope=%s',
                job['_id'],
                type(failure).__name__,
                self.callback_scope,
            )
