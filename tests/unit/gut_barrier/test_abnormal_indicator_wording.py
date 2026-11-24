"""Current report wording is generic; measurements and severity audit are not."""
from copy import deepcopy
import json
import re

import pytest

from adapters.gut_barrier.rendering import render_report_html
from modules.gut_barrier.display_versions import LATEST_DISPLAY_TEMPLATE
from modules.gut_barrier.publication import project_callback_report
from tests.unit.gut_barrier.test_review_patient_summary import _word_assessment, _word_internal


def _current(assessment):
    return project_callback_report(_word_internal(assessment, LATEST_DISPLAY_TEMPLATE))


def _marker_row(report, name):
    return next(row for section in report["sections"] if section["id"] == "overview"
                for block in section["blocks"] if block["title"] == "1.2 检测项目与结果总览"
                for row in block["rows"] if name in row[1])


@pytest.mark.parametrize("raw", ["0.51", "0.99", "1", "2.99", "3", "10", "12", ">0.5", ">1", ">3", ">10"])
def test_all_supported_abnormal_hscrp_results_use_generic_wording_and_keep_audit(raw):
    assessment = _word_assessment(values={"hsCRP": (raw, "mg/L")})
    before = deepcopy(assessment)
    report = _current(assessment)
    assert "hs-CRP" not in json.dumps(report, ensure_ascii=False)
    text = json.dumps(report, ensure_ascii=False)
    assert not any(term in text for term in ("轻度升高", "中度升高", "重度升高", "显著升高", "异常升高"))
    rendered = render_report_html(report, template_version=LATEST_DISPLAY_TEMPLATE)
    marker_table = re.search(r'<h3>1.2 检测项目与结果总览</h3>(.*?)</table>', rendered, re.S)[1]
    assert "hs-CRP" not in marker_table
    assert assessment == before
    assert json.loads(re.search(r'id="report-data">(.*?)</script>', rendered, re.S)[1]) == report


@pytest.mark.parametrize("raw", ["0", "0.49", "0.5", "<0.5", "<=0.5", "<1", ">=0.5", ">0.1", "<20", "<=20"])
def test_normal_or_crossing_normal_hscrp_is_never_marked_abnormal(raw):
    assessment = _word_assessment(values={"hsCRP": (raw, "mg/L")})
    before = deepcopy(assessment)
    report = _current(assessment)
    assert "hs-CRP" not in json.dumps(report, ensure_ascii=False)
    prose = "".join(block.get("text") or "" for section in report["sections"] for block in section["blocks"])
    assert "hs-CRP指标异常" not in prose
    assert assessment == before


@pytest.mark.parametrize("key,name,raw", [
    ("dLacticAcid", "D-乳酸", "14"), ("diamineOxidase", "DAO", "8.5"),
    ("dLacticAcid", "D-乳酸", "15"), ("dLacticAcid", "D-乳酸", "26"),
    ("diamineOxidase", "DAO", "9"), ("diamineOxidase", "DAO", "24"),
    ("bacterialEndotoxin", "LPS", "19"), ("bacterialEndotoxin", "LPS", "36"),
])
def test_barrier_marker_abnormal_wording_does_not_change_score_or_grade(key, name, raw):
    assessment = _word_assessment(values={key: (raw, "mg/L" if key == "dLacticAcid" else "U/L")})
    before = deepcopy(assessment)
    assert _marker_row(_current(assessment), name)[4] == f"{name}指标异常"
    assert assessment == before


@pytest.mark.parametrize("key,name,raw", [
    ("dLacticAcid", "D-乳酸", "<20"), ("bacterialEndotoxin", "LPS", "<22"),
])
def test_pending_or_range_barrier_results_are_not_invented_as_abnormal(key, name, raw):
    assessment = _word_assessment(values={key: (raw, "mg/L" if key == "dLacticAcid" else "U/L")})
    assert _marker_row(_current(assessment), name)[4] != f"{name}指标异常"


@pytest.mark.parametrize("age", [None, 0, 17])
def test_hscrp_age_limit_survives_generic_wording(age):
    assessment = _word_assessment(age=age)
    report = _current(assessment)
    assert "hs-CRP" not in json.dumps(report, ensure_ascii=False)
    assert "年龄适用的参考范围" in json.dumps(report, ensure_ascii=False)


def test_missing_indicator_and_frozen_callback_are_not_rewritten():
    report = _current(_word_assessment(omit=("hsCRP",)))
    assert "hs-CRP" not in json.dumps(report, ensure_ascii=False)
    # A frozen older receipt can still contain its original marker row.
    table = next(b for sec in report["sections"] for b in sec["blocks"] if b["title"] == "1.2 检测项目与结果总览")
    table["rows"].append(["炎症指标", "hs-CRP", "1.8 mg/L", "＜0.5 mg/L", "中度升高", "炎症"])
    frozen = deepcopy(report)
    assert project_callback_report(report) == frozen == report


@pytest.mark.parametrize("flag", ["acuteInfection", "acuteInflammation"])
def test_acute_context_retains_safety_advice_without_severity_copy(flag):
    assessment = _word_assessment(context={flag: True})
    before = deepcopy(assessment)
    report = _current(assessment)
    text = json.dumps(report, ensure_ascii=False)
    assert "hs-CRP指标异常" not in text
    assert "已有急性感染或炎症背景" in text
    assert "结合hs-CRP指标异常，提示需关注肠源性" not in text
    assert assessment == before
