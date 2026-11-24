"""Public Report-owned durable use case for peer GutBarrier follow-up requests."""

from __future__ import annotations

import asyncio
import math
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import replace
from typing import Any
from typing import Protocol

from modules.report.artifacts import CONFLICT
from modules.report.artifacts import bind_report_batch
from modules.report.artifacts import content_hash
from modules.report.artifacts import reset_report_batch
from modules.report.artifacts import save_report_artifact
from modules.report.durable import DurableReportService
from modules.report.durable import validate_callback_url
from modules.report.terminal import failure_payload

FOLLOWUP_CONTRACT_VERSION = 'report.gut-barrier-followups.v1'
FOLLOWUP_EXECUTION_VERSION = 'report.gut-barrier-followups.legacy-v1'
GUT_BARRIER_FOLLOWUP_TYPES = ('explain_gutBarrier', 'opinion', 'instruction')


class FollowupConflictError(ValueError):
    """The same delegation identity was supplied with a different frozen input."""


@dataclass(frozen=True)
class GutBarrierFollowupRequest:
    user_id: str
    session_id: str
    source_final_report_id: str
    body: dict[str, Any]
    contract_version: str = FOLLOWUP_CONTRACT_VERSION

    def __post_init__(self):
        if self.contract_version != FOLLOWUP_CONTRACT_VERSION:
            raise ValueError('unsupported follow-up contract version')
        if any(
            not isinstance(v, str) or not v.strip()
            for v in (
                self.user_id,
                self.session_id,
                self.source_final_report_id,
            )
        ):
            raise ValueError('follow-up identity is required')
        expected = {
            'userId': self.user_id,
            'sessionId': self.session_id,
            'finalReportId': self.source_final_report_id,
            'type': 'gutBarrier',
        }
        if not isinstance(self.body, dict) or any(self.body.get(k) != v for k, v in expected.items()):
            raise ValueError('invalid gut-barrier follow-up identity')
        object.__setattr__(self, 'body', deepcopy(self.body))

    @property
    def task_id(self) -> str:
        identity = [self.user_id, self.session_id, self.source_final_report_id, self.contract_version]
        return 'report_followup_' + content_hash(identity)

    @property
    def input_digest(self) -> str:
        return content_hash(self.body)


@dataclass(frozen=True)
class FollowupReceipt:
    task_id: str
    source_final_report_id: str
    generation_status: str
    execution_version: str


class FollowupGenerator(Protocol):
    async def generate(self, body: dict, *, user_id: str, session_id: str, execution_id: str): ...


class _FrozenArtifactWriter:
    def __init__(self, store, job):
        self.store = store
        self.job = job

    async def save_artifact(self, record, *, subject_id=None):
        body = record.body
        if (
            record.report_type not in GUT_BARRIER_FOLLOWUP_TYPES
            or record.user_id != self.job['userId']
            or record.session_id != self.job['sessionId']
            or body.get('type') != record.report_type
            or body.get('userId') != self.job['userId']
            or body.get('sessionId') != self.job['sessionId']
            or body.get('finalReportId') != self.job['sourceFinalReportId']
        ):
            return CONFLICT
        # Storage identity is private; the callback envelope retains its source identity.
        return await self.store.save_frozen_artifact(
            self.job,
            replace(record, final_report_id=self.job['_id']),
        )


class ReportFollowupService(DurableReportService):
    """Durably admit a fixed three-artifact task, independently of the source callback.

    No HTTP route is added. Recovery seals missing artifacts without re-running
    the model. This store must use separate collections from public Report lists.
    """

    def __init__(
        self,
        *,
        store,
        generator: FollowupGenerator,
        callback_url: str,
        execution_version=FOLLOWUP_EXECUTION_VERSION,
        heartbeat_seconds=30,
        concurrency=2,
        generation_timeout_seconds=1800,
    ):
        super().__init__(
            store=store,
            artifact_store=None,
            pipeline=None,
            callback_url=validate_callback_url(callback_url),
            heartbeat_seconds=heartbeat_seconds,
            concurrency=concurrency,
        )
        if (
            isinstance(generation_timeout_seconds, bool)
            or not math.isfinite(generation_timeout_seconds)
            or generation_timeout_seconds <= 0
        ):
            raise ValueError('follow-up generation timeout must be finite and positive')
        self.generation_timeout_seconds = generation_timeout_seconds
        self.generator = generator
        self.execution_version = execution_version

    def attach_main_agent(self, agent):
        agent.bind_followup_runner(self._generate_followup_result)
        self.main_agent = agent

    async def submit(self, request: GutBarrierFollowupRequest) -> FollowupReceipt:
        if not isinstance(request, GutBarrierFollowupRequest):
            raise TypeError('a validated GutBarrierFollowupRequest is required')
        # Revalidate and detach caller-owned nested data before the first await.
        request = replace(request)
        job = await self.store.admit_delegation(
            request,
            self.callback_url,
            self.execution_version,
            self.generation_timeout_seconds,
            **({'execution_manifest': self.main_agent.execution_manifest()} if self.main_agent is not None else {}),
        )
        return FollowupReceipt(job['_id'], job['sourceFinalReportId'], job['generationState'], job['executionVersion'])

    async def get_receipt(self, *, user_id: str, session_id: str, task_id: str) -> FollowupReceipt:
        job = await self.store.get_owned(task_id, user_id=user_id, session_id=session_id)
        return FollowupReceipt(job['_id'], job['sourceFinalReportId'], job['generationState'], job['executionVersion'])

    async def _generate_result(self, job: dict):
        if self.main_agent is not None:
            return await self.main_agent.execute_followup(job)
        return await self._generate_followup_result(job)

    async def _generate_followup_result(self, job: dict):
        if job['executionVersion'] != self.execution_version:
            raise ValueError('follow-up execution version is unavailable')
        token = bind_report_batch(
            store=_FrozenArtifactWriter(self.store, job),
            final_report_id=job['sourceFinalReportId'],
            user_id=job['userId'],
            session_id=job['sessionId'],
            defer_callbacks=True,
        )
        try:
            async with asyncio.timeout(job['generationTimeoutSeconds']):
                return await self.generator.generate(
                    deepcopy(job['request']),
                    user_id=job['userId'],
                    session_id=job['sessionId'],
                    execution_id=job['_id'],
                )
        finally:
            reset_report_batch(token)

    async def _complete_results(self, job: dict, *, error_code: str, result=None) -> None:
        if job['executionVersion'] != self.execution_version:
            error_code = 'REPORT_EXECUTION_VERSION_UNAVAILABLE'
        token = bind_report_batch(
            store=_FrozenArtifactWriter(self.store, job),
            final_report_id=job['sourceFinalReportId'],
            user_id=job['userId'],
            session_id=job['sessionId'],
            defer_callbacks=True,
        )
        try:
            existing = {a['reportType']: a for a in await self.store.artifacts_for(job['_id'])}
            for payload in () if result is None else result.payloads:
                kind = payload.get('type')
                if kind in GUT_BARRIER_FOLLOWUP_TYPES and kind not in existing:
                    await save_report_artifact(kind, payload, generation_status=payload.get('status', 'success'))
            existing = {a['reportType']: a for a in await self.store.artifacts_for(job['_id'])}
            for kind in GUT_BARRIER_FOLLOWUP_TYPES:
                if kind not in existing:
                    body = failure_payload(
                        kind,
                        {**job, 'finalReportId': job['sourceFinalReportId']},
                        error_code=error_code,
                        evaluate=job['request'],
                    )
                    await save_report_artifact(kind, body, generation_status='failed')
                await self.store.ensure_delivery(job['_id'], kind)
            await self.store.finish_reconciliation(job)
        finally:
            reset_report_batch(token)
