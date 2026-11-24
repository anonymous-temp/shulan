from datetime import date
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pymupdf

from adapters.gut_barrier.sources import GutBarrierSources, extract_observations, extract_json_observations
from modules.gut_barrier.schemas import GutBarrierRequest


def test_hs_crp_is_not_plain_crp_and_references_are_not_values():
    text = "CRP 4 mg/L\nhs-CRP 4.2 mg/L 参考0-3\nD-Lactate参考值0-14 mg/L\nDAO 13 U/L"
    observations, issues = extract_observations(text, source_id="s")
    assert [(row["code"], row["value"]) for row in observations] == [("DAO", "13")]
    assert issues[0]["code"] == "AMBIGUOUS_MARKER_ROW"
    assert all(row["isCurrent"] is False for row in observations)


def test_source_name_does_not_filter_results_or_change_sampling_date():
    text = "姓名：李明\n采样日期：2026-09-23\nDAO 13 U/L"
    observations, issues = extract_observations(text, source_id="s", today="2026-09-23")
    assert len(observations) == 1 and issues == []
    assert observations[0]["source"]["identityVerified"] is False
    assert observations[0]["date"] == "2026-09-23" and observations[0]["isCurrent"] is True


@pytest.mark.asyncio
async def test_all_detail_summary_and_file_sources_are_read_and_deduplicated():
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((50, 50), "hs-CRP 4.2 mg/L\nDAO 13 U/L")
        pdf = document.tobytes()
    downloader = SimpleNamespace(download=AsyncMock(return_value=pdf))
    sources = GutBarrierSources(downloader=downloader)
    request = GutBarrierRequest.model_validate({"userId": "u", "sessionId": "s", "fileList": [
        {"url": "https://example.org/upload.pdf", "fileType": "pdf"}], "testData": [
        {"resultDetailUrl": "https://example.org/detail.pdf", "resultSummaryUrl": "https://example.org/summary.pdf"},
        {"resultDetailUrl": "https://example.org/detail.pdf"}]})
    collected = await sources.collect(request)
    assert downloader.download.await_count == 3 and len(collected["sources"]) == 3
    assert all(row["status"] == "parsed" for row in collected["sources"])
    assert {row["code"] for row in collected["observations"]} == {"DAO"}
    assert "https://" not in str(collected)


@pytest.mark.asyncio
async def test_unavailable_and_image_without_ocr_are_explicit_not_normal():
    downloader = SimpleNamespace(download=AsyncMock(side_effect=[ConnectionError("secret URL"), b"\xff\xd8\xffpayload"]))
    source = GutBarrierSources(downloader=downloader)
    request = GutBarrierRequest.model_validate({"userId": "u", "sessionId": "s", "fileList": [
        {"url": "https://example.org/a.pdf", "fileType": "pdf"},
        {"url": "https://example.org/b.jpg", "fileType": "image"}]})
    collected = await source.collect(request)
    assert collected["observations"] == []
    assert {item["code"] for item in collected["issues"]} == {"SOURCE_UNAVAILABLE", "OCR_UNAVAILABLE"}
    assert "secret" not in str(collected)


def make_request(**extra):
    return GutBarrierRequest.model_validate({"userId": "u", "sessionId": "s", "name": "李明", **extra})


def microbiome_data():
    return {"diseaseRisk": {"便秘": "55.56", "炎症性肠病": "43.57", "ASD": "74.17", "age": "1", "conclusion": "metagenome_sample;human_gut_sample"},
            "microbeData": {"comprehensive": [{"latinName": "Enterotype", "value": "Bacteroides"},
                                             {"latinName": "GMWI2", "value": "-0.04"},
                                             {"latinName": "Species_Num", "value": "1484"},
                                             {"latinName": "Diversity_Index", "value": "3.4818"}],
                            "Summary_spp": [{"latinName": "Probiotics_val", "value": "6.5442"}]}}


@pytest.mark.asyncio
async def test_actual_shape_json_and_chinese_summary_are_auxiliary_not_ocr_or_prescriptions():
    payload = microbiome_data()
    summary_text = "1.肠菌健康指数为-0.04，低于58%人群。II度失调，菌种数1484，多样性指数3.6909。建议移植菌群。"
    downloader = SimpleNamespace(download=AsyncMock(side_effect=[json.dumps(payload, ensure_ascii=False).encode(), summary_text.encode()]))
    parser = SimpleNamespace(submit=AsyncMock(side_effect=AssertionError("do not use OCR")))
    source = GutBarrierSources(downloader=downloader, parsing_client=parser)
    request = make_request(testData=[{"resultDetailUrl": "https://example.org/detail", "resultSummaryUrl": "https://example.org/summary"}])
    result = await source.collect(request)
    assert result["observations"] == []
    assert [row["format"] for row in result["sources"]] == ["json", "text"]
    detail, summary = result["context"]["microbiomeSources"]
    assert detail["rawData"] == payload
    assert detail["status"] == summary["status"] == "source_only"
    assert detail["identityVerified"] is False and detail["sampleDate"] is None
    assert detail["enterotype"] == {"raw": "Bacteroides", "value": "Bacteroides", "status": "source_only", "eligibleForMatching": False}
    assert [row["name"] for row in detail["sourceModelIndices"]] == ["便秘", "炎症性肠病", "ASD"]
    assert all(row["interpretation"] == "source_model_index_not_probability" for row in detail["sourceModelIndices"])
    assert detail["modelMetadata"]["age"] == "1" and "age" not in result["context"]
    assert summary["summaryText"] == summary_text
    assert not any(key in result["context"] for key in ("treatment", "prescription", "enterotype"))
    conflict = next(issue for issue in result["issues"] if issue["code"] == "AUXILIARY_METRIC_CONFLICT")
    assert conflict["metric"] == "Diversity_Index" and conflict["values"] == ["3.4818", "3.6909"]


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [b'{"error":"unauthorized"}', b'DAO 13 U/L'])
async def test_filelist_does_not_accept_json_or_text_as_declared_pdf(content):
    downloader = SimpleNamespace(download=AsyncMock(return_value=content))
    result = await GutBarrierSources(downloader=downloader).collect(make_request(fileList=[{"url": "https://example.org/file.pdf", "fileType": "pdf"}]))
    assert result["observations"] == []
    assert result["issues"][0]["code"] == "SOURCE_UNAVAILABLE"
    assert result["sources"][0]["status"] == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [b'{"error":"unauthorized"}', b'<html>please login</html>', b'{bad json}', b'{"value":NaN}', b'\x00\xff'])
async def test_error_and_malformed_sources_do_not_become_clinical_data(content):
    downloader = SimpleNamespace(download=AsyncMock(return_value=content))
    result = await GutBarrierSources(downloader=downloader).collect(make_request(testData=[{"resultDetailUrl": "https://example.org/file"}]))
    assert result["observations"] == [] and result["context"] == {}
    assert result["issues"]


def test_nested_laboratory_json_keeps_source_path_sample_date_and_identity():
    value = {"patientName": "李明", "sampleDate": "2026-09-23", "results": [
        {"code": "DAO", "value": 13, "unit": "U/L", "referenceRange": "0-8.5"},
        {"name": "hs-CRP", "value": "4.2", "unit": "mg/L"},
        {"name": "CRP", "value": 99, "unit": "mg/L"},
        {"name": "D-Lactate", "referenceRange": "0-14", "unit": "mg/L"}]}
    observations, issues = extract_json_observations(value, source_id="s", today="2026-09-23")
    assert [(row["code"], row["value"]) for row in observations] == [("DAO", 13)]
    assert all(row["isCurrent"] is True for row in observations)
    assert observations[0]["source"]["path"] == "$.results[0]"
    assert issues[0]["code"] == "AMBIGUOUS_MARKER_ROW"


@pytest.mark.parametrize("source_name", ["李明", "王明", None])
def test_json_currentness_depends_on_date_not_name(source_name):
    value = {"patientName": source_name, "date": "2026-09-23", "name": "DAO", "value": 13, "unit": "U/L"}
    observations, issues = extract_json_observations(value, source_id="s", today="2026-09-23")
    assert observations[0]["isCurrent"] is True
    assert observations[0]["source"]["identityVerified"] is False
    assert issues == []


def test_source_name_does_not_exclude_microbiome_json():
    value = {**microbiome_data(), "patientName": "王明"}
    result = GutBarrierSources._structured_or_text(json.dumps(value).encode(), summary={"sourceId": "s"})
    assert result["observations"] == [] and len(result["auxiliarySources"]) == 1
    assert result["auxiliarySources"][0]["rawData"] == value
    assert result["auxiliarySources"][0]["identityVerified"] is False
    assert result["summary"]["status"] == "parsed"
    assert result["issues"] == []


def test_nested_json_names_do_not_filter_marker_values_or_assert_identity_verification():
    value = {"patientName": "来源甲", "sampleDate": "2026-09-23", "results": [
        {"subjectName": "来源乙", "name": "DAO", "value": "24.9", "unit": "U/L"},
        {"patient": {"name": "来源丙"}, "name": "hs-CRP", "value": "1.02", "unit": "mg/L"},
    ]}
    observations, issues = extract_json_observations(value, source_id="s", today="2026-09-23")
    assert [(row["code"], row["value"]) for row in observations] == [("DAO", "24.9")]
    assert all(row["isCurrent"] is True and row["source"]["identityVerified"] is False for row in observations)
    assert issues == []


@pytest.mark.parametrize("row", ["DAO 13 0–10 U/L", "DAO 13 LPS 24 U/L", "hs-CRP 1-3 mg/L", "DAO 参考范围 0-8.5 U/L", "hs-CRP/PA 0.2 mg/L"])
def test_ambiguous_text_does_not_take_reference_or_another_marker_value(row):
    observations, _ = extract_observations(row, source_id="s")
    assert not any(item["code"] in ("DAO", "HS_CRP") for item in observations)


@pytest.mark.parametrize("row,expected", [("DAO -5 U/L", "-5"), ("D-Lactate 1.9e1 mg/L", "1.9e1"), ("D-Lactate .42 mg/L", ".42"), ("D-乳酸（D-Lactate） 19 mg/L", "19")])
def test_text_preserves_numeric_sign_and_scientific_notation(row, expected):
    observations, _ = extract_observations(row, source_id="s")
    assert observations[0]["value"] == expected


def test_sample_date_wins_over_today_report_date():
    text = "姓名：李明\n报告日期：2026-09-23\n采样日期：2025-03-02\nDAO 13 U/L"
    observations, _ = extract_observations(text, source_id="s", today="2026-09-23")
    assert observations[0]["date"] == "2025-03-02" and observations[0]["isCurrent"] is False


def test_report_date_alone_and_no_identity_do_not_make_current():
    observations, _ = extract_observations("报告日期：2026-09-23\nDAO 13 U/L", source_id="s", today="2026-09-23")
    assert observations[0]["date"] is None and observations[0]["isCurrent"] is False


def test_different_name_on_later_page_does_not_discard_observations():
    observations, issues = extract_observations("姓名：王明\nDAO 13 U/L", header="姓名：李明\n采样日期：2026-09-23", source_id="s", today="2026-09-23")
    assert len(observations) == 1 and observations[0]["value"] == "13"
    assert observations[0]["isCurrent"] is True
    assert observations[0]["source"]["identityVerified"] is False
    assert issues == []


@pytest.mark.asyncio
@pytest.mark.parametrize("is_json", [False, True])
async def test_retries_use_frozen_evaluation_date_not_wall_clock_today(is_json):
    data = {"patientName": "李明", "sampleDate": "2020-01-02", "name": "DAO", "value": 13, "unit": "U/L"}
    content = json.dumps(data).encode() if is_json else "姓名：李明\n采样日期：2020-01-02\nDAO 13 U/L".encode()
    source = GutBarrierSources(downloader=SimpleNamespace(download=AsyncMock(return_value=content)))
    result = await source.collect(make_request(testData=[{"resultDetailUrl": "https://example.org/report"}]), evaluation_date="2020-01-02")
    assert result["observations"][0]["isCurrent"] is True
    assert result["observations"][0]["date"] == "2020-01-02"
def test_remote_json_preserves_large_integer_exactly_and_rejects_overflow_float():
    from adapters.gut_barrier.sources import _bounded_json
    import pytest
    huge = 10 ** 30
    assert _bounded_json(('{"value":' + str(huge) + '}').encode())["value"] == str(huge)
    with pytest.raises(ValueError):
        _bounded_json(b'{"value":1e999}')
