from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from adapters.gut_barrier.sources import GutBarrierSources
from modules.gut_barrier.pipeline import GutBarrierPipeline
from modules.gut_barrier.publication import project_callback_report
from modules.gut_barrier.schemas import GutBarrierRequest
from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.interventions import build_interventions
from tests.unit.gut_barrier.final_input_fixture import final_input


def latest_survey():
    return {
        "seqNo": "21", "submitTime": "2026/10/8 11:24:35", "usedTime": "48秒",
        "source": "微信", "sourceDetail": "N/A", "fromIp": "redacted-test-ip",
        "name": "合成问卷", "gender": "女", "age": "26",
        "heightCm": None, "weightKg": None, "diet": "无", "digestion": "良好",
        "lifestyle": None, "skin": None, "sleep": None, "fourHigh": "其他 / 无",
        "pastDisease": "无", "familyDisease": None, "allergy": "无",
        "bowelMovement": "排便规律", "currentMedication": None, "specialMedication": "无",
        "wantImprove": "便秘、腹泻、腹胀等肠道不适┋瘦身减脂、改善易胖体质┋改善皮肤状态、祛痘提亮┋提升免疫力、减少生病┋改善睡眠、缓解疲劳┋调理代谢、预防慢病",
    }


def test_final_fields_are_declared_preserved_and_bounded():
    payload = final_input()
    request = GutBarrierRequest.model_validate(payload)
    assert request.health_survey == payload["healthSurvey"]
    assert request.microbiome_data is None
    assert not request.model_extra
    dumped = request.model_dump(mode="json", by_alias=True)
    assert dumped["healthSurvey"] == payload["healthSurvey"]
    assert "microbiomeData" in dumped and dumped["microbiomeData"] is None
    assert request.age == 26 and request.sex == "女"


def test_latest_survey_alias_preserves_answers_and_canonical_serialization():
    payload = final_input()
    payload["healthSurvey"] = latest_survey()
    canonical = GutBarrierRequest.model_validate(payload)
    payload["healthSurvery"] = payload.pop("healthSurvey")
    request = GutBarrierRequest.model_validate(payload)
    assert request == canonical
    assert request.health_survey == payload["healthSurvery"]
    dumped = request.model_dump(mode="json", by_alias=True)
    assert dumped["healthSurvey"] == latest_survey()
    assert "healthSurvery" not in dumped
    assert "idCard" not in dumped["healthSurvey"]
    assert GutBarrierRequest.model_validate(dumped) == request


@pytest.mark.parametrize("different", [False, True])
def test_both_survey_aliases_use_latest_answers_without_changing_public_fields(different):
    payload = final_input()
    payload["healthSurvey"] = latest_survey()
    payload["healthSurvery"] = deepcopy(payload["healthSurvey"])
    if different:
        payload["healthSurvery"]["digestion"] = "消化不良"
    original = deepcopy(payload)
    request = GutBarrierRequest.model_validate(payload)
    assert request.health_survey == payload["healthSurvery"]
    assert request.model_dump(by_alias=True)["healthSurvey"] == payload["healthSurvery"]
    assert "healthSurvery" not in request.model_dump(by_alias=True)
    assert payload == original


def test_old_questionnaire_only_fills_missing_latest_answers():
    payload = final_input()
    payload["healthSurvey"] = {"name": "合成问卷", "age": "26", "diet": "喜辣", "pastDisease": "胃炎"}
    payload["healthSurvery"] = {"name": "合成问卷", "age": "26岁", "diet": None, "pastDisease": "无",
                                "fourHigh": "其他 / 无"}
    request = GutBarrierRequest.model_validate(payload)
    assert request.health_survey == {"name": "合成问卷", "age": "26岁", "diet": "喜辣",
                                     "pastDisease": "无", "fourHigh": "其他 / 无"}


@pytest.mark.parametrize("identity", [{"name": "另一姓名"}, {"age": "27"}, {"gender": "男"}, {"idCard": "other-id"}])
def test_different_questionnaire_subjects_are_not_combined(identity):
    payload = final_input()
    payload["healthSurvey"] = {"name": "合成问卷", "age": "26", "gender": "女", "idCard": "source-id",
                              "pastDisease": "胃炎"}
    payload["healthSurvery"] = {**{key: payload["healthSurvey"][key] for key in ("name", "age", "gender", "idCard")},
                                **identity, "digestion": "良好"}
    request = GutBarrierRequest.model_validate(payload)
    assert request.health_survey == payload["healthSurvery"]
    assert "pastDisease" not in request.health_survey


@pytest.mark.parametrize("empty", [None, "", "  ", "N/A", []])
def test_empty_latest_questionnaire_can_use_existing_questionnaire(empty):
    payload = final_input()
    payload["healthSurvery"] = empty
    assert GutBarrierRequest.model_validate(payload).health_survey == payload["healthSurvey"]


@pytest.mark.parametrize("field", ["healthSurvey", "healthSurvery", "microbiomeData"])
@pytest.mark.parametrize("value", [None, {}, "", "  ", "N/A", []])
def test_nullable_source_objects(field, value):
    payload = final_input()
    if field == "healthSurvery":
        payload.pop("healthSurvey")
    payload[field] = value
    output_field = "healthSurvey" if field == "healthSurvery" else field
    expected = {} if isinstance(value, dict) else None
    assert GutBarrierRequest.model_validate(payload).model_dump(by_alias=True)[output_field] == expected


@pytest.mark.parametrize("field", ["healthSurvey", "healthSurvery", "microbiomeData"])
@pytest.mark.parametrize("value", [["not-an-object"], "not-an-object", {"data": "x" * 20001}, {"value": float("nan")}])
def test_invalid_optional_sources_rejected(field, value):
    payload = final_input()
    if field == "healthSurvery":
        payload.pop("healthSurvey")
    payload[field] = value
    with pytest.raises(ValidationError):
        GutBarrierRequest.model_validate(payload)


@pytest.mark.asyncio
async def test_latest_survey_pipeline_keeps_missing_data_and_callback_contract():
    payload = final_input()
    payload.pop("healthSurvey")
    payload["healthSurvery"] = latest_survey()
    payload["fileList"] = []
    report, audit = await GutBarrierPipeline().generate(
        GutBarrierRequest.model_validate(payload), report_id="latest-survey", evaluation_date="2026-10-08")
    context = report["assessment"]["context"]
    assert not {"heightCm", "weightKg", "bmi", "immuneStatus", "diagnoses"} & context.keys()
    assert context["symptoms"] == []
    assert context["medicalHistory"] == [] and context["allergies"] == []
    assert all(context[field] is None for field in (
        "habits", "skinConcerns", "sleep", "conditions", "familyHistory", "medications"))
    assert context["medicationHistoryComplete"] is False
    assert "medications" in audit["missingData"]
    assert len(context["improvementGoals"]) == 6
    assert not any(row["code"] == "obesity" for row in report["assessment"]["diseaseRisks"])

    public = project_callback_report(report)
    assert "redacted-test-ip" not in str(public)
    assert "fromIp" not in str(public) and "healthSurvery" not in str(public)
    payload["healthSurvey"] = payload.pop("healthSurvery")
    canonical_report, _ = await GutBarrierPipeline().generate(
        GutBarrierRequest.model_validate(payload), report_id="latest-survey", evaluation_date="2026-10-08")
    assert project_callback_report(canonical_report) == public
    payload["healthSurvey"]["wantImprove"] = None
    without_goals, _ = await GutBarrierPipeline().generate(
        GutBarrierRequest.model_validate(payload), report_id="latest-survey", evaluation_date="2026-10-08")
    assert without_goals["assessment"]["diseaseRisks"] == report["assessment"]["diseaseRisks"]


@pytest.mark.asyncio
async def test_final_survey_remains_internal_without_public_questionnaire_or_pii():
    payload = final_input()
    request = GutBarrierRequest.model_validate(payload)
    report, audit = await GutBarrierPipeline().generate(request, report_id="test-final", evaluation_date="2026-09-24")
    assessed = report["assessment"]
    assert "bmi" not in assessed["context"]
    assert "35.2 kg/m²" in dict(assessed["context"]["healthSurveyRows"])["BMI"]
    assert assessed["context"]["allergies"] is None
    assert assessed["context"]["medications"] is None
    assert "allergies" in audit["missingData"] and "medications" in audit["missingData"]
    assert assessed["barrier"]["score"] == 0
    assert "inflammation" not in assessed
    assert "sampleDate" not in assessed["context"]
    assert assessed["context"].get("immuneStatus") is None
    public = project_callback_report(report)
    rows = assessed["context"]["healthSurveyRows"]
    assert "35.2" in str(rows) and "时常腹泻" in str(rows) and "消化不良" in str(rows)
    assert "redacted-test-ip" not in str(public) and "redacted-test-id-card" not in str(public)
    assert "fromIp" not in str(public) and "idCard" not in str(public)
    assert not any(b["title"] in {"健康问卷背景（自述）", "问卷相关管理关注"}
                   for s in public["sections"] for b in s["blocks"])
    assert audit["partial"] is True
    unchanged = deepcopy(payload)
    unchanged["healthSurvey"] = None
    old_report, _ = await GutBarrierPipeline().generate(GutBarrierRequest.model_validate(unchanged), report_id="test-final", evaluation_date="2026-09-24")
    assert old_report["assessment"]["markers"] == assessed["markers"]
    assert old_report["assessment"]["barrier"] == assessed["barrier"]
    assert old_report["assessment"]["riskModel"] == assessed["riskModel"]
    assert all(row["probability"] is None for row in assessed["diseaseRisks"])
    with_survey = {row["code"]: row for row in assessed["diseaseRisks"]}
    without_survey = {row["code"]: row for row in old_report["assessment"]["diseaseRisks"]}
    assert with_survey == without_survey
    assert "obesity" not in with_survey
    assert assessed["riskRuleAssessment"]["items"] == old_report["assessment"]["riskRuleAssessment"]["items"]


@pytest.mark.asyncio
async def test_inline_microbiome_data_is_read_without_downloading_and_is_not_a_disease_model():
    payload = final_input()
    payload["fileList"] = []
    payload["microbiomeData"] = {"patientName": "测试小陈", "sampleDate": "2026-09-24", "microbeData": {
        "comprehensive": [{"latinName": "GMWI2", "value": "1.2"}, {"latinName": "Enterotype", "value": "Bacteroides"}]},
        "diseaseRisk": {"便秘": "0.9"}}
    downloader = SimpleNamespace(download=AsyncMock(side_effect=AssertionError("network forbidden")))
    source = GutBarrierSources(downloader=downloader)
    request = GutBarrierRequest.model_validate(payload)
    collected = await source.collect(request, evaluation_date="2026-09-24")
    assert collected["sources"][0]["location"] == "microbiomeData"
    auxiliary = collected["context"]["microbiomeSources"][0]
    assert auxiliary["origin"] == "microbiomeData" and auxiliary["identityVerified"] is False
    assert auxiliary["enterotype"]["eligibleForMatching"] is False
    report, _ = await GutBarrierPipeline(sources=source).generate(request, report_id="inline", evaluation_date="2026-09-24")
    assert all(r["probability"] is None for r in report["assessment"]["diseaseRisks"])
    baseline, _ = await GutBarrierPipeline().generate(request, report_id="baseline", evaluation_date="2026-09-24")
    assert [(r["code"], r["riskIndex"]) for r in report["assessment"]["diseaseRisks"]] == [
        (r["code"], r["riskIndex"]) for r in baseline["assessment"]["diseaseRisks"]]
    assert report["assessment"]["context"].get("enterotype") is None
    downloader.download.assert_not_awaited()


@pytest.mark.asyncio
async def test_inline_microbiome_name_difference_does_not_exclude_data():
    payload = final_input()
    payload["fileList"] = []
    payload["microbiomeData"] = {"patientName": "其他患者", "microbeData": {}}
    collected = await GutBarrierSources().collect(GutBarrierRequest.model_validate(payload))
    assert collected["observations"] == []
    assert collected["context"]["microbiomeSources"][0]["rawData"] == payload["microbiomeData"]
    assert collected["sources"][0]["status"] == "parsed"
    assert collected["issues"] == []


def test_partial_survey_safety_information_is_used_but_still_needs_completion():
    payload = final_input()
    payload["healthSurvey"].update(allergy="海鲜┋其他", currentMedication="四环素", specialMedication="其他")
    assessed = assess_case(GutBarrierRequest.model_validate(payload), context={"currentDate": "2026-09-24"})
    assert assessed["context"]["allergies"] == ["海鲜"]
    assert assessed["context"]["medications"] == ["四环素"]
    assert assessed["missingData"].count("allergies") == assessed["missingData"].count("medications") == 1
    plans = build_interventions(assessed)
    assert any("鱼类和鱼油相关方案已被抑制" in item for item in plans["reviewItems"])
    assert any("锌补充剂量已被抑制" in item for item in plans["reviewItems"])


def test_family_history_and_negation_do_not_trigger_personal_safety_holds():
    payload = final_input()
    payload["healthSurvey"].update(familyDisease="慢性肾病", pastDisease="无肾功能不全", allergy="无海鲜过敏",
                                    currentMedication="未用四环素", specialMedication="无")
    assessed = assess_case(GutBarrierRequest.model_validate(payload), context={"currentDate": "2026-09-24"})
    plans = build_interventions(assessed)
    assert not any("肝肾功能问题" in item or "鱼类和鱼油相关方案已被抑制" in item
                   for item in plans["reviewItems"])


def test_survey_name_difference_keeps_body_measurements_and_top_level_display_name():
    payload = final_input()
    payload["healthSurvey"]["name"] = "其他人"
    assessed = assess_case(GutBarrierRequest.model_validate(payload), context={"currentDate": "2026-09-24"})
    assert "bmi" not in assessed["context"]
    assert "35.2 kg/m²" in dict(assessed["context"]["healthSurveyRows"])["BMI"]
    assert assessed["subject"]["name"] == "测试小陈"
    assert not any("IDENTITY" in item["code"] for item in assessed["context"].get("healthSurveyIssues", []))


@pytest.mark.asyncio
async def test_filelist_microbiome_pdf_keeps_auxiliary_results_without_diagnostic_promotion():
    payload = final_input()
    payload["fileList"] = payload["fileList"][:1]
    source = GutBarrierSources(downloader=SimpleNamespace(download=AsyncMock(return_value=b"%PDF-fixture")))
    source._pdf_pages = Mock(return_value=["姓名：测试小陈\n采样日期：2026-09-24\n肠道菌群报告\n肠菌健康指数：1.2\n多样性指数：3.6\n菌种数：180"])
    collected = await source.collect(GutBarrierRequest.model_validate(payload), evaluation_date="2026-09-24")
    assert collected["observations"] == []
    auxiliary = collected["context"]["microbiomeSources"][0]
    assert auxiliary["origin"] == "fileList" and auxiliary["format"] == "pdf"
    assert auxiliary["identityVerified"] is False and auxiliary["status"] == "source_only"
    assert {m["code"]: m["value"] for m in auxiliary["metrics"]} == {
        "GMWI2": "1.2", "Diversity_Index": "3.6", "Species_Num": "180"}
    assert collected["sources"][0]["auxiliaryCount"] == 1
    assert "enterotype" not in collected["context"]


@pytest.mark.asyncio
@pytest.mark.parametrize("text,code", [
    ("姓名：测试小陈\n仅有封面，没有检测内容", "UNRECOGNIZED_SOURCE_CONTENT"),
])
async def test_unrecognized_pdf_is_not_silent_success(text, code):
    payload = final_input()
    payload["fileList"] = payload["fileList"][:1]
    source = GutBarrierSources(downloader=SimpleNamespace(download=AsyncMock(return_value=b"%PDF-fixture")))
    source._pdf_pages = Mock(return_value=[text])
    collected = await source.collect(GutBarrierRequest.model_validate(payload), evaluation_date="2026-09-24")
    assert collected["observations"] == [] and collected["context"] == {}
    assert any(issue["code"] == code for issue in collected["issues"])
