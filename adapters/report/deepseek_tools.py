"""Outbound adapter for DeepSeek's standalone tool APIs: Search, Fetch and OCR.

These are plain read-only HTTP tools (``/v1/tools/search``, ``/v1/tools/fetch``
and ``/v1/tools/ocr``), not model calls. Model traffic keeps going through
AION; this adapter is used by the report evidence channel and by the document
text fallback in the same way ``EviMedClient`` and ``PublicEvidenceClient`` are.

Retries follow the provider guidance: 408/429/5xx are retried after a wait,
other 4xx are not. Search and Fetch are read-only, so retrying is safe.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from urllib.parse import urlsplit

import httpx

DEEPSEEK_BASE_URL = 'https://api.deepseek.com'
_RETRYABLE = frozenset({408, 429, 500, 502, 503, 504})


class DeepSeekToolError(RuntimeError):
    """A tool call failed; ``status`` is the HTTP status when one was received."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str = ''
    summary: str = ''
    content: str = ''
    site_name: str | None = None
    date: str | None = None
    rerank_score: float = 0.0
    authority: int = -1

    @property
    def host(self) -> str:
        return (urlsplit(self.url).hostname or '').lower()

    @property
    def year(self) -> int | None:
        text = self.date or ''
        return int(text[:4]) if len(text) >= 4 and text[:4].isdigit() else None


@dataclass(frozen=True)
class FetchedPage:
    url: str
    title: str
    content: str


@dataclass(frozen=True)
class OcrResult:
    page_count: int
    pages: tuple[str, ...]
    truncated_pages: tuple[int, ...] = ()

    @property
    def text(self) -> str:
        return '\n\n'.join(f'===== PAGE {index + 1} =====\n{page}' for index, page in enumerate(self.pages))


@dataclass
class ToolUsage:
    """Billed-call counters for the private audit; never part of a callback."""

    search_calls: int = 0
    search_billed: int = 0
    fetch_calls: int = 0
    fetch_billed: int = 0
    ocr_calls: int = 0
    ocr_pages: int = 0
    failures: dict[str, int] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        return {
            'searchCalls': self.search_calls,
            'searchBilled': self.search_billed,
            'fetchCalls': self.fetch_calls,
            'fetchBilled': self.fetch_billed,
            'ocrCalls': self.ocr_calls,
            'ocrPages': self.ocr_pages,
            'failures': dict(self.failures),
        }


def _valid_url(url: Any) -> bool:
    if not isinstance(url, str) or not url or len(url) > 8192 or any(ch.isspace() for ch in url):
        return False
    parts = urlsplit(url)
    return (
        parts.scheme in {'http', 'https'} and bool(parts.hostname) and parts.username is None and parts.password is None
    )


class DeepSeekToolsClient:
    """Small async client with bounded concurrency for the three tool APIs."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEEPSEEK_BASE_URL,
        timeout_seconds: float = 30.0,
        attempts: int = 3,
        search_concurrency: int = 8,
        fetch_concurrency: int = 4,
        ocr_concurrency: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = (api_key or '').strip()
        self._base_url = base_url.rstrip('/')
        self._timeout = float(timeout_seconds)
        self._attempts = max(1, int(attempts))
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._search_gate = asyncio.Semaphore(max(1, int(search_concurrency)))
        self._fetch_gate = asyncio.Semaphore(max(1, int(fetch_concurrency)))
        self._ocr_gate = asyncio.Semaphore(max(1, int(ocr_concurrency)))
        self.usage = ToolUsage()

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                transport=self._transport,
                follow_redirects=False,
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    def _fail(self, kind: str, reason: str) -> None:
        key = f'{kind}:{reason}'
        self.usage.failures[key] = self.usage.failures.get(key, 0) + 1

    async def _post_json(self, kind: str, path: str, body: dict[str, Any], *, timeout: float | None = None):
        if not self.enabled:
            raise DeepSeekToolError('DeepSeek tools are not configured')
        headers = {'Authorization': f'Bearer {self._api_key}', 'Content-Type': 'application/json'}
        last: DeepSeekToolError | None = None
        for attempt in range(self._attempts):
            try:
                response = await self._http().post(
                    self._base_url + path, json=body, headers=headers, timeout=timeout or self._timeout
                )
            except httpx.HTTPError as error:
                last = DeepSeekToolError(f'{kind} transport failure: {type(error).__name__}', retryable=True)
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError as error:
                        raise DeepSeekToolError(f'{kind} returned invalid JSON') from error
                last = DeepSeekToolError(
                    f'{kind} returned HTTP {response.status_code}',
                    status=response.status_code,
                    retryable=response.status_code in _RETRYABLE,
                )
            if not last.retryable or attempt + 1 >= self._attempts:
                break
            await asyncio.sleep(min(4.0, 0.6 * (2**attempt)))
        self._fail(kind, str(last.status or 'transport'))
        raise last

    async def search(
        self, query: str, *, count: int = 10, need_content: bool = True, original_question: str | None = None
    ) -> list[SearchHit]:
        """Return ranked hits; an empty list means the provider found nothing."""
        query = ' '.join(str(query or '').split())
        if not 1 <= len(query) <= 128:
            raise ValueError('search query must contain 1 to 128 characters')
        body: dict[str, Any] = {
            'query': query,
            'count': max(1, min(50, int(count))),
            'need_content': bool(need_content),
        }
        if original_question and 1 <= len(original_question.strip()) <= 128:
            body['ori_question'] = original_question.strip()
        async with self._search_gate:
            self.usage.search_calls += 1
            payload = await self._post_json('search', '/v1/tools/search', body)
        rows = payload.get('results') if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            self._fail('search', 'invalid_shape')
            raise DeepSeekToolError('search returned an invalid results list')
        hits = []
        for row in rows:
            if not isinstance(row, dict) or not _valid_url(row.get('url')):
                continue
            title = row.get('title')
            if not isinstance(title, str) or not title.strip():
                continue
            score = row.get('rerank_score')
            authority = row.get('authority')
            hits.append(
                SearchHit(
                    title=title.strip(),
                    url=row['url'],
                    snippet=row.get('snippet') if isinstance(row.get('snippet'), str) else '',
                    summary=row.get('summary') if isinstance(row.get('summary'), str) else '',
                    content=row.get('content') if isinstance(row.get('content'), str) else '',
                    site_name=row.get('site_name') if isinstance(row.get('site_name'), str) else None,
                    date=row.get('date') if isinstance(row.get('date'), str) else None,
                    rerank_score=float(score)
                    if isinstance(score, (int, float)) and not isinstance(score, bool)
                    else 0.0,
                    authority=int(authority) if isinstance(authority, int) and not isinstance(authority, bool) else -1,
                )
            )
        if hits:
            self.usage.search_billed += 1
        return hits

    async def fetch(self, url: str, *, timeout: float = 45.0) -> FetchedPage | None:
        """Read one page as Markdown. ``None`` means no usable content (HTTP 403/404)."""
        if not _valid_url(url):
            raise ValueError('fetch requires an http(s) URL without credentials')
        async with self._fetch_gate:
            self.usage.fetch_calls += 1
            try:
                payload = await self._post_json('fetch', '/v1/tools/fetch', {'url': url}, timeout=timeout)
            except DeepSeekToolError as error:
                if error.status in {403, 404}:
                    return None
                raise
        content = payload.get('content') if isinstance(payload, dict) else None
        if not isinstance(content, str) or not content.strip():
            return None
        self.usage.fetch_billed += 1
        title = payload.get('title')
        return FetchedPage(url=url, title=title.strip() if isinstance(title, str) else '', content=content)

    async def ocr(
        self, filename: str, content: bytes, *, rich_layout: bool = False, timeout: float = 200.0
    ) -> OcrResult:
        """Recognise a PDF/Office/image file page by page."""
        if not self.enabled:
            raise DeepSeekToolError('DeepSeek tools are not configured')
        if not isinstance(content, (bytes, bytearray)) or not content:
            raise ValueError('ocr requires file content')
        purpose = 'rich_layout_recognition' if rich_layout else 'text_recognition'
        headers = {'Authorization': f'Bearer {self._api_key}'}
        last: DeepSeekToolError | None = None
        async with self._ocr_gate:
            for attempt in range(self._attempts):
                self.usage.ocr_calls += 1
                try:
                    # ``purpose`` must precede ``file`` in the multipart body.
                    response = await self._http().post(
                        self._base_url + '/v1/tools/ocr',
                        headers=headers,
                        data={'purpose': purpose},
                        files={'file': (filename or 'document', bytes(content))},
                        timeout=timeout,
                    )
                except httpx.HTTPError as error:
                    last = DeepSeekToolError(f'ocr transport failure: {type(error).__name__}', retryable=True)
                else:
                    if response.status_code == 200:
                        # The stream starts with keep-alive newlines; the body parses as JSON.
                        try:
                            payload = json.loads(response.text)
                        except ValueError as error:
                            raise DeepSeekToolError('ocr returned invalid JSON') from error
                        if isinstance(payload, dict) and isinstance(payload.get('error'), dict):
                            code = str(payload['error'].get('code') or payload['error'].get('type') or 'error')
                            last = DeepSeekToolError(
                                f'ocr failed: {code}',
                                retryable=code
                                in {'gateway_timeout_error', 'service_unavailable_error', 'internal_error'},
                            )
                        else:
                            pages = payload.get('pages') if isinstance(payload, dict) else None
                            if not isinstance(pages, list):
                                raise DeepSeekToolError('ocr returned an invalid page list')
                            ordered = sorted(
                                (page for page in pages if isinstance(page, dict)),
                                key=lambda page: page.get('index') if isinstance(page.get('index'), int) else 0,
                            )
                            texts = tuple(
                                page.get('text') if isinstance(page.get('text'), str) else '' for page in ordered
                            )
                            truncated = tuple(
                                index for index, page in enumerate(ordered) if page.get('finish_reason') == 'length'
                            )
                            self.usage.ocr_pages += len(texts)
                            return OcrResult(page_count=len(texts), pages=texts, truncated_pages=truncated)
                    else:
                        last = DeepSeekToolError(
                            f'ocr returned HTTP {response.status_code}',
                            status=response.status_code,
                            retryable=response.status_code in _RETRYABLE,
                        )
                if not last.retryable or attempt + 1 >= self._attempts:
                    break
                await asyncio.sleep(min(8.0, 1.5 * (2**attempt)))
        self._fail('ocr', str(last.status or 'error'))
        raise last


class DeepSeekDocumentTextClient:
    """``DocumentTextGateway`` backed by DeepSeek OCR.

    Used only as the fallback for scan-only or image uploads, after the local
    table-preserving extraction has produced nothing usable.
    """

    def __init__(self, tools: DeepSeekToolsClient) -> None:
        self._tools = tools

    @property
    def enabled(self) -> bool:
        return self._tools.enabled

    async def extract_text(self, source: Any, *, filename: str, content: bytes) -> str:
        result = await self._tools.ocr(filename, content)
        if not any(page.strip() for page in result.pages):
            raise DeepSeekToolError('ocr produced no text')
        return result.text

    async def aclose(self) -> None:
        await self._tools.aclose()


__all__ = [
    'DEEPSEEK_BASE_URL',
    'DeepSeekDocumentTextClient',
    'DeepSeekToolError',
    'DeepSeekToolsClient',
    'FetchedPage',
    'OcrResult',
    'SearchHit',
    'ToolUsage',
]
