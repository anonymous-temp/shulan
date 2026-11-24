"""Candidate retrieval is tested offline; no secrets, EviMed or public traffic."""
import asyncio
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from adapters.gut_barrier import evidence as evidence_module
from adapters.gut_barrier.evidence import EUROPE_PMC_SEARCH_URL, GutBarrierEvidence


QUERY = "obesity adults cardiometabolic guidelines"
CARDIO_QUERY = "cardiovascular prevention dyslipidemia guideline"
IBS_QUERY = "irritable bowel syndrome diarrhea fecal calprotectin clinical guideline"


def record(index=1, **changes):
    return {"title": "Adult obesity and cardiometabolic outcomes guideline", "source": "Clinical Journal",
            "year": 2025, "doi": f"10.1234/obesity.{index}", "url": None,
            "abstract": "A prospective cohort of adults with obesity.",
            "study_types": ["cohort"], **changes}


def outcome(records, channel="literature", status="success"):
    return SimpleNamespace(channel=channel, status=status, records=records)


def primary(outcomes):
    return SimpleNamespace(enabled=True, search_detailed=AsyncMock(return_value=outcomes), aclose=AsyncMock())


def public_row(index=1, **changes):
    return {"id": str(1000 + index), "source": "MED", "title": "Adult obesity and cardiometabolic outcomes guideline",
            "pubYear": "2025", "abstractText": "<p>A cohort of adults with <b>obesity</b>.</p>",
            "journalInfo": {"journal": {"title": "Clinical Journal"}},
            "pubTypeList": {"pubType": ["Journal Article"]}, **changes}


def public_transport(rows=None, *, calls=None):
    def handle(request):
        if calls is not None:
            calls.append(request)
        assert str(request.url).split("?", 1)[0] == EUROPE_PMC_SEARCH_URL
        assert request.url.params["resultType"] == "core"
        assert request.url.params["format"] == "json"
        assert "authorization" not in request.headers
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, json={"resultList": {"result": rows if rows is not None else [public_row()]}})
    return httpx.MockTransport(handle)


@pytest.mark.asyncio
async def test_evimed_is_preferred_and_candidates_never_create_patient_scores():
    client = primary([outcome([record()])])
    provider = GutBarrierEvidence(evimed_client=client,
        transport=httpx.MockTransport(lambda _: pytest.fail("public fallback must not be used")))
    result = await provider.retrieve([QUERY])
    assert result["status"] == "success" and result["partial"] is False
    assert result["provider"] == "evimed_v2" and result["issues"] == []
    candidate = result["candidates"][0]
    assert candidate["candidateOnly"] is True and candidate["doi"] == "10.1234/obesity.1"
    assert candidate["url"] == "https://doi.org/10.1234/obesity.1"
    assert not ({"risk", "score", "probability", "recommendation"} & candidate.keys())
    client.search_detailed.assert_awaited_once_with(QUERY, supports=[], channels=("guide", "literature"))
    await provider.aclose()
    client.aclose.assert_not_awaited()  # Injected/shared clients remain caller-owned.


@pytest.mark.asyncio
async def test_existing_evimed_client_public_signature_and_evidence_models_are_supported():
    from adapters.report.clients import EviMedClient
    requests = []
    def handle(request):
        requests.append(json.loads(request.content))
        channel = requests[-1]["type"]
        rows = [] if channel == "guide" else [{
            "id": "local-1", "title": "Adult obesity and cardiometabolic outcomes guideline",
            "journal": "Clinical Journal", "year": 2025,
            "abstract": "Prospective cohort of adults with obesity.",
            "doi": "10.1234/obesity.1", "studyType": "队列研究",
        }]
        return httpx.Response(200, json={"code": 200, "data": {channel: {"list": rows}}})
    client = EviMedClient(base_url="https://evimed.invalid", api_key="local-test-only",
        attempts=1, result_count=4, transport=httpx.MockTransport(handle))
    provider = GutBarrierEvidence(evimed_client=client,
        transport=httpx.MockTransport(lambda _: pytest.fail("public fallback not needed")))
    try:
        result = await provider.retrieve([QUERY])
    finally:
        await provider.aclose()
        await client.aclose()
    assert result["provider"] == "evimed_v2" and result["partial"] is False
    assert result["candidates"][0]["studyTypes"] == ["队列研究"]
    assert {row["type"] for row in requests} == {"guide", "literature"}
    assert all(row["query"] == QUERY and row["count"] == 4 for row in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["missing", "disabled", "empty", "failed", "unrelated", "malformed"])
async def test_primary_unavailable_empty_or_irrelevant_uses_official_public_fallback(mode):
    client = None if mode == "missing" else primary([outcome([])])
    if mode == "disabled":
        client.enabled = False
    elif mode == "failed":
        client.search_detailed.side_effect = ConnectionError("secret-provider-token")
    elif mode == "unrelated":
        client.search_detailed.return_value = [outcome([record(title="Cancer opioid conversion guidelines", abstract="Cancer opioid treatment.")], "guide")]
    elif mode == "malformed":
        client.search_detailed.return_value = {"unexpected": []}
    calls = []
    provider = GutBarrierEvidence(evimed_client=client, transport=public_transport(calls=calls))
    try:
        result = await provider.retrieve([QUERY])
    finally:
        await provider.aclose()
    assert result["status"] == "success" and result["partial"] is False
    assert result["provider"] == "europe_pmc"
    assert all(issue["recovered"] for issue in result["issues"])
    assert result["candidates"][0]["pmid"] == "1001"
    assert result["candidates"][0]["abstract"] == "A cohort of adults with obesity."
    assert len(calls) == 1 and calls[0].url.params["query"] == "obesity adults clinical guideline"
    assert "secret-provider-token" not in json.dumps(result)


@pytest.mark.asyncio
async def test_one_failed_channel_with_other_candidates_is_partial_without_second_provider():
    provider = GutBarrierEvidence(evimed_client=primary([
        outcome([], "guide", "failed"), outcome([record()])]),
        transport=httpx.MockTransport(lambda _: pytest.fail("usable primary exists")))
    result = await provider.retrieve([QUERY])
    assert result["partial"] is True and result["candidates"]
    assert result["issues"][0]["code"] == "EVIDENCE_CHANNEL_UNAVAILABLE"


@pytest.mark.asyncio
async def test_empty_fallback_is_audited_not_fabricated():
    provider = GutBarrierEvidence(transport=public_transport([]))
    try:
        result = await provider.retrieve([QUERY])
    finally:
        await provider.aclose()
    assert result["candidates"] == [] and result["partial"] is True
    assert result["queries"][0]["status"] == "nohit"
    assert any(issue["code"] == "EVIDENCE_PUBLIC_EMPTY_OR_UNSUITABLE" for issue in result["issues"])


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [None, {}, "raw assessment", [None], [""], ["x" * 241],
    ["姓名：李明 obesity"], ["patient John Smith obesity"], ["userid 1234567"],
    ["user@example.com obesity"], ["2026-09-24 obesity"], ["https://private.invalid obesity"],
    ["obesity\nprivate"], ["guidelines adults clinical"]])
async def test_invalid_or_identifying_input_never_reaches_any_provider(invalid):
    client = primary([])
    provider = GutBarrierEvidence(evimed_client=client,
        transport=httpx.MockTransport(lambda _: pytest.fail("must not transmit invalid input")))
    result = await provider.retrieve(invalid)
    assert result["candidates"] == [] and result["partial"] is True
    assert result["queries"] == []
    client.search_detailed.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_query_list_is_not_a_failure_or_a_network_call():
    provider = GutBarrierEvidence(transport=httpx.MockTransport(lambda _: pytest.fail("not requested")))
    result = await provider.retrieve([])
    assert result["status"] == "not_requested" and result["partial"] is False


@pytest.mark.asyncio
async def test_result_count_and_query_count_are_bounded_and_duplicate_sources_merge_provenance():
    client = primary([outcome([record(index) for index in range(100)])])
    provider = GutBarrierEvidence(evimed_client=client, max_queries=2, per_query_results=3, max_results=2)
    result = await provider.retrieve([QUERY, QUERY.upper(), "obesity metabolism", "obesity interventions"])
    assert client.search_detailed.await_count == 2
    assert len(result["queries"]) == 3 and len(result["candidates"]) == 2
    assert result["queries"][-1]["status"] == "skipped_limit"
    assert all(len(row["queryIds"]) == 2 for row in result["candidates"])
    assert {issue["code"] for issue in result["issues"]} == {"EVIDENCE_QUERY_LIMIT_REACHED", "EVIDENCE_RESULT_LIMIT_REACHED"}


@pytest.mark.asyncio
async def test_metadata_validation_discards_untraceable_retracted_and_unrelated_results():
    rows = [public_row(id="bad", pmid=None, doi=None), public_row(isRetracted="Y"),
            public_row(title="Cancer pain", abstractText="Opioid conversion"), public_row(5)]
    provider = GutBarrierEvidence(transport=public_transport(rows))
    try:
        result = await provider.retrieve([QUERY])
    finally:
        await provider.aclose()
    assert [row["pmid"] for row in result["candidates"]] == ["1005"]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://127.0.0.1/article", "https://localhost/article",
    "https://user:secret@example.org/a", "https://example.org/a?token=secret", "https://example.org:bad/a"])
async def test_unsafe_primary_citation_url_is_not_reflected(url):
    client = primary([outcome([record(url=url, doi=None)])])
    provider = GutBarrierEvidence(evimed_client=client, transport=public_transport())
    try:
        result = await provider.retrieve([QUERY])
    finally:
        await provider.aclose()
    assert result["provider"] == "europe_pmc"
    assert "secret" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [[], {}, {"resultList": []}, {"resultList": {"result": {}}}])
async def test_malformed_public_response_is_partial_and_safe(body):
    provider = GutBarrierEvidence(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)))
    try:
        result = await provider.retrieve([QUERY])
    finally:
        await provider.aclose()
    assert result["partial"] is True and result["candidates"] == []
    assert result["issues"][-1]["code"] == "EVIDENCE_PUBLIC_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [httpx.Response(302, headers={"location": "https://private.invalid"}),
    httpx.Response(429), httpx.Response(200, content=b"x" * 101),
    httpx.Response(200, headers={"content-length": "999"}, content=b"{}")])
async def test_public_redirect_status_and_byte_limits_do_not_retry_unboundedly(monkeypatch, response):
    monkeypatch.setattr(evidence_module, "MAX_RESPONSE_BYTES", 100)
    calls = []
    def handle(request):
        calls.append(request)
        return response
    provider = GutBarrierEvidence(transport=httpx.MockTransport(handle))
    try:
        result = await provider.retrieve([QUERY])
    finally:
        await provider.aclose()
    assert len(calls) == 1 and result["partial"] is True
    assert result["candidates"] == []


@pytest.mark.asyncio
async def test_primary_timeout_uses_fallback_and_cancels_the_original_request():
    stopped = asyncio.Event()
    async def block(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    client = primary([])
    client.search_detailed.side_effect = block
    provider = GutBarrierEvidence(evimed_client=client, request_timeout_seconds=0.02, transport=public_transport())
    try:
        result = await provider.retrieve([QUERY])
    finally:
        await provider.aclose()
    assert stopped.is_set() and result["provider"] == "europe_pmc"
    assert result["issues"][0]["errorType"] == "TimeoutError"


@pytest.mark.asyncio
async def test_overall_deadline_includes_waiting_for_shared_capacity():
    active = peak = stopped = 0
    async def block(*args, **kwargs):
        nonlocal active, peak, stopped
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1
            stopped += 1
    client = primary([])
    client.search_detailed.side_effect = block
    provider = GutBarrierEvidence(evimed_client=client, concurrency=2, timeout_seconds=0.02,
        request_timeout_seconds=1, transport=httpx.MockTransport(lambda _: pytest.fail("no deadline for fallback")))
    results = await asyncio.gather(*(provider.retrieve([QUERY, "obesity metabolism", "obesity interventions"])
                                     for _ in range(3)))
    # Separate retrieve calls have slightly staggered deadlines. Cancellation
    # can free a slot while a later call still has time; every started request
    # must be joined, but the cumulative number started need not equal peak.
    assert active == 0 and peak == 2 and stopped >= peak
    assert all(row["partial"] and not row["candidates"] and len(row["queries"]) == 3 for row in results)
    assert all(issue["code"] == "EVIDENCE_TIMEOUT" for row in results for issue in row["issues"])


@pytest.mark.asyncio
async def test_external_cancellation_is_propagated_and_retrieval_children_are_joined():
    started, stopped = asyncio.Event(), asyncio.Event()
    async def block(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
    client = primary([])
    client.search_detailed.side_effect = block
    provider = GutBarrierEvidence(evimed_client=client)
    task = asyncio.create_task(provider.retrieve([QUERY]))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()


def test_adapter_import_does_not_load_deployment_config_or_initialize_report_package():
    result = subprocess.run([sys.executable, "-c", "import sys; import adapters.gut_barrier.evidence; "
        "assert 'config.env' not in sys.modules; assert 'adapters.report' not in sys.modules"],
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("kwargs", [{"concurrency": 0}, {"max_queries": 100}, {"per_query_results": True},
    {"max_results": 41}, {"timeout_seconds": float("nan")}, {"request_timeout_seconds": -1}])
def test_resource_limits_cannot_be_disabled(kwargs):
    with pytest.raises(ValueError):
        GutBarrierEvidence(**kwargs)


@pytest.mark.parametrize("query,title,summary", [
    (CARDIO_QUERY, "2025 NICE指南：肺炎的诊断和治疗",
     "This guideline covers pneumonia diagnosis and management. It highlights C-reactive protein and high sensitivity biomarkers."),
    (CARDIO_QUERY, "2024更新 —2016 NICE指南：结核（NG33）",
     "Prevention and management of tuberculosis in adults. Recommendations cover high-risk groups."),
    (CARDIO_QUERY, "Stereotactic radiosurgery for high Spetzler-Martin Grade arteriovenous malformations: practice guideline.", ""),
    (CARDIO_QUERY, "2025 NICE指南：超重和肥胖的管理",
     "There may be cost implications for meal replacement diets. The diets can be cost effective. Bariatric surgery improves cardiovascular outcomes."),
    (CARDIO_QUERY, "Sensitivity of C-reactive protein for chronic inflammation", "High protein levels were investigated."),
    (IBS_QUERY, "ACR Appropriateness Criteria Horner syndrome.",
     "This guideline provides imaging recommendations for Horner syndrome. It covers acute and chronic presentations."),
    (IBS_QUERY, "Inflammatory bowel diseases guideline", "Clinical manifestations include chronic diarrhea and abdominal pain."),
    (IBS_QUERY, "Cancer treatment guideline",
     "Guidance for cancer chemotherapy. Treatment complications require monitoring. Other conditions include irritable bowel syndrome."),
    (IBS_QUERY, "AGA guideline on fecal microbiota-based therapies for gastrointestinal disease",
     "Recommendations address recurrent CDI, inflammatory bowel disease and irritable bowel syndrome. Routine FMT for IBS is discouraged outside clinical trials."),
])
def test_wrong_disease_generic_words_and_incidental_late_mentions_are_not_candidates(query, title, summary):
    candidate = evidence_module._candidate(record(title=title, abstract=None, summary=summary),
        provider="evimed_v2", channel="guide", query=query, query_id="q-offline")
    assert candidate is None


@pytest.mark.parametrize("query,title,summary,channel,expected", [
    (CARDIO_QUERY, "中国心血管病一级预防指南", "", "guide", "cardiovascular"),
    (CARDIO_QUERY, "冠心病诊疗专家共识", "", "guide", "cardiovascular"),
    (CARDIO_QUERY, "Primary prevention clinical practice guideline",
     "This guideline focuses on atherosclerotic cardiovascular disease prevention.", "literature", "cardiovascular"),
    (CARDIO_QUERY, "Guideline on coronary heart disease prevention", "", "literature", "cardiovascular"),
    (IBS_QUERY, "中国肠易激综合征诊治专家共识意见", "", "guide", "irritable_bowel_syndrome"),
    (IBS_QUERY, "腹泻型肠易激综合征的临床评估共识", "", "literature", "irritable_bowel_syndrome"),
    (IBS_QUERY, "IBS-D diagnosis and management guideline", "", "literature", "irritable_bowel_syndrome"),
    (IBS_QUERY, "Chronic diarrhea evaluation guideline", "Fecal calprotectin testing supports the evaluation of chronic diarrhea.", "literature", "calprotectin"),
    (IBS_QUERY, "慢性腹泻诊断流程指南", "粪便钙卫蛋白用于鉴别诊断。", "guide", "calprotectin"),
    ("adult obesity metabolic fatty liver diabetes clinical practice guideline", "中国脂肪性肝病诊疗指南", "", "guide", "fatty_liver"),
    ("intestinal barrier chronic inflammation", "肠道屏障功能与慢性炎症", "", "literature", "intestinal_barrier"),
])
def test_strong_subjects_chinese_titles_and_clinical_core_are_supported(query, title, summary, channel, expected):
    candidate = evidence_module._candidate(record(title=title, abstract=None, summary=summary),
        provider="evimed_v2", channel=channel, query=query, query_id="q-offline")
    assert candidate is not None and expected in candidate["matchedTerms"]


def test_cardiovascular_guideline_query_does_not_treat_a_nonguideline_study_as_a_guideline():
    candidate = evidence_module._candidate(record(title="Cardiovascular outcomes in adults", study_types=["cohort"]),
        provider="europe_pmc", channel="literature", query=CARDIO_QUERY, query_id="q-offline")
    assert candidate is None


@pytest.mark.asyncio
@pytest.mark.parametrize("public_needed", [False, True])
@pytest.mark.parametrize("query", [CARDIO_QUERY, IBS_QUERY])
async def test_rejected_guides_cannot_fill_slots_or_block_literature_then_public_fallback(query, public_needed):
    if query == CARDIO_QUERY:
        bad_title = "2025 NICE指南：肺炎的诊断和治疗"
        bad_summary = "High sensitivity C-reactive protein for pneumonia monitoring."
        good_title = "Guideline on prevention of atherosclerotic cardiovascular disease"
    else:
        bad_title = "ACR Appropriateness Criteria Horner syndrome"
        bad_summary = "Imaging assessment of Horner syndrome."
        good_title = "Guideline on irritable bowel syndrome diagnosis"
    client = primary([
        outcome([record(index, title=bad_title, abstract=None, summary=bad_summary) for index in range(8)], "guide"),
        outcome([] if public_needed else [record(99, title=good_title, abstract=None)]),
    ])
    calls = []
    transport = public_transport([public_row(title=good_title)], calls=calls)
    provider = GutBarrierEvidence(evimed_client=client, transport=transport)
    try:
        result = await provider.retrieve([query])
    finally:
        await provider.aclose()
    assert len(calls) == int(public_needed)
    assert result["provider"] == ("europe_pmc" if public_needed else "evimed_v2")
    assert [row["title"] for row in result["candidates"]] == [good_title]
    assert all(row["candidateOnly"] for row in result["candidates"])
