"""Independent self-report scopes and conservative safety decisions."""

import pytest

from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.interventions import build_interventions
from modules.gut_barrier.presentation import build_report
from modules.gut_barrier.publication import project_callback_report
from modules.gut_barrier.schemas import GutBarrierRequest


def case(survey=None, *, context=None):
    request = GutBarrierRequest.model_validate({
        "userId": "survey-review", "sessionId": "survey-review", "name": "测试", "age": 26, "sex": "女",
        "healthSurvey": survey,
        "gutHealthIndicator": {
            "dLacticAcid": {"value": 19, "unit": "mg/L"},
            "diamineOxidase": {"value": 13, "unit": "U/L"},
            "bacterialEndotoxin": {"value": 24, "unit": "U/L"},
            "hsCRP": {"value": 4.2, "unit": "mg/L"},
        },
    })
    assessed = assess_case(request, context={"currentDate": "2026-09-24", **(context or {})})
    plans = build_interventions(assessed)
    report = build_report(assessed, plans, report_id="survey-review")
    return assessed, plans, report, project_callback_report(report)


def nutrient(plans, name):
    return next(item for item in plans["nutrition"] if item["name"] == name)


def public_blocks(public):
    return [block for section in public["sections"] for block in section["blocks"]]
@pytest.mark.parametrize("separator", ["┋", "；", ";", "，", ",", "。", "\n", "但", "但是"])
@pytest.mark.parametrize("reverse", [False, True])
def test_negation_cannot_cross_distinct_history_answers_or_clauses(separator, reverse):
    answers = ["无肾病", "肝功能不全"]
    if reverse:
        answers.reverse()
    assessed, plans, _, public = case({"pastDisease": separator.join(answers)})
    assert assessed["barrier"]["pattern"] == "+++"
    item = nutrient(plans, "口服谷氨酰胺")
    assert item["status"] == "safety_hold_liver_kidney"
    assert item["dose"] is None
    assert any(block.get("title") == "个体干预限制" for block in public_blocks(public))


@pytest.mark.parametrize("separator", ["┋", "；", "，", "\n"])
@pytest.mark.parametrize("reverse", [False, True])
def test_negated_fish_answer_cannot_hide_separate_seafood_allergy(separator, reverse):
    answers = ["无鱼过敏", "海鲜"]
    if reverse:
        answers.reverse()
    _, plans, _, public = case({"allergy": separator.join(answers)})
    item = nutrient(plans, "Omega-3 食物来源")
    assert item["status"] == "suppressed_due_to_allergy" and item["dose"] is None
    assert any(block.get("title") == "个体干预限制" for block in public_blocks(public))


def test_negated_fields_cannot_hide_positive_different_field():
    _, plans, _, _ = case({"fourHigh": "无肾病", "pastDisease": "肝功能不全",
                           "currentMedication": "未使用四环素", "specialMedication": "青霉胺"})
    assert nutrient(plans, "口服谷氨酰胺")["status"] == "safety_hold_liver_kidney"
    # Isolate drug interaction from the higher-priority liver safety hold.
    _, drug_plans, _, _ = case({"currentMedication": "未使用四环素", "specialMedication": "青霉胺"})
    assert nutrient(drug_plans, "锌（仅缺乏/摄入不足时）")["status"] == "interaction_review_required"


@pytest.mark.parametrize("history", [
    "无肝功能不全", "否认肝功能不全", "未见肝功能不全", "肝功能不全：无", "肝功能不全（否认）",
    "家族史：父亲肝功能不全", "父亲肝功能不全", "Family history of liver failure",
    "如果出现肝功能不全请就医", "预防肝功能不全", "If liver failure occurs, seek care",
    "No liver failure", "Liver failure absent",
])
def test_negative_family_and_hypothetical_history_does_not_become_own_positive(history):
    _, plans, _, _ = case({"pastDisease": history})
    assert nutrient(plans, "口服谷氨酰胺")["status"] != "safety_hold_liver_kidney"


@pytest.mark.parametrize("history", ["父亲肾功能不全，本人肝功能不全", "本人肝功能不全，父亲肾功能不全"])
def test_positive_personal_clause_remains_usable_beside_family_history(history):
    _, plans, _, _ = case({"pastDisease": history})
    assert nutrient(plans, "口服谷氨酰胺")["status"] == "safety_hold_liver_kidney"


def test_family_field_is_not_promoted_into_personal_history():
    _, plans, _, _ = case({"familyDisease": "肝功能不全┋海鲜过敏"})
    assert nutrient(plans, "口服谷氨酰胺")["status"] != "safety_hold_liver_kidney"
    assert nutrient(plans, "Omega-3 食物来源")["status"] == "food_first"


def test_boolean_and_structured_absence_cannot_mask_independent_positive_item():
    _, plans, _, _ = case(context={"allergies": {"fish": False, "seafood": True}})
    assert nutrient(plans, "Omega-3 食物来源")["status"] == "suppressed_due_to_allergy"
    _, plans, _, _ = case(context={"allergies": {"fish": "no", "seafood": "yes"}})
    assert nutrient(plans, "Omega-3 食物来源")["status"] == "suppressed_due_to_allergy"
    _, plans, _, _ = case(context={"allergies": {"fish": False, "seafood": "no"}})
    assert nutrient(plans, "Omega-3 食物来源")["status"] == "food_first"


def test_conflicting_candidates_still_hold_and_remain_subject_to_verification():
    assessed, plans, _, _ = case({"allergy": "无┋海鲜", "currentMedication": "无┋四环素"})
    assert nutrient(plans, "Omega-3 食物来源")["status"] == "suppressed_due_to_allergy"
    assert nutrient(plans, "锌（仅缺乏/摄入不足时）")["status"] == "interaction_review_required"
    assert {"medications", "allergies"} <= set(assessed["missingData"])


def test_different_questionnaire_demographics_do_not_disable_clinical_personalization():
    assessed, plans, _, _ = case({
        "name": "问卷名称", "age": "42", "gender": "男", "heightCm": "160", "weightKg": "90",
        "digestion": "消化不良", "bowelMovement": "时常腹泻", "pastDisease": "肝功能不全",
        "allergy": "海鲜", "currentMedication": "四环素", "specialMedication": "无",
    })
    assert assessed["subject"]["name"] == "测试"
    assert assessed["subject"]["age"] == 26
    assert assessed["subject"]["sex"] == "女"
    assert "bmi" not in assessed["context"]
    assert "35.2 kg/m²" in dict(assessed["context"]["healthSurveyRows"])["BMI"]
    assert assessed["context"]["symptoms"] == ["消化不良", "时常腹泻"]
    assert nutrient(plans, "口服谷氨酰胺")["status"] == "safety_hold_liver_kidney"
    assert nutrient(plans, "Omega-3 食物来源")["status"] == "suppressed_due_to_allergy"
    assert not any(issue["code"] == "HEALTH_SURVEY_IDENTITY_CONFLICT" for issue in assessed["context"].get("healthSurveyIssues", []))
