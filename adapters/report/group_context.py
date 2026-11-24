"""Read evaluate's group history from the deployment-owned SLMC service."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from time import monotonic
from urllib.parse import urlsplit, urlunsplit

import httpx

from config.report_endpoints import validate_report_endpoint
from modules.report.fallback_policy import DEFAULT_REPORT_FALLBACK_POLICY
from modules.report.group_context import GroupContextResult, parse_group_context
from modules.report.performance import increment_report_metric
from modules.report.ports import ExternalSourceError
from modules.report.schemas import RemoteFileInput
from adapters.report.clients import SourceClient, _detect_type, _reject_json_constant


class GroupContextClient:
    """Use current source retry/budget policy, without public URL overrides."""

    def __init__(self, *, callback_url, source_client=None,
                 policy=DEFAULT_REPORT_FALLBACK_POLICY, transport=None,
                 timeout_seconds=30, max_response_bytes=4*1024*1024,
                 read_timeout_seconds=55, attachment_timeout_seconds=20):
        self.callback_url = callback_url
        self._owns_source_client = source_client is None
        self.source_client = source_client or SourceClient(policy=policy)
        self.policy, self.transport = policy, transport
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        # Leave room under the pipeline's 60-second outer source boundary.
        # In particular, an acquired conversation survives a slow attachment.
        self.read_timeout_seconds = max(0.001, min(55.0, float(read_timeout_seconds)))
        self.attachment_timeout_seconds = max(0.001, min(20.0, float(attachment_timeout_seconds)))
        self._http_client = None

    @property
    def endpoint(self):
        # Both report callback and OpenIM API are owned by the same deployed
        # SLMC service. Query tokens/path components belong only to callback.
        parsed = urlsplit(validate_report_endpoint(self.callback_url))
        return urlunsplit((parsed.scheme, parsed.netloc, '/api/openim/open-api/message/search', '', ''))

    def _client(self):
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_seconds),
                transport=self.transport, follow_redirects=False,
                limits=httpx.Limits(max_connections=self.policy.source_parse_concurrency,
                    max_keepalive_connections=self.policy.source_parse_concurrency))
        return self._http_client

    async def _query(self, group_id):
        attempts = self.policy.attempts_for(self.policy.source_fetch_attempts)
        for attempt in range(attempts):
            try:
                increment_report_metric('group_context.request_attempt')
                async with self._client().stream('POST', self.endpoint, json={'groupId':group_id}) as response:
                    if 300 <= response.status_code < 400:
                        raise ExternalSourceError('group redirects are not accepted')
                    response.raise_for_status()
                    chunks, count = [], 0
                    async for chunk in response.aiter_bytes():
                        count += len(chunk)
                        if count > self.max_response_bytes:
                            raise ExternalSourceError('group response exceeds size limit')
                        chunks.append(chunk)
                value = json.loads(b''.join(chunks), parse_constant=_reject_json_constant)
                increment_report_metric('group_context.success')
                return value
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as error:
                retry = not isinstance(error, httpx.HTTPStatusError) or error.response.status_code == 429 or error.response.status_code >= 500
                if not retry or attempt + 1 == attempts:
                    raise ExternalSourceError('group query failed') from error
                delay = self.policy.backoff_after(attempt)
                if delay:
                    await asyncio.sleep(delay)

    async def _attachment(self, item):
        # Validate all caller/group resource URLs through the same source
        # network guard before handing them to shared document processing.
        await self.source_client._validate_public_url(item.url, schemes={'https'})
        if item.file_type is not None:
            RemoteFileInput(url=item.url, fileType=item.file_type)
            return item
        # An opaque 105 URL does not imply PDF. Refine only from actual bytes
        # using the existing download and magic sniff helpers; shared parsing
        # remains the sole interpretation path.
        content, _, _ = await self.source_client._fetch_bytes(item.url)
        detected = _detect_type(content)
        if detected not in {'pdf', 'image'}:
            raise ExternalSourceError('unsupported group file content')
        return replace(item, file_type=detected)

    async def read(self, *, user_id, group_id):
        deadline = monotonic() + self.read_timeout_seconds
        try:
            async with asyncio.timeout(self.read_timeout_seconds):
                body = await self._query(group_id)
            result = parse_group_context(body, user_id=user_id, group_id=group_id)
        except TimeoutError:
            return GroupContextResult(source_errors=['GROUP_CONTEXT_TIMEOUT'])
        except (ExternalSourceError, ValueError, httpx.HTTPError, UnicodeError):
            return GroupContextResult(source_errors=['GROUP_CONTEXT_UNAVAILABLE'])
        if len(result.attachments) > 10:
            result.attachments = result.attachments[-10:]
            result.source_errors.append('GROUP_ATTACHMENTS_TRUNCATED')
        semaphore = asyncio.Semaphore(self.policy.source_parse_concurrency)
        async def resolve(item):
            remaining = deadline - monotonic()
            if remaining <= 0:
                result.source_errors.append('GROUP_ATTACHMENT_BUDGET_EXHAUSTED')
                return None
            try:
                # Queued work shares the same overall budget. Acquiring a
                # slot must not start a fresh 20-second window after it expires.
                async with asyncio.timeout(remaining):
                    async with semaphore:
                        async with asyncio.timeout(min(self.attachment_timeout_seconds,
                                max(0.001, deadline - monotonic()))):
                            return await self._attachment(item)
            except TimeoutError:
                result.source_errors.append('GROUP_ATTACHMENT_TIMEOUT:'+item.message_id)
                if monotonic() >= deadline:
                    result.source_errors.append('GROUP_ATTACHMENT_BUDGET_EXHAUSTED')
                return None
            except (ExternalSourceError, ValueError, httpx.HTTPError, OSError):
                result.source_errors.append('GROUP_ATTACHMENT_UNAVAILABLE:'+item.message_id)
                return None
        result.attachments = [item for item in await asyncio.gather(*(resolve(item) for item in result.attachments))
            if item is not None]
        result.source_errors = list(dict.fromkeys(result.source_errors))
        return result

    async def aclose(self):
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()
        if self._owns_source_client:
            await self.source_client.aclose()
