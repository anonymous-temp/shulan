"""Candidate retrieval for the dated EXAM gastritis follow-up direction.

Only the generic clinical question leaves the assessment layer. A matching
guideline remains a candidate, not a new gastritis/HP/cancer diagnosis.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from adapters.gut_barrier.evidence import GutBarrierEvidence, _candidate, _query_topics


QUERY = "adult chronic non-atrophic gastritis evaluation clinical guideline"
GUIDELINE = "Guidelines for diagnosis and treatment of chronic gastritis in China (2022, Shanghai)"


def candidate(title, *, query=QUERY, **values):
    reasons = {}
    result = _candidate({"title": title, "pmid": "37245073", **values},
                        query=query, query_id="gastritis-offline", provider="evimed_v2",
                        channel="guide", exclusions=reasons)
    return result, reasons


@pytest.mark.parametrize("query", [QUERY, "成人 慢性胃炎 诊断 指南 共识"])
def test_chronic_gastritis_is_a_dedicated_coverage_topic(query):
    assert _query_topics(query) == ["gastritis"]


@pytest.mark.parametrize("title", [
    GUIDELINE, "中国慢性胃炎诊治指南（2022年，上海）",
    "Chinese consensus on chronic gastritis (2017, Shanghai)",
    "Chronic non-atrophic gastritis diagnostic clinical guideline",
])
def test_comprehensive_gastritis_guidance_covers_nonatrophic_followup_without_diagnosing(title):
    result, reasons = candidate(title)
    assert result is not None and result["candidateOnly"] is True
    assert result["matchedTerms"] == ["gastritis"]
    assert not ({"diagnosis", "risk", "score", "recommendation"} & result.keys())
    assert reasons == {}


@pytest.mark.parametrize("title,reason", [
    ("Gastric cancer diagnosis and treatment guideline", "disease_not_title_subject"),
    ("Helicobacter pylori treatment guideline", "disease_not_title_subject"),
    ("Guideline on chronic atrophic gastritis", "gastritis_subtype_not_requested"),
    ("中国慢性萎缩性胃炎诊治指南", "gastritis_subtype_not_requested"),
    ("Autoimmune gastritis clinical guideline", "gastritis_subtype_not_requested"),
    ("Kyoto global consensus report on Helicobacter pylori gastritis", "gastritis_subtype_not_requested"),
    ("中国慢性胃炎诊治指南解读", "secondary_guideline_publication"),
    ("Drug therapy for chronic gastritis guideline", "clinical_question_mismatch"),
])
def test_other_diseases_subtypes_and_treatment_only_topics_do_not_fill_followup_coverage(title, reason):
    result, reasons = candidate(title, summary="Chronic gastritis can be discussed in related disease guidance.")
    assert result is None
    assert reasons == {reason: 1}


def test_broad_guideline_can_discuss_atrophy_and_hp_without_becoming_restricted():
    result, reasons = candidate(GUIDELINE,
        summary="The recommendations address chronic gastritis evaluation and classification. "
                "Atrophy, Helicobacter pylori and non-atrophic gastritis are discussed as different clinical contexts.")
    assert result is not None and reasons == {}


@pytest.mark.asyncio
async def test_general_chinese_gastritis_guideline_recovers_after_unsuitable_primary_hit():
    def outcome(title):
        return SimpleNamespace(channel="guide", status="success", records=[{"title": title, "pmid": "37245073"}])

    client = SimpleNamespace(enabled=True, search_detailed=AsyncMock(side_effect=[
        [outcome("Gastric cancer clinical guideline")], [outcome(GUIDELINE)],
    ]))
    adapter = GutBarrierEvidence(evimed_client=client,
        transport=httpx.MockTransport(lambda _: pytest.fail("expanded primary query recovers")))
    try:
        result = await adapter.retrieve([QUERY])
    finally:
        await adapter.aclose()
    assert [call.args[0] for call in client.search_detailed.await_args_list] == [
        QUERY, "成人 慢性胃炎 诊断 指南 共识",
    ]
    audit = result["queries"][0]
    assert audit["requestedTopics"] == audit["coveredTopics"] == ["gastritis"]
    assert audit["attempts"][0]["channels"][0]["exclusions"] == {"disease_not_title_subject": 1}
    assert result["partial"] is False
    assert all(row["candidateOnly"] for row in result["candidates"])


@pytest.mark.asyncio
async def test_public_gastritis_fallback_uses_generic_scope_and_original_screen():
    calls = []

    def public(request):
        calls.append(request.url.params["query"])
        return httpx.Response(200, json={"resultList": {"result": [{
            "source": "MED", "id": "37245073", "title": GUIDELINE,
        }]}})

    adapter = GutBarrierEvidence(transport=httpx.MockTransport(public))
    try:
        result = await adapter.retrieve([QUERY])
    finally:
        await adapter.aclose()
    assert calls == ["chronic gastritis adults clinical guideline"]
    assert result["queries"][0]["coveredTopics"] == ["gastritis"]
    assert result["candidates"][0]["candidateOnly"] is True
