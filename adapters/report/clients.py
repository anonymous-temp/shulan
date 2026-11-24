"""Small HTTP clients used by the report pipeline.

No client writes to MongoDB or Redis.  The document adapter may create a
short-lived private temporary file when an OCR SDK only accepts file paths;
the file is removed before the request returns.  Redirects are disabled and
public-address validation is performed before downloading caller-supplied
URLs.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
import tempfile
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from modules.report.fallback_policy import (
    DEFAULT_REPORT_FALLBACK_POLICY,
    ReportFallbackPolicy,
)
from modules.report.performance import (
    add_report_elapsed_ms,
    increment_report_metric,
)
from modules.report.ports import (
    DocumentServiceError,
    DownloadedFile,
    DownloadedTestData,
    EvidenceServiceError,
    EviMedChannelResult,
    ExternalSourceError,
    ExternalSourceSecurityError,
    RetryableExternalSourceError,
)
from modules.report.schemas import (
    EvidenceRecord,
    RemoteFileInput,
    TestDataInput,
    stable_id,
)

Resolver = Callable[[str], Awaitable[list[str]]]


def _reject_json_constant(value: str):
    raise ValueError("non-finite JSON number")


class SourceClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 60.0,
        max_bytes: int = 50 * 1024 * 1024,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
        policy: ReportFallbackPolicy = DEFAULT_REPORT_FALLBACK_POLICY,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.transport = transport
        self.resolver = resolver or _resolve_host
        self.policy = policy
        self._http_client: httpx.AsyncClient | None = None

    async def fetch_file(self, source: RemoteFileInput) -> DownloadedFile:
        await self._validate_public_url(source.url, schemes={"https"})
        content, content_type, headers = await self._fetch_bytes(source.url)
        detected = _detect_type(content)
        if source.file_type == "pdf" and detected != "pdf":
            raise ExternalSourceError("fileType=pdf does not match downloaded content")
        if source.file_type == "image" and detected != "image":
            raise ExternalSourceError("fileType=image does not match downloaded content")
        return DownloadedFile(
            filename=_safe_filename(source.url, headers, detected),
            content_type=content_type,
            content=content,
            declared_type=source.file_type,
        )

    async def fetch_evaluation_json(self, url: str) -> DownloadedTestData:
        """Download evaluate's template payload with the existing URL guards.

        Recognition belongs to the evaluate adapter. The older testData
        endpoint retains its original provider-shape validation.
        """
        await self._validate_public_url(url, schemes={"https"})
        content, _, _ = await self._fetch_bytes(url)
        if len(content) > 2 * 1024 * 1024:
            raise ExternalSourceError("evaluate JSON exceeds 2 MiB")
        try:
            detail = json.loads(_decode_text(content), parse_constant=_reject_json_constant)
        except (UnicodeError, ValueError) as exc:
            raise ExternalSourceError("evaluate source is not valid JSON") from exc
        if not isinstance(detail, dict):
            raise ExternalSourceError("evaluate source must be a JSON object")
        return DownloadedTestData(detail=detail, detail_checksum=hashlib.sha256(content).hexdigest())

    async def fetch_test_data(self, source: TestDataInput) -> DownloadedTestData:
        await self._validate_public_url(source.result_detail_url, schemes={"http", "https"})
        detail_content, _, _ = await self._fetch_bytes(source.result_detail_url)
        try:
            detail = json.loads(_decode_text(detail_content))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ExternalSourceError("testData detail is not valid JSON") from exc
        if not isinstance(detail, dict):
            raise ExternalSourceError("testData detail must be a JSON object")
        if not (
            isinstance(detail.get("microbiomeData"), dict)
            or (
                isinstance(detail.get("microbeData"), dict)
                and isinstance(detail.get("diseaseRisk"), dict)
            )
        ):
            raise ExternalSourceError("testData detail has no supported microbiome payload")

        summary = None
        if source.result_summary_url:
            try:
                await self._validate_public_url(
                    source.result_summary_url, schemes={"http", "https"}
                )
                summary_content, _, _ = await self._fetch_bytes(source.result_summary_url)
                summary = " ".join(_decode_text(summary_content).split()) or None
            except ExternalSourceError:
                # Detail JSON remains authoritative; the optional prose summary
                # must never make the whole report fail.
                summary = None
        return DownloadedTestData(
            detail=detail,
            detail_checksum=hashlib.sha256(detail_content).hexdigest(),
            summary=summary,
        )

    async def _fetch_bytes(self, url: str) -> tuple[bytes, str, httpx.Headers]:
        policy = getattr(self, "policy", DEFAULT_REPORT_FALLBACK_POLICY)
        attempts = policy.attempts_for(policy.source_fetch_attempts)
        last_error: RetryableExternalSourceError | None = None
        for attempt in range(attempts):
            try:
                return await self._fetch_bytes_once(url)
            except RetryableExternalSourceError as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    break
                delay = policy.backoff_after(attempt)
                if delay:
                    await asyncio.sleep(delay)
        raise ExternalSourceError("source download failed after retries") from last_error

    async def _fetch_bytes_once(
        self,
        url: str,
    ) -> tuple[bytes, str, httpx.Headers]:
        try:
            async with self._shared_client().stream("GET", url) as response:
                if 300 <= response.status_code < 400:
                    raise ExternalSourceSecurityError("redirects are not accepted")
                response.raise_for_status()
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > self.max_bytes:
                    raise ExternalSourceError("source exceeds the size limit")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise ExternalSourceError("source exceeds the size limit")
                    chunks.append(chunk)
        except ExternalSourceError:
            raise
        except httpx.TimeoutException as exc:
            raise RetryableExternalSourceError("source download timed out") from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            error_type = (
                RetryableExternalSourceError
                if status == 429 or status >= 500
                else ExternalSourceError
            )
            raise error_type(f"source returned HTTP {status}") from exc
        except httpx.TransportError as exc:
            raise RetryableExternalSourceError("source download failed") from exc
        except httpx.HTTPError as exc:
            raise ExternalSourceError("source download failed") from exc
        content = b"".join(chunks)
        if not content:
            raise ExternalSourceError("source response is empty")
        content_type = response.headers.get("content-type", "").split(";", 1)[0]
        return content, content_type, response.headers

    def _shared_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            connection_limit = max(
                1,
                int(self.policy.source_parse_concurrency),
            )
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds),
                transport=self.transport,
                follow_redirects=False,
                limits=httpx.Limits(
                    max_connections=connection_limit,
                    max_keepalive_connections=connection_limit,
                ),
            )
        return self._http_client

    async def aclose(self) -> None:
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()

    async def _validate_public_url(self, url: str, *, schemes: set[str]) -> None:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").casefold()
        if parsed.scheme.casefold() not in schemes or not hostname:
            raise ExternalSourceSecurityError("source URL has an invalid scheme or host")
        if parsed.username or parsed.password or parsed.fragment:
            raise ExternalSourceSecurityError("source URL contains prohibited components")
        try:
            addresses = [str(ipaddress.ip_address(hostname))]
        except ValueError:
            addresses = await self.resolver(hostname)
        if not addresses:
            raise ExternalSourceSecurityError("source host did not resolve")
        for value in addresses:
            address = ipaddress.ip_address(value)
            if (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_multicast
                or address.is_unspecified
                or address.is_reserved
            ):
                raise ExternalSourceSecurityError("source host resolves to a private address")


class DocMindTextClient:
    """External OCR/layout service for images and scan/hybrid PDFs."""

    _CACHE_VERSION = "docmind-text-v1"

    def __init__(
        self,
        *,
        access_key_id: str,
        access_key_secret: str,
        endpoint: str,
        timeout_seconds: float = 120.0,
        cache_ttl_seconds: float = 3600.0,
        cache_size: int = 16,
        policy: ReportFallbackPolicy = DEFAULT_REPORT_FALLBACK_POLICY,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.policy = policy
        self.cache_ttl_seconds = max(0.0, cache_ttl_seconds)
        self.cache_size = max(0, cache_size)
        self._cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[str]] = {}
        self._cache_lock = asyncio.Lock()
        try:
            from aion.aliyun import docmind

            self.client = docmind.DocMindClient(
                access_key_id=access_key_id,
                access_key_secret=access_key_secret,
                endpoint=endpoint,
            )
        except Exception as exc:
            raise DocumentServiceError("DocMind client is unavailable") from exc

    async def extract_text(
        self,
        source: RemoteFileInput,
        *,
        filename: str,
        content: bytes | None = None,
    ) -> str:
        # Caller-supplied URLs can be mutable, so only downloaded bytes are
        # eligible for reuse.  The content digest plus parser version makes
        # repeated reports with the same scan deterministic without adding a
        # cross-process cache or persisting private medical content.
        if content is None:
            return await self._extract_text_uncached(
                source,
                filename=filename,
                content=None,
            )

        self._ensure_cache_state()
        extension = Path(filename).suffix.lstrip(".").casefold()
        cache_key = hashlib.sha256(
            b"|".join(
                (
                    self._CACHE_VERSION.encode("utf-8"),
                    source.file_type.encode("utf-8"),
                    extension.encode("utf-8"),
                    hashlib.sha256(content).digest(),
                )
            )
        ).hexdigest()
        now = time.monotonic()
        async with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached is not None and cached[0] > now:
                increment_report_metric("docmind.cache_hit")
                self._cache.move_to_end(cache_key)
                return cached[1]
            if cached is not None:
                self._cache.pop(cache_key, None)
            task = self._inflight.get(cache_key)
            owner = task is None
            if task is None:
                increment_report_metric("docmind.cache_miss")
                task = asyncio.create_task(
                    self._extract_text_uncached(
                        source,
                        filename=filename,
                        content=content,
                    )
                )
                self._inflight[cache_key] = task
            else:
                increment_report_metric("docmind.inflight_reuse")
        try:
            text = await task
        finally:
            if owner:
                async with self._cache_lock:
                    self._inflight.pop(cache_key, None)
        if owner and self.cache_size and self.cache_ttl_seconds:
            async with self._cache_lock:
                self._cache[cache_key] = (
                    time.monotonic() + self.cache_ttl_seconds,
                    text,
                )
                self._cache.move_to_end(cache_key)
                while len(self._cache) > self.cache_size:
                    self._cache.popitem(last=False)
        return text

    def _ensure_cache_state(self) -> None:
        """Initialize cache fields for legacy tests constructing via __new__."""

        if not hasattr(self, "cache_ttl_seconds"):
            self.cache_ttl_seconds = 3600.0
        if not hasattr(self, "cache_size"):
            self.cache_size = 16
        if not hasattr(self, "_cache"):
            self._cache = OrderedDict()
        if not hasattr(self, "_inflight"):
            self._inflight = {}
        if not hasattr(self, "_cache_lock"):
            self._cache_lock = asyncio.Lock()

    async def _extract_text_uncached(
        self,
        source: RemoteFileInput,
        *,
        filename: str,
        content: bytes | None,
    ) -> str:
        def run() -> str:
            # ``RemoteFileInput.file_type`` is the public API's broad
            # ``pdf``/``image`` discriminator.  DocMind expects the actual
            # filename extension (for example ``jpg`` or ``png``), and rejects
            # the literal value ``image`` even when the remote bytes are a
            # valid image.  ``SourceClient`` has already detected the bytes and
            # produced a safe filename, so use that extension here.
            extension = Path(filename).suffix.lstrip(".").casefold()
            if not extension:
                extension = "pdf" if source.file_type == "pdf" else "png"
            if content is not None:
                safe_filename = Path(filename).name or f"document.{extension}"
                with tempfile.TemporaryDirectory(prefix="shulan-report-doc-") as temp_dir:
                    local_path = Path(temp_dir) / safe_filename
                    local_path.write_bytes(content)
                    task_id = self.client.submit_local_file(
                        str(local_path),
                        file_name=safe_filename,
                        file_type=extension,
                    )
                    result = self.client.get_result(task_id, poll_interval=1)
            else:
                task_id = self.client.submit_online_file(
                    source.url,
                    file_name=filename,
                    file_type=extension,
                )
                result = self.client.get_result(task_id, poll_interval=1)
            if not result.get("success"):
                raise DocumentServiceError("DocMind parsing failed")
            text = str(result.get("text") or "").strip()
            if not text:
                raise DocumentServiceError("DocMind returned no text")
            return text

        policy = getattr(self, "policy", DEFAULT_REPORT_FALLBACK_POLICY)
        attempts = policy.attempts_for(policy.source_fetch_attempts)
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(run),
                    timeout=self.timeout_seconds,
                )
            except TimeoutError as exc:
                last_error = exc
            except DocumentServiceError:
                # A completed OCR response with no usable text is permanent.
                raise
            except Exception as exc:  # network/service SDK failures are transient.
                last_error = exc
            if attempt + 1 < attempts:
                delay = policy.backoff_after(attempt)
                if delay:
                    await asyncio.sleep(delay)
        raise DocumentServiceError("DocMind parsing unavailable after retries") from last_error


class EviMedClient:
    """EviMed V2 guide/literature client.

    Guide searches request text blocks so downstream screening can assess the
    exact passage while citations still retain the parent guide metadata.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 20.0,
        result_count: int = 15,
        attempts: int = 3,
        # Keep exact channel/query results for longer than the maximum
        # 30-minute report run.  This guarantees request-lifetime reuse while
        # retaining the existing process-local bounded LRU behavior.
        cache_ttl_seconds: float = 3600.0,
        cache_size: int = 512,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.timeout_seconds = timeout_seconds
        self.result_count = result_count
        self.attempts = max(1, attempts)
        self.transport = transport
        self._http_client: httpx.AsyncClient | None = None
        self.cache_ttl_seconds = max(0.0, cache_ttl_seconds)
        self.cache_size = max(0, cache_size)
        self._cache: OrderedDict[
            tuple[str, str], tuple[float, list[EvidenceRecord]]
        ] = OrderedDict()
        self._inflight: dict[
            tuple[str, str], asyncio.Task[list[EvidenceRecord]]
        ] = {}
        self._cache_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key)

    async def search(self, query: str, *, supports: list[str]) -> list[EvidenceRecord]:
        results = await self.search_detailed(query, supports=supports)
        successful = [item for item in results if item.status == "success"]
        if not successful:
            raise EvidenceServiceError("all EviMed channels failed")
        return [record for item in successful for record in item.records]

    async def search_detailed(
        self,
        query: str,
        *,
        supports: list[str],
        channels: tuple[str, ...] = ("guide", "literature"),
    ) -> list[EviMedChannelResult]:
        if not self.enabled:
            raise EvidenceServiceError("EviMed is not configured")
        requested = tuple(
            channel
            for channel in ("guide", "literature")
            if channel in channels
        )
        async def run_channel(channel: str):
            started = time.monotonic()
            records = await self._search_channel(channel, query, supports=supports)
            return records, round((time.monotonic() - started) * 1000, 3)

        results = await asyncio.gather(
            *(run_channel(channel) for channel in requested),
            return_exceptions=True,
        )
        outcomes: list[EviMedChannelResult] = []
        by_channel = dict(zip(requested, results, strict=True))
        for channel in ("guide", "literature"):
            if channel not in requested:
                outcomes.append(EviMedChannelResult(channel, "not_requested", []))
                continue
            result = by_channel[channel]
            if isinstance(result, Exception):
                outcomes.append(
                    EviMedChannelResult(
                        channel, "failed", [], provider="evimed_v2"
                    )
                )
            else:
                records, elapsed_ms = result
                outcomes.append(
                    EviMedChannelResult(
                        channel,
                        "success",
                        records,
                        provider="evimed_v2",
                        elapsed_ms=elapsed_ms,
                    )
                )
        return outcomes

    async def _search_channel(
        self,
        channel: LiteralChannel,
        query: str,
        *,
        supports: list[str],
    ) -> list[EvidenceRecord]:
        cache_key = (channel, " ".join(query.casefold().split()))
        now = time.monotonic()
        async with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached is not None and cached[0] > now:
                increment_report_metric("evimed.cache_hit")
                self._cache.move_to_end(cache_key)
                return _records_for_supports(cached[1], supports)
            if cached is not None:
                self._cache.pop(cache_key, None)
            task = self._inflight.get(cache_key)
            owner = task is None
            if task is None:
                increment_report_metric("evimed.cache_miss")
                task = asyncio.create_task(
                    self._request_channel(channel, query, supports=[])
                )
                self._inflight[cache_key] = task
            else:
                increment_report_metric("evimed.inflight_reuse")
        try:
            records = await task
        finally:
            if owner:
                async with self._cache_lock:
                    self._inflight.pop(cache_key, None)
        if owner and self.cache_size and self.cache_ttl_seconds:
            async with self._cache_lock:
                self._cache[cache_key] = (
                    time.monotonic() + self.cache_ttl_seconds,
                    [record.model_copy(deep=True) for record in records],
                )
                self._cache.move_to_end(cache_key)
                while len(self._cache) > self.cache_size:
                    self._cache.popitem(last=False)
        return _records_for_supports(records, supports)

    async def _request_channel(
        self,
        channel: LiteralChannel,
        query: str,
        *,
        supports: list[str],
    ) -> list[EvidenceRecord]:
        url = self._endpoint()
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        client = self._shared_client()
        for attempt in range(self.attempts):
            increment_report_metric("evimed.request_attempt")
            attempt_started = time.monotonic()
            retryable_error: httpx.HTTPError | None = None
            try:
                response = await client.post(
                    url,
                    headers=headers,
                    json=self._request_payload(channel, query),
                )
                response.raise_for_status()
                payload = response.json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500 and exc.response.status_code != 429:
                    raise EvidenceServiceError(
                        f"EviMed {channel} request failed"
                    ) from exc
                retryable_error = exc
            except httpx.TransportError as exc:
                retryable_error = exc
            except ValueError as exc:
                raise EvidenceServiceError(
                    f"EviMed {channel} request failed"
                ) from exc
            else:
                break
            finally:
                add_report_elapsed_ms(
                    "evimed.http",
                    (time.monotonic() - attempt_started) * 1000,
                )
            if attempt + 1 >= self.attempts:
                increment_report_metric("evimed.request_failed")
                raise EvidenceServiceError(
                    f"EviMed {channel} request failed"
                ) from retryable_error
            increment_report_metric("evimed.retry")
            await asyncio.sleep(0.35 * (2**attempt))
        else:  # pragma: no cover - the loop always breaks or raises
            raise EvidenceServiceError(f"EviMed {channel} request failed")
        if not isinstance(payload, dict) or payload.get("code") != 200:
            increment_report_metric("evimed.business_error")
            raise EvidenceServiceError(f"EviMed {channel} returned a business error")
        data = payload.get("data") or {}
        result_group = data.get(channel) or {}
        if not isinstance(result_group, dict):
            raise EvidenceServiceError(f"EviMed {channel} returned malformed data")
        rows = result_group.get("list") or []
        records = [
            item
            for row in rows
            if isinstance(row, dict)
            and (item := _evidence_from_row(channel, row, supports=supports)) is not None
        ]
        if channel == "guide":
            parents = {
                str(row.get("id")): row
                for row in rows
                if isinstance(row, dict) and row.get("id")
            }
            records.extend(
                item
                for block in result_group.get("blocks") or []
                if isinstance(block, dict)
                and (
                    item := _evidence_from_guide_block(
                        block,
                        parent=parents.get(str(block.get("guideId"))),
                        supports=supports,
                    )
                )
                is not None
            )
        return records

    def _shared_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds),
                transport=self.transport,
                limits=httpx.Limits(
                    max_connections=8,
                    max_keepalive_connections=8,
                ),
            )
        return self._http_client

    async def aclose(self) -> None:
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()

    def _endpoint(self) -> str:
        if self.base_url.endswith("/review/api/v2/literature-guide"):
            return self.base_url
        root = self.base_url.split("/review/api", 1)[0]
        return f"{root}/review/api/v2/literature-guide"

    def _request_payload(self, channel: str, query: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "query": query,
            "type": channel,
            "useLlm": False,
            "count": min(max(1, self.result_count), 100),
        }
        if channel == "guide":
            payload["searchBlock"] = True
        else:
            # Exclude animal, in-vitro, case-report and unclassified records
            # at retrieval time. The same restriction is rechecked locally
            # because upstream metadata can be incomplete.
            payload["articleTypes"] = [
                "系统综述/Meta分析",
                "传统综述",
                "随机对照试验",
                "队列研究",
                "病例对照研究",
                "横断面研究",
                "指南/共识",
                "临床试验",
            ]
        return payload


LiteralChannel = str


def _records_for_supports(
    records: list[EvidenceRecord], supports: list[str]
) -> list[EvidenceRecord]:
    output = [record.model_copy(deep=True) for record in records]
    for record in output:
        record.supports = list(supports)
    return output


def _evidence_from_row(
    channel: str,
    row: dict[str, Any],
    *,
    supports: list[str],
) -> EvidenceRecord | None:
    title = _clean(row.get("title"), 1000)
    if not title:
        return None
    study_type = " ".join(_as_strings(row.get("studyType"))).casefold()
    category = "guideline" if channel == "guide" else "literature"
    if channel == "literature" and any(
        marker in f"{study_type} {title.casefold()}"
        for marker in ("clinical trial", "randomized", "randomised", "临床试验", "随机")
    ):
        category = "clinical_trial"
    raw_year = row.get("year") or row.get("publicationDate")
    year_match = re.search(r"(?:19|20)\d{2}", str(raw_year or ""))
    year = int(year_match.group(0)) if year_match else None
    abstract = _clean(row.get("abstract"), 6000)
    ai_summary = _clean(row.get("aiSummary"), 4000)
    summary = _clean(row.get("summary"), 4000) or abstract or ai_summary
    doi = _clean(row.get("doi"), 256) or _doi_from_url_map(row.get("url"))
    url = _normalize_url(row.get("url"))
    source = _clean(
        row.get("publisher") if channel == "guide" else row.get("journal"), 512
    ) or _clean(row.get("source") or row.get("sourceName"), 512)
    if not source:
        source = _citation_source(url, doi)
    source_id = _clean(row.get("id"), 256)
    source_identity = source_id or f"{title}|{source}"
    key = stable_id("evidence", f"evimed_v2|{channel}|{source_identity}")
    impact_factor = _as_float(row.get("impactFactor"))
    return EvidenceRecord(
        evidence_key=key,
        source_evidence_key=key,
        provider="evimed_v2",
        source_id=source_id,
        category=category,
        title=title,
        source=source,
        year=year,
        summary=summary,
        abstract=abstract,
        ai_summary=ai_summary,
        evidence_text=_clean(row.get("fullText"), 12000),
        authors=[item for item in _as_strings(row.get("authors")) if item.strip()],
        study_types=[item for item in _as_strings(row.get("studyType")) if item.strip()],
        impact_factor=impact_factor,
        core_journals=[
            item for item in _as_strings(row.get("coreJournals")) if item.strip()
        ],
        language=_clean(row.get("language"), 32),
        url=url,
        doi=doi,
        supports=supports,
    )


def _evidence_from_guide_block(
    block: dict[str, Any],
    *,
    parent: dict[str, Any] | None,
    supports: list[str],
) -> EvidenceRecord | None:
    parent = parent or {}
    text = _clean(block.get("text"), 12000)
    title = _clean(block.get("title") or parent.get("title"), 1000)
    if not text or not title:
        return None
    guide_id = _clean(block.get("guideId") or parent.get("id"), 256)
    source = _clean(block.get("publisher") or parent.get("publisher"), 512)
    if not source:
        source = _citation_source(_normalize_url(parent.get("url")), None)
    raw_year = block.get("year") or parent.get("year") or parent.get("publicationDate")
    year_match = re.search(r"(?:19|20)\d{2}", str(raw_year or ""))
    year = int(year_match.group(0)) if year_match else None
    parent_identity = guide_id or f"{title}|{source}"
    parent_key = stable_id("evidence", f"evimed_v2|guide|{parent_identity}")
    block_key = stable_id("evidence", f"{parent_key}|block|{text}")
    return EvidenceRecord(
        evidence_key=block_key,
        source_evidence_key=parent_key,
        provider="evimed_v2",
        source_id=guide_id,
        parent_guide_id=guide_id,
        category="guideline",
        title=title,
        source=source,
        year=year,
        summary=text,
        evidence_text=text,
        language=_clean(block.get("language"), 32),
        url=_normalize_url(parent.get("url")),
        supports=supports,
    )


async def _resolve_host(hostname: str) -> list[str]:
    def resolve() -> list[str]:
        rows = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        return list(dict.fromkeys(str(row[4][0]) for row in rows))

    return await asyncio.to_thread(resolve)


def _detect_type(content: bytes) -> str:
    if content.startswith(b"%PDF-"):
        return "pdf"
    image_signatures = (
        b"\xff\xd8\xff",
        b"\x89PNG\r\n\x1a\n",
        b"II*\x00",
        b"MM\x00*",
        b"BM",
    )
    if content.startswith(image_signatures):
        return "image"
    if len(content) >= 12 and (
        content[:4] == b"RIFF" or content[4:8] == b"ftyp"
    ):
        return "image"
    raise ExternalSourceError("downloaded source is not a supported PDF or image")


def _safe_filename(url: str, headers: httpx.Headers, detected: str) -> str:
    candidates: list[str] = []
    disposition = headers.get("content-disposition", "")
    encoded = re.search(r"filename\*=UTF-8''([^;]+)", disposition, flags=re.IGNORECASE)
    plain = re.search(r'filename="?([^";]+)', disposition, flags=re.IGNORECASE)
    if encoded:
        candidates.append(unquote(encoded.group(1)))
    elif plain:
        candidates.append(plain.group(1))
    parsed = urlparse(url)
    candidates.extend(parse_qs(parsed.query).get("objectName", []))
    candidates.append(Path(unquote(parsed.path)).name)
    suffix = ".pdf" if detected == "pdf" else ".png"
    for candidate in candidates:
        name = re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]", "_", Path(candidate).name)
        if name:
            return (name if Path(name).suffix else f"{name}{suffix}")[:240]
    return f"source-report{suffix}"


def _decode_text(content: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ExternalSourceError("source text encoding is unsupported")


def _clean(value: Any, limit: int) -> str | None:
    text = " ".join(str(value or "").split()).strip()
    return text[:limit] or None


def _as_strings(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return [str(value)] if value is not None else []


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _doi_from_url_map(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    candidate = value.get("DOI") or value.get("doi")
    if not isinstance(candidate, str):
        return None
    match = re.search(r"10\.\d{4,9}/\S+", unquote(candidate), flags=re.IGNORECASE)
    return match.group(0).rstrip(".,;)")[:256] if match else None


def _normalize_url(value: Any) -> str | None:
    if isinstance(value, dict):
        preferred = ("Pubmed", "DOI", "Cochrane", "CNKI", "WANGFANG", "Embase")
        for key in preferred:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                return candidate[:2048]
        for candidate in value.values():
            if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                return candidate[:2048]
        return None
    text = _clean(value, 2048)
    return text if text and text.startswith(("http://", "https://")) else None


def _citation_source(url: str | None, doi: str | None) -> str:
    hostname = (urlparse(url).hostname or "").casefold() if url else ""
    if "pubmed" in hostname or "ncbi.nlm.nih.gov" in hostname:
        return "PubMed"
    if "embase" in hostname:
        return "Embase"
    if "cochrane" in hostname:
        return "Cochrane Library"
    if "cnki" in hostname:
        return "CNKI"
    if "wanfang" in hostname:
        return "万方"
    if doi:
        return "DOI"
    return "原始文献"
