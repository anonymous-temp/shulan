"""Precision agreement never grants source verification or treatment authority."""
from copy import deepcopy
from itertools import permutations
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.gut_barrier.sources import GutBarrierSources, _auxiliary_metric_values_agree
from modules.gut_barrier.interventions import build_interventions
from modules.gut_barrier.source_scope import source_scope_block
from tests.unit.gut_barrier.test_interventions import _assessment, _catalog
from tests.unit.gut_barrier.test_sources import make_request, microbiome_data


@pytest.mark.parametrize("code,values", [
    ("GMWI2", ["-0.0400053346136992", "-0.04"]),
    ("GMWI2", ["0.0400053346136992", "0.04"]),
    ("GMWI2", ["-0.046", "-0.05"]),
    ("GMWI2", ["0.046", "0.05"]),
    ("Diversity_Index", ["3.4818", "3.48"]),
    ("Probiotics_val", ["6.5442", "6.54"]),
    ("GMWI2", ["-4.00053346136992e-2", "-0.04"]),
    ("Species_Num", ["1484", "1484.0"]),
    ("GMWI2", ["0", "0.00", "-0.00"]),
])
def test_rounding_or_exact_numeric_identity_is_order_independent(code, values):
    before = deepcopy(values)
    for ordered in permutations(values):
        assert _auxiliary_metric_values_agree(code, ordered)
    assert values == before


@pytest.mark.parametrize("code,values", [
    ("Diversity_Index", ["3.4818", "3.6909"]),
    ("GMWI2", ["-0.0400053346136992", "-0.05"]),
    ("GMWI2", ["0.041", "0.042"]),
    ("GMWI2", ["-0.041", "-0.042"]),
    ("GMWI2", ["0.045", "0.04"]),
    ("GMWI2", ["0.045", "0.05"]),
    ("GMWI2", ["-0.045", "-0.04"]),
    ("GMWI2", ["-0.045", "-0.05"]),
    ("GMWI2", ["0.000001", "0.00"]),
    ("GMWI2", ["-0.000001", "0.00"]),
    ("GMWI2", ["0.004", "-0.00"]),
    ("GMWI2", ["0.041", "-0.04"]),
    ("GMWI2", ["0.041", "0.0"]),
    ("GMWI2", ["1.004", "1"]),
    ("Species_Num", ["1484", "1485"]),
    ("Species_Num", ["1484.004", "1484.00"]),
    ("Unknown_Metric", ["3.4818", "3.48"]),
    ("GMWI2", ["0.041", "0.04", "0.044"]),
    ("GMWI2", ["-0.041", "-0.04", "-0.044"]),
    ("GMWI2", ["0.040001", "0.04", "0.040002"]),
])
def test_real_ambiguous_or_coarse_bridged_differences_remain_conflicts(code, values):
    for ordered in permutations(values):
        assert not _auxiliary_metric_values_agree(code, ordered)


@pytest.mark.parametrize("invalid", [
    "NaN", "Infinity", "-Infinity", "true", ">0.04", "0.04 units",
    "1e999", "1e1000", "1000000000001", "1e-25", "0." + "0" * 81 + "4",
])
def test_malformed_or_out_of_bound_values_do_not_gain_rounding_agreement(invalid):
    assert not _auxiliary_metric_values_agree("GMWI2", [invalid, "0.04"])


@pytest.mark.asyncio
@pytest.mark.parametrize("inline", [False, True])
async def test_collection_retains_original_precision_and_real_conflict_without_identity_promotion(inline):
    payload = microbiome_data()
    payload["microbeData"]["comprehensive"][1]["value"] = "-0.0400053346136992"
    summary_text = "肠菌健康指数为-0.04，低于58%人群。II度失调，菌种数1484，多样性指数3.6909。建议移植菌群。"
    contents = [summary_text.encode()] if inline else [
        json.dumps(payload, ensure_ascii=False).encode(), summary_text.encode()]
    downloader = SimpleNamespace(download=AsyncMock(side_effect=contents))
    parser = SimpleNamespace(submit=AsyncMock(side_effect=AssertionError("no OCR for structured sources")))
    source = GutBarrierSources(downloader=downloader, parsing_client=parser)
    request = make_request(
        microbiomeData=payload if inline else None,
        testData=[{"resultSummaryUrl": "https://example.org/summary"} if inline else {
            "resultDetailUrl": "https://example.org/detail", "resultSummaryUrl": "https://example.org/summary"}],
    )
    collected = await source.collect(request)
    rows = collected["context"]["microbiomeSources"]
    detail = next(row for row in rows if row["format"] == "json")
    summary = next(row for row in rows if row["format"] == "text")
    assert detail["rawData"] == payload
    assert summary["summaryText"] == summary_text
    assert [metric["value"] for metric in detail["metrics"] if metric["code"] == "GMWI2"] == ["-0.0400053346136992"]
    assert [metric["value"] for metric in summary["metrics"] if metric["code"] == "GMWI2"] == ["-0.04"]
    conflicts = [issue for issue in collected["issues"] if issue["code"] == "AUXILIARY_METRIC_CONFLICT"]
    assert len(conflicts) == 1 and conflicts[0]["metric"] == "Diversity_Index"
    assert set(conflicts[0]["values"]) == {"3.4818", "3.6909"}
    assert len(conflicts[0]["sourceIds"]) == 2
    for row in rows:
        assert row["identityVerified"] is False and row["sampleDate"] is None
        assert row["status"] == "source_only"
        assert "conflicting_source_metric:GMWI2" not in row["warnings"]
        assert "conflicting_source_metric:Diversity_Index" in row["warnings"]
    assert detail["enterotype"]["eligibleForMatching"] is False
    assert collected["observations"] == []
    assert set(collected["context"]) == {"microbiomeSources"}
    parser.submit.assert_not_awaited()
    assert downloader.download.await_count == (1 if inline else 2)


@pytest.mark.asyncio
async def test_three_collected_sources_cannot_use_coarse_summary_to_hide_precise_conflict():
    def detail_payload(value):
        return {"microbeData": {"comprehensive": [{"latinName": "GMWI2", "value": value}]}}

    first, second = detail_payload("0.041"), detail_payload("0.044")
    downloader = SimpleNamespace(download=AsyncMock(side_effect=[
        json.dumps(first).encode(), "肠菌健康指数为0.04。".encode(), json.dumps(second).encode()]))
    request = make_request(testData=[
        {"resultDetailUrl": "https://example.org/first", "resultSummaryUrl": "https://example.org/summary"},
        {"resultDetailUrl": "https://example.org/second"},
    ])
    result = await GutBarrierSources(downloader=downloader).collect(request)
    conflicts = [issue for issue in result["issues"] if issue["code"] == "AUXILIARY_METRIC_CONFLICT"]
    assert len(conflicts) == 1 and conflicts[0]["metric"] == "GMWI2"
    assert set(conflicts[0]["values"]) == {"0.041", "0.04", "0.044"}
    assert len(conflicts[0]["sourceIds"]) == 3
    assert all("conflicting_source_metric:GMWI2" in row["warnings"]
               for row in result["context"]["microbiomeSources"])


@pytest.mark.parametrize("issues,sources", [
    ([{"code": "SOURCE_UNAVAILABLE", "errorType": "private-source-detail"}], []),
    ([], [{"status": "parsed", "failedPages": [1]}]),
])
def test_partial_source_wording_does_not_claim_independent_lab_verification(issues, sources):
    block = source_scope_block(issues, sources)
    assert set(block) == {"kind", "title", "text"}
    assert "本次提供且可用的资料" in block["text"]
    assert "不代表已完成原始检验单的独立核验" in block["text"]
    assert "已核实的可用结果" not in block["text"]
    assert "private-source-detail" not in block["text"]


@pytest.mark.parametrize("options,expected_status", [
    ({"pattern": "---", "grade": "normal"}, "not_indicated"),
    ({}, "no_guideline_indication_identified"),
    ({"context": {"immuneStatus": "重度免疫抑制"}}, "safety_hold_severe_immunocompromise"),
    ({"context": {"conditions": ["肠易激综合征"]}}, "clinical_trial_only"),
    ({"context": {"conditions": ["复发性艰难梭菌感染"], "enterotype": "ET-B", "immuneStatus": "正常"}}, "specialist_review_only"),
])
def test_fmt_current_label_keeps_protocol_keys_original_product_names_and_safety(options, expected_status):
    catalog = _catalog()
    before = deepcopy(catalog)
    result = build_interventions(_assessment(**options), product_catalog=catalog)
    assert set(result["microbiome"]) == {"mdf", "pfmt", "sevenPatternPlans"}
    fmt = result["microbiome"]["pfmt"]
    assert set(fmt) == {
        "name", "status", "started", "reason", "condition", "clinicalEligibility", "catalogMatch",
        "product", "catalogUsage", "actions", "monitoring", "suppressedReasons", "references",
    }
    assert fmt["name"] == "肠道菌群移植（FMT）"
    assert fmt["status"] == expected_status
    assert fmt["started"] is False and fmt["catalogUsage"] is None
    assert "PFMT" not in fmt["reason"]
    assert all("PFMT" not in text for text in result["reviewItems"])
    if expected_status == "specialist_review_only":
        assert fmt["product"]["code"] == "T-ACTIVE"
        assert fmt["product"]["name"] == "女-ET-B-PFMT"
        assert fmt["clinicalEligibility"]["status"] == "possible_guideline_context_needs_confirmation"
    else:
        assert fmt["product"] is None
    assert catalog == before


def test_current_strategy_and_empty_catalog_messages_use_fmt_without_changing_codes():
    from modules.gut_barrier.interventions import _CATALOG_ISSUES

    assert "pfmt_catalog_empty" in _CATALOG_ISSUES and "pfmt_catalog_rows_invalid" in _CATALOG_ISSUES
    assert "FMT" in _CATALOG_ISSUES["pfmt_catalog_empty"]
    assert "PFMT" not in _CATALOG_ISSUES["pfmt_catalog_rows_invalid"]
    result = build_interventions(_assessment())
    plan = next(item for item in result["microbiome"]["sevenPatternPlans"] if item["pattern"] == "+++")
    strategy = "".join(plan["patientStrategy"])
    assert "FMT" in strategy and "PFMT" not in strategy
