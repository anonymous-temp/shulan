"""Chapter synthesis states supported facts without missing-data boilerplate."""
from copy import deepcopy
import re

import pytest

from modules.gut_barrier.dynamic_diseases import apply_dynamic_assessment
from modules.gut_barrier.risk_index_rules import apply_risk_index_rules
from modules.gut_barrier.template_risk_review import (
    _chapter_summary, _risk_summary, apply_template_risk_review,
)
from tests.unit.gut_barrier.test_template_risk_review import (
    _chapter, _clinical_assessment, _overview, _publish, _report, _rows,
)


_PROHIBITED = (
    "最低风险", "临床资料待补充", "资料不足", "资料缺失", "资料不全", "缺项",
    "待补充", "待评估", "不能评定", "无法评定", "尚不能", "不能确定",
    "无法确定", "尚不足以确定", "不用于确认", "不提示高风险", "均为低风险",
    "年龄适用的参考范围", "不能比较具体慢病", "无法比较具体慢病",
)
_MECHANISM = "促进全身性慢性低度炎症和氧化应激，并与相关慢病风险增加相联系。"
_ENDING = "修复肠道屏障功能，恢复肠道菌群稳态。"


def _summary(report):
    blocks = [block for block in _chapter(report)["blocks"]
              if (block.get("title") or "").endswith("本章小结")]
    assert len(blocks) == 1
    block = blocks[0]
    assert set(block) == {"kind", "title", "text"}
    assert block["kind"] == "paragraph"
    text = block["text"]
    assert text.count(_MECHANISM) == 1
    assert text.endswith(_ENDING)
    assert len(text.split("\n\n")) == 2
    assert all(text.split("\n\n"))
    assert not any(phrase in text for phrase in _PROHIBITED)
    return text


@pytest.mark.parametrize("panel", [
    (1, 1, 1, .3),  # Full, normal panel: the real low scores remain visible.
    (1, 1, 1, 3),  # Zero IBFC with inflammatory exception.
    (20.88, .5, 2.4, 2.35),
    (24, 18, 30, 3),
    (36, 22, 36, 3),
])
def test_complete_panels_keep_actual_scores_without_global_risk_disclaimers(panel):
    assessed = apply_risk_index_rules(apply_dynamic_assessment(_clinical_assessment(panel)))
    before = deepcopy(assessed)
    report = _publish(assessed)
    text = _summary(report)
    scored = [row for row in _rows(report) if row[1] is not None]
    assert len(scored) == 23
    ranked = sorted(scored, key=lambda row: (-{"低": 0, "中": 1, "高": 2}[row[2]], -row[1]))[:3]
    listed = re.findall(r"([^，。、；]+)（([\d.]+)分，([低中高])风险）",
                        text.split("需重点留意的慢病风险包括", 1)[1])
    assert listed == [(row[0], f"{row[1]:g}", row[2]) for row in ranked]
    assert assessed == before


@pytest.mark.parametrize("level,score", [("低", 2.4), ("中", 4.2), ("高", 7.9)])
@pytest.mark.parametrize("completeness", ["complete", "incomplete"])
def test_risk_summary_lists_real_grade_without_completeness_boilerplate(level, score, completeness):
    assessment = {"riskRuleAssessment": {"clinicalCompleteness": {
        "status": completeness, "missingDomains": ["lipids"], "basicAttention": True,
    }}}
    before = deepcopy(assessment)
    text = _risk_summary([("rule", ["合成疾病", score, level])], assessment)
    assert f"合成疾病（{score:g}分，{level}风险）" in text
    assert not any(phrase in text for phrase in _PROHIBITED)
    assert assessment == before


@pytest.mark.parametrize("answers", [
    {},
    {"diet": {"status": "unknown"}},
    {"bowelMovement": {"status": "known", "value": "排便规律"},
     "sleep": {"status": "unknown"}},
])
def test_auxiliary_questionnaire_gaps_do_not_leak_into_synthesis(answers):
    assessed = _clinical_assessment((1, 1, 1, .3))
    assessed["context"]["healthSurvey"] = {"source": "healthSurvey", "answers": answers}
    assessed = apply_risk_index_rules(apply_dynamic_assessment(assessed))
    assert assessed["riskRuleAssessment"]["clinicalCompleteness"]["status"] == "incomplete"
    before = deepcopy(assessed)
    report = _publish(assessed)
    text = _summary(report)
    assert "健康问卷" not in text
    assert len(_overview(report)["rows"]) == 23
    assert assessed == before


@pytest.mark.parametrize("panel", [
    None,
    (None, None, None, None),
    (20, 12, 23, None),
    (None, 12, 23, .3),
    (20, None, 23, .3),
    (20, 12, None, .3),
    (None, None, None, 3),
])
def test_missing_measurements_remain_unscored_without_summary_missing_data_notice(panel):
    assessed = apply_risk_index_rules(apply_dynamic_assessment(_clinical_assessment(panel)))
    before = deepcopy(assessed)
    report = _publish(assessed)
    text = _summary(report)
    if panel == (20, 12, 23, None):
        assert len(_overview(report)["rows"]) == 23
        assert "需重点留意的慢病风险包括" in text
    else:
        assert _overview(report)["rows"] == []
        assert all(row[1:3] == [None, None] for row in _rows(report))
        assert "需重点留意的慢病风险包括" not in text
        assert not re.search(r"（[\d.]+分，[低中高]风险）", text)
    assert assessed == before


@pytest.mark.parametrize("age", [None, 12])
def test_unknown_age_or_child_does_not_receive_invented_adult_assessment(age):
    assessed = _clinical_assessment((20, 12, 23, 3))
    assessed["subject"]["age"] = age
    before = deepcopy(assessed)
    report = _publish(assessed)
    text = _summary(report)
    assert "损伤等级为" not in text
    assert "损伤类型为" not in text
    assert "IBFC为" not in text
    assert all(row[1:3] == [None, None] for row in _rows(report))
    assert assessed == before


@pytest.mark.parametrize("candidates", [
    [],
    [("rule", ["合成疾病", None, None])],
    [("model", ["合成疾病", 9, "高"])],  # No declared cross-disease model scale.
])
def test_no_comparable_candidates_does_not_create_risk_or_replacement_disclaimer(candidates):
    assert _risk_summary(candidates, {}) == ""
    text = _chapter_summary({}, candidates)
    assert text.split("\n\n")[1] == _ENDING
    assert _MECHANISM in text
    assert "损伤等级为" not in text and "损伤类型为" not in text
    assert "需重点留意的慢病风险包括" not in text
    assert not any(phrase in text for phrase in _PROHIBITED)


@pytest.mark.parametrize("old_text", [
    "最低风险，临床资料待补充。",
    "现有资料不足，尚不能比较具体慢病风险高低。",
    "检查资料缺失，无法评定风险等级。",
    "临床资料不全，不能确定肠道屏障损伤等级。",
    "本次所选项目均为低风险，不提示高风险。",
    "存在缺项，需完善临床检查资料后再评估。",
    "本次结果尚不足以确定疾病风险，待评估。",
])
def test_regeneration_replaces_old_synonymous_summary_but_preserves_other_blocks(old_text):
    retained = {"kind": "callout", "title": "便血就医提示", "text": "出现便血应及时就医。", "tone": "caution"}
    report = _report(blocks=[retained])
    _chapter(report)["blocks"][-1]["text"] = old_text
    wire_keys = set(report)
    apply_template_risk_review(report, {})
    text = _summary(report)
    assert old_text not in text
    assert retained in _chapter(report)["blocks"]
    assert set(report) == wire_keys
    assert all(row[1:3] == [None, None] for row in _rows(report))
    before = deepcopy(report)
    apply_template_risk_review(report, {})
    assert report == before


def test_isolated_lps_keeps_combination_type_without_confirmation_caveat():
    assessed = _clinical_assessment((1, 1, 23, .3))
    before = deepcopy(assessed)
    text = _summary(_publish(assessed))
    assert "损伤类型为非肠源性移位（指标组合分型）。" in text
    assert "不用于确认内毒素来源" not in text
    assert assessed == before


@pytest.mark.parametrize("flag", ["acuteInfection", "acuteInflammation"])
def test_known_acute_background_keeps_real_safety_advice(flag):
    assessed = _clinical_assessment((20, 12, 23, 3))
    assessed["context"][flag] = True
    before = deepcopy(assessed)
    text = _summary(_publish(assessed))
    assert "已有急性感染或炎症背景，应先处理当前临床问题。" in text
    assert "提示需关注肠源性慢性低度炎症相关因素" not in text
    assert assessed == before
