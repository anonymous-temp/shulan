from copy import deepcopy

import pytest

from modules.gut_barrier.clinical_context import hospital_safety_signals


def finding(text, **updates):
    return {"dataType": "EXAM", "examResult": text, "date": "2026-09-24",
            "temporalStatus": "current", "source": [{"kind": "hospitalData", "index": 0}], **updates}


def codes(text, **updates):
    return {signal["code"] for signal in hospital_safety_signals([finding(text, **updates)])}


@pytest.mark.parametrize("text,expected", [
    ("诊断：肠梗阻。", {"fiber_hold"}),
    ("慢性肾病；肝功能不全", {"kidney_problem", "liver_problem"}),
    ("重度免疫抑制", {"immune_compromised", "severe_immune_compromise"}),
    ("不稳定型心绞痛", {"activity_hold"}),
    ("急性心肌梗死", {"activity_hold"}),
    ("急性细菌感染，急性炎症反应", {"acute_infection", "acute_inflammation"}),
    ("Acute infection. Chronic kidney disease.", {"acute_infection", "kidney_problem"}),
    ("Severely immunocompromised; intestinal stricture", {"immune_compromised", "severe_immune_compromise", "fiber_hold"}),
])
def test_only_explicit_condition_names_produce_conservative_signals(text, expected):
    assert codes(text) == expected


@pytest.mark.parametrize("text", [
    "无肠梗阻、肠狭窄及肠麻痹", "未见明显肾功能不全", "否认肝衰竭", "未发现免疫抑制",
    "不支持急性感染", "无急性炎症反应", "肠梗阻已排除", "肠梗阻，已治愈", "既往肠梗阻已痊愈",
    "既往急性感染，现已完全缓解", "No bowel obstruction or intestinal stricture",
    "No evidence of renal impairment", "Without liver failure", "Denies immunosuppression",
    "Bowel obstruction has resolved", "Acute infection not confirmed", "Kidney failure ruled out",
    "如果出现肠梗阻请及时就医", "如有急性感染，请就诊", "若发生急性炎症，请复诊",
    "If acute infection occurs, seek care", "预防肠梗阻", "Avoid bowel obstruction",
    "肠梗阻的风险因素包括手术史", "肺部条索影、磨玻璃影；双肾回声增强；肝实质回声增粗",
    "急性感染可导致炎症", "肠梗阻已经排除", "不存在肾衰竭", "肠梗阻（-）", "肠梗阻：未见",
    "家族史：父亲肾功能不全", "Family history of chronic kidney disease",
    "急性感染不存在", "肠梗阻未见", "肠梗阻时应禁食", "肠梗阻患者需及时就诊",
    "锌缺乏；维生素D不足；适宜肠道菌群移植",
])
def test_negated_resolved_hypothetical_and_non_diagnostic_prose_are_ignored(text):
    assert codes(text) == set()


def test_guidance_tail_does_not_create_safety_conditions():
    text = "慢性非萎缩性胃炎\r术后注意事项：此报告为内镜报告，如出现肠梗阻、急性感染，请及时就医。"
    assert codes(text) == set()
    assert codes("肠梗阻。术后注意事项：若急性感染则就医") == {"fiber_hold"}
    assert codes("慢性肾病，若急性感染请复诊") == {"kidney_problem"}
    assert codes("Bowel obstruction. Discharge instructions: if acute infection occurs seek care") == {"fiber_hold"}


@pytest.mark.parametrize("text", ["可能急性感染", "考虑急性肺炎", "疑似急性炎症", "不能排除急性感染", "Suspected acute infection", "Acute infection not ruled out"])
def test_uncertain_acute_diagnoses_never_set_positive_acute_flag(text):
    assert codes(text) == set()


def test_uncertain_non_acute_conditions_can_conservatively_hold():
    assert codes("不能排除肠梗阻") == {"fiber_hold"}
    assert codes("考虑肾功能不全") == {"kidney_problem"}
    assert codes("Possible intestinal stricture") == {"fiber_hold"}


def test_history_is_not_promoted_to_current_acute_state():
    assert codes("肠梗阻；急性感染；急性炎症", temporalStatus="historical") == {"fiber_hold"}
    assert codes("既往急性感染；曾经急性炎症") == set()
    assert codes("History of acute infection") == set()


@pytest.mark.parametrize("updates", [
    {"dataType": "INSPECTION"}, {"dataType": None}, {"temporalStatus": "date_unknown"},
    {"temporalStatus": "future_date_requires_review"}, {"conflictingResults": True},
    {"conflict": True}, {"status": "conflict"}, {"source": [{"kind": "NGS"}]},
    {"source": []}, {"source": {"kind": "hospitalData"}}, {"examResult": None},
])
def test_only_eligible_attributable_hospital_exam_records_are_read(updates):
    assert hospital_safety_signals([finding("肠梗阻；急性感染", **updates)]) == []


def test_original_basis_date_and_source_are_preserved_without_mutating_input():
    row = finding("诊断：肠梗阻。未见急性感染。")
    before = deepcopy(row)
    result = hospital_safety_signals([row])
    assert row == before
    assert result == [{"code": "fiber_hold", "basis": "诊断：肠梗阻", "date": "2026-09-24",
                       "temporalStatus": "current", "source": [{"kind": "hospitalData", "index": 0}]}]
    result[0]["source"][0]["index"] = 99
    assert row == before


def test_independent_clauses_do_not_spread_negation_or_resolution():
    assert codes("无发热，明确肠梗阻") == {"fiber_hold"}
    assert codes("肠梗阻已治愈，慢性肾功能不全") == {"kidney_problem"}
    assert codes("No renal impairment, bowel obstruction present") == {"fiber_hold"}


@pytest.mark.parametrize("value", [None, {}, "肠梗阻", [None, 1, "肠梗阻"]])
def test_malformed_containers_are_ignored(value):
    assert hospital_safety_signals(value) == []
