"""Audit fixes: incomplete inputs are not abnormal findings or invented stages."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.gut_barrier.pipeline import GutBarrierPipeline
from modules.gut_barrier.review_release import combined_text
from modules.gut_barrier.schemas import GutBarrierRequest


@pytest.mark.parametrize("barrier,comparison", [
    ({}, None),
    ({"score": 0, "grade": "normal"}, None),
    ({"score": None, "status": "missing"}, "within"),
    ({"score": None, "status": "needs_review"}, "above"),
    ({"score": 3.5, "grade": "II"}, None),
])
def test_incomplete_joint_assessment_does_not_assert_abnormal_findings(barrier, comparison):
    assessment = {"barrier": barrier, "reportReview": {"hsCRP": {"comparison": comparison}}}
    before = deepcopy(assessment)
    text = combined_text(assessment)
    if barrier.get("score") is None:
        assert "补充或核对检测结果" in text and "异常" not in text
    else:
        assert "补充或核对检测结果" not in text
    assert "hs-CRP" not in text
    assert assessment == before


@pytest.mark.parametrize("grade,comparison,value,expected", [
    ("normal", "within", 0.2, "屏障评分未见异常"),
    ("normal", "above", 1.8, "屏障评分未见异常"),
    ("II", "within", 0.2, "以屏障相关指标异常为主要检测发现"),
    ("II", "above", 1.8, "以屏障相关指标异常为主要检测发现"),
])
def test_available_findings_remain_distinct_from_missing_results(grade, comparison, value, expected):
    assessment = {
        "subject": {"age": 45},
        "barrier": {"score": 0 if grade == "normal" else 3.5, "grade": grade},
        "inflammation": {"status": "evaluated", "value": value, "operator": "=", "unit": "mg/L"},
        "reportReview": {"hsCRP": {"comparison": comparison}},
    }
    before = deepcopy(assessment)
    text = combined_text(assessment)
    assert expected in text and "尚需补充" not in text
    assert "hs-CRP" not in text
    assert assessment == before


@pytest.mark.asyncio
@pytest.mark.parametrize("crp", [None, "0.2"])
async def test_normal_barrier_and_missing_or_complete_crp_do_not_invent_overall_stage(crp):
    indicators = {
        "bacterialEndotoxin": {"value": "12.5", "unit": "U/L"},
        "diamineOxidase": {"value": "3.8", "unit": "U/L"},
        "dLacticAcid": {"value": "0.06", "unit": "mg/L"},
    }
    if crp is not None:
        indicators["hsCRP"] = {"value": crp, "unit": "mg/L"}
    request = GutBarrierRequest.model_validate({
        "userId": "audit-test", "sessionId": "audit-session", "age": 26,
        "gutHealthIndicator": indicators,
    })
    narrative = SimpleNamespace(arrange=AsyncMock())
    report, audit = await GutBarrierPipeline(narrative=narrative).generate(
        request, report_id="audit-test", evaluation_date="2026-09-28",
    )
    assert report["assessment"]["barrier"]["grade"] == "normal"
    assert audit["overallStage"]["status"] == "display_only"
    assert audit["overallStage"]["code"] is None and audit["overallStage"]["label"] is None
    assert audit["partial"] is True
    assert report["narrativeCandidates"] == []
    narrative.arrange.assert_not_called()
    text = combined_text(report["assessment"])
    assert "屏障评分未见异常" in text and "hs-CRP" not in text
