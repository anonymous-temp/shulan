"""Chapter synthesis hides hs-CRP prose after CRP retirement without changing barrier findings."""
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
from tests.unit.gut_barrier.test_template_risk_review import _clinical_assessment, _publish


_REMOVED_DISCLAIMER = "本次炎症结果需结合当前症状及临床情况评估，不能据此确定慢性炎症"
_FINAL_SENTENCE = "修复肠道屏障功能，恢复肠道菌群稳态。"


def _review(raw, *, acute_flag=None):
    assessment = _clinical_assessment((26, 17, 23, raw))
    if acute_flag is not None:
        assessment["context"][acute_flag] = True
    assessment = apply_risk_index_rules(apply_dynamic_assessment(assessment))
    before = deepcopy(assessment)
    callback = _publish(assessment)
    summary = next(block for section in callback["sections"] if section["id"] == "disease_risks"
                   for block in section["blocks"] if (block.get("title") or "").endswith("本章小结"))
    assert set(summary) == {"kind", "title", "text"}
    assert summary["kind"] == "paragraph" and summary["title"] == "2.9 本章小结"
    paragraphs = summary["text"].split("\n\n")
    assert len(paragraphs) == 2 and all(paragraphs)
    assert all("\n" not in paragraph for paragraph in paragraphs)
    assert _REMOVED_DISCLAIMER not in summary["text"]
    assert "crp" not in summary["text"].casefold()
    assert "C反应蛋白" not in summary["text"]
    assert "炎症相关因素" not in summary["text"]
    assert paragraphs[-1].endswith(_FINAL_SENTENCE)
    assert "内毒素等物质可能进入血液循环" in paragraphs[-1]
    assert "促进全身性慢性低度炎症和氧化应激" in paragraphs[-1]

    # Publication is a read-only view, not a chance to replace a range by an
    # exact measurement or to change the already calculated risk ledger.
    assert assessment == before
    assert "inflammation" not in assessment

    frozen = deepcopy(callback)
    html = render_report_html(callback, template_version=LATEST_DISPLAY_TEMPLATE)
    body = re.search(r'<div class="block" id="disease_risks-2-9">(.*?)</div>', html, re.S)[1]
    assert body.count(f'<h3>{summary["title"]}</h3>') == 1
    assert [unescape(text) for text in re.findall(r"<p>(.*?)</p>", body, re.S)] == paragraphs
    assert json.loads(re.search(r'id="report-data">(.*?)</script>', html, re.S)[1]) == frozen
    assert callback == frozen == project_callback_report(callback)
    assert assessment == before
    return assessment, callback, paragraphs


@pytest.mark.parametrize("raw", [">3.0", ">=3", "≥3", ">10"])
def test_open_high_range_hides_summary_crp_but_retains_confirmed_project_label(raw):
    assessment, _, paragraphs = _review(raw)
    assert "提示存在炎症相关因素" not in paragraphs[0]
    assert "肠源性" not in paragraphs[0] and "慢性" not in paragraphs[0]
    assert "inflammation" not in assessment


@pytest.mark.parametrize("raw", [12, ">1"])
def test_elevated_result_without_single_eligible_label_does_not_add_crp_summary(raw):
    _, _, paragraphs = _review(raw)
    assert "提示存在炎症相关因素" not in paragraphs[0]
    assert "肠源性" not in paragraphs[0] and "慢性" not in paragraphs[0]
    assert "中度升高" not in paragraphs[0] and "显著升高" not in paragraphs[0]


@pytest.mark.parametrize("raw", [.6, 1, 2.4, 3, 10])
def test_exact_elevated_result_hides_crp_summary_but_preserves_measurement(raw):
    assessment, callback, paragraphs = _review(raw)
    assert "提示需关注肠源性慢性低度炎症相关因素" not in paragraphs[0]
    assert "慢性炎症已确诊" not in paragraphs[0]
    assert "inflammation" not in assessment
    overview = next(section for section in callback["sections"] if section["id"] == "overview")
    assert "hs-CRP" not in json.dumps(overview, ensure_ascii=False)
    assert len(assessment["riskRuleAssessment"]["items"]) >= 22


@pytest.mark.parametrize("raw", [.3, .5, None])
def test_normal_or_missing_inflammation_does_not_create_elevated_result(raw):
    _, _, paragraphs = _review(raw)
    assert "hs-CRP" not in paragraphs[0]
    assert "升高" not in paragraphs[0] and "炎症相关因素" not in paragraphs[0]
    assert "肠源性慢性低度炎症" not in paragraphs[0]


@pytest.mark.parametrize("raw", ["<20", "<=20"])
def test_range_spanning_normal_and_high_is_not_declared_elevated(raw):
    assessment, _, paragraphs = _review(raw)
    assert "范围型结果" not in paragraphs[0] and "具体数值待定量确认" not in paragraphs[0]
    assert "hs-CRP升高" not in paragraphs[0] and "提示存在炎症" not in paragraphs[0]
    assert "肠源性" not in paragraphs[0] and "慢性" not in paragraphs[0]
    assert "inflammation" not in assessment


@pytest.mark.parametrize("acute_flag", ["acuteInfection", "acuteInflammation"])
def test_known_acute_context_keeps_existing_safety_advice_and_blocks_chronic_attribution(acute_flag):
    assessment, callback, paragraphs = _review(">3.0", acute_flag=acute_flag)
    assert "已有急性感染或炎症背景，应先处理当前临床问题。" in paragraphs[0]
    assert "再评估慢病风险" not in paragraphs[0]
    assert "肠源性" not in paragraphs[0] and "显著升高" not in paragraphs[0]
    assert assessment["context"][acute_flag] is True
    # The dedicated first-chapter inflammation advice is still published;
    # removing one repetitive summary sentence must not erase clinical holds.
    overview = next(section for section in callback["sections"] if section["id"] == "overview")
    assert "急性感染或炎症背景" in json.dumps(overview, ensure_ascii=False)
