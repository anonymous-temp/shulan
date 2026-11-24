import asyncio

import httpx
import pytest

import adapters.report.public_evidence as public_evidence_module
from adapters.report.public_evidence import PublicEvidenceClient, _pubmed_term
from modules.report.schemas import EvidenceRecord

PUBMED_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>12345678</PMID>
      <Article>
        <ArticleTitle>Fusobacterium nucleatum and colorectal cancer</ArticleTitle>
        <Abstract><AbstractText Label="RESULTS">A human cohort found Fusobacterium nucleatum associated with colorectal cancer.</AbstractText></Abstract>
        <AuthorList><Author><ForeName>Ada</ForeName><LastName>Researcher</LastName></Author></AuthorList>
        <Journal><JournalIssue><PubDate><Year>2025</Year></PubDate></JournalIssue><Title>Gut</Title></Journal>
        <PublicationTypeList><PublicationType>Cohort Study</PublicationType></PublicationTypeList>
        <Language>eng</Language>
      </Article>
    </MedlineCitation>
    <PubmedData><ArticleIdList>
      <ArticleId IdType="pubmed">12345678</ArticleId>
      <ArticleId IdType="doi">10.1000/example</ArticleId>
      <ArticleId IdType="pmc">PMC999</ArticleId>
    </ArticleIdList></PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""


PMC_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<article><body><sec>
  <p>This unrelated paragraph discusses laboratory methods only.</p>
  <p>In a human cohort, Fusobacterium nucleatum abundance in stool was associated with colorectal cancer.</p>
</sec></body></article>
"""


@pytest.mark.asyncio
async def test_pubmed_fallback_reads_pmc_only_after_candidate_is_screened():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/esearch.fcgi"):
            return httpx.Response(
                200,
                request=request,
                json={"esearchresult": {"idlist": ["12345678"]}},
            )
        if request.url.path.endswith("/efetch.fcgi"):
            if request.url.params.get("db") == "pmc":
                return httpx.Response(200, request=request, content=PMC_XML)
            return httpx.Response(200, request=request, content=PUBMED_XML)
        raise AssertionError(f"unexpected request: {request.url}")

    client = PublicEvidenceClient(
        ncbi_api_key="test-key",
        transport=httpx.MockTransport(handler),
    )
    outcomes = await client.search_detailed(
        "Fusobacterium nucleatum colorectal cancer",
        supports=["taxon-fn"],
        channels=("literature",),
    )

    literature = next(item for item in outcomes if item.channel == "literature")
    assert literature.status == "success"
    assert len(literature.records) == 1
    abstract = literature.records[0]
    assert abstract.provider == "pubmed"
    assert abstract.source_id == "12345678"
    assert abstract.authors == ["Ada Researcher"]
    assert abstract.study_types == ["Cohort Study"]
    assert abstract.doi == "10.1000/example"
    assert not any(request.url.params.get("db") == "pmc" for request in requests)

    full_texts = await client.enrich_screened(
        [abstract],
        query="Fusobacterium nucleatum colorectal cancer",
        supports=["taxon-fn"],
    )

    assert len(full_texts) == 1
    assert full_texts[0].provider == "pmc"
    assert full_texts[0].source_id == "PMC999"
    assert "associated with colorectal cancer" in full_texts[0].evidence_text
    request_count = len(requests)
    cached_full_texts = await client.enrich_screened(
        [abstract],
        query="  fusobacterium  nucleatum colorectal CANCER ",
        supports=["taxon-fn"],
    )
    assert len(cached_full_texts) == 1
    assert len(requests) == request_count
    assert all(
        request.url.host in {"eutils.ncbi.nlm.nih.gov", "api.crossref.org"}
        for request in requests
    )


@pytest.mark.asyncio
async def test_parallel_pmc_inherits_completed_crossref_metadata():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.crossref.org":
            await asyncio.sleep(0.02)
            return httpx.Response(
                200,
                request=request,
                json={
                    "message": {
                        "title": ["Exact article title"],
                        "container-title": ["Enriched Journal"],
                        "issued": {"date-parts": [[2024, 1, 2]]},
                        "type": "journal-article",
                    }
                },
            )
        if request.url.params.get("db") == "pmc":
            return httpx.Response(200, request=request, content=PMC_XML)
        raise AssertionError(f"unexpected request: {request.url}")

    client = PublicEvidenceClient(
        ncbi_api_key="test-key",
        transport=httpx.MockTransport(handler),
    )
    record = EvidenceRecord(
        evidenceKey="pubmed-parent",
        category="literature",
        title="Exact article title",
        source="",
        doi="10.1000/exact",
        sourceId="12345678",
        provider="pubmed",
    )
    client._pmc_ids[record.evidence_key] = "PMC999"

    full_texts = await client.enrich_screened(
        [record],
        query="Fusobacterium nucleatum colorectal cancer",
        supports=["taxon-fn"],
    )

    assert full_texts[0].source == "Enriched Journal"
    assert full_texts[0].year == 2024
    assert full_texts[0].study_types == ["journal-article"]


@pytest.mark.asyncio
async def test_pmc_enrichment_transport_failure_does_not_abort_records():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.PoolTimeout("pool exhausted", request=request)

    client = PublicEvidenceClient(transport=httpx.MockTransport(handler))
    record = EvidenceRecord(
        evidenceKey="pubmed-parent",
        category="literature",
        title="Retained abstract",
        source="Journal",
        sourceId="12345678",
        provider="pubmed",
        year=2024,
        studyTypes=["Cohort Study"],
    )
    client._pmc_ids[record.evidence_key] = "PMC999"

    full_texts = await client.enrich_screened(
        [record],
        query="microbiome disease",
        supports=["target"],
    )

    assert full_texts == []


@pytest.mark.asyncio
async def test_crossref_only_enriches_an_exact_doi_record():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            request=request,
            json={
                "status": "ok",
                "message": {
                    "title": ["Exact article title"],
                    "container-title": ["Clinical Journal"],
                    "published-online": {"date-parts": [[2024, 2, 1]]},
                    "type": "journal-article",
                },
            },
        )

    client = PublicEvidenceClient(transport=httpx.MockTransport(handler))
    record = EvidenceRecord(
        evidence_key="record",
        category="literature",
        title="Exact article title",
        source="",
        doi="10.1000/exact",
    )
    await client._enrich_crossref(record)

    assert record.source == "Clinical Journal"
    assert record.year == 2024
    assert record.url == "https://doi.org/10.1000/exact"
    assert record.study_types == ["journal-article"]
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_empty_pubmed_uses_crossref_discovery_only_after_exact_doi_verification():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/esearch.fcgi"):
            return httpx.Response(
                200,
                request=request,
                json={"esearchresult": {"idlist": []}},
            )
        if request.url.path == "/works":
            return httpx.Response(
                200,
                request=request,
                json={
                    "message": {
                        "items": [
                            {
                                "DOI": "10.1000/verified",
                                "title": ["Exact scholarly title"],
                            }
                        ]
                    }
                },
            )
        if request.url.path == "/works/10.1000/verified":
            return httpx.Response(
                200,
                request=request,
                json={
                    "message": {
                        "DOI": "10.1000/verified",
                        "title": ["Exact scholarly title"],
                        "container-title": ["Clinical Microbiome Journal"],
                        "abstract": (
                            "A human cohort found Fusobacterium nucleatum "
                            "associated with colorectal cancer."
                        ),
                        "published": {"date-parts": [[2025, 1, 2]]},
                        "type": "journal-article",
                        "author": [{"given": "Ada", "family": "Researcher"}],
                    }
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    client = PublicEvidenceClient(
        ncbi_api_key="test-key",
        transport=httpx.MockTransport(handler),
    )
    outcomes = await client.search_detailed(
        "Fusobacterium nucleatum colorectal cancer",
        supports=["taxon-fn"],
        channels=("literature",),
    )

    records = next(item.records for item in outcomes if item.channel == "literature")
    assert len(records) == 1
    assert records[0].provider == "crossref_verified"
    assert records[0].doi == "10.1000/verified"
    assert records[0].authors == ["Ada Researcher"]
    assert [request.url.path for request in requests] == [
        "/entrez/eutils/esearch.fcgi",
        "/works",
        "/works/10.1000/verified",
    ]


@pytest.mark.asyncio
async def test_crossref_discovery_without_verified_abstract_is_not_evidence():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/works":
            return httpx.Response(
                200,
                request=request,
                json={
                    "message": {
                        "items": [
                            {"DOI": "10.1000/no-abstract", "title": ["Candidate"]}
                        ]
                    }
                },
            )
        return httpx.Response(
            200,
            request=request,
            json={
                "message": {
                    "title": ["Candidate"],
                    "container-title": ["Journal"],
                    "type": "journal-article",
                }
            },
        )

    client = PublicEvidenceClient(transport=httpx.MockTransport(handler))
    records = await client._search_verified_crossref(
        "literature",
        "microbe disease",
        supports=["taxon"],
    )
    assert records == []


@pytest.mark.asyncio
async def test_crossref_exact_doi_verification_is_parallel_but_ordered():
    active = 0
    max_active = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, max_active
        if request.url.path == "/works":
            return httpx.Response(
                200,
                request=request,
                json={
                    "message": {
                        "items": [
                            {"DOI": f"10.1000/{index}", "title": [f"Title {index}"]}
                            for index in range(3)
                        ]
                    }
                },
            )
        active += 1
        max_active = max(max_active, active)
        index = int(request.url.path.rsplit("/", 1)[-1])
        await asyncio.sleep(0.03 - index * 0.005)
        active -= 1
        return httpx.Response(
            200,
            request=request,
            json={
                "message": {
                    "DOI": f"10.1000/{index}",
                    "title": [f"Title {index}"],
                    "container-title": ["Journal"],
                    "abstract": f"Human cohort evidence {index}.",
                    "type": "journal-article",
                }
            },
        )

    client = PublicEvidenceClient(transport=httpx.MockTransport(handler))
    records = await client._search_verified_crossref(
        "literature",
        "microbiome disease",
        supports=["target"],
    )

    assert max_active > 1
    assert [record.title for record in records] == [
        "Title 0",
        "Title 1",
        "Title 2",
    ]


def test_pubmed_queries_are_human_bounded_and_guides_are_type_filtered():
    literature_term = _pubmed_term("microbe disease", channel="literature")
    assert "humans[mh]" in literature_term
    assert "individual*[tiab]" in literature_term
    assert "cohort*[tiab]" in literature_term
    assert "case reports[pt]" in literature_term
    guide_term = _pubmed_term("disease diagnosis", channel="guide")
    assert "guideline[pt]" in guide_term
    assert "practice guideline[pt]" in guide_term


@pytest.mark.asyncio
async def test_public_cache_collapses_concurrent_identical_queries():
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if request.url.path.endswith("/esearch.fcgi"):
            await asyncio.sleep(0.01)
            return httpx.Response(
                200,
                request=request,
                json={"esearchresult": {"idlist": ["12345678"]}},
            )
        if request.url.params.get("db") == "pmc":
            return httpx.Response(200, request=request, content=PMC_XML)
        return httpx.Response(200, request=request, content=PUBMED_XML)

    client = PublicEvidenceClient(
        ncbi_api_key="test-key",
        transport=httpx.MockTransport(handler),
    )
    first, second = await asyncio.gather(
        client._search_pubmed("literature", "shared query", supports=["a"]),
        client._search_pubmed("literature", " shared  query ", supports=["b"]),
    )

    assert attempts == 2
    assert all(record.supports == ["a"] for record in first)
    assert all(record.supports == ["b"] for record in second)


@pytest.mark.asyncio
async def test_ncbi_transient_rate_limit_is_retried(monkeypatch):
    attempts = 0
    requests: list[httpx.Request] = []

    async def no_wait(_seconds: float) -> None:
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        requests.append(request)
        if attempts < 3:
            return httpx.Response(
                429,
                request=request,
                headers={"Retry-After": "1"},
            )
        return httpx.Response(200, request=request, json={"ok": True})

    monkeypatch.setattr(public_evidence_module.asyncio, "sleep", no_wait)
    client = PublicEvidenceClient(
        ncbi_api_key="test-key",
        transport=httpx.MockTransport(handler),
    )

    response = await client._ncbi_get(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        params={"db": "pubmed", "term": "test"},
    )

    assert response.status_code == 200
    assert attempts == 3
    assert all(
        request.url.params.get("tool") == "shulan_report_evidence"
        for request in requests
    )
    assert all(request.headers.get("User-Agent") for request in requests)
    assert client._ncbi_blocked_until > 0
