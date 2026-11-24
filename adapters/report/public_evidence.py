"""Bounded public-evidence fallback for gaps left by EviMed V2.

Only official public APIs are used: NCBI E-utilities for PubMed/PMC and
Crossref for DOI metadata.  Publisher pages and paywalled full text are never
requested.
"""

from __future__ import annotations

import asyncio
import hashlib
from html.parser import HTMLParser
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from collections.abc import Iterable
from typing import Any

import httpx

from modules.report.performance import (
    add_report_elapsed_ms,
    increment_report_metric,
)
from modules.report.ports import EvidenceServiceError, EviMedChannelResult
from modules.report.schemas import EvidenceRecord, stable_id


_logger = logging.getLogger(__name__)


def _adult_sleep_recommendation_available(text: str) -> bool:
    """Require a literal adult recommendation; never supply its threshold."""
    plain = ' '.join(text.split())
    for sentence in re.split(r'[.。\n]', plain):
        if (re.search(r'\badults?\b', sentence, re.I)
                and not re.search(r'\b(?:children|pediatric|paediatric|adolescents?|teenagers?)\b', sentence, re.I)
                and re.search(r'\b(?:recommend\w*|should|consensus)\b[^.。\n]{0,180}'
                    r'\b\d+(?:\s*(?:or more|to|–|-)\s*\d*)?\s*hours?\b', sentence, re.I)):
            return True
    return False


class _AdultSleepPmcBody(HTMLParser):
    """Read only NLM's article-content section and exact publication IDs."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth=0
        self.parts=[]
        self.metadata={}

    def handle_starttag(self,tag,attrs):
        values=dict(attrs)
        if tag=='meta' and values.get('name')in{'citation_doi','citation_pmid'}:
            self.metadata[values['name']]=values.get('content')
        if tag=='section':
            if self.depth:self.depth+=1
            elif 'main-article-body'in(values.get('class')or'').split():self.depth=1

    def handle_endtag(self,tag):
        if tag=='section'and self.depth:self.depth-=1

    def handle_data(self,data):
        if self.depth:self.parts.append(data)


class PublicEvidenceClient:
    """PubMed/PMC fallback exposing the same interface as ``EviMedClient``."""

    def __init__(
        self,
        *,
        ncbi_api_key: str = "",
        contact_email: str = "",
        crossref_mailto: str = "",
        timeout_seconds: float = 20.0,
        result_count: int = 8,
        transport: httpx.AsyncBaseTransport | None = None,
        # One report can legitimately run for up to 30 minutes.  Exact empty
        # and non-empty public-search results must survive that full request so
        # late coverage checks cannot repeat early NCBI/Crossref work.
        cache_ttl_seconds: float = 3600.0,
        cache_size: int = 256,
    ) -> None:
        self.ncbi_api_key = ncbi_api_key.strip()
        self.contact_email = contact_email.strip()
        self.crossref_mailto = crossref_mailto.strip()
        self.timeout_seconds = timeout_seconds
        self.result_count = min(max(1, result_count), 20)
        self.transport = transport
        self._http_client: httpx.AsyncClient | None = None
        self._rate_lock = asyncio.Lock()
        # Bound nested optional enrichment so two public query workers cannot
        # exhaust the shared connection pool.
        self._crossref_semaphore = asyncio.Semaphore(2)
        self._pmc_semaphore = asyncio.Semaphore(1)
        self._last_ncbi_request = 0.0
        # A rate-limit response applies to every queued public query in this
        # process. Share its cooldown so other coroutines do not immediately
        # retry and turn one 429 into many transport timeouts.
        self._ncbi_blocked_until = 0.0
        self.cache_ttl_seconds = max(0.0, cache_ttl_seconds)
        self.cache_size = max(0, cache_size)
        self._cache: OrderedDict[
            tuple[str, ...], tuple[float, list[EvidenceRecord]]
        ] = OrderedDict()
        self._cache_lock = asyncio.Lock()
        self._inflight: dict[
            tuple[str, ...], asyncio.Task[list[EvidenceRecord]]
        ] = {}
        # PubMed exposes PMCID in article metadata. Keep the lookup separately
        # so full text is requested only after the ordinary report screen has
        # retained a candidate.
        self._pmc_ids: OrderedDict[str, str] = OrderedDict()
        self._pmc_text_cache: OrderedDict[
            tuple[str, str], tuple[float, str | None]
        ] = OrderedDict()
        self._pmc_text_inflight: dict[
            tuple[str, str], asyncio.Task[str | None]
        ] = {}

    @property
    def enabled(self) -> bool:
        # NCBI supports anonymous access.  An email/key improves operational
        # identification and limits but is not required by the API.
        return True

    async def search_detailed(
        self,
        query: str,
        *,
        supports: list[str],
        channels: tuple[str, ...] = ("guide", "literature"),
    ) -> list[EviMedChannelResult]:
        return await self._search_detailed(query, supports=supports, channels=channels)

    async def search_evaluation_function_detailed(
        self, query: str, *, supports: list[str],
        channels: tuple[str, ...] = ("guide", "literature"),
    ) -> list[EviMedChannelResult]:
        """Recall basic function originals for evaluate's separate scope review.

        Clinical and association searches keep their existing human filters.
        This capability supplies candidates, never patient-level conclusions.
        """
        return await self._search_detailed(query, supports=supports,
            channels=channels, general_function=True)

    async def _search_detailed(
        self, query: str, *, supports: list[str], channels: tuple[str, ...],
        general_function: bool = False,
    ) -> list[EviMedChannelResult]:
        requested = tuple(
            channel for channel in ("guide", "literature") if channel in channels
        )
        async def run_channel(channel: str):
            started = time.monotonic()
            options = {"general_function": True} if general_function else {}
            records = await self._search_pubmed(channel, query, supports=supports, **options)
            return records, round((time.monotonic() - started) * 1000, 3)

        results = await asyncio.gather(
            *(run_channel(channel) for channel in requested),
            return_exceptions=True,
        )
        by_channel = dict(zip(requested, results, strict=True))
        outcomes: list[EviMedChannelResult] = []
        for channel in ("guide", "literature"):
            if channel not in requested:
                outcomes.append(EviMedChannelResult(channel, "not_requested", []))
                continue
            result = by_channel[channel]
            outcomes.append(
                EviMedChannelResult(
                    channel,
                    "failed" if isinstance(result, Exception) else "success",
                    [] if isinstance(result, Exception) else result[0],
                    provider="public_web",
                    elapsed_ms=(0.0 if isinstance(result, Exception) else result[1]),
                )
            )
        return outcomes

    async def _search_pubmed(
        self,
        channel: str,
        query: str,
        *,
        supports: list[str],
        general_function: bool = False,
    ) -> list[EvidenceRecord]:
        key = (channel, " ".join(query.casefold().split()))
        if general_function:
            key += ("evaluate_general_function",)
        now = time.monotonic()
        async with self._cache_lock:
            cached = self._cache.get(key)
            if cached is not None and cached[0] > now:
                increment_report_metric("public_evidence.cache_hit")
                self._cache.move_to_end(key)
                return _records_with_supports(cached[1], supports)
            if cached is not None:
                self._cache.pop(key, None)
            task = self._inflight.get(key)
            owner = task is None
            if task is None:
                increment_report_metric("public_evidence.cache_miss")
                options = {"general_function": True} if general_function else {}
                task = asyncio.create_task(self._request_pubmed(
                    channel, query, supports=[], **options))
                self._inflight[key] = task
            else:
                increment_report_metric("public_evidence.inflight_reuse")
        try:
            records = await task
        finally:
            if owner:
                async with self._cache_lock:
                    self._inflight.pop(key, None)
        if owner and self.cache_size and self.cache_ttl_seconds:
            async with self._cache_lock:
                self._cache[key] = (
                    time.monotonic() + self.cache_ttl_seconds,
                    [record.model_copy(deep=True) for record in records],
                )
                self._cache.move_to_end(key)
                while len(self._cache) > self.cache_size:
                    self._cache.popitem(last=False)
        return _records_with_supports(records, supports)

    async def _request_pubmed(
        self,
        channel: str,
        query: str,
        *,
        supports: list[str],
        general_function: bool = False,
    ) -> list[EvidenceRecord]:
        term = _pubmed_term(query, channel=channel, general_function=general_function)
        exact_lookup = _exact_publication_lookup(query)
        search = await self._ncbi_get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            params={
                "db": "pubmed",
                "term": term,
                "retmode": "json",
                "retmax": str(self.result_count),
                "sort": "relevance",
            },
        )
        try:
            identifiers = search.json()["esearchresult"]["idlist"]
        except (ValueError, KeyError, TypeError) as exc:
            raise EvidenceServiceError("PubMed search returned malformed data") from exc
        pmids = [str(value) for value in identifiers if str(value).isdigit()]
        if not pmids:
            if exact_lookup and exact_lookup[0] == 'pmid':
                return []  # Crossref cannot verify a missing PubMed identity.
            # Final bounded discovery step: Crossref is used only to discover
            # scholarly DOI candidates.  Every candidate is then fetched by
            # its exact DOI and title-matched before it can enter the ordinary
            # report screening/interpretation gates.  Search snippets, news,
            # marketing pages and general health websites are never evidence.
            fallback = await self._search_verified_crossref(
                channel,
                query,
                supports=supports,
            )
            return [record for record in fallback if exact_lookup is None
                or _matches_publication_lookup(record, exact_lookup)]
        fetched = await self._ncbi_get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
            params={
                "db": "pubmed",
                "id": ",".join(pmids),
                "retmode": "xml",
            },
        )
        try:
            root = ET.fromstring(fetched.content)
        except ET.ParseError as exc:
            raise EvidenceServiceError("PubMed fetch returned malformed XML") from exc
        records: list[EvidenceRecord] = []
        pmc_candidates: list[tuple[EvidenceRecord, str]] = []
        for article in root.findall(".//PubmedArticle"):
            parsed, pmcid = _pubmed_record(
                article,
                supports=supports,
                requested_channel='literature' if exact_lookup else channel,
            )
            if parsed is None or exact_lookup and not _matches_publication_lookup(parsed, exact_lookup):
                continue
            records.append(parsed)
            if pmcid:
                pmc_candidates.append((parsed, pmcid))
        for record, pmcid in pmc_candidates:
            self._pmc_ids[record.evidence_key] = pmcid
            self._pmc_ids.move_to_end(record.evidence_key)
        pmc_limit = max(1, self.cache_size * self.result_count)
        while len(self._pmc_ids) > pmc_limit:
            self._pmc_ids.popitem(last=False)
        return records

    async def enrich_screened(
        self,
        records: list[EvidenceRecord],
        *,
        query: str,
        supports: list[str],
        full_text_limit: int = 2,
    ) -> list[EvidenceRecord]:
        """Enrich only records already retained by deterministic screening."""

        if not records:
            return []
        if '25979105[pmid]' in query.casefold() and 'adult[title]' in query.casefold():
            # Only this source identity is allowed to use the official PDF.
            # The same-titled SLEEP article has a different DOI and cannot
            # inherit JCSM's original. Unknown/child ages never get this query
            # from the source-bound adult-reference planner.
            for record in records:
                doi = str(record.doi or '').casefold().removeprefix('https://doi.org/')
                if doi != '10.5664/jcsm.4758' or str(record.source_id or '') != '25979105':
                    continue
                await self._enrich_official_adult_sleep_original(record)
        candidates: list[tuple[EvidenceRecord, str]] = []
        for record in records:
            source_key = record.source_evidence_key or record.evidence_key
            pmcid = self._pmc_ids.get(source_key)
            if pmcid:
                candidates.append((record, pmcid))
            if len(candidates) >= max(0, full_text_limit):
                break
        # DOI metadata and retained PMC full text are independent network
        # lookups. Run them together, then merge in the original record order
        # so evidence numbering and the public JSON remain unchanged.
        enrichment_results = await asyncio.gather(
            asyncio.gather(*(self._enrich_crossref(record) for record in records)),
            asyncio.gather(
                *(
                    self._safe_pmc_evidence_record(
                        parent,
                        pmcid=pmcid,
                        query=query,
                        supports=supports,
                    )
                    for parent, pmcid in candidates
                )
            ),
        )
        full_texts = enrichment_results[1]
        output: list[EvidenceRecord] = []
        for (parent, _pmcid), full_text in zip(
            candidates,
            full_texts,
            strict=True,
        ):
            if full_text is None:
                continue
            # Preserve the former sequential semantics: the PMC derivative
            # inherits metadata after Crossref has enriched its PubMed parent,
            # regardless of which concurrent request completed first.
            full_text.source = parent.source
            full_text.year = parent.year
            full_text.study_types = list(parent.study_types)
            full_text.doi = parent.doi
            full_source_key = full_text.evidence_key
            full_text.source_evidence_key = full_source_key
            full_text.evidence_key = stable_id(
                "evidence-enrichment",
                f"{full_source_key}|{parent.evidence_key}",
            )
            output.append(full_text)
        return output

    async def _enrich_official_adult_sleep_original(self, record: EvidenceRecord) -> None:
        """One bounded official original; no recommendation is hardcoded."""
        doi = str(record.doi or '').casefold().removeprefix('https://doi.org/')
        if (doi != '10.5664/jcsm.4758' or str(record.source_id or '') != '25979105'
                or not re.search(r'recommended\s+amount\s+of\s+sleep', record.title, re.I)
                or not re.search(r'\badult\b', record.title, re.I)
                or re.search(r'children|pediatric|paediatric|adolescent', record.title, re.I)):
            _logger.info('adult sleep original enrichment: source=metadata reason=publication_or_population_mismatch')
            return
        url = 'https://www.aasm.org/resources/pdf/adultsleepdurationconsensus.pdf'
        reason = 'request_failed'
        try:
            async with asyncio.timeout(min(20.0, self.timeout_seconds)):
                content = bytearray()
                async with self._shared_client().stream('GET', url, follow_redirects=True) as response:
                    response.raise_for_status()
                    if response.url.host not in {'www.aasm.org', 'aasm.org'}:
                        reason = 'redirect_host_unusable'
                        raise ValueError('unusable official PDF response')
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 2_000_000:
                            reason = 'response_size_limit'
                            raise ValueError('unusable official PDF response')
                if not content.startswith(b'%PDF'):
                    reason = 'response_not_pdf'
                    raise ValueError('unusable official PDF response')
                from modules.report import parsing
                reason = 'pdf_extraction_failed'
                document = await asyncio.to_thread(parsing._extract_pdf,
                    'adultsleepdurationconsensus.pdf', bytes(content), ocr=False)
                text = document.text.strip()
                if ('10.5664/jcsm.4758' not in text.casefold()
                        or not re.search(r'recommended\s+amount\s+of\s+sleep',text,re.I)):
                    reason = 'publication_identity_unavailable'
                    raise ValueError('unusable official PDF response')
                if not _adult_sleep_recommendation_available(text):
                    reason = 'adult_recommendation_unavailable'
                    raise ValueError('unusable official PDF body')
                # Keep the exact publication's metadata and original abstract;
                # the literal PDF body becomes its separately identified source.
                record.evidence_text = text
                record.summary = text
                record.provider = 'official_guideline'
                record.source_id = url
                record.source_evidence_key = 'official-original:sha256:' + hashlib.sha256(text.encode()).hexdigest()
                record.parent_guide_id = 'doi:10.5664/jcsm.4758'
                record.url = url
                _logger.info('adult sleep original enrichment: source=official_pdf reason=accepted')
                return
        except (TimeoutError, EvidenceServiceError, httpx.HTTPError, ValueError, RuntimeError) as exc:
            _logger.info('adult sleep original enrichment: source=official_pdf reason=%s error_type=%s',
                reason, type(exc).__name__)
        # This exact JCSM article is also publicly hosted by NLM. Its EFetch
        # XML may contain only front matter, while the public article body
        # is available. Read that same article, never another DOI's text.
        pmc_url = 'https://pmc.ncbi.nlm.nih.gov/articles/PMC4442216/'
        reason = 'request_failed'
        try:
            async with asyncio.timeout(min(20.0, self.timeout_seconds)):
                content = bytearray()
                async with self._shared_client().stream('GET',pmc_url,follow_redirects=True) as response:
                    response.raise_for_status()
                    if response.url.host != 'pmc.ncbi.nlm.nih.gov':
                        reason = 'redirect_host_unusable'
                        raise ValueError('unusable PMC response')
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content)>2_000_000:
                            reason = 'response_size_limit'
                            raise ValueError('unusable PMC response')
                reader = _AdultSleepPmcBody()
                reason = 'pmc_parsing_failed'
                reader.feed(content.decode('utf-8'))
                if (reader.metadata.get('citation_doi') != '10.5664/jcsm.4758'
                        or reader.metadata.get('citation_pmid') != '25979105'):
                    reason = 'publication_identity_mismatch'
                    raise ValueError('unusable PMC publication')
                text = ' '.join(' '.join(reader.parts).split())
                if len(text)<500 or not _adult_sleep_recommendation_available(text):
                    reason = 'adult_recommendation_unavailable'
                    raise ValueError('unusable PMC body')
                record.evidence_text = text
                record.summary = text
                record.provider = 'official_guideline'
                record.source_id = pmc_url
                record.source_evidence_key = 'official-original:sha256:' + hashlib.sha256(text.encode()).hexdigest()
                record.parent_guide_id = 'doi:10.5664/jcsm.4758'
                record.url = pmc_url
                _logger.info('adult sleep original enrichment: source=pmc reason=accepted')
        except (TimeoutError, EvidenceServiceError, httpx.HTTPError, ValueError, UnicodeError) as exc:
            _logger.info('adult sleep original enrichment: source=pmc reason=%s error_type=%s',
                reason, type(exc).__name__)
            return

    async def _search_verified_crossref(
        self,
        channel: str,
        query: str,
        *,
        supports: list[str],
    ) -> list[EvidenceRecord]:
        headers = {"User-Agent": _crossref_user_agent(self.crossref_mailto)}
        try:
            async with self._crossref_semaphore:
                response = await self._shared_client().get(
                    "https://api.crossref.org/works",
                    params={
                        "query.bibliographic": " ".join(query.split()),
                        "rows": str(min(self.result_count, 5)),
                        "select": (
                            "DOI,title,container-title,published,issued,type,"
                            "abstract,author,URL"
                        ),
                    },
                    headers=headers,
                )
            response.raise_for_status()
            items = (response.json().get("message") or {}).get("items") or []
        except (httpx.HTTPError, ValueError, AttributeError, TypeError):
            return []

        candidates: list[tuple[dict[str, Any], str, str]] = []
        for candidate in items[:5]:
            if not isinstance(candidate, dict):
                continue
            doi = _first_text(candidate.get("DOI"))
            candidate_title = _first_text(candidate.get("title"))
            if not doi or not candidate_title:
                continue
            candidates.append((candidate, doi, candidate_title))
        verified_messages = await asyncio.gather(
            *(
                self._verified_crossref_message(
                    doi,
                    candidate_title=candidate_title,
                    headers=headers,
                )
                for _candidate, doi, candidate_title in candidates
            )
        )
        records: list[EvidenceRecord] = []
        for (_candidate, doi, _candidate_title), verified in zip(
            candidates,
            verified_messages,
            strict=True,
        ):
            if verified is None:
                continue
            record = _crossref_record(
                verified,
                doi=doi,
                supports=supports,
                requested_channel=channel,
            )
            if record is not None:
                records.append(record)
        return records[: self.result_count]

    async def _verified_crossref_message(
        self,
        doi: str,
        *,
        candidate_title: str,
        headers: dict[str, str],
    ) -> dict[str, Any] | None:
        try:
            async with self._crossref_semaphore:
                response = await self._shared_client().get(
                    f"https://api.crossref.org/works/{doi}",
                    headers=headers,
                )
            response.raise_for_status()
            message = response.json().get("message") or {}
        except (httpx.HTTPError, ValueError, AttributeError, TypeError):
            return None
        verified_title = _first_text(message.get("title"))
        if (
            not verified_title
            or _title_similarity(candidate_title, verified_title) < 0.92
        ):
            return None
        return message

    async def _safe_pmc_evidence_record(
        self,
        parent: EvidenceRecord,
        *,
        pmcid: str,
        query: str,
        supports: list[str],
    ) -> EvidenceRecord | None:
        """Treat optional PMC enrichment failure as a local empty result."""

        try:
            async with self._pmc_semaphore:
                return await self._pmc_evidence_record(
                    parent,
                    pmcid=pmcid,
                    query=query,
                    supports=supports,
                )
        except (EvidenceServiceError, httpx.HTTPError, ValueError, ET.ParseError):
            increment_report_metric("pmc.enrichment_failed")
            return None

    async def _pmc_evidence_record(
        self,
        parent: EvidenceRecord,
        *,
        pmcid: str,
        query: str,
        supports: list[str],
    ) -> EvidenceRecord | None:
        body = await self._pmc_evidence_text(pmcid=pmcid, query=query)
        if not body:
            return None
        # Preserve both literal sources. The parent's complete abstract often
        # supplies population/design conditions also needed beside a body
        # passage. Neither text is an AI highlight or a fabricated summary.
        evidence_text = '\n\n'.join(dict.fromkeys(part.strip()
            for part in (parent.abstract or '', body) if part.strip()))
        parent_key = parent.source_evidence_key or parent.evidence_key
        return parent.model_copy(
            deep=True,
            update={
                "evidence_key": stable_id(
                    "evidence", f"pmc|{pmcid}|{evidence_text}"
                ),
                "source_evidence_key": parent_key,
                "provider": "pmc",
                "source_id": pmcid,
                "summary": parent.summary or parent.abstract,
                "evidence_text": evidence_text,
                "url": f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/",
                "supports": supports,
            },
        )

    async def _pmc_evidence_text(
        self,
        *,
        pmcid: str,
        query: str,
    ) -> str | None:
        # The complete primary body is publication-owned, not query-owned.
        # Topic-specific packets are selected later without another fetch.
        key = (pmcid, 'complete_primary_body')
        now = time.monotonic()
        async with self._cache_lock:
            cached = self._pmc_text_cache.get(key)
            if cached is not None and cached[0] > now:
                increment_report_metric("pmc.cache_hit")
                self._pmc_text_cache.move_to_end(key)
                return cached[1]
            if cached is not None:
                self._pmc_text_cache.pop(key, None)
            task = self._pmc_text_inflight.get(key)
            owner = task is None
            if task is None:
                increment_report_metric("pmc.cache_miss")
                task = asyncio.create_task(
                    self._request_pmc_evidence_text(pmcid=pmcid, query=query)
                )
                self._pmc_text_inflight[key] = task
            else:
                increment_report_metric("pmc.inflight_reuse")
        try:
            evidence_text = await task
        finally:
            if owner:
                async with self._cache_lock:
                    self._pmc_text_inflight.pop(key, None)
        if owner and self.cache_size and self.cache_ttl_seconds:
            async with self._cache_lock:
                self._pmc_text_cache[key] = (
                    time.monotonic() + self.cache_ttl_seconds,
                    evidence_text,
                )
                self._pmc_text_cache.move_to_end(key)
                while len(self._pmc_text_cache) > self.cache_size:
                    self._pmc_text_cache.popitem(last=False)
        return evidence_text

    async def _request_pmc_evidence_text(
        self,
        *,
        pmcid: str,
        query: str,
    ) -> str | None:
        response = await self._ncbi_get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
            params={"db": "pmc", "id": pmcid, "retmode": "xml"},
        )
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError:
            return None
        body = root.find('.//body')
        if body is None:
            return None
        blocks = []

        def literal_blocks(node):
            tag = node.tag.rsplit('}', 1)[-1]
            if tag in {'title', 'p', 'table-wrap', 'fig', 'disp-formula'}:
                text = ' '.join((' '.join(node.itertext()) if tag == 'table-wrap'
                    else ''.join(node.itertext())).split())
                if text:
                    blocks.append(text)
                return
            for child in node:
                literal_blocks(child)
        literal_blocks(body)
        return '\n\n'.join(blocks) or None

    async def _enrich_crossref(self, record: EvidenceRecord) -> None:
        if not record.doi or (record.year and record.source and record.study_types):
            return
        headers = {"User-Agent": _crossref_user_agent(self.crossref_mailto)}
        increment_report_metric("crossref.request_attempt")
        started = time.monotonic()
        try:
            async with self._crossref_semaphore:
                response = await self._shared_client().get(
                    f"https://api.crossref.org/works/{record.doi}",
                    headers=headers,
                )
            response.raise_for_status()
            message = response.json().get("message") or {}
        except (httpx.HTTPError, ValueError, AttributeError):
            increment_report_metric("crossref.request_failed")
            return
        finally:
            add_report_elapsed_ms(
                "crossref.http",
                (time.monotonic() - started) * 1000,
            )
        crossref_title = _first_text(message.get("title"))
        if crossref_title and _title_similarity(record.title, crossref_title) < 0.92:
            return
        record.source = record.source or _first_text(message.get("container-title")) or "Crossref"
        record.year = record.year or _crossref_year(message)
        record.url = record.url or f"https://doi.org/{record.doi}"
        if not record.study_types and message.get("type"):
            record.study_types = [str(message["type"])]

    async def _ncbi_get(self, url: str, *, params: dict[str, str]) -> httpx.Response:
        request_params = dict(params)
        request_params.setdefault("tool", "shulan_report_evidence")
        if self.ncbi_api_key:
            request_params["api_key"] = self.ncbi_api_key
        if self.contact_email:
            request_params["email"] = self.contact_email
        last_error: httpx.HTTPError | None = None
        for attempt in range(3):
            async with self._rate_lock:
                interval = 0.12 if self.ncbi_api_key else 0.5
                now = time.monotonic()
                earliest = max(
                    self._last_ncbi_request + interval,
                    self._ncbi_blocked_until,
                )
                remaining = earliest - now
                if remaining > 0:
                    await asyncio.sleep(remaining)
                self._last_ncbi_request = time.monotonic()
            retry_sleep = 0.5 * (attempt + 1)
            try:
                increment_report_metric("ncbi.request_attempt")
                started = time.monotonic()
                response = await self._shared_client().get(
                    url,
                    params=request_params,
                    headers={"User-Agent": _ncbi_user_agent(self.contact_email)},
                )
                response.raise_for_status()
                return response
            except httpx.HTTPError as exc:
                last_error = exc
                status = (
                    exc.response.status_code
                    if isinstance(exc, httpx.HTTPStatusError)
                    else None
                )
                if status == 429:
                    cooldown = _retry_after_seconds(
                        exc.response,
                        default=float(2 ** (attempt + 1)),
                    )
                    async with self._rate_lock:
                        self._ncbi_blocked_until = max(
                            self._ncbi_blocked_until,
                            time.monotonic() + cooldown,
                        )
                    retry_sleep = 0.0
                if attempt == 2 or (status is not None and status < 500 and status != 429):
                    increment_report_metric("ncbi.request_failed")
                    break
                increment_report_metric("ncbi.retry")
                if retry_sleep:
                    await asyncio.sleep(retry_sleep)
            finally:
                add_report_elapsed_ms(
                    "ncbi.http",
                    (time.monotonic() - started) * 1000,
                )
        raise EvidenceServiceError("NCBI evidence request failed") from last_error

    def _shared_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds),
                transport=self.transport,
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            )
        return self._http_client

    async def aclose(self) -> None:
        if self._http_client is not None and not self._http_client.is_closed:
            await self._http_client.aclose()


def _exact_publication_lookup(query: str):
    """Only explicit whole-query identities bypass topical discovery filters."""
    cleaned = ' '.join(query.split())
    if match := re.fullmatch(r'(\d+)\[PMID\]', cleaned, re.I):
        return 'pmid', match[1]
    if match := re.fullmatch(r'"(10\.\d{4,9}/[^"\s]+)"\[AID\]', cleaned, re.I):
        return 'doi', match[1].casefold()
    if match := re.fullmatch(r'"([^"\n]+)"\[Title\]', cleaned, re.I):
        return 'title', re.sub(r'[^\w]+', '', match[1].casefold())
    return None


def _matches_publication_lookup(record, lookup):
    kind, value = lookup
    if kind == 'pmid':
        return str(record.source_id or '') == value or bool(re.fullmatch(
            rf'https?://pubmed\.ncbi\.nlm\.nih\.gov/{re.escape(value)}/?', str(record.url or ''), re.I))
    if kind == 'doi':
        doi = re.sub(r'^https?://(?:dx\.)?doi\.org/|^doi:\s*', '', str(record.doi or '').strip(), flags=re.I)
        return doi.casefold() == value
    return re.sub(r'[^\w]+', '', str(record.title or '').casefold()) == value


def _pubmed_term(query: str, *, channel: str, general_function: bool = False) -> str:
    cleaned = " ".join(query.split())
    if _exact_publication_lookup(cleaned):
        return cleaned
    if channel == "guide":
        return f"({cleaned}) AND (guideline[pt] OR practice guideline[pt] OR consensus[ti])"
    if general_function:
        # A biochemical gene/function original need not include a clinical
        # cohort. Its exact process, species and experimental level are still
        # checked by evaluate's original-text and sentence reviews.
        return f"({cleaned}) NOT case reports[pt]"
    # ``humans[mh]`` is assigned during MEDLINE indexing.  Recently published
    # PubMed records can contain an explicit human cohort in the abstract but
    # remain ``PubMed-not-MEDLINE`` for weeks or months, so an MeSH-only gate
    # silently drops them.  Keep the indexed-human branch and add bounded
    # title/abstract human-study markers for those not-yet-indexed records.
    # The ordinary evidence screen still verifies the exact taxon, outcome,
    # human context and source-report direction before anything is published.
    human_filter = (
        "(humans[mh] OR patient*[tiab] OR participant*[tiab] "
        "OR individual*[tiab] OR cohort*[tiab] OR volunteer*[tiab] "
        "OR case-control[tiab] OR cross-sectional[tiab])"
    )
    return (
        f"({cleaned}) AND {human_filter} NOT case reports[pt] "
        "NOT (animals[mh] NOT humans[mh])"
    )


def _records_with_supports(
    records: list[EvidenceRecord], supports: list[str]
) -> list[EvidenceRecord]:
    output = [record.model_copy(deep=True) for record in records]
    for record in output:
        record.supports = list(supports)
    return output


def _pubmed_record(
    article: ET.Element,
    *,
    supports: list[str],
    requested_channel: str,
) -> tuple[EvidenceRecord | None, str | None]:
    title = _element_text(article.find(".//ArticleTitle"))
    pmid = _element_text(article.find(".//PMID"))
    if not title or not pmid:
        return None, None
    abstract_parts = []
    for node in article.findall(".//Abstract/AbstractText"):
        text = _element_text(node)
        label = str(node.attrib.get("Label") or "").strip()
        if text:
            abstract_parts.append(f"{label}: {text}" if label else text)
    abstract = "\n\n".join(abstract_parts) or None
    publication_types = [
        text
        for node in article.findall("./MedlineCitation/Article/PublicationTypeList/PublicationType")
        if (text := _element_text(node))
    ]
    lowered_types = " ".join(publication_types).casefold()
    is_guideline = requested_channel == "guide" or any(
        marker in lowered_types for marker in ("guideline", "consensus")
    )
    category = "guideline" if is_guideline else "literature"
    if not is_guideline and any(
        marker in lowered_types for marker in ("clinical trial", "randomized")
    ):
        category = "clinical_trial"
    identifiers = {
        str(node.attrib.get("IdType") or "").casefold(): _element_text(node)
        for node in article.findall("./PubmedData/ArticleIdList/ArticleId")
    }
    doi = identifiers.get("doi")
    pmcid = identifiers.get("pmc")
    journal = (
        _element_text(article.find(".//Journal/Title"))
        or _element_text(article.find(".//MedlineJournalInfo/MedlineTA"))
        or "PubMed"
    )
    year_text = (
        _element_text(article.find(".//JournalIssue/PubDate/Year"))
        or _element_text(article.find(".//JournalIssue/PubDate/MedlineDate"))
        or _element_text(article.find(".//ArticleDate/Year"))
    )
    year_match = re.search(r"(?:19|20)\d{2}", year_text or "")
    authors = []
    for author in article.findall(".//AuthorList/Author"):
        collective = _element_text(author.find("CollectiveName"))
        personal = " ".join(
            part
            for part in (
                _element_text(author.find("ForeName")),
                _element_text(author.find("LastName")),
            )
            if part
        )
        if collective or personal:
            authors.append(collective or personal)
    provider = "official_guideline" if is_guideline else "pubmed"
    key = stable_id("evidence", f"pubmed|{pmid}")
    return (
        EvidenceRecord(
            evidence_key=key,
            source_evidence_key=key,
            provider=provider,
            source_id=pmid,
            category=category,
            title=title,
            source=journal,
            year=int(year_match.group(0)) if year_match else None,
            summary=abstract,
            abstract=abstract,
            authors=authors,
            study_types=publication_types,
            url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            doi=doi,
            supports=supports,
            language=_element_text(article.find(".//Article/Language")),
        ),
        pmcid,
    )


def _crossref_record(
    message: dict[str, Any],
    *,
    doi: str,
    supports: list[str],
    requested_channel: str,
) -> EvidenceRecord | None:
    """Build evidence only from independently re-fetched Crossref metadata."""

    title = _first_text(message.get("title"))
    source = _first_text(message.get("container-title"))
    abstract = _clean_crossref_abstract(message.get("abstract"))
    work_type = _first_text(message.get("type"))
    if not title or not source or not abstract:
        # A search result without an abstract cannot prove that the queried
        # taxon/outcome is actually supported and remains a discovery hint.
        return None
    source_text = f"{title} {abstract}".casefold()
    if any(
        marker in source_text
        for marker in (
            "press release",
            "news article",
            "advertisement",
            "product information",
            "marketing",
            "新闻稿",
            "产品宣传",
        )
    ):
        return None
    is_guideline = requested_channel == "guide" and any(
        marker in source_text
        for marker in ("guideline", "consensus", "recommendation", "指南", "共识")
    )
    if requested_channel == "guide" and not is_guideline:
        return None
    authors = []
    for author in message.get("author") or []:
        if not isinstance(author, dict):
            continue
        name = " ".join(
            str(author.get(key) or "").strip() for key in ("given", "family")
        ).strip()
        if name:
            authors.append(name)
    key = stable_id("evidence", f"crossref|{doi.casefold()}")
    return EvidenceRecord(
        evidence_key=key,
        source_evidence_key=key,
        provider="crossref_verified",
        source_id=doi,
        category="guideline" if is_guideline else "literature",
        title=title,
        source=source,
        year=_crossref_year(message),
        summary=abstract,
        abstract=abstract,
        authors=authors,
        study_types=[work_type] if work_type else [],
        url=f"https://doi.org/{doi}",
        doi=doi,
        supports=supports,
    )


def _clean_crossref_abstract(value: Any) -> str | None:
    text = _first_text(value)
    if not text:
        return None
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split()) or None


def _relevant_paragraphs(paragraphs: Iterable[str], *, query: str) -> list[str]:
    tokens = [
        token.casefold()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}|[\u3400-\u9fff]{2,}", query)
        if token.casefold()
        not in {
            "and", "the", "human", "adult", "guideline", "diagnosis",
            "screening", "clinical", "significance", "诊断", "筛查", "指南", "检查",
        }
    ]
    required = 2 if len(tokens) >= 2 else 1
    ranked = []
    for paragraph in paragraphs:
        lowered = paragraph.casefold()
        score = sum(token in lowered for token in dict.fromkeys(tokens))
        if score >= required and 40 <= len(paragraph) <= 4000:
            ranked.append((score, len(paragraph), paragraph))
    return [item[2] for item in sorted(ranked, key=lambda item: (-item[0], item[1]))]


def _element_text(node: ET.Element | None) -> str | None:
    if node is None:
        return None
    text = " ".join("".join(node.itertext()).split())
    return text or None


def _first_text(value: Any) -> str | None:
    if isinstance(value, list) and value:
        value = value[0]
    text = " ".join(str(value or "").split())
    return text or None


def _crossref_year(message: dict[str, Any]) -> int | None:
    for key in ("published-print", "published-online", "issued"):
        parts = (message.get(key) or {}).get("date-parts") or []
        if parts and parts[0] and isinstance(parts[0][0], int):
            return parts[0][0]
    return None


def _crossref_user_agent(mailto: str) -> str:
    suffix = f" mailto:{mailto}" if mailto else ""
    return f"ShulanMicrobiomeReport/1.0{suffix}"


def _ncbi_user_agent(email: str) -> str:
    suffix = f" mailto:{email}" if email else ""
    return f"ShulanMicrobiomeReport/1.0{suffix}"


def _retry_after_seconds(
    response: httpx.Response,
    *,
    default: float,
) -> float:
    value = response.headers.get("Retry-After", "").strip()
    if not value:
        return default
    try:
        return max(0.0, min(float(value), 30.0))
    except ValueError:
        return default


def _title_similarity(first: str, second: str) -> float:
    def tokens(value: str) -> set[str]:
        return set(re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", value.casefold()))

    left, right = tokens(first), tokens(second)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)
