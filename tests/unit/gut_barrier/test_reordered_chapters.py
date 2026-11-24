import json
import re

import pytest

from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.interventions import build_interventions
from modules.gut_barrier.presentation import build_report
from modules.gut_barrier.publication import project_public_report
from modules.gut_barrier.schemas import GutBarrierRequest


HEADINGS = {
    "overview": ["1.1 检测综合评估结论", "1.2 检测指标分项阐述", "1.3 肠道屏障功能评分（IBFC）与损伤分级",
                 "1.4 肠道屏障受损类型与受损状况"],
    "disease_risks": ["2.1 消化系统疾病风险评估", "2.2 代谢系统疾病风险评估", "2.3 心脑血管系统疾病风险评估",
                      "2.4 精神系统疾病风险评估", "2.5 神经系统疾病风险评估", "2.6 免疫系统疾病风险评估", "2.7 肿瘤风险评估"],
    "repair_plan": ["3.1 肠道屏障修复 5 原则总览", "3.2 第一原则 · 去除：健康评估与诱因去除",
                    "3.3 第二原则 · 改善：肠道营养补充", "3.4 第三原则 · 重建：精准肠道菌群移植（PFMT）",
                    "3.5 第四原则 · 修复：MDF 靶向功能菌群调节", "3.6 第五原则 · 平衡：生活方式干预"],
    "education": ["4.1 肠道屏障：一堵“会呼吸”的四重城墙", "4.2 城墙失守：肠道屏障受损的发生与发展",
                  "4.3 慢火点燃：慢性低度炎症的发生机制", "4.4 互为因果：屏障受损与慢性炎症的放大环路",
                  "4.5 慢性疾病的“共同土壤”", "4.6 炎症性衰老：当慢火遇上时间"],
    "appendix": ["5.1 质量控制校验", "5.2 检测方法学介绍", "5.3 风险指数标尺说明", "5.4 免责声明", "5.5 参考文献"],
}


def report_for(pattern="+++", *, context=None, observations=None):
    request = GutBarrierRequest.model_validate({
        "userId": "directory-test", "sessionId": "directory-session", "name": "目录验收样本", "age": 50,
        "gutHealthIndicator": {
            key: {"value": high if flag == "+" else 5, "unit": unit}
            for key, high, flag, unit in zip(
                ("dLacticAcid", "diamineOxidase", "bacterialEndotoxin"), (26, 17, 23), pattern, ("mg/L", "U/L", "U/L"))},
    })
    assessed = assess_case(request, observations=observations, context={"currentDate": "2026-09-24", **(context or {})})
    return build_report(assessed, build_interventions(assessed), report_id="gbr_directory_test")


def test_exact_user_directory_and_subsection_order_no_old_extra_numbered_titles():
    public = project_public_report(report_for())
    assert [s["id"] for s in public["sections"]] == list(HEADINGS)
    for section in public["sections"]:
        actual = [b["title"] for b in section["blocks"] if re.match(r"^\d+\.\d+\s", b.get("title") or "")]
        assert actual == HEADINGS[section["id"]]
    assert "第三章所列机制" not in json.dumps(public, ensure_ascii=False)


@pytest.mark.parametrize("pattern", ["+--", "-+-", "--+", "++-", "+-+", "-++", "+++", "---"])
def test_first_chapter_retains_patient_pattern_and_third_chapter_is_generic(pattern):
    public = project_public_report(report_for(pattern))
    overview = public["sections"][0]
    actual = next(b for b in overview["blocks"] if b.get("title") == HEADINGS["overview"][3])
    assert actual["text"] and ("本次" in actual["text"] or "当前" in actual["text"])
    fixed = public["sections"][2]
    assert fixed == project_public_report(report_for("---"))["sections"][2]
    assert "目录验收样本" not in json.dumps(fixed, ensure_ascii=False)
    assert "catalogUsage" not in json.dumps(public)


def test_current_method_and_lab_range_remain_internal_after_auxiliary_blocks_removed():
    observation = {"code": "DAO", "value": 17, "unit": "U/L", "date": "2026-09-24",
                   "method": "实验室实际酶学方法", "referenceRange": "本实验室参考：0–10 U/L",
                   "source": {"kind": "observations", "internalOnly": "SECRET"}}
    report = report_for(observations=[observation])
    public = project_public_report(report)
    marker = next(item for item in report["assessment"]["markers"] if item["code"] == "DAO")
    assert marker["method"] == "实验室实际酶学方法"
    assert [item["value"] for item in marker["laboratoryReferenceRanges"]] == ["本实验室参考：0–10 U/L"]
    assert not any(b.get("title") == "本次检测方法与参考信息" for s in public["sections"] for b in s["blocks"])
    assert "SECRET" not in json.dumps(public, ensure_ascii=False)
    assert "实验室实际酶学方法" not in json.dumps(public, ensure_ascii=False)


def test_existing_public_field_contract_is_not_expanded_by_directory_or_figures():
    public = project_public_report(report_for())
    assert set(public) == {"schemaVersion", "meta", "sections"}
    assert set(public["meta"]) == {"title", "reportId"}
    assert all(set(s) == {"id", "title", "subtitle", "blocks", "visible"} for s in public["sections"])
    assert all(b["kind"] in {"facts", "cards", "table", "list", "bands", "references", "callout", "paragraph"}
               for s in public["sections"] for b in s["blocks"])


def test_confirmed_nonhospital_safety_context_is_not_lost_when_repairs_are_fixed():
    public = project_public_report(report_for(context={
        "allergies": "鱼类过敏", "liverAndKidneyFunction": "肾功能不全", "immuneStatus": "免疫抑制",
        "acuteInfection": True,
    }))
    overview = json.dumps(public["sections"][0], ensure_ascii=False)
    assert "鱼类/海鲜过敏" in overview
    assert "肝肾功能" in overview
    assert "免疫状态" in overview
    assert "急性感染" in overview
    assert public["sections"][2] == project_public_report(report_for())["sections"][2]
    assert "见上表" not in overview
