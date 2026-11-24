"""Current and historical callback tables retain one disease row and its facts."""
from collections import Counter
from copy import deepcopy
import json

import pytest

from modules.gut_barrier.pipeline import GutBarrierPipeline
from modules.gut_barrier.publication import project_callback_report
from modules.gut_barrier.schemas import GutBarrierRequest
from modules.report import reports
from modules.report.gut_barrier_context import build_gut_barrier_contexts
from modules.report.gut_barrier_defaults import default_section
from modules.report.gut_barrier_normalize import normalize_section


def _source(*, columns=None, rows=None, risk_blocks=()):
    return {"type": "gutBarrier", "status": "success", "userId": "u", "sessionId": "s",
            "finalReportId": "gbr-downstream-contract", "report": {
                "schemaVersion": "gut-barrier-public.v2",
                "meta": {"reportId": "gbr-downstream-contract", "title": "肠道屏障功能评估报告"},
                "sections": [
                    {"id": "overview", "visible": True, "blocks": [{
                        "kind": "table", "title": "1.2 检测项目与结果总览",
                        "columns": columns or [], "rows": rows or [],
                    }]},
                    {"id": "disease_risks", "visible": True, "blocks": list(risk_blocks)},
                ],
            }}


@pytest.mark.parametrize("title,columns", [
    ("本次疾病风险评估总览", ["系统", "疾病", "风险指数", "风险等级"]),
    ("本次健康关注指数总览", ["系统", "疾病", "健康关注指数", "关注等级"]),
])
def test_current_and_historical_overview_is_not_a_second_disease_table(title, columns):
    detail = {"kind": "table", "title": "2.1 消化系统疾病风险评估",
              "columns": ["疾病", "风险指数", "风险等级", "机制关联"],
              "rows": [["肠易激综合征", 7.1, "高", "来源中的机制说明"],
                       ["炎症性肠病", 3.5, "中", "来源中的另一项机制"]]}
    source = _source(risk_blocks=[
        {"kind": "table", "title": title, "columns": columns,
         "rows": [["消化系统", "肠易激综合征", 7.1, "高"],
                  ["消化系统", "炎症性肠病", 3.5, "中"]]}, detail,
    ])
    before = deepcopy(source)
    context = build_gut_barrier_contexts(source)
    assert len(context["part2"]["systems"]) == 1
    assert [r["disease"] for r in context["part2"]["stats"]["assessed_rows"]] == ["肠易激综合征", "炎症性肠病"]
    section = normalize_section(2, default_section(2, context), context)
    assert section["risk_overview"]["stats"] == {"total": 2, "assessed": 2, "unassessed": 0}
    converted = reports._gut_barrier_followup_input(source)
    assert [f["problem"] for f in converted["diagnostic_basis"]["abnormal_findings"]] == ["肠易激综合征", "炎症性肠病"]
    assert len(converted["healthAssessment"]["dimensions"]) == 2  # Barrier plus the actual system.
    assert source == before


def test_similar_overview_title_does_not_remove_a_real_disease_table():
    source = _source(risk_blocks=[{
        "kind": "table", "title": "本次疾病风险评估总览",
        "columns": ["疾病", "风险指数", "风险等级", "机制关联"],
        "rows": [["肠易激综合征", 7.1, "高", "病例中仅有这一张疾病表"]],
    }])
    context = build_gut_barrier_contexts(source)
    assert context["part2"]["stats"]["total"] == 1
    assert context["part2"]["systems"][0]["rows"][0]["disease"] == "肠易激综合征"


@pytest.mark.parametrize("columns,cells", [
    (["项目", "说明"], ["临床说明", "按既有方案复查"]),
    (["疾病", "说明"], ["既往胃炎", "医院来源中的背景记录"]),
    (["项目", "风险等级"], ["资料质量", "高"]),
    (["项目", "风险指数"], ["其他量表", 9]),
])
def test_supporting_tables_remain_extra_content_without_becoming_disease_rows(columns, cells):
    supporting = {"kind": "table", "title": "临床说明与安全记录", "columns": columns, "rows": [cells]}
    source = _source(risk_blocks=[
        {"kind": "table", "title": "消化系统疾病风险评估",
         "columns": ["疾病", "风险指数", "风险等级", "机制关联"],
         "rows": [["肠易激综合征", 7.1, "高", "来源机制"]]}, supporting,
    ])
    before = deepcopy(source)
    context = build_gut_barrier_contexts(source)
    assert len(context["part2"]["systems"]) == 1
    assert context["part2"]["stats"]["total"] == 1
    assert context["part2"]["extra_blocks"] == [{
        **supporting, "rows": [dict(zip(columns, map(str, cells)))],
    }]
    section = normalize_section(2, default_section(2, context), context)
    assert section["risk_overview"]["stats"] == {"total": 1, "assessed": 1, "unassessed": 0}
    converted = reports._gut_barrier_followup_input(source)
    assert [f["problem"] for f in converted["diagnostic_basis"]["abnormal_findings"]] == ["肠易激综合征"]
    assert source == before


@pytest.mark.parametrize("columns,cells", [
    (["风险等级", "机制关联", "疾病", "风险指数"], ["高", "来源机制", "肠易激综合征", 7.1]),
    (["风险指数", "疾病", "风险等级", "机制关联"], [7.1, "肠易激综合征", "高", "来源机制"]),
])
def test_reordered_risk_columns_keep_disease_name_and_values_bound_to_named_columns(columns, cells):
    source = _source(risk_blocks=[{
        "kind": "table", "title": "消化系统疾病风险评估", "columns": columns, "rows": [cells],
    }])
    before = deepcopy(source)
    context = build_gut_barrier_contexts(source)
    assert context["part2"]["systems"][0]["rows"] == [{
        "disease": "肠易激综合征", "risk_index": "7.1", "risk_level": "高", "mechanism": "来源机制",
    }]
    converted = reports._gut_barrier_followup_input(source)
    finding = converted["diagnostic_basis"]["abnormal_findings"][0]
    assert finding["problem"] == "肠易激综合征"
    assert [item["value"] for item in finding["indicators"]] == ["7.1", "高"]
    assert source == before


@pytest.mark.parametrize("layout", ["current_six_columns", "historical_four_columns"])
@pytest.mark.parametrize("reference", ["＜8.5 U/L", "0–8.5 U/L", ""])
def test_reference_range_uses_named_current_or_historical_column(layout, reference):
    if layout == "current_six_columns":
        columns = ["检测项目", "检测指标", "检测结果", "参考区间", "结果评价", "反映维度"]
        rows = [["肠道屏障功能", "二胺氧化酶（DAO）", "20.01 U/L", reference, "DAO指标异常", "肠上皮细胞损伤"]]
    else:
        columns = ["检测指标", "检测结果", "参考范围", "结果评价"]
        rows = [["二胺氧化酶（DAO）", "20.01 U/L", reference, "DAO指标异常"]]
    source = _source(columns=columns, rows=rows)
    before = deepcopy(source)
    context = build_gut_barrier_contexts(source)
    model = default_section(1, context)
    model["indicator_talk"]["items"][0]["reference"] = "模型误写999 U/L"
    normalized = normalize_section(1, model, context)
    item = normalized["indicator_talk"]["items"][0]
    assert item["reference"] == reference
    assert item["result"] == "20.01 U/L"
    converted = reports._gut_barrier_followup_input(source)
    evidence = converted["healthAssessment"]["dimensions"][0]["evidenceUsed"][0]
    assert evidence["referenceRange"] == reference
    assert evidence["value"] == "20.01 U/L"
    assert source == before


@pytest.mark.parametrize("reference_columns,reference_cells", [
    ([], []),
    (["其他信息"], ["不能作为参考区间"]),
    (["参考区间", "参考范围"], ["", "不应替换显式空的新列"]),
])
def test_absent_or_explicitly_empty_reference_column_never_uses_a_measurement(reference_columns, reference_cells):
    source = _source(
        columns=["检测项目", "检测指标", "检测结果", *reference_columns, "结果评价"],
        rows=[["肠道屏障功能", "二胺氧化酶（DAO）", "20.01 U/L", *reference_cells, "DAO指标异常"]],
    )
    context = build_gut_barrier_contexts(source)
    section = normalize_section(1, default_section(1, context), context)
    assert section["indicator_talk"]["items"][0]["reference"] == ""
    converted = reports._gut_barrier_followup_input(source)
    evidence = converted["healthAssessment"]["dimensions"][0]["evidenceUsed"][0]
    assert evidence["referenceRange"] == ""
    assert evidence["value"] == "20.01 U/L"


async def _current_callback(*, hospital_data=()):
    request = GutBarrierRequest.model_validate({
        "userId": "u", "sessionId": "s", "name": "合成下游回归样本", "age": 42, "sex": "男",
        "gutHealthIndicator": {
            "dLacticAcid": {"value": 10.83, "unit": "mg/L"},
            "diamineOxidase": {"value": 20.01, "unit": "U/L"},
            "bacterialEndotoxin": {"value": 22.98, "unit": "U/L"},
        },
        "hospitalData": list(hospital_data),
    })
    document, _ = await GutBarrierPipeline().generate(
        request, report_id="gbr-downstream-contract", evaluation_date="2026-10-10")
    return {"type": "gutBarrier", "schemaVersion": "2.0", "finalReportId": "gbr-downstream-contract",
            "userId": "u", "sessionId": "s", "status": "partial",
            "report": project_callback_report(document), "error": None}


@pytest.mark.asyncio
@pytest.mark.parametrize("hospital_data", [
    [],
    [{"dataType": "EXAM", "checkTime": "2024-05-09", "itemName": "CT全腹平扫+增强",
      "examResult": "结肠肝曲见结节样强化灶，考虑间质瘤可能。双肾见小结石。"},
     {"dataType": "EXAM", "checkTime": "2024-05-09", "itemName": "胃镜",
      "examResult": "慢性非萎缩性胃炎。"}],
], ids=["fixed_catalog_only", "historical_followups_outside_catalog"])
async def test_current_callback_routes_23_diseases_and_correct_ranges_to_all_three_followups(
    monkeypatch, hospital_data,
):
    source = await _current_callback(hospital_data=hospital_data)
    if hospital_data:
        chapter = next(s for s in source["report"]["sections"] if s["id"] == "disease_risks")
        chapter["blocks"].append({"kind": "table", "title": "临床说明与安全记录",
                                  "columns": ["项目", "说明"], "rows": [["既往检查", "保留资料供临床复查参考"]]})
    before = deepcopy(source)
    overview = next(b for s in source["report"]["sections"] if s["id"] == "disease_risks"
                    for b in s["blocks"] if b.get("title") == "本次疾病风险评估总览")
    expected = Counter((r[1], float(r[2]), r[3]) for r in overview["rows"])
    assert len(expected) == 23
    details = [b for s in source["report"]["sections"] if s["id"] == "disease_risks" for b in s["blocks"]
               if b.get("columns") == ["疾病", "风险指数", "风险等级", "机制关联"]]
    assert len(details) == 8
    assert Counter((r[0], float(r[1]), r[2]) for table in details for r in table["rows"]) == expected
    assert all("复核" not in r[0] and "随访" not in r[0] for table in details for r in table["rows"])
    captured = {}

    async def model(prompt, *, task_type):
        if task_type == "gut_barrier_opening":
            return json.dumps({"opening_script": {"self_intro": "我们一起核对这份报告。"}}, ensure_ascii=False)
        return json.dumps({"script": {"opening": "按本次检测结果逐项说明。"}}, ensure_ascii=False)

    async def opinion(data, user_id, session_id):
        captured["opinion"] = deepcopy(data)
        return {"type": "opinion"}

    async def generate(report_type, factory, **kwargs):
        return await factory(), "success"

    async def instruction(data, user_id, session_id, *, policy, spawn):
        captured["instruction"] = deepcopy(data)

        async def result():
            return {"type": "instruction"}, "success"

        return spawn(result()), {}

    async def publish(payloads, *args, **kwargs):
        return payloads

    monkeypatch.setattr(reports, "safe_complete_async", model)
    monkeypatch.setattr(reports, "opinion_report", opinion)
    # Keep actual dispatch and interpretation normalization; isolate only
    # model-independent orchestration tests from persistence and delivery.
    monkeypatch.setattr(reports, "_generate_followup_module", generate)
    monkeypatch.setattr(reports, "_spawn_instruction_chain", instruction)
    monkeypatch.setattr(reports, "_publish_followups", publish)
    payloads = await reports._push_report(source, "u", "s")

    manual = next(p for p in payloads if p["type"] == "explain_gutBarrier")
    assert manual["sections"][1]["risk_overview"]["stats"] == {"total": 23, "assessed": 23, "unassessed": 0}
    ranges = ["＜8.5 U/L", "＜18 U/L", "＜14 mg/L"]
    assert [r["reference"] for r in manual["sections"][0]["indicator_talk"]["items"]] == ranges
    for kind in ("opinion", "instruction"):
        data = captured[kind]
        findings = [f for f in data["diagnostic_basis"]["abnormal_findings"] if f["item_id"].startswith("gb-risk-")]
        actual = Counter((f["problem"], float(f["indicators"][0]["value"]), f["indicators"][1]["value"]) for f in findings)
        assert actual == expected
        assert len(data["healthAssessment"]["dimensions"]) == 9  # Barrier plus eight systems.
        assert [r["referenceRange"] for r in data["healthAssessment"]["dimensions"][0]["evidenceUsed"]] == ranges
    assert source == before
