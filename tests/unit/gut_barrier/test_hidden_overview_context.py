"""Auxiliary overview blocks are private; their clinical inputs still apply."""

from copy import deepcopy
import json

import pytest

from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.interventions import build_interventions
from modules.gut_barrier.presentation import build_report
from modules.gut_barrier.publication import project_callback_report, project_public_report
from modules.gut_barrier.schemas import GutBarrierRequest


HIDDEN_TITLES = {
    "健康问卷背景（自述）", "问卷相关管理关注", "本次检测方法与参考信息", "本次样本与质控记录",
}


def report_for(*, populated=True, safety=False):
    payload = {
        "userId": "overview-input", "sessionId": "overview-input", "name": "测试", "age": 26, "sex": "女",
        "gutHealthIndicator": {
            "dLacticAcid": {"value": 19, "unit": "mg/L"},
            "diamineOxidase": {"value": 13, "unit": "U/L"},
            "bacterialEndotoxin": {"value": 24, "unit": "U/L"},
            "hsCRP": {"value": 4.2, "unit": "mg/L"},
        },
    }
    context = {"currentDate": "2026-09-24"}
    observations = []
    if populated:
        payload["healthSurvey"] = {
            "heightCm": "160", "weightKg": "90", "diet": "喜辣", "digestion": "消化不良",
            "lifestyle": "熬夜", "bowelMovement": "时常腹泻",
        }
        context.update(sampleType="血清", qualityControl={
            "sampleQuality": "内部样本质控记录甲", "preparation": "内部制备质控记录乙",
            "analytical": "内部分析质控记录丙", "examiner": "内部检测人员丁", "reviewer": "内部审核人员戊",
        })
        observations = [{"code": "DAO", "value": 13, "unit": "U/L", "date": "2026-09-24",
                         "method": "内部实际方法甲", "referenceRange": "内部实验室参考乙"}]
    if safety:
        payload.setdefault("healthSurvey", {}).update(
            allergy="无鱼过敏┋海鲜", pastDisease="无肾病┋肝功能不全",
        )
    assessment = assess_case(GutBarrierRequest.model_validate(payload), observations=observations, context=context)
    plans = build_interventions(assessment)
    before = deepcopy(assessment)
    report = build_report(assessment, plans, report_id="gbr_hidden_overview")
    assert assessment == before
    return report


@pytest.mark.parametrize("populated", [False, True])
@pytest.mark.parametrize("projector", [project_public_report, project_callback_report])
def test_new_report_json_does_not_include_removed_blocks(populated, projector):
    report = report_for(populated=populated)
    public = projector(report)
    assert set(public) == {"schemaVersion", "meta", "sections"}
    assert all(set(section) == {"id", "title", "subtitle", "blocks", "visible"} for section in public["sections"])
    for document in (report, public):
        assert not HIDDEN_TITLES & {block.get("title") for section in document["sections"] for block in section["blocks"]}
    encoded = json.dumps(public, ensure_ascii=False)
    assert all(title not in encoded for title in HIDDEN_TITLES)
    assert all(value not in encoded for value in (
        "内部样本质控记录甲", "内部制备质控记录乙", "内部分析质控记录丙",
        "内部检测人员丁", "内部审核人员戊", "内部实际方法甲", "内部实验室参考乙",
    ))
    assert "三项检测指标" in encoded and "肠道屏障功能评分（IBFC）" in encoded


def test_hidden_inputs_remain_in_internal_assessment():
    report = report_for()
    assessment = report["assessment"]
    context = assessment["context"]
    assert "bmi" not in context
    assert "35.2 kg/m²" in dict(context["healthSurveyRows"])["BMI"]
    assert "35.2" in str(context["healthSurveyRows"])
    assert "时常腹泻" in str(context["healthSurvey"]["answers"])
    assert context["qualityControl"]["sampleQuality"] == "内部样本质控记录甲"
    assert context["qualityControl"]["reviewer"] == "内部审核人员戊"
    marker = next(marker for marker in assessment["markers"] if marker["code"] == "DAO")
    assert marker["method"] == "内部实际方法甲"
    assert [item["value"] for item in marker["laboratoryReferenceRanges"]] == ["内部实验室参考乙"]


@pytest.mark.parametrize("projector", [project_public_report, project_callback_report])
def test_questionnaire_safety_limits_are_still_applied_and_published(projector):
    report = report_for(safety=True)
    nutrients = {item["name"]: item for item in report["interventions"]["nutrition"]}
    assert nutrients["Omega-3 食物来源"]["status"] == "suppressed_due_to_allergy"
    assert nutrients["口服谷氨酰胺"]["status"] == "safety_hold_liver_kidney"
    assert nutrients["Omega-3 食物来源"]["dose"] is None
    assert nutrients["口服谷氨酰胺"]["dose"] is None
    public = projector(report)
    restrictions = next(block for section in public["sections"] if section["id"] == "overview"
                        for block in section["blocks"] if block.get("title") == "个体干预限制")
    assert restrictions["kind"] == "callout" and restrictions["text"]
    assert not HIDDEN_TITLES & {block.get("title") for section in public["sections"] for block in section["blocks"]}
