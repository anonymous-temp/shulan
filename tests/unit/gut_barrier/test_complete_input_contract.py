"""Complete input contract using synthetic identities and offline source adapters.

The uploaded example's structure and clinical values are retained; signed URLs,
contact details and routing identifiers are deliberately not copied into tests.
"""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from adapters.gut_barrier.sources import GutBarrierSources
from modules.gut_barrier.pipeline import GutBarrierPipeline
from modules.gut_barrier.publication import project_callback_report
from modules.gut_barrier.schemas import GutBarrierRequest
from tests.unit.gut_barrier.final_input_fixture import final_input
from tests.unit.gut_barrier.test_service import environment


EVALUATION_DATE = "2026-09-28"
_URL_ROOT = "https://complete-input.invalid/"


def complete_input(*, matching_survey=False, inline=False):
    payload = final_input()
    payload.update(userId="complete-contract-user", sessionId="complete-contract-session",
                   name="登记受检者", age="28", sex="男")
    payload["healthSurvey"].update(name="问卷受检者", fromIp="redacted-test-ip",
                                   idCard="redacted-test-id-card")
    if matching_survey:
        payload.update(name=payload["healthSurvey"]["name"], age="26", sex="女")
    payload["fileList"] = [
        {"url": _URL_ROOT + "uploaded.pdf", "fileType": "pdf"},
        {"url": _URL_ROOT + "uploaded.jpg", "fileType": "image"},
    ]
    payload["testData"] = [{"resultDetailUrl": _URL_ROOT + "detail.json",
                            "resultSummaryUrl": _URL_ROOT + "summary.json"}]
    exams = [
        ("2016-08-12 15:02:17", "CT胸部平扫", "两肺CT平扫未见明显异常。"),
        ("2025-12-18 08:12:37", "无痛胃镜检查(胃)", "慢性非萎缩性胃炎"),
        ("2025-12-18 08:16:50", "无痛肠镜检查(肠)", "所见结直肠粘膜大致正常"),
        ("2025-12-15 08:48:14", "常规心电图([心脏])", "窦性心律 正常范围心电图"),
    ]
    inspections = [
        ("2025-12-15 09:31:08", "低密度脂蛋白(LDL-c)", "3.70", "0.00-3.37", 1, "偏高"),
        ("2025-12-15 09:31:08", "总胆固醇(TC)", "6.48", "3.14-5.86", 1, "偏高"),
        ("2025-12-15 09:31:08", "AST/ALT", "0.9", "1.0-2.0", 2, "偏低"),
        ("2025-12-15 08:57:07", "红细胞分布宽度", "12.0", "12.2-14.8", 2, "偏低"),
        ("2025-10-29 12:44:25", "总胆固醇(TC)", "6.09", "3.14-5.86", 1, "偏高"),
        ("2025-10-29 12:44:25", "低密度脂蛋白(LDL-c)", "3.44", "0.00-3.37", 1, "偏高"),
        ("2025-10-29 12:44:25", "天门冬氨基转移酶(AST)", "13", "15-40", 2, "偏低"),
    ]
    payload["hospitalData"] = [
        {"checkTime": date, "reportTime": date, "dataType": "EXAM", "itemName": name, "examResult": result}
        for date, name, result in exams
    ] + [
        {"checkTime": date, "reportTime": date, "dataType": "INSPECTION", "itemName": name,
         "testResultValue": result, "referenceRange": reference,
         "resultStatus": status, "resultStatusName": status_name}
        for date, name, result, reference, status, status_name in inspections
    ]
    payload["microbiomeData"] = _microbiome(payload["name"]) if inline else None
    return payload


def _microbiome(name):
    return {"patientName": name, "sampleDate": EVALUATION_DATE,
            "microbeData": {"comprehensive": [{"latinName": "GMWI2", "value": 0}]},
            "reviewed": False}


def offline_sources(name):
    data = json.dumps(_microbiome(name), ensure_ascii=False).encode()
    documents = {_URL_ROOT + "uploaded.pdf": b"%PDF-offline-fixture",
                 _URL_ROOT + "uploaded.jpg": b"\xff\xd8\xffoffline-fixture",
                 _URL_ROOT + "detail.json": data, _URL_ROOT + "summary.json": data}
    downloader = SimpleNamespace(download=AsyncMock(side_effect=lambda url: documents[url]))
    source = GutBarrierSources(downloader=downloader, parsing_client=object())
    text = f"姓名：{name}\n采样日期：{EVALUATION_DATE}\nDAO 3.8 U/L"
    source._pdf_pages = Mock(return_value=[text])
    source._parse_page = Mock(return_value={"success": True, "text": text})
    return source, downloader


def assert_public_contract(public):
    assert set(public) == {"schemaVersion", "meta", "sections"}
    assert public["schemaVersion"] == "gut-barrier-public.v2"
    assert set(public["meta"]) == {"title", "reportId"}
    assert [section["id"] for section in public["sections"]] == ["overview", "disease_risks", "appendix"]
    item_fields = {"facts": {"label", "value"}, "cards": {"label", "value", "detail"},
                   "bands": {"range", "label", "selected"},
                   "references": {"citation", "url", "year", "publisher", "doi", "location"}}
    for section in public["sections"]:
        assert set(section) == {"id", "title", "subtitle", "visible", "blocks"}
        assert isinstance(section["visible"], bool) and isinstance(section["blocks"], list)
        for block in section["blocks"]:
            kind = block["kind"]
            fields = ({"columns", "rows"} if kind == "table" else {"items"} if kind in
                      {"facts", "cards", "bands", "references", "list"} else
                      {"text", "tone"} if kind == "callout" else {"text"})
            assert set(block) == {"kind", "title"} | fields
            if kind in item_fields:
                assert isinstance(block["items"], list)
                for item in block["items"]:
                    assert set(item) == item_fields[kind]
            if kind == "table":
                assert isinstance(block["columns"], list) and isinstance(block["rows"], list)
                for row in block["rows"]:
                    if isinstance(row, dict):
                        assert set(row) == {"cells", "current"} and isinstance(row["current"], bool)
                    assert len(row["cells"] if isinstance(row, dict) else row) == len(block["columns"])


@pytest.mark.asyncio
@pytest.mark.parametrize("inline", [False, True])
async def test_all_sources_and_survey_answers_contribute_without_identity_filtering(inline):
    payload = complete_input(inline=inline)
    before = deepcopy(payload)
    request = GutBarrierRequest.model_validate(payload)
    source, downloader = offline_sources(request.name)
    report, audit = await GutBarrierPipeline(sources=source).generate(
        request, report_id="complete-contract-report", evaluation_date=EVALUATION_DATE,
    )
    assert payload == before
    assert downloader.download.await_count == 4
    assert {call.args[0] for call in downloader.download.await_args_list} == {
        _URL_ROOT + suffix for suffix in ("uploaded.pdf", "uploaded.jpg", "detail.json", "summary.json")}
    assert len(audit["sources"]) == 4 + int(inline)
    assert all(item["status"] == "parsed" for item in audit["sources"])
    source._parse_page.assert_called_once()
    assessment = report["assessment"]
    assert assessment["subject"]["name"] == "登记受检者"
    assert assessment["subject"]["age"] == 28 and assessment["subject"]["sex"] == "男"
    assert not any("IDENTITY_CONFLICT" in item["code"] for item in audit["issues"])
    assert "bmi" not in assessment["context"]
    assert set(assessment["context"]["symptoms"]) == {"消化不良", "时常腹泻"}
    assert assessment["context"]["healthSurvey"]["age"] == 26
    assert assessment["context"]["healthSurvey"]["gender"] == "女"
    assert "obesity" not in {row["code"] for row in assessment["diseaseRisks"]}
    assert assessment["barrier"]["score"] == 0 and assessment["barrier"]["pattern"] == "---"
    assert "inflammation" not in assessment
    assert "hsCRP" not in assessment["reportReview"]
    assert audit["partial"] is True
    assert len(report["microbiomeSources"]) == 2 + int(inline)
    for auxiliary in report["microbiomeSources"]:
        assert auxiliary["identityVerified"] is False
        assert auxiliary["metrics"][0]["value"] == 0
        assert "rawData" not in auxiliary
    if inline:
        dumped = request.model_dump(mode="json", by_alias=True)
        assert dumped["microbiomeData"]["reviewed"] is False
        assert dumped["microbiomeData"]["microbeData"]["comprehensive"][0]["value"] == 0
    findings = assessment["context"]["clinicalFindings"]
    assert len(findings) == 11 and all(item["temporalStatus"] == "historical" for item in findings)
    assert {(item["date"], item["value"]) for item in findings if item["code"] == "LDL_C"} == {
        ("2025-10-29", "3.44"), ("2025-12-15", "3.70")}
    assert {(item["date"], item["value"]) for item in findings if item["code"] == "TOTAL_CHOLESTEROL"} == {
        ("2025-10-29", "6.09"), ("2025-12-15", "6.48")}
    assert all(item["unit"] is None for item in findings)
    public = project_callback_report(report)
    assert_public_contract(public)
    encoded = json.dumps(public, ensure_ascii=False)
    for private in ("redacted-test-ip", "redacted-test-id-card", "fromIp", "idCard", "问卷受检者", _URL_ROOT):
        assert private not in encoded
    cards = [item for section in public["sections"] for block in section["blocks"]
             if block["kind"] == "cards" for item in block["items"]]
    assert any(item["value"] == 0 and not isinstance(item["value"], bool) for item in cards)
    bands = [item for section in public["sections"] for block in section["blocks"]
             if block["kind"] == "bands" for item in block["items"]]
    assert any(item["selected"] is False for item in bands)


@pytest.mark.asyncio
async def test_matching_survey_contributes_self_report_without_overriding_measurements():
    payload = complete_input(matching_survey=True)
    source, _ = offline_sources(payload["name"])
    report, audit = await GutBarrierPipeline(sources=source).generate(
        GutBarrierRequest.model_validate(payload), report_id="matched-contract-report", evaluation_date=EVALUATION_DATE,
    )
    assessment = report["assessment"]
    assert not any(item["code"] == "HEALTH_SURVEY_IDENTITY_CONFLICT" for item in audit["issues"])
    assert "bmi" not in assessment["context"]
    assert set(assessment["context"]["symptoms"]) == {"消化不良", "时常腹泻"}
    assert "obesity" not in {row["code"] for row in assessment["diseaseRisks"]}
    assert assessment["context"]["allergies"] is None and assessment["context"]["medications"] is None
    assert assessment["barrier"]["score"] == 0 and "inflammation" not in assessment
    assert_public_contract(project_callback_report(report))


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_style", ["null", "empty", "omitted"])
async def test_all_optional_sources_may_be_missing_without_dropping_callback_fields(empty_style):
    payload = {"userId": "empty-contract-user", "sessionId": "empty-contract-session"}
    if empty_style != "omitted":
        payload.update(name=None, age=None, sex=None)
        payload.update({key: None if empty_style == "null" else []
                        for key in ("fileList", "testData", "hospitalData")})
        payload.update({key: None if empty_style == "null" else {}
                        for key in ("gutHealthIndicator", "healthSurvey", "microbiomeData")})
    request = GutBarrierRequest.model_validate(payload)
    dumped = request.model_dump(mode="json", by_alias=True)
    assert all(key in dumped for key in ("fileList", "testData", "hospitalData", "gutHealthIndicator",
                                        "healthSurvey", "microbiomeData"))
    assert dumped["fileList"] == dumped["testData"] == dumped["hospitalData"] == []
    env = environment(pipeline=GutBarrierPipeline())
    job = await env.service.submit(request)
    assert await env.service.drain_generation() == 1
    saved = env.collection.documents[job["_id"]]
    callback = saved["body"]
    assert set(callback) == {"type", "schemaVersion", "finalReportId", "userId", "sessionId", "status", "report", "error"}
    assert callback["type"] == "gutBarrier" and callback["schemaVersion"] == "2.0"
    assert callback["status"] == "partial" and callback["error"] is None
    assessment = saved["audit"]["reportDocument"]["assessment"]
    assert assessment["barrier"]["score"] is None
    assert "inflammation" not in assessment
    assert_public_contract(callback["report"])
    card = next(item for section in callback["report"]["sections"] for block in section["blocks"]
                if block["kind"] == "cards" for item in block["items"] if item["label"] == "总体阶段")
    assert card == {"label": "总体阶段", "value": None, "detail": None}
