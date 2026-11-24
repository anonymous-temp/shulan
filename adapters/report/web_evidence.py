"""Web evidence channel: DeepSeek Search/Fetch as a third report evidence source.

The channel returns the same ``EvidenceRecord`` objects as the EviMed and
PubMed channels and is merged into the same candidate pool, so web pages go
through the identical object matching, applicability selection, citation and
review steps. Only allow-listed sources are admitted
(``modules.report.web_source_policy``); the literal page text retrieved for the
query is the citable original, never a model summary of it.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Awaitable
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote
from urllib.parse import urlsplit

import httpx

from adapters.report.deepseek_tools import DeepSeekToolError
from adapters.report.deepseek_tools import DeepSeekToolsClient
from adapters.report.deepseek_tools import SearchHit
from modules.report.performance import add_report_elapsed_ms
from modules.report.performance import increment_report_metric
from modules.report.ports import EviMedChannelResult
from modules.report.schemas import EvidenceRecord
from modules.report.schemas import stable_id
from modules.report.web_source_policy import classify_web_source
from modules.report.web_source_policy import clean_page_title
from modules.report.web_source_policy import evidence_document_title
from modules.report.web_source_policy import source_display_name

WEB_PROVIDER = 'deepseek_web'
_MIN_SCORE = 1.2
_ENRICHMENT_BATCH_SIZE = 4
_NOISE_TITLE = re.compile(r'^(?:ouci\b|search|filters?\b|browse\b|统一检索|login|sign in)', re.I)
_DOI = re.compile(r'10\.\d{4,9}/[^\s<>?#]+', re.I)


@dataclass
class _MetadataFlight:
    task: asyncio.Task[dict[str, Any]]
    waiters: int = 0


def web_original_key(text: str) -> str:
    """Integrity key that marks a record's ``evidence_text`` as fetched page text."""
    return 'web-original:sha256:' + hashlib.sha256(text.encode()).hexdigest()


def _clean_page_text(text: str, limit: int) -> str:
    """Drop navigation boilerplate lines; keep literal sentences in order."""
    lines = []
    for raw in str(text or '').replace('\r', '\n').split('\n'):
        line = raw.strip()
        if not line:
            if lines and lines[-1] != '':
                lines.append('')
            continue
        if re.fullmatch(r'[-*#|\s>_=]+', line) or re.match(r'^(?:\[?!?\[|!\[)', line):
            continue
        if len(line) < 25 and re.search(
            r'cookie|skip to|sign in|log ?in|subscribe|share|download pdf|view pdf|open in|'
            r'full text links|actions|cite|menu|search|登录|注册|分享|下载|收藏|返回',
            line,
            re.I,
        ):
            continue
        lines.append(line)
    cleaned = '\n'.join(lines).strip()
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit]
    end = max(cut.rfind('。'), cut.rfind('. '), cut.rfind('\n'))
    return cut[: end + 1].strip() if end > limit * 0.6 else cut.strip()


def _language(text: str) -> str:
    sample = text[:2000]
    cjk = len(re.findall(r'[一-鿿]', sample))
    return 'zh' if cjk > len(sample) * 0.2 else 'en'


def _publication_title_key(title: str) -> str:
    value = unicodedata.normalize('NFKC', re.sub(r'<[^>]+>', '', title)).casefold()
    return re.sub(r'[\W_]+', '', value)


def _registry_years(*values):
    years = []
    for value in values:
        if isinstance(value, dict):
            parts = value.get('date-parts') or []
            value = str(parts[0][0]) if parts and parts[0] else ''
        match = re.search(r'\b(?:19|20)\d{2}\b', str(value or ''))
        if match is not None and (year := int(match.group())) not in years:
            years.append(year)
    return years


def _crossref_years(row):
    publication_fields = ('issued', 'published-print', 'published-online', 'published', 'posted')
    return _registry_years(*(row.get(key) for key in publication_fields))


class _ArticleCitationHead(HTMLParser):
    def __init__(self):
        super().__init__()
        self.fields = {}

    def handle_starttag(self, tag, attrs):
        if tag != 'meta':
            return
        meta = dict(attrs)
        name = str(meta.get('name') or meta.get('property') or '').casefold()
        value = meta.get('content')
        if isinstance(value, str) and value.strip():
            self.fields.setdefault(name, []).append(value.strip())


class WebEvidenceClient:
    """Evidence gateway over DeepSeek Search/Fetch with bibliographic enrichment."""

    def __init__(
        self,
        tools: DeepSeekToolsClient,
        *,
        results_per_query: int = 10,
        keep_per_query: int = 4,
        max_text_chars: int = 16000,
        min_text_chars: int = 600,
        fetch_when_short: bool = True,
        metadata_timeout: float = 8.0,
        metadata_cache_size: int = 512,
        metadata_cache_ttl_seconds: float = 3600.0,
        metadata_negative_cache_ttl_seconds: float = 30.0,
        ncbi_api_key: str = '',
        crossref_mailto: str = '',
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._tools = tools
        self._results = max(1, min(50, int(results_per_query)))
        self._keep = max(1, int(keep_per_query))
        self._max_chars = max(1000, int(max_text_chars))
        self._min_chars = max(100, int(min_text_chars))
        self._fetch_when_short = bool(fetch_when_short)
        self._metadata_timeout = float(metadata_timeout)
        self._ncbi_api_key = (ncbi_api_key or '').strip()
        self._crossref_mailto = (crossref_mailto or '').strip()
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._metadata_gate = asyncio.Semaphore(2)
        self._metadata_cache_size = max(0, int(metadata_cache_size))
        self._metadata_cache_ttl = max(0.0, float(metadata_cache_ttl_seconds))
        self._metadata_negative_cache_ttl = max(0.0, float(metadata_negative_cache_ttl_seconds))
        self._metadata_cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._metadata_inflight: dict[str, _MetadataFlight] = {}

    @property
    def enabled(self) -> bool:
        return self._tools.enabled

    @property
    def usage(self) -> dict[str, Any]:
        return self._tools.usage.snapshot()

    async def aclose(self) -> None:
        tasks = [flight.task for flight in self._metadata_inflight.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._metadata_inflight.clear()
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()
        await self._tools.aclose()

    def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(self._metadata_timeout), transport=self._transport, follow_redirects=True
            )
        return self._http

    async def _cached_metadata(
        self, key: str, load: Callable[[], Awaitable[dict[str, Any]]], *, metric: str = 'web_metadata',
    ) -> dict[str, Any]:
        cached = self._metadata_cache.get(key)
        if cached is not None:
            if cached[0] > time.monotonic():
                self._metadata_cache.move_to_end(key)
                increment_report_metric(metric + ('.cache_hit' if cached[1] else '.negative_cache_hit'))
                return deepcopy(cached[1])
            self._metadata_cache.pop(key, None)
        flight = self._metadata_inflight.get(key)
        if flight is None:
            increment_report_metric(metric + '.cache_miss')
            flight = _MetadataFlight(asyncio.create_task(self._load_metadata(key, load, metric)))
            self._metadata_inflight[key] = flight
        else:
            increment_report_metric(metric + '.inflight_reuse')
        flight.waiters += 1
        try:
            return deepcopy(await asyncio.shield(flight.task))
        finally:
            flight.waiters -= 1
            # One cancelled report must not cancel another report's lookup.
            # Once no caller needs the lookup, do not leave HTTP work running.
            if not flight.waiters and not flight.task.done():
                if self._metadata_inflight.get(key) is flight:
                    self._metadata_inflight.pop(key, None)
                flight.task.cancel()
                await asyncio.gather(flight.task, return_exceptions=True)

    async def _load_metadata(
        self, key: str, load: Callable[[], Awaitable[dict[str, Any]]], metric: str,
    ) -> dict[str, Any]:
        started = time.monotonic()
        increment_report_metric(metric + '.lookup')
        try:
            result = await load()
            ttl = self._metadata_cache_ttl if result else self._metadata_negative_cache_ttl
            if self._metadata_cache_size and ttl:
                self._metadata_cache[key] = (time.monotonic() + ttl, deepcopy(result))
                self._metadata_cache.move_to_end(key)
                while len(self._metadata_cache) > self._metadata_cache_size:
                    self._metadata_cache.popitem(last=False)
            return result
        finally:
            add_report_elapsed_ms(metric + '.lookup', (time.monotonic() - started) * 1000)
            flight = self._metadata_inflight.get(key)
            if flight is not None and flight.task is asyncio.current_task():
                self._metadata_inflight.pop(key, None)

    async def _metadata(self, *, pmcid=None, pmid=None, doi=None) -> dict[str, Any]:
        """Best-effort bibliographic metadata; a failure never drops the page."""
        key = pmcid.upper() if pmcid else ('pmid:' + pmid if pmid else ('doi:' + doi.casefold() if doi else None))
        if key is None:
            return {}
        return await self._cached_metadata(key, lambda: self._request_metadata(pmcid=pmcid, pmid=pmid, doi=doi))

    async def _request_metadata(self, *, pmcid=None, pmid=None, doi=None) -> dict[str, Any]:
        result: dict[str, Any] = {}
        try:
            async with self._metadata_gate:
                if pmcid or pmid:
                    result_id = pmcid[3:] if pmcid else pmid
                    params = {'db': 'pmc' if pmcid else 'pubmed', 'id': result_id, 'retmode': 'json'}
                    if self._ncbi_api_key:
                        params['api_key'] = self._ncbi_api_key
                    response = await self._client().get(
                        'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi', params=params
                    )
                    response.raise_for_status()
                    row = (response.json().get('result') or {}).get(result_id) or {}
                    ids = {
                        item.get('idtype'): item.get('value')
                        for item in row.get('articleids') or []
                        if isinstance(item, dict)
                    }
                    years = _registry_years(row.get('pubdate'), row.get('epubdate'))
                    result = {
                        'title': row.get('title'),
                        'source': row.get('fulljournalname') or row.get('source'),
                        'year': years[0] if years else None,
                        'publicationYears': years,
                        'authors': [
                            a.get('name') for a in row.get('authors') or [] if isinstance(a, dict) and a.get('name')
                        ],
                        'doi': (ids.get('doi') or '').casefold() or None,
                        'pmid': ids.get('pmid'),
                    }
                    if not self._ncbi_api_key:
                        await asyncio.sleep(0.34)
                else:
                    params = {'mailto': self._crossref_mailto} if self._crossref_mailto else None
                    response = await self._client().get('https://api.crossref.org/works/' + doi, params=params)
                    response.raise_for_status()
                    row = response.json().get('message') or {}
                    years = _crossref_years(row)
                    result = {
                        'title': (row.get('title') or [None])[0],
                        'source': (row.get('container-title') or [None])[0],
                        'year': years[0] if years else None,
                        'publicationYears': years,
                        'authors': [
                            ' '.join(filter(None, (a.get('given'), a.get('family')))) or a.get('name')
                            for a in row.get('author') or []
                            if isinstance(a, dict)
                        ],
                        'doi': doi,
                        'type': row.get('type'),
                    }
                    result['authors'] = [name for name in result['authors'] if name]
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            result = {}
        return result

    async def _metadata_by_title(self, title: str, *, exact=False) -> dict[str, Any]:
        """Crossref lookup by title; accepted only when the titles agree."""
        prefix = 'exact-title:' if exact else 'title:'
        key = prefix + ' '.join(title.casefold().split())
        return await self._cached_metadata(key, lambda: self._request_metadata_by_title(title, exact=exact))

    async def _request_metadata_by_title(self, title: str, *, exact=False) -> dict[str, Any]:
        result: dict[str, Any] = {}
        try:
            async with self._metadata_gate:
                params = {'query.bibliographic': title[:240], 'rows': '4' if exact else '1'}
                if self._crossref_mailto:
                    params['mailto'] = self._crossref_mailto
                response = await self._client().get('https://api.crossref.org/works', params=params)
                response.raise_for_status()
                items = (response.json().get('message') or {}).get('items') or []
            if exact and items:
                candidates = []
                wanted = _publication_title_key(title)
                for row in items:
                    found = (row.get('title') or [''])[0]
                    if not isinstance(found, str) or _publication_title_key(found) != wanted:
                        continue
                    years = _crossref_years(row)
                    candidates.append({
                        'title': found,
                        'doi': row.get('DOI'),
                        'year': years[0] if years else None,
                        'publicationYears': years,
                        'type': row.get('type'),
                    })
                return {'exactCandidates': candidates}
            if items:
                row = items[0]
                found = (row.get('title') or [''])[0]
                wanted = set(re.findall(r'[a-z0-9\u4e00-\u9fff]+', title.casefold()))
                actual = set(re.findall(r'[a-z0-9\u4e00-\u9fff]+', re.sub(r'<[^>]+>', '', found).casefold()))
                if wanted and actual and len(wanted & actual) / len(wanted | actual) >= 0.8 and row.get('DOI'):
                    parts = ((row.get('issued') or {}).get('date-parts') or [[None]])[0]
                    authors = [
                        ' '.join(filter(None, (a.get('given'), a.get('family')))) or a.get('name')
                        for a in row.get('author') or []
                        if isinstance(a, dict)
                    ]
                    result = {
                        'title': found,
                        'source': (row.get('container-title') or [None])[0],
                        'year': parts[0] if parts and isinstance(parts[0], int) else None,
                        'authors': [name for name in authors if name],
                        'doi': str(row['DOI']).casefold(),
                        'type': row.get('type'),
                    }
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError):
            result = {}
        return result

    async def reference_metadata(self, *, title: str, year: int | None, url: str | None) -> dict[str, Any]:
        """Bind bibliography additions to a previously selected publication."""
        source = classify_web_source(url, title) if url else None
        parts = urlsplit(url or '')
        url_doi = source.doi if source else None
        if parts.scheme in {'http', 'https'} and parts.hostname in {'doi.org', 'dx.doi.org'}:
            url_doi = unquote(parts.path.lstrip('/'))
        actual_title = _publication_title_key(title)
        if not actual_title:
            return {}

        def verified(candidate):
            candidate_title = candidate.get('title')
            doi = candidate.get('doi')
            candidate_year = candidate.get('year')
            if (
                not isinstance(candidate_title, str)
                or _publication_title_key(candidate_title) != actual_title
                or year is not None and year not in candidate.get('publicationYears', (candidate_year,))
                or not isinstance(doi, str)
                or not _DOI.fullmatch(doi)
            ):
                return {}
            return {'doi': doi.lower(), 'year': candidate_year}

        if url_doi and _DOI.fullmatch(url_doi):
            url_doi = url_doi.casefold()
            if year is not None:
                return {'doi': url_doi}
            registry = verified(await self._metadata(doi=url_doi))
            return registry if registry.get('doi') == url_doi else {'doi': url_doi}
        if source and source.pmcid:
            metadata = verified(await self._metadata(pmcid=source.pmcid))
            if metadata:
                return metadata
        pmid = re.fullmatch(r'/(\d+)/?', parts.path) if parts.hostname == 'pubmed.ncbi.nlm.nih.gov' else None
        if pmid:
            # The same article can be published online and in an issue in different calendar years.
            metadata = verified(await self._metadata(pmid=pmid[1]))
            if metadata:
                return metadata
        if not clean_page_title(title):
            return {}
        candidates = await self._metadata_by_title(title, exact=True)
        preprint = parts.hostname == 'europepmc.org' and bool(re.match(r'^/article/PPR/', parts.path, re.I))
        compatible = {
            row['doi']: row
            for candidate in candidates.get('exactCandidates', ())
            if (candidate.get('type') == 'posted-content') == preprint and (row := verified(candidate))
        }
        if len(compatible) == 1:
            return next(iter(compatible.values()))
        if source and source.tier == 'peer_reviewed' and not preprint:
            return verified(await self._publisher_identity(source.canonical_url))
        return {}

    async def _source_page(self, url: str) -> dict[str, Any]:
        async def load():
            try:
                page = await self._tools.fetch(url)
            except (DeepSeekToolError, ValueError):
                page = None
            if page is None:
                return {}
            return {'text': _clean_page_text(page.content, self._max_chars), 'title': clean_page_title(page.title)}

        return await self._cached_metadata('page:' + url, load, metric='web_source_page')

    async def _publisher_identity(self, url: str) -> dict[str, Any]:
        parts = urlsplit(url)
        article = parts._replace(path=parts.path.rstrip('/').removesuffix('/pdf'), query='', fragment='').geturl()
        if classify_web_source(article) is None:
            return {}

        async def load():
            try:
                async with asyncio.timeout(1.5):
                    async with self._client().stream('GET', article, follow_redirects=False) as response:
                        response.raise_for_status()
                        parser = _ArticleCitationHead()
                        size = 0
                        for chunk in response.aiter_text():
                            parser.feed(chunk)
                            size += len(chunk)
                            if size >= 160000 or re.search(r'</head\s*>', chunk, re.I):
                                break
                titles = parser.fields.get('citation_title') or parser.fields.get('dc.title', ())
                identity = {_publication_title_key(title): title for title in titles if clean_page_title(title)}
                if len(identity) != 1:
                    return {}
                dois = {value.casefold() for value in parser.fields.get('citation_doi', ()) if _DOI.fullmatch(value)}
                years = _registry_years(*parser.fields.get('citation_publication_date', ()))
                return {'title': next(iter(identity.values())), 'doi': next(iter(dois)) if len(dois) == 1 else None,
                    'year': years[0] if years else None, 'url': article}
            except (TimeoutError, httpx.HTTPError, ValueError):
                return {}

        return await self._cached_metadata('publisher:' + article, load, metric='web_article_identity')

    async def _record(self, hit: SearchHit, supports: list[str], *, bibliography: bool = True) -> EvidenceRecord | None:
        if hit.rerank_score < _MIN_SCORE or _NOISE_TITLE.search(hit.title):
            return None
        source = classify_web_source(hit.url, hit.title)
        if source is None:
            return None
        text = _clean_page_text(hit.content, self._max_chars)
        page = None
        if len(text) < self._min_chars and self._fetch_when_short:
            page = await self._source_page(source.canonical_url)
            text = page.get('text', text)
        if len(text) < self._min_chars:
            return None
        metadata = await self._metadata(pmcid=source.pmcid, doi=source.doi) if bibliography else {}
        page_title = (page or {}).get('title') or clean_page_title(hit.title)
        if page_title is None and page is None and not clean_page_title(metadata.get('title') or ''):
            # Search engines can return a chapter heading as the result
            # title. Recover identity from this exact page, never another
            # publication or a fabricated title inferred from its topic.
            page = await self._source_page(source.canonical_url)
            page_title = page.get('title')
            fetched_text = page.get('text') or ''
            if len(fetched_text) >= self._min_chars:
                text = fetched_text
        if not bibliography and page_title is None and source.pmcid:
            # Resolve a missing page identity only. Routine bibliography
            # enrichment is deferred until the actual reference is used.
            metadata = await self._metadata(pmcid=source.pmcid, doi=None)
        if bibliography and not metadata and source.tier == 'peer_reviewed' and page_title:
            # A publisher page without a DOI in its URL: confirm the article
            # and complete its citation from the bibliographic index.
            metadata = await self._metadata_by_title(page_title)
        title = clean_page_title(metadata.get('title') or '') or page_title
        if not bibliography and not evidence_document_title(title, text):
            identity = await self._publisher_identity(source.canonical_url)
            title = identity.get('title')
            if title:
                metadata = identity
        if title is None:
            return None  # a search-result fragment is not a citable title
        study_types = list(source.study_types)
        reclassified = classify_web_source(source.canonical_url, title)
        if reclassified is not None:
            study_types = list(dict.fromkeys([*reclassified.study_types, *study_types]))
            category = reclassified.category
        else:
            category = source.category
        year = metadata.get('year') or hit.year
        key_text = hashlib.sha256(text.encode()).hexdigest()[:16]
        return EvidenceRecord(
            evidence_key=stable_id('evidence', f'web|{source.canonical_url}|{key_text}'),
            source_evidence_key=web_original_key(text),
            provider=WEB_PROVIDER,
            source_id=metadata.get('url') or source.canonical_url,
            category=category,
            title=title,
            source=str(metadata.get('source') or source_display_name(source.canonical_url, hit.site_name)),
            year=year if isinstance(year, int) else None,
            summary=(hit.summary or hit.snippet or None),
            evidence_text=text,
            authors=list(metadata.get('authors') or [])[:12],
            study_types=study_types,
            url=metadata.get('url') or source.canonical_url,
            doi=metadata.get('doi') or source.doi,
            relevance=round(min(1.0, hit.rerank_score / 3.0), 4),
            supports=list(supports),
            language=_language(text),
        )

    async def search(self, query: str, *, supports: list[str]) -> list[EvidenceRecord]:
        outcomes = await self.search_detailed(query, supports=supports)
        return [record for outcome in outcomes if outcome.status == 'success' for record in outcome.records]

    async def search_detailed(
        self, query: str, *, supports: list[str], channels: tuple[str, ...] = ('guide', 'literature')
    ) -> list[EviMedChannelResult]:
        """One web search, reported per requested channel like the other gateways."""
        return await self._search_detailed(query, supports=supports, channels=channels, bibliography=True)

    async def search_recall_detailed(
        self, query: str, *, supports: list[str], channels: tuple[str, ...] = ('guide', 'literature')
    ) -> list[EviMedChannelResult]:
        """Recall original passages without an index lookup for each hit."""
        return await self._search_detailed(query, supports=supports, channels=channels, bibliography=False)

    async def _search_detailed(self, query, *, supports, channels, bibliography):
        started = time.monotonic()
        requested = [channel for channel in ('guide', 'literature') if channel in channels]
        try:
            hits = await self._tools.search(query[:128], count=self._results, need_content=True)
            status = 'success'
        except (DeepSeekToolError, ValueError):
            hits, status = [], 'failed'
        ranked = sorted(hits, key=lambda hit: (hit.authority >= 3, hit.rerank_score), reverse=True)
        records, seen = [], set()
        batch_size = min(_ENRICHMENT_BATCH_SIZE, self._keep)
        for offset in range(0, len(ranked), batch_size):
            batch = ranked[offset : offset + batch_size]
            increment_report_metric('web_enrichment.examined_hits', len(batch))
            built = await asyncio.gather(
                *(self._record(hit, supports, bibliography=bibliography) for hit in batch),
                return_exceptions=True,
            )
            # gather preserves the existing rank, even when HTTP calls finish
            # out of order. Rejected/duplicate hits leave room for later batches.
            for record in built:
                if not isinstance(record, EvidenceRecord):
                    continue
                identity = (record.doi or record.source_id, record.source_evidence_key)
                if identity in seen:
                    continue
                seen.add(identity)
                records.append(record)
                if len(records) >= self._keep:
                    break
            if len(records) >= self._keep:
                increment_report_metric('web_enrichment.skipped_hits', max(0, len(ranked) - offset - len(batch)))
                break
        elapsed = round((time.monotonic() - started) * 1000, 3)
        outcomes = []
        for channel in ('guide', 'literature'):
            if channel not in requested:
                outcomes.append(EviMedChannelResult(channel, 'not_requested', []))
                continue
            chosen = [record for record in records if (record.category == 'guideline') == (channel == 'guide')]
            outcomes.append(EviMedChannelResult(channel, status, chosen, provider=WEB_PROVIDER, elapsed_ms=elapsed))
        if 'guide' not in requested and 'literature' in requested:
            outcomes[-1].records.extend(record for record in records if record.category == 'guideline')
        return outcomes


__all__ = ['WEB_PROVIDER', 'WebEvidenceClient', 'web_original_key']
