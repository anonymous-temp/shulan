"""Storage and HTTP ports for durable report execution and delivery."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from typing import Protocol

# Inherited by the pipeline's child tasks; never supplied by an HTTP caller.
ACTIVE_GENERATION: ContextVar[tuple[str, str] | None] = ContextVar(
    'report_generation_lease',
    default=None,
)


@dataclass(frozen=True)
class CallbackResponse:
    status: int
    retry_after: str | None = None


class CallbackTransport(Protocol):
    async def post_once(
        self,
        url: str,
        body: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> CallbackResponse: ...


class ReportWorkflowStore(Protocol):
    lease_seconds: float

    async def admit(self, request: dict, fingerprint: str, callback_url: str, *, execution_manifest=None) -> dict: ...
    async def claim_generation(self, owner: str, *, expired: bool = False) -> dict | None: ...
    async def renew_generation(self, job: dict) -> bool: ...
    async def mark_generation_started(self, job: dict) -> bool: ...
    async def release_generation(self, job: dict, *, known_not_started: bool = False) -> bool: ...
    async def finalize_generation(self, job: dict, error_code: str) -> bool: ...
    async def finish_reconciliation(self, job: dict) -> bool: ...
    async def reconciliation_jobs(self, limit: int = 100) -> list[dict]: ...
    async def apply_redrive(self, job: dict) -> None: ...
    async def artifacts_for(self, final_report_id: str) -> list[dict]: ...
    async def ensure_delivery(self, final_report_id: str, report_type: str) -> None: ...
    async def claim_delivery(self, owner: str) -> dict | None: ...
    async def delivery_body(self, job: dict) -> dict: ...
    async def acknowledge_delivery(self, job: dict) -> bool: ...
    async def retry_delivery(
        self,
        job: dict,
        *,
        next_retry_at: datetime,
        error: dict,
    ) -> bool: ...
    async def backlog(self) -> dict: ...
