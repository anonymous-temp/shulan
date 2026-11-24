"""instruction 配套生活方式干预里的「用药禁忌」条目。"""

from modules.report.followup_defaults import (
    LIFESTYLE_DEFAULTS, LIFESTYLE_PRODUCT_CLAUSES, lifestyle_intervention_base,
)
from modules.report import reports


def item(result, key):
    return next(entry for entry in result["items"] if entry["key"] == key)


def test_medication_item_is_listed_before_self_monitoring():
    keys = [entry["key"] for entry in lifestyle_intervention_base()["items"]]
    assert keys == ["diet", "routine_exercise", "follow_up", "hydration", "medication", "self_monitoring", "substance"]
    assert item(lifestyle_intervention_base(), "medication")["title"] == "用药禁忌"


def test_medication_uses_model_content_without_pfmt_and_default_when_missing():
    personalized = {"medication": {"summary": "避免自行使用泻药。", "points": ["抗生素需遵医嘱"]}}
    assert item(lifestyle_intervention_base(personalized=personalized), "medication") == {
        "key": "medication", "title": "用药禁忌", "summary": "避免自行使用泻药。", "points": ["抗生素需遵医嘱"],
    }
    assert item(lifestyle_intervention_base(), "medication")["summary"] == LIFESTYLE_DEFAULTS["medication"]["summary"]


def test_medication_uses_fixed_pfmt_clause():
    personalized = {"medication": {"summary": "模型写的", "points": ["模型要点"]}}
    medication = item(lifestyle_intervention_base(has_pfmt=True, personalized=personalized), "medication")
    assert medication["summary"] == LIFESTYLE_PRODUCT_CLAUSES["medication"] and medication["points"] == []


def test_lifestyle_prompt_asks_for_medication():
    prompt = reports._render_followup_prompt("lifestyle", evaluate_json="{}", recommend_method="生活方式干预")
    assert "用药禁忌 (medication)" in prompt and '"medication"' in prompt
