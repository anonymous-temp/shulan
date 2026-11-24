from copy import deepcopy

import asyncio

import json
import re

import pytest

from modules.gut_barrier.assessment import assess_case

from modules.gut_barrier.disease_assessment import apply_qualitative_assessment
from modules.gut_barrier.dynamic_diseases import evidence_queries

from modules.gut_barrier.dynamic_diseases import evidence_queries as dynamic_queries

from modules.gut_barrier.pipeline import GutBarrierPipeline

from modules.gut_barrier.schemas import GutBarrierRequest

from modules.gut_barrier.publication import project_callback_report

from adapters.gut_barrier.rendering import render_report_html
from modules.gut_barrier.display_versions import LATEST_DISPLAY_TEMPLATE

def request(**overrides):
    data = {"userId": "private-user", "sessionId": "private-session", "age": 26, "sex": "女", "name": "私人姓名",
            "healthSurvey": {"heightCm": "160", "weightKg": "90", "bowelMovement": "时常腹泻"},
            "gutHealthIndicator": {"bacterialEndotoxin": {"value": "12.5", "unit": "U/L"},
                "diamineOxidase": {"value": "3.8", "unit": "U/L"}, "dLacticAcid": {"value": "0.06", "unit": "mg/L"},
                "hsCRP": {"value": ">3.0", "unit": "mg/L"}}}
    return GutBarrierRequest.model_validate({**data, **overrides})


def clinical_request():
    return request(hospitalData=[{
        "dataType": "EXAM", "itemName": "胃镜", "checkTime": "2025-12-18",
        "examResult": "慢性非萎缩性胃炎",
    }])

def assess(**kwargs):
    return assess_case(request(**kwargs), context={"currentDate": "2026-09-24"})

def rows(assessment):
    return {row["code"]: row for row in assessment["diseaseRisks"]}

@pytest.mark.asyncio
async def test_pipeline_callback_and_html_have_case_evidence_but_no_internal_search_trace():
    class Evidence:
        async def retrieve(self, terms):
            assert terms == dynamic_queries(assess_case(clinical_request(), context={"currentDate": "2026-09-24"}))
            return {"queries": terms, "candidates": [{"title": "PRIVATE_RETRIEVAL"}], "issues": []}
    report, audit = await GutBarrierPipeline(evidence=Evidence()).generate(clinical_request(), report_id="gbr_case", evaluation_date="2026-09-24")
    reference_audit = {}
    callback = project_callback_report(report, reference_audit=reference_audit)
    html = render_report_html(callback, template_version=LATEST_DISPLAY_TEMPLATE)
    assert project_callback_report(callback) == callback
    assert "PRIVATE_RETRIEVAL" not in json.dumps(callback)
    assert "PRIVATE_RETRIEVAL" in json.dumps(audit)
    assert "gastritis_followup" in {row["code"] for row in report["assessment"]["diseaseRisks"]}
    assert "既往胃炎随访" not in json.dumps(callback, ensure_ascii=False)
    assert "既往胃炎随访" not in html
    visible_row = next(row for section in callback["sections"] for block in section["blocks"]
                       for row in block.get("rows", []) if row[0] == "肠易激综合征")
    from modules.gut_barrier.risk_index_rules import confirmed_rule_result
    assert confirmed_rule_result(report["assessment"], "gastritis_followup") is None
    assert visible_row[0] in html
    assert "[评估" not in html and 'href="#reference-primary-' in html
    # The selected bibliography may omit this row's display number while
    # retaining its original source association in the private ledger.
    citation_free = lambda text: re.sub(r"\[\d+(?:,\d+)*\]", "", text).strip()
    claim = next(item for item in reference_audit["referencePublication"]["claims"]
                 if item.get("cellIndex") == 3
                 and citation_free(item["text"]) == citation_free(visible_row[3]))
    assert claim["sourceRoots"] and re.search(r"\[\d+\]", claim["text"])
    assert [int(n) for group in re.findall(r"\[([\d,]+)\]", visible_row[3])
            for n in group.split(",")] == claim["selectedBodyNumbers"]
    assert "35.15625" not in html
    assert "时常腹泻" not in html  # raw survey answers remain internal
    assert "健康问卷背景（自述）" not in html
    table = next(b for s in callback["sections"] for b in s["blocks"] if b["title"] == "2.2 代谢系统疾病风险评估")
    assert table["columns"] == ["疾病", "风险指数", "风险等级", "机制关联"]
    from tests.unit.gut_barrier.template_risk_assertions import assert_template_risk_contract
    assert_template_risk_contract(callback, report["assessment"])
    assert all(1 <= row[1] <= 10 and row[2] in {"低", "中", "高"} and row[3]
               for row in table["rows"] if row[1] is not None)
    introduction = next(s for s in callback["sections"] if s["id"] == "disease_risks")["blocks"][0]
    assert "风险等级（低、中、高）" in introduction["text"]
    assert introduction["text"] in html
    refs = next(b for s in callback["sections"] if s["id"] == "appendix"
                for b in s["blocks"] if b["kind"] == "references")["items"]
    assert all(set(ref) == {"citation", "url", "year", "publisher", "doi", "location"} for ref in refs)
