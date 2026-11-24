"""GutBarrier-owned durable peer delegation, independent from callback delivery."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from dataclasses import dataclass
from typing import Protocol

DELEGATION_VERSION = 'gut-barrier.report-delegation.v1'


@dataclass(frozen=True)
class DelegationPolicy:
    version: str = DELEGATION_VERSION
    max_attempts: int = 8
    deadline_seconds: int = 300
    call_timeout_seconds: int = 15

    def __post_init__(self):
        if self.version != DELEGATION_VERSION:
            raise ValueError('unsupported GutBarrier delegation version')
        for value in (self.max_attempts, self.deadline_seconds, self.call_timeout_seconds):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError('delegation budgets must be positive integers')

    def snapshot(self):
        return asdict(self)


class ReportDelegationPort(Protocol):
    async def receipt(self, body: dict) -> str | None: ...
    async def submit(self, body: dict) -> str: ...


class GutBarrierDelegationWorker:
    def __init__(self, *, store, port: ReportDelegationPort):
        self.store, self.port = store, port
        self._accepting = True
        self._lock = asyncio.Lock()

    def stop_accepting(self):
        self._accepting = False

    async def drain_pending(self, *, limit=100):
        if not self._accepting or self._lock.locked():
            return 0
        processed = 0
        async with self._lock:
            while self._accepting and processed < limit:
                job = await self.store.claim()
                if job is None:
                    break
                processed += 1
                try:
                    policy = DelegationPolicy(**job['delegationPolicy'])
                    # A lost acknowledgement is resolved using the same identity,
                    # including after the submission budget has been exhausted.
                    async with asyncio.timeout(policy.call_timeout_seconds):
                        task_id = await self.port.receipt(job['body'])
                    if task_id is not None:
                        await self.store.finish(job, status='accepted', task_id=task_id)
                        continue
                    if not await self.store.begin_submission(job, policy=policy):
                        await self.store.finish(
                            job,
                            status='needs_reconciliation' if job['delegationAttempts'] else 'budget_exhausted',
                            error_code='DELEGATION_BUDGET_EXHAUSTED',
                        )
                        continue
                    try:
                        remaining = (job['delegationDeadlineAt'] - self.store.clock()).total_seconds()
                        async with asyncio.timeout(max(0, min(policy.call_timeout_seconds, remaining))):
                            task_id = await self.port.submit(job['body'])
                    except (ValueError, PermissionError):
                        await self.store.finish(job, status='rejected', error_code='DELEGATION_REJECTED')
                    else:
                        await self.store.finish(job, status='accepted', task_id=task_id)
                except asyncio.CancelledError:
                    # The fenced lease and pre-call unknown marker survive shutdown.
                    raise
                except ValueError:
                    await self.store.finish(job, status='rejected', error_code='UNSUPPORTED_DELEGATION_VERSION')
                except Exception:
                    await self.store.retry(job)
        return processed
