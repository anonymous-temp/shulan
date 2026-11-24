"""Gut-barrier-only durable orchestration; no legacy report mutations."""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import suppress
from copy import deepcopy
from datetime import UTC
from zoneinfo import ZoneInfo

from utils.log import getLogger
from utils.log.context import task_log_context

from .agent import GutBarrierExecutionVersionError
from .policy import EXPLANATION_TYPE
from .policy import GUT_BARRIER_GENERATION_CONCURRENCY
from .policy import SERVICE_VERSION
from .publication import CALLBACK_SCHEMA_VERSION
from .publication import CALLBACK_TYPE
from .publication import project_callback_report
from .schemas import GutBarrierRequest

logger = getLogger(__name__)

_GENERATION_RETRY_INITIAL_SECONDS = 0.1
_GENERATION_RETRY_MAX_SECONDS = 5.0


def evaluation_day(value):
    # Mongo clients may return UTC-naive datetimes. Never interpret them as the
    # application host's local zone, especially around a Shanghai midnight.
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()


class GutBarrierService:
    def __init__(
        self,
        *,
        store,
        pipeline,
        transport=None,
        callback_url='',
        timeout_seconds=1800,
        concurrency=GUT_BARRIER_GENERATION_CONCURRENCY,
        callback_worker=None,
        heartbeat_seconds=30,
        followup_handler=None,
        delegation_worker=None,
        delegation_policy=None,
        main_agent=None,
    ):
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
            raise ValueError('gut-barrier generation concurrency must be a positive integer')
        self.store, self.pipeline = store, pipeline
        self.main_agent = main_agent
        self.transport, self.callback_url = transport, callback_url
        self.timeout_seconds, self.concurrency = timeout_seconds, concurrency
        self.callback_worker, self.heartbeat_seconds = callback_worker, heartbeat_seconds
        if followup_handler is not None and delegation_worker is not None:
            raise ValueError('legacy and durable follow-up paths cannot both run')
        self.followup_handler = followup_handler
        self.delegation_worker, self.delegation_policy = delegation_worker, delegation_policy
        self._admitting = True
        self._accepting = True
        self._generation_lock, self._recovery_lock = asyncio.Lock(), asyncio.Lock()
        self._generation_wakeup = asyncio.Event()

    def stop_admission(self):
        """Reject new submissions while the admitted-version worker drains."""
        self._admitting = False

    def stop_workers(self):
        """Stop claiming work; already supervised generation is awaited."""
        self._accepting = False
        self._generation_wakeup.set()
        if self.callback_worker is not None:
            self.callback_worker.stop_accepting()
        if self.delegation_worker is not None:
            self.delegation_worker.stop_accepting()

    def stop_accepting(self):
        # Preserve the runtime's existing full-shutdown contract. Deployment
        # drain is a separate stop_admission phase, not this SIGTERM hook.
        self.stop_admission()
        self.stop_workers()

    async def submit(self, request: GutBarrierRequest):
        if not self._admitting or not self._accepting:
            raise RuntimeError('service draining')
        payload = request.model_dump(mode='json', by_alias=True, exclude_none=True)
        version = SERVICE_VERSION + ':' + self.pipeline.version
        digest = hashlib.sha256(
            json.dumps(
                [version, payload], ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False
            ).encode()
        ).hexdigest()
        job = await self.store.admit(
            fingerprint=digest,
            request=payload,
            callback_url=self.callback_url,
            version=version,
            **({'execution_manifest': self.main_agent.execution_manifest()} if self.main_agent is not None else {}),
            **({'delegation_policy': self.delegation_policy.snapshot()} if self.delegation_policy is not None else {}),
        )
        self._generation_wakeup.set()
        return job

    @task_log_context(clear=True)
    async def drain_generation(self):
        if not self._accepting:
            return 0
        if self._generation_lock.locked():
            # A worker tick also discovers jobs admitted by other processes;
            # the single coordinator owns every generation task and claim slot.
            self._generation_wakeup.set()
            return 0
        async with self._generation_lock:
            tasks, claimed, observed_error = set(), 0, None
            pending_job = None
            version = SERVICE_VERSION + ':' + self.pipeline.version
            retry_failures, retry_at = 0, None
            loop = asyncio.get_running_loop()

            def defer_claim(error, *, stage):
                nonlocal observed_error, retry_failures, retry_at
                observed_error = observed_error or error
                retry_failures += 1
                exponent = min(retry_failures - 1, 16)
                delay = min(_GENERATION_RETRY_MAX_SECONDS, _GENERATION_RETRY_INITIAL_SECONDS * (2**exponent))
                retry_at = max(retry_at or 0, loop.time() + delay)
                logger.warning(
                    'gut-barrier generation coordinator will retry: stage=%s error_type=%s retry_seconds=%.3f',
                    stage,
                    type(error).__name__,
                    delay,
                )

            def reap():
                nonlocal retry_failures
                completed_cleanly, failures = False, []
                for task in tuple(tasks):
                    if task.done():
                        tasks.remove(task)
                        try:
                            task.result()
                        except BaseException as error:
                            failures.append(error)
                        else:
                            completed_cleanly = True
                if completed_cleanly:
                    retry_failures = 0
                for error in failures:
                    # The started job keeps its lease for fenced recovery;
                    # freeing this slot must not wait for healthy siblings.
                    defer_claim(error, stage='completion')

            try:
                while True:
                    self._generation_wakeup.clear()
                    reap()
                    while self._accepting and len(tasks) < self.concurrency:
                        if retry_at is not None:
                            if retry_at > loop.time():
                                break
                            retry_at = None
                        # The coordinator reserves the one remaining slot while
                        # claim awaits; no second coordinator can over-admit.
                        try:
                            # If this call has an uncertain outcome, never invent a
                            # job reference to release or execute. Its durable lease,
                            # if any, is owned by normal fenced recovery.
                            pending_job = None
                            pending_job = await self.store.claim_generation(version=version)
                        except Exception as error:
                            defer_claim(error, stage='claim')
                            break
                        retry_at = None
                        if pending_job is None:
                            break
                        if not self._accepting:
                            await self._release_unstarted(pending_job)
                            pending_job = None
                            break
                        # Start each known claim immediately. Later claim errors
                        # cannot abandon it or delay its heartbeat for a batch.
                        generation = self._generate(pending_job)
                        try:
                            task = asyncio.create_task(generation)
                        except BaseException:
                            generation.close()
                            raise
                        tasks.add(task)
                        claimed += 1
                        pending_job = None
                        await asyncio.sleep(0)
                        reap()
                    if not self._accepting:
                        # Shutdown stops refilling but never cancels healthy
                        # siblings or releases their leases early.
                        await asyncio.gather(*tasks, return_exceptions=True)
                        reap()
                        break
                    if not tasks and retry_at is None:
                        if self._generation_wakeup.is_set():
                            continue
                        break
                    wake = asyncio.create_task(self._generation_wakeup.wait())
                    retry = None
                    waiters = tasks | {wake}
                    if retry_at is not None:
                        retry = asyncio.create_task(asyncio.sleep(max(0, retry_at - loop.time())))
                        waiters.add(retry)
                    try:
                        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        helpers = [wake]
                        if retry is not None:
                            helpers.append(retry)
                        for helper in helpers:
                            helper.cancel()
                        # Only consume the wake task's cancellation, never a
                        # new cancellation of this coordinator during cleanup.
                        await asyncio.gather(*helpers, return_exceptions=True)
                if observed_error is not None:
                    raise observed_error
                return claimed
            finally:
                # Parent cancellation must not detach running children. Leases
                # of started jobs recover as interrupted, never re-entering the
                # clinical pipeline. Repeated cancellation cannot skip cleanup.
                for task in tasks:
                    task.cancel()
                if tasks:
                    cleanup = asyncio.gather(*tasks, return_exceptions=True)
                    while not cleanup.done():
                        with suppress(asyncio.CancelledError):
                            await asyncio.shield(cleanup)
                if pending_job is not None:
                    await self._release_unstarted(pending_job)

    @task_log_context(job='job', task='gut-barrier-release')
    async def _release_unstarted(self, job):
        try:
            async with asyncio.timeout(5):
                return await self.store.release_generation(job, known_not_started=True)
        except Exception as error:
            # An uncertain release stays leased for fenced recovery. Never
            # assume it requeued or hide an exception behind a false success.
            logger.warning('gut-barrier unstarted claim release unavailable: error_type=%s', type(error).__name__)
            return False

    @task_log_context(clear=True)
    async def recover_pending(self):
        """Close interrupted jobs once, without rerunning clinical generation."""
        if not self._accepting or self._recovery_lock.locked():
            return 0
        async with self._recovery_lock:
            recovered = 0
            while self._accepting and recovered < 100:
                job = await self.store.claim_generation(expired=True)
                if job is None:
                    break
                await self._generate(job, recovery=True)
                recovered += 1
            return recovered

    @task_log_context(job='job', task='gut-barrier-generation')
    async def _generate(self, job, *, recovery=False):
        current = asyncio.current_task()
        lost_lease = False
        completed_body = None

        async def heartbeat():
            nonlocal lost_lease
            while True:
                await asyncio.sleep(min(self.heartbeat_seconds, max(1, self.store.lease_seconds / 3)))
                try:
                    renewed = await self.store.renew_generation(job)
                except Exception:
                    renewed = False
                if not renewed:
                    lost_lease = True
                    current.cancel()
                    return

        renewal = asyncio.create_task(heartbeat())
        try:
            if job.get('jobType') == EXPLANATION_TYPE:
                # Historical children are no longer generated, recovered or sent.
                # The store excludes them; also fence any stale caller-held job.
                return
            try:
                if recovery:
                    raise InterruptedError('generation interrupted')
                request = GutBarrierRequest.model_validate(job['request'])
                async with asyncio.timeout(
                    (job.get('executionManifest') or {}).get('generationTimeoutSeconds', self.timeout_seconds)
                ):
                    if not await self.store.mark_generation_started(job):
                        # Another owner or an expired lease must not enter the
                        # model merely because this coroutine scheduled late.
                        return
                    evaluation_date = evaluation_day(job['createdAt'])
                    if self.main_agent is not None:
                        report, audit = await self.main_agent.generate(job, evaluation_date=evaluation_date)
                    else:
                        report, audit = await self.pipeline.generate(
                            request, report_id=job['_id'], evaluation_date=evaluation_date
                        )
                    if len(json.dumps(report, ensure_ascii=False, allow_nan=False).encode('utf-8')) > 8 * 1024 * 1024:
                        raise ValueError('report artifact exceeds bounded storage size')
                status = 'partial' if audit.get('partial') else 'success'
                public_report = project_callback_report(report, reference_audit=audit)
                audit = {**audit, 'reportDocument': report}
                body = {
                    'type': CALLBACK_TYPE,
                    'schemaVersion': CALLBACK_SCHEMA_VERSION,
                    'finalReportId': job['_id'],
                    'userId': job['userId'],
                    'sessionId': job['sessionId'],
                    'status': status,
                    'report': public_report,
                    'error': None,
                }
                # Keep enough room for job metadata under Mongo's document cap,
                # including the original internal document retained for audit.
                if (
                    len(json.dumps([body, audit], ensure_ascii=False, allow_nan=False).encode('utf-8'))
                    > 12 * 1024 * 1024
                ):
                    raise ValueError('report and audit exceed bounded storage size')
            except asyncio.CancelledError:
                raise
            except Exception as error:
                code = (
                    'GENERATION_INTERRUPTED'
                    if recovery
                    else 'GENERATION_TIMEOUT'
                    if isinstance(error, TimeoutError)
                    else error.code
                    if isinstance(error, GutBarrierExecutionVersionError)
                    else 'GENERATION_FAILED'
                )
                body = {
                    'type': CALLBACK_TYPE,
                    'schemaVersion': CALLBACK_SCHEMA_VERSION,
                    'finalReportId': job['_id'],
                    'userId': job['userId'],
                    'sessionId': job['sessionId'],
                    'status': 'failed',
                    'report': None,
                    'error': {'code': code, 'message': '报告生成未完成，请核对资料或联系服务方'},
                }
                audit = {'errorType': type(error).__name__}
            # A failed save is not converted to successful completion or delivery.
            if await self.store.complete(
                job,
                body=body,
                audit=audit,
                **(
                    {'delegation_policy': self.delegation_policy.snapshot()}
                    if self.delegation_policy is not None
                    else {}
                ),
            ):
                completed_body = body
        except asyncio.CancelledError:
            # Leave the lease for the same interruption recovery used by the
            # existing report service. A cancelled model call is not rerun.
            if not lost_lease:
                raise
        finally:
            renewal.cancel()
            with suppress(asyncio.CancelledError):
                await renewal

        # The generation lease ends at durable completion. Its heartbeat must
        # be stopped before the existing follow-up workflow runs, or the now
        # terminal main job would cancel a healthy interpretation after 30s.
        # Every terminal outcome is handed off, including failures/recovery;
        # neither primary callback delivery nor its acknowledgement is a gate.
        if completed_body is not None and self.followup_handler is not None:
            try:
                await self.followup_handler(deepcopy(completed_body), job['userId'], job['sessionId'])
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # The existing report method owns interpretation fallback and
                # delivery. Do not replace the main JSON or push its returned
                # interpretation a second time here.
                logger.error(
                    'gut-barrier follow-up invocation failed: report_id=%s error_type=%s',
                    job['_id'],
                    type(error).__name__,
                )

    @task_log_context(clear=True)
    async def drain_delivery(self):
        if not self._accepting:
            return 0
        if self.callback_worker is None:
            if self.transport is None:
                return 0
            # Lazy compatibility composition loads the shared Report worker
            # only when this service actually needs to drain callbacks.
            from modules.report.durable import ReportCallbackWorker

            self.callback_worker = ReportCallbackWorker(
                store=self.store, transport=self.transport, clock=self.store.clock, callback_scope='gut_barrier'
            )
        return await self.callback_worker.drain_pending()

    async def drain_delegation(self):
        if not self._accepting or self.delegation_worker is None:
            return 0
        return await self.delegation_worker.drain_pending()
