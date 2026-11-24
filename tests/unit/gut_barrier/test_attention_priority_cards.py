"""Supported system summaries, not repeated tables or unlike model scales."""
from copy import deepcopy

import pytest

from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.dynamic_diseases import apply_dynamic_assessment
from modules.gut_barrier.presentation import build_report
from modules.gut_barrier.publication import project_callback_report
from modules.gut_barrier.schemas import GutBarrierRequest


def _assessment():
    request = GutBarrierRequest.model_validate({
        "userId": "synthetic-priority", "sessionId": "synthetic-priority", "age": 26, "sex": "女",
        "healthSurvey": {"heightCm": "160", "weightKg": "90", "digestion": "消化不良",
                         "bowelMovement": "时常腹泻", "skin": "痤疮"},
    })
    return apply_dynamic_assessment(assess_case(request, context={"currentDate": "2026-09-25", "bmi": 35}))


def _report_and_summary(assessment):
    before = deepcopy(assessment)
    public = project_callback_report(build_report(assessment, {}, report_id="synthetic-priority"))
    assert assessment == before
    section = next(section for section in public["sections"] if section["id"] == "disease_risks")
    summary = next(block for block in section["blocks"] if block["title"] == "本章小结")
    assert set(summary) == {"kind", "title", "items"} and summary["kind"] == "list"
    return section, summary["items"]


def _expected(rows):
    systems = {}
    for row in rows:
        systems.setdefault(row["systemName"], []).append(row["name"])
    return [f"{system}关注方向：{'、'.join(names)}。" for system, names in systems.items()]


def test_system_summaries_cover_supported_rows_without_repeating_details():
    assessment = _assessment()
    rows = sorted(assessment["diseaseRisks"], key=lambda row: (-row["attention"]["score"], row["code"]))
    _, items = _report_and_summary(assessment)
    assert items == _expected(rows)
    assert len(items) == len({row["systemName"] for row in rows})
    assert all(row["qualitative"]["basis"] not in "".join(items) for row in rows)


def test_tie_ranking_is_stable_when_input_order_changes():
    assessment = _assessment()
    _, items = _report_and_summary(assessment)
    assessment["diseaseRisks"].reverse()
    assert _report_and_summary(assessment)[1] == items


@pytest.mark.parametrize("count", [1, 2, 3])
def test_fewer_supported_rows_are_not_padded_with_template_diseases(count):
    assessment = _assessment()
    assessment["diseaseRisks"] = assessment["diseaseRisks"][:count]
    _, items = _report_and_summary(assessment)
    rows = sorted(assessment["diseaseRisks"], key=lambda row: (-row["attention"]["score"], row["code"]))
    assert items == _expected(rows)
    assert len(items) == len({row["systemName"] for row in rows})


@pytest.mark.parametrize("model_value", [0, 10, 100])
def test_registered_models_stay_in_the_table_but_not_in_custom_attention_cards(model_value):
    assessment = _assessment()
    row = deepcopy(assessment["diseaseRisks"][0])
    row.update(code="synthetic_registered", name="独立注册模型", status="evaluated", riskIndex=model_value)
    # Retain an artificial stale attention object: status still excludes it.
    row["attention"]["score"] = 999
    assessment["diseaseRisks"] = [row, *assessment["diseaseRisks"][1:3]]
    section, items = _report_and_summary(assessment)
    assert items == _expected(sorted(assessment["diseaseRisks"][1:],
                                     key=lambda item: (-item["attention"]["score"], item["code"])))
    assert items and not any("独立注册模型" in item for item in items)
    cells = [cell for block in section["blocks"] if block["kind"] == "table" for cell in block["rows"]]
    model_row = next(cell for cell in cells if cell[0] == "独立注册模型")
    assert model_row[1] == model_value
    assert "不与本章自定义关注指数比较" in model_row[3]


@pytest.mark.parametrize("missing", ["facts", "evidence", "score"])
def test_unsupported_or_non_numeric_attention_never_creates_a_card(missing):
    assessment = _assessment()
    row = assessment["diseaseRisks"][0]
    if missing == "facts":
        row["attention"]["facts"] = []
    elif missing == "evidence":
        row["qualitative"]["evidenceIds"] = []
    else:
        row["attention"]["score"] = False
    assessment["diseaseRisks"] = [row]
    _, items = _report_and_summary(assessment)
    assert items == []
