import json

import re

import pytest

from adapters.gut_barrier.rendering import render_report_html

from modules.gut_barrier.assessment import assess_case

from modules.gut_barrier.interventions import build_interventions

from modules.gut_barrier.presentation import build_report

from modules.gut_barrier.publication import project_public_report

from modules.gut_barrier.schemas import GutBarrierRequest

HEADINGS = {
    "overview": ["1.1 检测综合评估结论", "1.2 检测指标分项阐述", "1.3 肠道屏障功能评分（IBFC）与损伤分级",
                 "1.4 肠道屏障受损类型与受损状况", "1.5 慢性低度炎症评估"],
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

def test_existing_public_field_contract_is_not_expanded_by_directory_or_figures():
    public = project_public_report(report_for())
    assert set(public) == {"schemaVersion", "meta", "sections"}
    assert set(public["meta"]) == {"title", "reportId"}
    assert all(set(s) == {"id", "title", "subtitle", "blocks", "visible"} for s in public["sections"])
    assert all(b["kind"] in {"facts", "cards", "table", "list", "bands", "references", "callout", "paragraph"}
               for s in public["sections"] for b in s["blocks"])
    body = render_report_html(public)
    data = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', body, re.S)
    assert json.loads(data.group(1)) == public
