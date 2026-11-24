"""Catalog completeness is separate from availability of a validated model."""

from collections import Counter
from copy import deepcopy
import json

import pytest

from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.risk_model import build_disease_risks, DISEASE_CATALOG_VERSION
from modules.gut_barrier.schemas import GutBarrierRequest


def test_all_twenty_three_directions_follow_v27_order_and_eight_systems():
    rows, model = build_disease_risks()
    assert [row["name"] for row in rows] == [
        "炎症性肠病", "肠道炎症与功能紊乱", "非酒精性脂肪性肝病", "肠易激综合征", "肝硬化",
        "2型糖尿病", "肥胖", "高脂血症", "高尿酸血症",
        "动脉粥样硬化", "冠心病", "脑卒中", "高血压",
        "阿尔茨海默病", "帕金森病", "抑郁症", "焦虑症",
        "类风湿性关节炎", "系统性红斑狼疮", "慢性肾脏病", "肝癌", "结直肠癌", "肺癌",
    ]
    assert list(dict.fromkeys(row["systemName"] for row in rows)) == [
        "消化系统", "代谢系统", "心脑血管系统", "神经系统", "精神系统", "免疫系统", "泌尿系统", "肿瘤"]
    assert list(Counter(row["systemCode"] for row in rows).values()) == [5, 4, 4, 2, 2, 2, 1, 3]
    assert len({row["code"] for row in rows}) == 23
    assert model["catalogVersion"] == DISEASE_CATALOG_VERSION


def test_catalog_never_copies_example_scores_or_presents_a_validated_model():
    rows, model = build_disease_risks()
    assert model["status"] == "not_configured"
    assert model["modelId"] is model["modelVersion"] is model["scale"] is None
    for row in rows:
        assert row["riskIndex"] is row["riskLevel"] is row["probability"] is None
        assert row["status"] == "not_evaluated"
        assert row["mechanismStatus"] == "provided_reference_not_individual_diagnosis"
        assert row["mechanism"] and row["source"]["location"]
        assert "disease_risk_model_not_configured" in row["reasons"]
    serialized = json.dumps(rows, ensure_ascii=False)
    for example_index in ("8.5", "7.5", "7.0", "6.5", "6.0", "5.5"):
        assert example_index not in serialized


def test_returned_catalog_and_model_are_independent_copies():
    rows, model = build_disease_risks()
    before = deepcopy((rows, model))
    rows[0]["name"] = "changed"
    rows[0]["source"]["sha256"] = "changed"
    rows[0]["reasons"].append("changed")
    model["reasons"].clear()
    assert build_disease_risks() == before


@pytest.mark.parametrize("age", [None, 5, 50])
@pytest.mark.parametrize("d,dao,lps,crp", [(5, 5, 10, 0.4), (26, 17, 23, 2.7), (31, 21, 36, 25)])
def test_normal_abnormal_minor_or_acute_values_do_not_magically_supply_a_disease_model(age, d, dao, lps, crp):
    request = GutBarrierRequest.model_validate({
        "userId": "u", "sessionId": "s", "age": age,
        "gutHealthIndicator": {
            "dLacticAcid": {"value": d, "unit": "mg/L"}, "diamineOxidase": {"value": dao, "unit": "U/L"},
            "bacterialEndotoxin": {"value": lps, "unit": "U/L"}, "hsCRP": {"value": crp, "unit": "mg/L"}},
    })
    context = {"bmi": 31, "clinicianApproved": True,
               "microbiomeSources": [{"sourceModelIndices": [{"name": "冠心病", "value": 95}]}]}
    before = deepcopy(context)
    result = assess_case(request, context=context)
    assert len(result["diseaseRisks"]) == 23
    assert result["riskModel"]["status"] == "not_configured"
    assert all(row["riskIndex"] is row["riskLevel"] is row["probability"] is None for row in result["diseaseRisks"])
    assert context == before
    # Older qualitative outputs remain available, without upgrading them into
    # an index or assigning those findings to all diseases in the catalog.
    assert isinstance(result["risks"], list)
    assert all(row["probability"] is None for row in result["risks"])
