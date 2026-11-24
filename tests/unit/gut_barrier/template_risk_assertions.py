"""Independent current-release assertions shared by HTTP and preview tests."""
from modules.gut_barrier.risk_model import build_disease_risks
from modules.gut_barrier.risk_index_rules import VERSION as RISK_RULE_VERSION


RISK_COLUMNS = ["疾病", "风险指数", "风险等级", "机制关联"]
OVERVIEW_COLUMNS = ["系统", "疾病", "风险指数", "风险等级"]


def assert_template_risk_contract(public, assessment):
    chapter = next(section for section in public["sections"] if section["id"] == "disease_risks")
    tables = [block for block in chapter["blocks"] if block.get("columns") == RISK_COLUMNS]
    rows = [row for table in tables for row in table["rows"]]
    by_name = {row[0]: row for row in rows}
    assert len(rows) == len(by_name)
    catalog, _ = build_disease_risks()
    assert len(catalog) == 23 and len({row["systemCode"] for row in catalog}) == 8
    names = {row["code"]: ("代谢相关脂肪性肝病" if row["code"] == "nafld" else row["name"])
             for row in catalog}
    selected = assessment["diseaseRisks"]
    rule_ledger = assessment.get("riskRuleAssessment") or {}
    current_rules = rule_ledger.get("version") == RISK_RULE_VERSION
    rule_rows = {row["code"]: row for row in rule_ledger.get("items", []) if row["status"] == "evaluated"}
    if current_rules:
        assert len(tables) == 8 and len(rows) == 23
    else:
        names.update({row["code"]: row["name"] for row in selected if row["code"] not in names})
    assert set(by_name) == set(names.values())
    selected_by_code = {row["code"]: row for row in selected}
    for code, name in names.items():
        cells = by_name[name]
        assert len(cells) == 4 and isinstance(cells[3], str) and cells[3]
        source = selected_by_code.get(code)
        if current_rules and code in rule_rows:
            ruled = rule_rows[code]
            assert rule_ledger["kind"] == "rule_based_relative_index" and rule_ledger["validated"] is False
            assert rule_ledger["panelCoverage"] == {"available": 3, "required": 3}
            assert cells[1:3] == [ruled["riskIndex"], ruled["riskLevel"]]
            assert 1 <= cells[1] <= 10 and cells[2] in {"低", "中", "高"}
        elif current_rules:
            assert cells[1:3] == [None, None]
        elif source is not None and source["status"] == "evaluated":
            audit = assessment["riskModel"]
            assert audit["status"] in {"evaluated", "partial"} and audit["modelId"] and audit["modelVersion"]
            assert any(item["code"] == code and item["status"] == "evaluated" for item in audit["items"])
            expected_level = {"low": "低", "medium": "中", "high": "高",
                              "低风险": "低", "中风险": "中", "高风险": "高"}.get(
                                  source["riskLevel"], source["riskLevel"])
            if expected_level not in {"低", "中", "高"}:
                expected_level = None
            assert cells[1:3] == [source["riskIndex"], expected_level]
            assert type(cells[1]) is type(source["riskIndex"])
        elif code in rule_rows:
            ruled = rule_rows[code]
            assert rule_ledger["kind"] == "rule_based_relative_index" and rule_ledger["validated"] is False
            assert rule_ledger["panelCoverage"] == {"available": 3, "required": 3}
            assert cells[1:3] == [ruled["riskIndex"], ruled["riskLevel"]]
            assert 1 <= cells[1] <= 10 and cells[2] in {"低", "中", "高"}
        elif source is None:
            assert cells[1:3] == [None, None]
        else:
            assert source["status"] in {"attention_index", "qualitative_followup"}
            assert source["qualitative"]["evidenceIds"] and source["qualitative"]["basis"]
            assert cells[1:3] == [None, None]
    chart = next(block for block in chapter["blocks"] if block["title"] == "本次疾病风险评估总览")
    assert chart["kind"] == "table" and chart["columns"] == OVERVIEW_COLUMNS
    assert [row[1:] for row in chart["rows"]] == [row[:3] for row in rows if row[1] is not None]
    assert all(row[2] is not None for row in chart["rows"])
    assert not any(block["title"] in {"本次健康关注指数总览", "本次关注指数"} for block in chapter["blocks"])
    return by_name, chart, tables
