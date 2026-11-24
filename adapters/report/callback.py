"""HTTP delivery adapter for the report callback contract."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from config.report_endpoints import validate_report_endpoint
from modules.report.callback import _fixed_callback_body
from modules.report.evaluation_callback import build_frontend_evaluation_callback
from modules.report.fallback_policy import DEFAULT_REPORT_FALLBACK_POLICY
from modules.report.fallback_policy import ReportFallbackPolicy
from modules.report.schemas import CallbackPayload


class CallbackDeliveryError(RuntimeError):
    """Raised when a fixed report callback cannot be delivered."""


class CallbackClient:
    """Build and deliver the fixed report callback over HTTP."""

    def __init__(
        self,
        *,
        url: str,
        timeout_seconds: float = 15.0,
        attempts: int | None = None,
        policy: ReportFallbackPolicy = DEFAULT_REPORT_FALLBACK_POLICY,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds
        self.policy = policy
        self.attempts = policy.attempts_for(
            policy.callback_attempts if attempts is None else attempts
        )
        self.transport = transport
        self._http_client: httpx.AsyncClient | None = None

    def build_body(
        self,
        payload: CallbackPayload | dict[str, Any],
    ) -> dict[str, Any]:
        """Build the exact fixed JSON independently of network delivery."""

        return build_frontend_evaluation_callback(payload)

    def build_followup_body(
        self,
        payload: CallbackPayload | dict[str, Any],
    ) -> dict[str, Any]:
        """Keep the complete validated source/evidence context for generation."""

        return _fixed_callback_body(payload)

    async def send(self, payload: CallbackPayload | dict[str, Any]) -> dict[str, Any]:
        """Deliver the fixed callback and return the exact accepted JSON body."""

        body = self.build_body(payload)
        await self.deliver(body)
        return body

    async def deliver(self, body: dict[str, Any]) -> None:
        """Deliver an already-fixed body with retry only for transient errors."""

        url = validate_report_endpoint(self.url)
        last_error: Exception | None = None
        client = self._shared_client()
        for attempt in range(self.attempts):
            try:
                response = await client.post(url, json=body)
                if 300 <= response.status_code < 400:
                    raise CallbackDeliveryError(
                        "callback redirects are not accepted"
                    )
                retryable = (
                    response.status_code == 429 or response.status_code >= 500
                )
                response.raise_for_status()
                return
            except (httpx.HTTPError, CallbackDeliveryError) as exc:
                last_error = exc
                if isinstance(exc, httpx.HTTPStatusError):
                    status = exc.response.status_code
                    retryable = status == 429 or status >= 500
                elif isinstance(exc, CallbackDeliveryError):
                    retryable = False
                else:
                    retryable = True
                if not retryable or attempt + 1 >= self.attempts:
                    break
                delay = self.policy.backoff_after(attempt)
                if delay:
                    await asyncio.sleep(delay)
        raise CallbackDeliveryError("report callback delivery failed") from last_error

    def _shared_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds),
                transport=self.transport,
                follow_redirects=False,
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
            )
        return self._http_client

    async def aclose(self) -> None:
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()
