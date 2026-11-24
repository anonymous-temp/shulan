"""Synthetic chapter regression: resolved findings cannot return as cautions."""
from copy import deepcopy

import pytest

from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.chapters import _diseases
from modules.gut_barrier.clinical_attention import imaging_attention_findings
from modules.gut_barrier.dynamic_diseases import apply_dynamic_assessment
from modules.gut_barrier.schemas import GutBarrierRequest


def reviewed(*texts):
    request = GutBarrierRequest.model_validate({
        "userId": "synthetic-reconciled-imaging", "sessionId": "chapter-regression", "age": 40,
        "hospitalData": [{"dataType": "EXAM", "itemName": "腹部CT",
                          "checkTime": day, "examResult": text}
                         for day, text in zip(("2022-02-01", "2025-08-09"), texts)],
    })
    return apply_dynamic_assessment(assess_case(request, context={"currentDate": "2026-01-01"}))


def cautions(assessment):
    return [block for block in _diseases(assessment)["blocks"]
            if block.get("title") == "影像检查待核实事项"]


@pytest.mark.parametrize("latest", [
    "结肠肝曲结节，复查已消失。", "结肠肝曲未见结节。", "结肠未见异常。",
])
def test_resolved_same_site_is_absent_from_both_disease_rows_and_chapter_caution(latest):
    assessment = reviewed("结肠肝曲结节，间质瘤可能。", latest)
    before = deepcopy(assessment)
    assert not any(row["code"] == "digestive_lesion_followup" for row in assessment["diseaseRisks"])
    assert imaging_attention_findings(assessment) == []
    assert cautions(assessment) == []
    assert assessment == before


@pytest.mark.parametrize("latest", ["直肠未见结节。", "右肺未见结节。", "胃体未见异常。"])
def test_normal_different_site_does_not_remove_the_unresolved_bowel_caution(latest):
    assessment = reviewed("结肠肝曲结节，间质瘤可能。", latest)
    assert any(row["code"] == "digestive_lesion_followup" for row in assessment["diseaseRisks"])
    assert len(cautions(assessment)) == 1
    assert "2022-02-01" in cautions(assessment)[0]["text"]


@pytest.mark.parametrize("latest", [
    "结肠肝曲结节，性质待定。", "结肠肝曲结节未见增大。", "不能排除结肠肝曲结节。",
])
def test_uncertain_or_stable_later_finding_stays_visible_with_its_latest_date(latest):
    assessment = reviewed("结肠肝曲结节，间质瘤可能。", latest)
    items = imaging_attention_findings(assessment)
    assert len(items) == 1 and items[0]["date"] == "2025-08-09"
    assert latest.rstrip("。") == items[0]["excerpt"]
    assert len(cautions(assessment)) == 1


def test_excluding_a_histologic_type_does_not_clear_the_structural_finding():
    assessment = reviewed("结肠肝曲结节，间质瘤可能。", "结肠肝曲间质瘤已排除。")
    assert len(cautions(assessment)) == 1
    assert "2022-02-01" in cautions(assessment)[0]["text"]


def test_unrelated_structural_finding_in_a_mixed_sentence_is_not_silently_removed():
    assessment = reviewed("结肠肝曲结节，右肺结节。", "结肠肝曲结节，复查已消失。")
    assert len(cautions(assessment)) == 1
    assert "右肺结节" in cautions(assessment)[0]["text"]
