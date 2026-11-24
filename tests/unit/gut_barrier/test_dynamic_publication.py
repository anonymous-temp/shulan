"""The v2 callback carries patient results, not front-end-owned fixed copy."""

from copy import deepcopy
import json

import pytest

from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.display_versions import CANONICAL_DISPLAY_TEMPLATE
from modules.gut_barrier.interventions import build_interventions
from modules.gut_barrier.integration_content import DISEASE_INTRODUCTION
from modules.gut_barrier.presentation import build_report
from modules.gut_barrier.publication import project_callback_report, project_public_report
from modules.gut_barrier.schemas import GutBarrierRequest


PATTERNS = ("+--", "-+-", "--+", "++-", "+-+", "-++", "+++", "---")
REFERENCE_KEYS = {"citation", "url", "year", "publisher", "doi", "location"}
SECTION_KEYS = {"id", "title", "subtitle", "blocks", "visible"}
RISK_COLUMNS = ["疾病", "风险指数", "风险等级", "机制关联"]
QC_COLUMNS = ["校验项目", "本次记录", "校验项目", "本次记录"]
PRIVATE_KEYS = {
    "assessment", "interventions", "subject", "sourceSummary", "rawData",
    "internalOnly", "audit", "sourcePath", "localPath", "signedUrl", "secret",
}


def clinical_report(pattern="+++", *, age=35, crp=2.7, missing=False, context=None):
    payload = {"userId": "dynamic-user", "sessionId": "dynamic-session", "name": "动态回调样本",
               "age": age, "sex": "男"}
    if not missing:
        payload["gutHealthIndicator"] = {
            key: {"value": high if sign == "+" else low, "unit": unit}
            for key, high, low, unit, sign in zip(
                ("dLacticAcid", "diamineOxidase", "bacterialEndotoxin"),
                (26, 17, 23), (5, 5, 10), ("mg/L", "U/L", "U/L"), pattern)
        }
        payload["gutHealthIndicator"]["hsCRP"] = {"value": crp, "unit": "mg/L"}
    assessed = assess_case(GutBarrierRequest.model_validate(payload),
                          context={"currentDate": "2026-09-24", **(context or {})})
    plans = build_interventions(assessed, context=assessed.get("context", {}))
    return build_report(assessed, plans, report_id="gbr_dynamic_test")


def section(report, identifier):
    return next(item for item in report["sections"] if item["id"] == identifier)


def block(report, identifier, title):
    return next(item for item in section(report, identifier)["blocks"] if item["title"] == title)


def references(report):
    return block(report, "appendix", "5.5 参考文献")["items"]


def keys(value):
    if isinstance(value, dict):
        yield from value
        for child in value.values():
            yield from keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from keys(child)


def test_current_exact_json_keeps_confirmed_fields_null_zero_false_and_patient_text():
    internal = {
        "meta": {"title": "精确合同样本", "reportId": "exact-current",
                 "displayTemplateVersion": CANONICAL_DISPLAY_TEMPLATE},
        "sections": [
            {"id": "overview", "title": "第一章", "blocks": [
                {"kind": "table", "title": "1.1 检测综合评估结论", "columns": ["评价项目", "综合结论"],
                 "rows": [["屏障结论", "动态结论"]]},
                {"kind": "table", "title": "三项检测指标",
                 "columns": ["检测指标", "检测结果", "参考范围", "结果评价", "单项评分"],
                 "rows": [["hs-CRP", None, "≤1", None, None], ["D-乳酸", False, None, False, None],
                          {"cells": ["DAO", 0, "≤10", "未超出", 0], "current": False},
                          ["LPS", None, "≤20", None, None]]},
                {"kind": "paragraph", "title": "1.3 肠道屏障功能评分（IBFC）与损伤分级", "text": "动态评分解释"},
                {"kind": "paragraph", "title": "1.4 肠道屏障受损类型与受损状况", "text": "动态类型解释"},
                {"kind": "paragraph", "title": "1.5 慢性低度炎症评估", "text": "动态炎症解释"},
                {"kind": "facts", "title": "实际记录", "items": [{"label": "未提供", "value": None},
                    {"label": "明确否定", "value": False}]},
                {"kind": "cards", "title": "肠道屏障功能评分（IBFC）", "items": [
                    {"label": "IBFC评分", "value": 0, "detail": None}]},
                {"kind": "bands", "title": "屏障评分分级", "items": [
                    {"range": None, "label": None, "selected": False}]},
            ]},
            {"id": "disease_risks", "title": "第二章", "blocks": [
                {"kind": "list", "title": "本章小结", "items": ["本次资料不足"]}]},
            {"id": "repair_plan", "title": "第三章", "blocks": [{"kind": "paragraph", "text": "FIXED-REPAIR"}]},
            {"id": "education", "title": "第四章", "blocks": [{"kind": "paragraph", "text": "FIXED-EDUCATION"}]},
            {"id": "appendix", "title": "第五章 附录", "blocks": [
                {"kind": "table", "title": "5.1 质控校验", "columns": [], "rows": []}]},
        ],
    }
    original = deepcopy(internal)
    expected = {"schemaVersion": "gut-barrier-public.v2", "meta": {"title": "精确合同样本", "reportId": "exact-current"},
        "sections": [
            {"id": "overview", "title": "第一章", "subtitle": None, "visible": True, "blocks": [
                {"kind": "paragraph", "title": "1.1 综合评估指数（IBFC · 肠屏障功能评分）", "text": "动态结论"},
                {"kind": "cards", "title": "肠道屏障功能评分（IBFC）", "items": [
                    {"label": "IBFC评分", "value": 0, "detail": None},
                    {"label": "评分占满分比例", "value": "0.00%", "detail": "IBFC 满分为 15 分"}]},
                {"kind": "bands", "title": "屏障评分分级", "items": [
                    {"range": None, "label": None, "selected": False}]},
                {"kind": "paragraph", "title": None, "text": "动态评分解释"},
                {"kind": "paragraph", "title": None, "text": "动态类型解释"},
                {"kind": "table", "title": "1.2 检测项目与结果总览",
                 "columns": ["检测项目", "检测指标", "检测结果", "参考区间", "结果评价", "反映维度"],
                 "rows": [{"cells": ["肠道屏障功能", "DAO", 0, "≤10", "未超出", "肠上皮细胞损伤"], "current": False},
                          ["肠道屏障功能", "LPS", None, "≤20", None, "内毒素入血、炎症级联激活"],
                          ["肠道屏障功能", "D-乳酸", False, None, False, "紧密连接损伤、肠通透性增加"],
                          ["炎症指标", "hs-CRP", None, "≤1", None, "慢性低度炎症"]]},
                {"kind": "paragraph", "title": None, "text": "动态炎症解释"},
                {"kind": "facts", "title": "实际记录", "items": [{"label": "未提供", "value": None},
                    {"label": "明确否定", "value": False}]},
            ]},
            {"id": "disease_risks", "title": "第二章", "subtitle": None, "visible": True, "blocks": [
                {"kind": "paragraph", "title": None, "text": DISEASE_INTRODUCTION},
                {"kind": "table", "title": "本次健康关注指数总览",
                 "columns": ["系统", "疾病", "健康关注指数", "关注等级"], "rows": []},
                {"kind": "paragraph", "title": "2.1 本章小结",
                 "text": "本次资料尚未形成可由相应证据支持的具体疾病关注方向，不能据此解释为低风险或已排除疾病。"
                         "肠道屏障与炎症检测结果仍需结合症状、病史及相关检查共同解读；"
                         "后续应先完善缺失或待核实的资料，再确定需要关注的系统与复评内容。"},
                {"kind": "list", "title": "2.1 本章小结", "items": ["本次资料不足"]}]},
            {"id": "appendix", "title": "第四章 附录", "subtitle": None, "visible": True, "blocks": [
                {"kind": "table", "title": "4.1 质控校验", "columns": QC_COLUMNS, "rows": [
                    ["姓名", None, "性别", None], ["年龄", None, "样本类型", "血液"],
                    ["送检项目", "肠道屏障三项、hs-CRP", "样本状态", "合格"],
                    ["样本制备质控", "合格", "检测数据质控", "合格"],
                    ["检验医师", None, "审核医师", None]]},
                {"kind": "references", "title": "4.4 参考文献", "items": []}]},
        ]}
    report = project_callback_report(internal)
    assert report == expected
    assert json.loads(json.dumps(report, ensure_ascii=False)) == expected
    assert project_callback_report(report) == expected
    assert internal == original


def test_current_confirmed_qc_uses_fixed_copy_and_dynamic_identity_preserving_zero_false():
    internal = clinical_report()
    internal["meta"]["displayTemplateVersion"] = CANONICAL_DISPLAY_TEMPLATE
    facts = block(internal, "overview", "受检者信息")
    values = {"姓名": False, "性别": None, "年龄": 0, "样本类型": "血清", "检测项目": "不采用的固定项目说明"}
    for item in facts["items"]:
        if item["label"] in values:
            item["value"] = values[item["label"]]
    block(internal, "overview", "三项检测指标")["rows"] = [
        ["hs-CRP", "", None, None, None], ["D-乳酸", False, None, None, None],
        {"cells": ["DAO", 0, None, None, 0], "current": False}, ["LPS", None, None, None, None]]
    original = deepcopy(internal)
    report = project_callback_report(internal)
    qc = block(report, "appendix", "4.1 质控校验")
    assert qc == {"kind": "table", "title": "4.1 质控校验", "columns": QC_COLUMNS, "rows": [
        ["姓名", False, "性别", None], ["年龄", 0, "样本类型", "血液"],
        ["送检项目", "肠道屏障三项、hs-CRP", "样本状态", "合格"],
        ["样本制备质控", "合格", "检测数据质控", "合格"], ["检验医师", None, "审核医师", None]]}
    assert qc["rows"][0][1] is False
    assert type(qc["rows"][1][1]) is int and qc["rows"][1][1] == 0
    assert next(item["value"] for item in block(report, "overview", "受检者信息")["items"]
                if item["label"] == "样本类型") == "血液"
    assert [b["title"] for b in section(report, "appendix")["blocks"]] == ["4.1 质控校验", "4.4 参考文献"]
    assert project_callback_report(report) == report
    assert internal == original


@pytest.mark.parametrize("score, percentage", [
    (0, "0.00%"), (5, "33.33%"), (7.5, "50.00%"), (15, "100.00%"),
    (None, None), (False, None), (True, None), (-1, None), (15.01, None),
    ("5", None), ("未评分", None), (float("nan"), None), (float("inf"), None),
])
def test_current_score_share_uses_only_valid_ibfc_and_preserves_original_score_and_grade(score, percentage):
    internal = clinical_report()
    score_cards = block(internal, "overview", "肠道屏障功能评分（IBFC）")["items"]
    next(item for item in score_cards if item["label"] == "IBFC评分")["value"] = score
    baseline_cards = block(project_callback_report(internal), "overview", "肠道屏障功能评分（IBFC）")["items"]
    internal["meta"]["displayTemplateVersion"] = CANONICAL_DISPLAY_TEMPLATE
    original = deepcopy(internal)
    report = project_callback_report(internal)
    published = block(report, "overview", "肠道屏障功能评分（IBFC）")["items"]
    assert [item for item in published if item["label"] == "评分占满分比例"] == [
        {"label": "评分占满分比例", "value": percentage, "detail": "IBFC 满分为 15 分"}]
    assert [item for item in published if item["label"] != "评分占满分比例"] == baseline_cards
    assert project_callback_report(report) == report
    assert internal == original


def test_frozen_score_share_is_not_added_or_recalculated():
    report = project_callback_report(clinical_report())
    original = deepcopy(report)
    assert not any(item["label"] == "评分占满分比例" for item in
                   block(report, "overview", "肠道屏障功能评分（IBFC）")["items"])
    assert project_callback_report(report) == original
    block(report, "overview", "肠道屏障功能评分（IBFC）")["items"].append(
        {"label": "评分占满分比例", "value": "12.34%", "detail": "已冻结解释"})
    original = deepcopy(report)
    assert project_callback_report(report) == original


@pytest.mark.parametrize("indicators", [{}, {"complete": True}, {"complete": False}, {"complete": None}])
def test_current_confirmed_qc_is_fixed_for_all_completion_flags_and_missing_results(indicators):
    request = GutBarrierRequest.model_validate({"userId": "qc-empty", "sessionId": "qc-empty",
                                                "gutHealthIndicator": indicators})
    assessed = assess_case(request, context={"currentDate": "2026-10-01"})
    internal = build_report(assessed, build_interventions(assessed), report_id="qc-empty")
    internal["meta"]["displayTemplateVersion"] = CANONICAL_DISPLAY_TEMPLATE
    report = project_callback_report(internal)
    qc = block(report, "appendix", "4.1 质控校验")
    assert qc["columns"] == QC_COLUMNS
    assert qc["rows"] == [["姓名", None, "性别", None], ["年龄", None, "样本类型", "血液"],
                          ["送检项目", "肠道屏障三项、hs-CRP", "样本状态", "合格"],
                          ["样本制备质控", "合格", "检测数据质控", "合格"], ["检验医师", None, "审核医师", None]]
    # Confirmed report copy is not inferred from completion or clinical scores.
    assert next(item["value"] for item in block(report, "overview", "受检者信息")["items"]
                if item["label"] == "样本类型") == "血液"
    assert all(row[2] is None for row in block(report, "overview", "1.2 检测项目与结果总览")["rows"])


@pytest.mark.parametrize("qc_state", ["absent", "null", "recorded"])
def test_frozen_v2_does_not_create_or_rebuild_quality_control(qc_state):
    report = project_callback_report(clinical_report())
    appendix = section(report, "appendix")
    appendix["title"] = "第四章 附录"
    appendix["blocks"][0]["title"] = "4.4 参考文献"
    if qc_state != "absent":
        appendix["blocks"].insert(0, {"kind": "table", "title": "4.1 质控校验", "columns": QC_COLUMNS, "rows": [
            ["姓名", "已冻结姓名", "性别", False], ["年龄", 0, "样本类型", "已记录样本"],
            ["送检项目", "已记录项目", "样本状态", "已记录状态"],
            ["样本制备质控", "已记录制备", "检测数据质控", "已记录分析"],
            ["检验医师", "已记录检验人", "审核医师", None]]})
        if qc_state == "null":
            for row in appendix["blocks"][0]["rows"]:
                row[1] = row[3] = None
    original = deepcopy(report)
    assert project_callback_report(report) == original
    assert project_callback_report(project_callback_report(report)) == original
    assert report == original


@pytest.mark.parametrize("pattern", PATTERNS)
def test_all_patterns_keep_patient_results_but_omit_fixed_chapters_and_copy(pattern):
    report = clinical_report(pattern)
    full = project_public_report(report)
    dynamic = project_callback_report(report)
    assert set(dynamic) == {"schemaVersion", "meta", "sections"}
    assert dynamic["schemaVersion"] == "gut-barrier-public.v2"
    assert dynamic["meta"] == full["meta"]
    assert [item["id"] for item in dynamic["sections"]] == ["overview", "disease_risks", "appendix"]
    assert all(set(item) == SECTION_KEYS for item in dynamic["sections"])
    assert report["assessment"]["barrier"]["pattern"] == pattern
    for title in ("1.1 检测综合评估结论", "三项检测指标", "1.3 肠道屏障功能评分（IBFC）与损伤分级",
                  "1.4 肠道屏障受损类型与受损状况", "屏障评分分级"):
        assert block(dynamic, "overview", title) == block(full, "overview", title)
    titles = {item["title"] for item in section(dynamic, "overview")["blocks"]}
    assert "1.2 检测指标分项阐述" not in titles
    assert "指标意义" not in titles
    assert not titles & {"健康问卷背景（自述）", "问卷相关管理关注", "本次检测方法与参考信息", "本次样本与质控记录"}
    assert not any(item["kind"] == "paragraph" and not item["title"]
                   for item in section(dynamic, "disease_risks")["blocks"])
    assert len(section(dynamic, "appendix")["blocks"]) == 1
    encoded = json.dumps(dynamic, ensure_ascii=False)
    assert "5.1 质量控制校验" not in encoded
    assert "5.2 检测方法学介绍" not in encoded
    assert "5.3 风险指数标尺说明" not in encoded
    assert "5.4 免责声明" not in encoded
    assert len(encoded) < len(json.dumps(full, ensure_ascii=False))


def test_fixed_fact_value_and_card_detail_are_null_without_removing_keys():
    report = clinical_report()
    full = project_public_report(report)
    dynamic = project_callback_report(report)
    facts = block(dynamic, "overview", "受检者信息")["items"]
    item = next(item for item in facts if item["label"] == "检测项目")
    assert item == {"label": "检测项目", "value": None}
    score = next(item for item in block(dynamic, "overview", "肠道屏障功能评分（IBFC）")["items"]
                 if item["label"] == "IBFC评分")
    assert score == {"label": "IBFC评分", "value": 9.5, "detail": None}
    assert next(item for item in block(full, "overview", "受检者信息")["items"]
                if item["label"] == "检测项目")["value"] == "肠道屏障三项"
    assert next(item for item in block(full, "overview", "肠道屏障功能评分（IBFC）")["items"]
                if item["label"] == "IBFC评分")["detail"] == "满分15分"


@pytest.mark.parametrize("kwargs, expected", [
    ({"missing": True}, "暂不能评分"),
    ({"age": 17}, "不套用成人"),
    ({"age": None}, "不套用成人"),
    ({"crp": ">3.0"}, "屏障"),
    ({"crp": 12}, "屏障"),
    ({"pattern": "---", "crp": 0.2}, "未见评分异常"),
])
def test_missing_age_range_high_crp_and_normal_paths_remain_case_specific(kwargs, expected):
    report = clinical_report(**kwargs)
    dynamic = project_callback_report(report)
    overview = json.dumps(section(dynamic, "overview"), ensure_ascii=False)
    assert expected in overview
    assert "hs-CRP 项目功能分层" not in overview
    if kwargs.get("missing"):
        assert all(row[1] is None for row in block(dynamic, "overview", "三项检测指标")["rows"])
    if kwargs.get("pattern") == "---":
        assert block(dynamic, "overview", "肠道屏障功能评分（IBFC）")["items"][0]["value"] == 0


def test_personal_safety_limitations_are_not_mistaken_for_static_repair_copy():
    dynamic = project_callback_report(clinical_report(context={
        "allergies": "鱼类过敏", "liverAndKidneyFunction": "肾功能不全",
        "immuneStatus": "免疫抑制", "acuteInfection": True,
    }))
    overview = json.dumps(section(dynamic, "overview"), ensure_ascii=False)
    for fragment in ("鱼类/海鲜过敏", "肝肾功能", "免疫状态", "急性感染"):
        assert fragment in overview
    assert all(item["id"] != "repair_plan" for item in dynamic["sections"])


def test_seven_systems_twenty_one_diseases_keep_index_level_and_null_mechanism_column():
    report = clinical_report()
    full = project_public_report(report)
    dynamic = project_callback_report(report)
    tables = [item for item in section(dynamic, "disease_risks")["blocks"]
              if item["kind"] == "table" and item["columns"] == RISK_COLUMNS]
    assert len(tables) == 7
    assert sum(len(item["rows"]) for item in tables) == 22
    for table in tables:
        original = block(full, "disease_risks", table["title"])
        for row, source in zip(table["rows"], original["rows"], strict=True):
            assert len(row) == 4
            assert row[:3] == source[:3]
            assert row[3] is None
            assert source[3]


def test_dynamic_bibliography_is_exactly_selected_allowed_case_sources_not_static_lists():
    report = clinical_report()
    report["interventions"]["references"] = [
        {"type": "official_fact_sheet", "title": "本例实际引用", "url": "https://evidence.invalid/case",
         "year": 2026, "source": "权威来源", "doi": "10.test/case", "location": "第2节", "audit": "PRIVATE-AUDIT"},
        {"type": "scientific_statement", "title": "本例第二依据", "url": "https://evidence.invalid/second"},
        {"type": "raw_user_upload", "title": "PRIVATE-UNVERIFIED", "url": "https://evidence.invalid/private"},
    ]
    dynamic = project_callback_report(report)
    assert references(dynamic) == [
        {"citation": "本例实际引用", "url": "https://evidence.invalid/case", "year": 2026,
         "publisher": "权威来源", "doi": "10.test/case", "location": "第2节"},
        {"citation": "本例第二依据", "url": "https://evidence.invalid/second", "year": None,
         "publisher": None, "doi": None, "location": None},
    ]
    assert all(set(item) == REFERENCE_KEYS for item in references(dynamic))
    assert "PRIVATE-" not in json.dumps(dynamic)
    assert "Surface area of the digestive tract" not in json.dumps(dynamic)
    assert "Butyrate Enhances the Intestinal Barrier" not in json.dumps(dynamic)
    assert "mechanism_evidence" not in json.dumps(dynamic)


@pytest.mark.parametrize("selected", [[], None, "not-a-list", [{"type": "unsupported", "title": "忽略"}]])
def test_no_case_references_keeps_reference_block_and_empty_items(selected):
    report = clinical_report()
    report["interventions"]["references"] = selected
    dynamic = project_callback_report(report)
    assert section(dynamic, "appendix")["blocks"] == [
        {"kind": "references", "title": "5.5 参考文献", "items": []}
    ]


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///private/result.json",
                                 "https://evidence.invalid/\nsecret", {"rawData": "PRIVATE-URL"}])
def test_dynamic_reference_urls_use_existing_safe_public_projection(url):
    report = clinical_report()
    report["interventions"]["references"] = [{"type": "official_fact_sheet", "title": "依据", "url": url}]
    dynamic = project_callback_report(report)
    assert references(dynamic) == [{"citation": "依据", "url": None, "year": None,
                                    "publisher": None, "doi": None, "location": None}]
    assert "PRIVATE-" not in json.dumps(dynamic)


def test_null_zero_false_and_structured_rows_survive_dynamic_projection():
    report = clinical_report("---")
    section(report, "overview")["blocks"].extend([
        {"kind": "facts", "title": "实际记录", "items": [
            {"label": "未取得", "value": None}, {"label": "零值", "value": 0}, {"label": "否", "value": False}]},
        {"kind": "table", "title": "实际原始值", "columns": ["状态", "数值"], "rows": [
            {"cells": [False, 0], "current": False}, [None, ""]]},
    ])
    dynamic = project_callback_report(report)
    assert block(dynamic, "overview", "实际记录")["items"] == [
        {"label": "未取得", "value": None}, {"label": "零值", "value": 0}, {"label": "否", "value": False}]
    assert block(dynamic, "overview", "实际原始值")["rows"] == [
        {"cells": [False, 0], "current": False}, [None, ""]]
    bands = block(dynamic, "overview", "屏障评分分级")["items"]
    assert all(set(item) == {"range", "label", "selected"} and item["selected"] is False for item in bands)


@pytest.mark.parametrize("pattern", PATTERNS)
def test_projection_is_idempotent_independent_and_does_not_mutate_original(pattern):
    report = clinical_report(pattern)
    before = deepcopy(report)
    dynamic = project_callback_report(report)
    assert report == before
    assert project_callback_report(dynamic) == dynamic
    assert project_callback_report(project_callback_report(dynamic)) == dynamic
    dynamic["meta"]["title"] = "修改副本"
    block(dynamic, "overview", "三项检测指标")["rows"][0][1] = "只修改动态副本"
    references(dynamic).append({"citation": "只修改副本"})
    assert report == before


def test_private_fields_are_excluded_recursively_from_dynamic_projection():
    report = clinical_report()

    def inject(value):
        if isinstance(value, dict):
            for child in list(value.values()):
                inject(child)
            value["internalOnly"] = {"rawData": "PRIVATE-RAW", "sourcePath": "/private/PRIVATE-PATH"}
        elif isinstance(value, list):
            for child in value:
                inject(child)

    inject(report)
    report["sections"].append({"id": "internal_audit", "title": "PRIVATE-AUDIT", "blocks": [
        {"kind": "paragraph", "text": "PRIVATE-SOURCE"}]})
    before = deepcopy(report)
    dynamic = project_callback_report(report)
    assert report == before
    assert not PRIVATE_KEYS.intersection(keys(dynamic))
    assert "PRIVATE-" not in json.dumps(dynamic)
    assert [item["id"] for item in dynamic["sections"]] == ["overview", "disease_risks", "appendix"]


def test_existing_v1_projection_still_preserves_all_five_chapters_and_fixed_bibliography():
    full = project_public_report(clinical_report())
    assert full["schemaVersion"] == "gut-barrier-public.v1"
    assert [item["id"] for item in full["sections"]] == [
        "overview", "disease_risks", "repair_plan", "education", "appendix"]
    assert len(references(full)) == 14
    assert block(full, "overview", "1.2 检测指标分项阐述")["text"]
    assert len(block(full, "overview", "指标意义")["items"]) == 3
    assert block(full, "appendix", "5.1 质量控制校验")["rows"]
    assert block(full, "appendix", "5.4 免责声明")["items"]
    assert project_public_report(full) == full


@pytest.mark.parametrize("bad_row", [["疾病", 0, "低"], ["疾病", 0, "低", "机制", "额外"]])
def test_malformed_disease_row_cannot_silently_shift_frontend_column_contract(bad_row):
    report = clinical_report()
    table = next(item for item in section(report, "disease_risks")["blocks"]
                 if item.get("columns") == RISK_COLUMNS)
    table["rows"][0] = bad_row
    with pytest.raises(ValueError, match="four-column"):
        project_callback_report(report)
