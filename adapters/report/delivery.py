"""One HTTP attempt; all scheduling/retries belong to the durable outbox."""

from __future__ import annotations

import httpx

from config.report_endpoints import validate_report_endpoint
from modules.report.durable_ports import CallbackResponse


class ReportCallbackTransport:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout_seconds: float = 15, concurrency: int = 4) -> None:
        self._transport = transport
        self.timeout_seconds = timeout_seconds
        self.concurrency = concurrency
        self._client: httpx.AsyncClient | None = None

    async def post_once(self, url: str, body: dict, *, idempotency_key: str) -> CallbackResponse:
        url = validate_report_endpoint(url)
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout_seconds, follow_redirects=False, transport=self._transport,
                limits=httpx.Limits(max_connections=self.concurrency, max_keepalive_connections=self.concurrency),
            )
        # Only the status/Retry-After are part of the acknowledgement contract;
        # never buffer an arbitrary receiver response body.
        async with self._client.stream(
            'POST', url, json=body, headers={'Idempotency-Key': idempotency_key},
        ) as response:
            return CallbackResponse(response.status_code, response.headers.get('Retry-After'))

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
