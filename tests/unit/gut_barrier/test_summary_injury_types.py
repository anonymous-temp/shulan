"""Section 2.9 uses the supplied combination display without changing scoring."""
from copy import deepcopy
from html import unescape
import json
import re

import pytest

from adapters.gut_barrier.rendering import render_report_html
from modules.gut_barrier.display_versions import LATEST_DISPLAY_TEMPLATE
from modules.gut_barrier.dynamic_diseases import apply_dynamic_assessment
from modules.gut_barrier.publication import project_callback_report
from modules.gut_barrier.risk_index_rules import apply_risk_index_rules
from modules.gut_barrier.template_risk_review import _chapter_summary, _summary_injury_clause
from tests.unit.gut_barrier.test_template_risk_review import _clinical_assessment, _publish


_TYPES = [
    ("+++", "应激性 + 炎症性损伤"),
    ("+-+", "炎症性损伤（伴移位）"),
    ("-++", "应激性损伤（伴移位）"),
    ("--+", "非肠源性移位"),
    ("+--", "单纯炎症性损伤"),
    ("-+-", "单纯应激性损伤"),
    ("++-", "炎症性 + 应激性损伤（无移位）"),
]


def _assessed(pattern, *, crp=.3):
    high = (26, 17, 23)
    panel = tuple(value if sign == "+" else 1 for sign, value in zip(pattern, high))
    return apply_risk_index_rules(apply_dynamic_assessment(_clinical_assessment((*panel, crp))))


@pytest.mark.parametrize("pattern,injury_type", _TYPES)
def test_all_seven_supplied_combination_sentences_match_callback_and_html(pattern, injury_type):
    assessment = _assessed(pattern)
    before = deepcopy(assessment)
    callback = _publish(assessment)
    chapter = next(section for section in callback["sections"] if section["id"] == "disease_risks")
    summary = next(block for block in chapter["blocks"] if block.get("title") == "2.9 本章小结")
    sentence = f"损伤类型为{injury_type}"
    assert summary["text"].count(sentence) == 1
    assert set(summary) == {"kind", "title", "text"}
    paragraphs = summary["text"].split("\n\n")
    assert len(paragraphs) == 2 and sentence in paragraphs[0]
    assert paragraphs[1].endswith("修复肠道屏障功能，恢复肠道菌群稳态。")
    if pattern == "--+":
        assert "损伤类型为非肠源性移位（指标组合分型）。" in summary["text"]
        assert "不用于确认" not in summary["text"]
    else:
        assert sentence + "。" in summary["text"]

    html = render_report_html(callback, template_version=LATEST_DISPLAY_TEMPLATE)
    body = re.search(r'<div class="block" id="disease_risks-2-9">(.*?)</div>', html, re.S)[1]
    assert [unescape(text) for text in re.findall(r"<p>(.*?)</p>", body, re.S)] == paragraphs
    assert json.loads(re.search(r'id="report-data">(.*?)</script>', html, re.S)[1]) == callback
    assert assessment == before and project_callback_report(callback) == callback


def test_subhealth_elevation_counts_only_for_the_product_summary_combination():
    # Real CSV's three-reference-elevation case has a subhealth D-lactate.
    assessment = apply_risk_index_rules(apply_dynamic_assessment(_clinical_assessment((14.96, 18, 24.2, .3))))
    before = deepcopy(assessment)
    d_lactate = next(row for row in assessment["markers"] if row["code"] == "D_LACTATE")
    assert d_lactate["score"] == 1 and d_lactate["abnormal"] is None
    assert "损伤类型为应激性 + 炎症性损伤。" in _chapter_summary(assessment, [])
    assert assessment == before


def test_all_three_subhealth_elevations_are_a_display_combination_not_a_clinical_pattern():
    assessment = _clinical_assessment((15, 9, 20, .3))
    before = deepcopy(assessment)
    assert all(row["abnormal"] is None for row in assessment["markers"] if row["code"] != "HS_CRP")
    assert _summary_injury_clause(assessment) == "损伤类型为应激性 + 炎症性损伤。"
    assert assessment == before


def test_normal_complete_panel_does_not_receive_an_injury_type():
    assessment = _assessed("---")
    assert _summary_injury_clause(assessment) == ""
    assert "损伤类型为" not in _chapter_summary(assessment, [])


@pytest.mark.parametrize("code", ["D_LACTATE", "DAO", "LPS"])
def test_missing_marker_does_not_invent_a_combination(code):
    assessment = _assessed("+++")
    assessment["markers"] = [row for row in assessment["markers"] if row["code"] != code]
    assert _summary_injury_clause(assessment) == ""


@pytest.mark.parametrize("panel", [(14, 17, 23), (26, 8.5, 23), (26, 17, 18)])
def test_confirmed_word_endpoints_receive_their_accepted_combination(panel):
    assessment = _clinical_assessment((*panel, .3))
    assert _summary_injury_clause(assessment) == "损伤类型为应激性 + 炎症性损伤。"
    assert "损伤类型为应激性 + 炎症性损伤。" in _chapter_summary(assessment, [])


@pytest.mark.parametrize("code", ["D_LACTATE", "DAO", "LPS"])
@pytest.mark.parametrize("field,value", [("operator", ">"), ("unit", "unsupported"),
                                         ("status", "needs_review"), ("score", 0)])
def test_range_unsupported_units_review_states_and_inconsistent_scores_are_not_typed(code, field, value):
    assessment = _assessed("+++")
    marker = next(row for row in assessment["markers"] if row["code"] == code)
    marker[field] = value
    assert _summary_injury_clause(assessment) == ""


def test_duplicate_marker_records_are_not_reduced_to_an_invented_combination():
    assessment = _assessed("+++")
    assessment["markers"].append(deepcopy(assessment["markers"][0]))
    assert _summary_injury_clause(assessment) == ""


@pytest.mark.parametrize("crp", [.3, 1, 3, 12, None, ">3.0"])
def test_hscrp_does_not_change_the_three_marker_injury_type(crp):
    assessment = _assessed("+++", crp=crp)
    assert _summary_injury_clause(assessment) == "损伤类型为应激性 + 炎症性损伤。"
    assert "损伤类型为应激性 + 炎症性损伤。" in _chapter_summary(assessment, [])


@pytest.mark.parametrize("age", [None, 12])
def test_age_inapplicable_summary_does_not_assert_an_adult_injury_type(age):
    assessment = _assessed("+++")
    assessment["subject"]["age"] = age
    assert "损伤类型为" not in _chapter_summary(assessment, [])


def test_internal_pattern_is_not_renamed_or_trusted_as_a_public_type():
    assessment = _assessed("+-+")
    assessment["barrier"]["type"] = "TEST_ONLY_untrusted_type"
    before = deepcopy(assessment)
    assert _summary_injury_clause(assessment) == "损伤类型为炎症性损伤（伴移位）。"
    assert "TEST_ONLY_untrusted_type" not in _chapter_summary(assessment, [])
    assert assessment == before
