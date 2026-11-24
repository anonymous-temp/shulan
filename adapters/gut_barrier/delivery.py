"""Gut-barrier-only callback headers; the JSON body and shared worker stay fixed."""
from __future__ import annotations

import httpx

from adapters.gut_barrier.render_context import render_context_headers
from adapters.report.delivery import ReportCallbackTransport
from config.report_endpoints import validate_report_endpoint
from modules.gut_barrier.policy import is_explanation_callback
from modules.report.durable_ports import CallbackResponse


class GutBarrierCallbackTransport(ReportCallbackTransport):
    def __init__(self, *, store, **kwargs):
        super().__init__(**kwargs)
        self.store = store

    async def post_once(self, url: str, body: dict, *, idempotency_key: str) -> CallbackResponse:
        url = validate_report_endpoint(url)
        if is_explanation_callback(body):
            # Defense in depth for old outbox claims: this endpoint no longer
            # sends a second report, even if a caller retained a historic body.
            raise ValueError("independent gut-barrier explanation delivery is disabled")
        context = await self.store.delivery_render_context(body, idempotency_key=idempotency_key)
        headers = {"Idempotency-Key": idempotency_key, **render_context_headers(body, context)}
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self.timeout_seconds, follow_redirects=False, transport=self._transport,
                limits=httpx.Limits(max_connections=self.concurrency, max_keepalive_connections=self.concurrency),
            )
        # Keep the original acknowledgement contract: do not buffer or interpret
        # the receiver body. The durable worker owns retries and lease handling.
        async with self._client.stream("POST", url, json=body, headers=headers) as response:
            return CallbackResponse(response.status_code, response.headers.get("Retry-After"))
