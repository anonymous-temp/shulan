"""Product-facing chapter changes preserve provenance and frozen receipts."""
from copy import deepcopy
import json
import re

import pytest

from adapters.gut_barrier.frontend_template import expand_dynamic_report, project_display_report
from adapters.gut_barrier.rendering import render_report_html
from modules.gut_barrier.display_versions import PRODUCT_FEEDBACK_DISPLAY_TEMPLATE, LATEST_DISPLAY_TEMPLATE
from modules.gut_barrier.integration_content import (
    ATTENTION_OVERVIEW_TITLE, PRODUCT_DISEASE_INTRODUCTION,
)
from modules.gut_barrier.publication import project_callback_report
from tests.unit.gut_barrier.test_dynamic_publication import (
    REFERENCE_KEYS, RISK_COLUMNS, SECTION_KEYS, clinical_report, section,
)


@pytest.fixture(autouse=True)
def archived_product_release(monkeypatch):
    from adapters.gut_barrier import frontend_template
    from tests.unit.gut_barrier.preview.archive_support import historical_archive
    original = frontend_template.load_static_template

    def load(version, **kwargs):
        if version == PRODUCT_FEEDBACK_DISPLAY_TEMPLATE:
            kwargs.setdefault("archive_directory", historical_archive())
        return original(version, **kwargs)

    monkeypatch.setattr(frontend_template, "load_static_template", load)


def _source(*, background=True, model=False, symptom=None):
    report = clinical_report()
    report["meta"]["displayTemplateVersion"] = PRODUCT_FEEDBACK_DISPLAY_TEMPLATE
    report["assessment"]["diseaseAssessment"] = {"mode": "data_driven_attention", "references": [
        {"citation": "[评估4]炎症关联资料", "url": "https://example.test/inflammation", "year": 2024},
        {"citation": "[评估6]临床评估资料", "url": "https://example.test/clinical", "year": 2025},
    ]}
    report["interventions"]["references"] = [
        {"type": "professional_guideline", "title": "干预资料", "url": "https://example.test/care"}]
    rows = [["消化道相关问题", 3, "基础关注", "屏障通透性改变与局部炎症反应相关。[评估4][评估6]"]]
    report["assessment"]["diseaseRisks"] = [
        {"name": rows[0][0], "status": "attention_index"}]
    if model:
        rows.append(["独立模型结果", 0, "基础关注",
                     "本项数值来自独立的已配置模型，不与本章自定义关注指数比较。血压相关因素参与血管负担。[评估6]"])
        report["assessment"]["diseaseRisks"].append({"name": "独立模型结果", "status": "evaluated"})
    section(report, "disease_risks")["blocks"] = [
        {"kind": "callout", "title": "本次关注指数", "text": "旧版很长的说明", "tone": "default"},
        {"kind": "table", "title": "2.7 消化系统疾病风险评估", "columns": RISK_COLUMNS[:], "rows": rows},
        {"kind": "list", "title": "本章小结", "items": ["消化系统关注方向：消化道相关问题。"]},
    ]
    if background:
        section(report, "disease_risks")["blocks"].append({
            "kind": "table", "title": "个体临床背景", "columns": ["检查日期", "项目", "时间属性", "原始检查摘要"],
            "rows": [["2026-09-01", "已有检查", "既往", "保留原始摘要"]]})
    if symptom is not None:
        report["assessment"]["context"]["healthSurvey"] = {
            "answers": {"bowelMovement": {"status": "known", "raw": symptom}}}
    return report


def _references(report):
    return next(block["items"] for block in section(report, "appendix")["blocks"]
                if block["kind"] == "references")


def _visible(html):
    return re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", html, flags=re.S)


def test_new_callback_uses_final_reference_numbers_and_unchanged_json_shapes():
    source = _source()
    before = deepcopy(source)
    report = project_callback_report(source)
    rows = next(block["rows"] for block in section(report, "disease_risks")["blocks"]
                if block.get("columns") == RISK_COLUMNS)
    assert rows[0] == ["消化道相关问题", 3, "基础关注", "屏障通透性改变与局部炎症反应相关。[2][3]"]
    assert [item["citation"] for item in _references(report)] == ["干预资料", "炎症关联资料", "临床评估资料"]
    assert all(set(item) == REFERENCE_KEYS for item in _references(report))
    assert all(set(item) == SECTION_KEYS for item in report["sections"])
    assert set(report["meta"]) == {"title", "reportId"}
    assert set(report) == {"schemaVersion", "meta", "sections"}
    assert source == before and project_callback_report(report) == report


@pytest.mark.parametrize("background,summary_title", [(True, "2.3 本章小结"), (False, "2.2 本章小结")])
def test_chapter_intro_background_and_summary_have_one_clear_number_sequence(background, summary_title):
    report = project_callback_report(_source(background=background))
    blocks = section(report, "disease_risks")["blocks"]
    assert blocks[0]["text"] == PRODUCT_DISEASE_INTRODUCTION
    titles = [block["title"] for block in blocks]
    assert "2.1 消化系统疾病风险评估" in titles
    assert ("2.2 个体临床背景" in titles) is background
    assert all(block["title"] == summary_title for block in blocks[-2:])
    assert blocks[-2]["text"] == "本次主要关注消化系统，共1项相关问题。"
    chapter_text = json.dumps(blocks, ensure_ascii=False)
    assert "下方总览与各系统明细" not in chapter_text
    assert "机制关联不代表已患病" not in chapter_text
    assert chapter_text.count("不构成疾病诊断") == 1


def test_product_intro_keeps_template_scope_but_uses_actual_attention_grade_names():
    assert "本章基于本次检测结果与相关健康评估，评估相关疾病风险" in PRODUCT_DISEASE_INTRODUCTION
    assert "用于风险提示与预防参考，不构成疾病诊断" in PRODUCT_DISEASE_INTRODUCTION
    assert "基础关注、加强关注、优先评估" in PRODUCT_DISEASE_INTRODUCTION
    assert "低、中、高" not in PRODUCT_DISEASE_INTRODUCTION
    assert "共同上游机制" not in PRODUCT_DISEASE_INTRODUCTION


@pytest.mark.asyncio
@pytest.mark.parametrize("values,score,grade,label", [
    ((19, 1, 1), 2, "I", "I 级（亚健康）"),
    ((1, 1, 36), 6, "III", "III 级（中度）"),
    ((19, 1, 36), 8, "IV", "IV 级（重度）"),
    ((19, 13, 1), 4.5, "II", "II 级（轻度）"),
])
async def test_source_endpoint_calculation_callback_bands_and_html_agree(values, score, grade, label):
    from modules.gut_barrier.pipeline import GutBarrierPipeline
    from modules.gut_barrier.schemas import GutBarrierRequest

    request = GutBarrierRequest.model_validate({
        "userId": "endpoint-test", "sessionId": "endpoint-test", "age": 31,
        "gutHealthIndicator": {key: {"value": value, "unit": unit}
            for key, value, unit in zip(("dLacticAcid", "diamineOxidase", "bacterialEndotoxin"),
                                       values, ("mg/L", "U/L", "U/L"))},
    })
    document, _ = await GutBarrierPipeline().generate(request, report_id="endpoint-test", evaluation_date="2026-10-06")
    assert document["assessment"]["barrier"]["score"] == score
    assert document["assessment"]["barrier"]["grade"] == grade
    source_cards = next(block["items"] for block in section(document, "overview")["blocks"] if block["kind"] == "cards")
    assert next(item["value"] for item in source_cards if item["label"] == "损伤等级") == label
    report = project_callback_report(document)
    blocks = section(report, "overview")["blocks"]
    bands = next(block for block in blocks if block["title"] == "屏障评分分级")["items"]
    assert [item["label"] for item in bands] == ["健康", "亚健康", "疾病"]
    assert all(item == {"range": None, "label": item["label"], "selected": False} for item in bands)
    cards = next(block for block in blocks if block["kind"] == "cards")["items"]
    assert next(item for item in cards if item["label"] == "损伤等级") == {"label": "损伤等级", "value": None, "detail": None}
    assert next(item for item in cards if item["label"] == "总体阶段") == {"label": "总体阶段", "value": None, "detail": None}
    assert next(item["value"] for item in cards if item["label"] == "IBFC评分") == score
    html = render_report_html(report, template_version=LATEST_DISPLAY_TEMPLATE)
    assert json.loads(re.search(r'id="report-data">(.*?)</script>', html, re.S)[1]) == report
    visible = _visible(html)
    summary = next(block["text"] for block in blocks
                   if block.get("title") == "1.1 综合评估指数（IBFC · 肠屏障功能评分）")
    assert summary in visible and f"IBFC 为 {score:g} 分" in summary
    if grade is not None:
        assert f"损伤分级为 {grade} 级" in summary
        assert set(re.findall(r"(?:Ⅰ|Ⅱ|Ⅲ|Ⅳ|Ⅴ|I{1,3}|IV|V)\s*级", visible)) == {f"{grade} 级"}
    else:
        assert "损伤分级需进一步确认" in summary
        assert not re.search(r"(?:Ⅰ|Ⅱ|Ⅲ|Ⅳ|Ⅴ|I{1,3}|IV|V)\s*级", visible)
    assert not re.search(r"(?:轻度|中度|重度|极重度)损伤", visible)
    assert 'class="fact-label">损伤等级<' not in visible and 'class="fact-label">总体阶段<' not in visible
    assert "分级待复核" not in visible


def test_patient_reference_prefix_survives_static_merge_and_html_links():
    report = project_callback_report(_source())
    original = deepcopy(report)
    expanded = expand_dynamic_report(report, template_version=PRODUCT_FEEDBACK_DISPLAY_TEMPLATE)
    assert _references(expanded)[:3] == _references(report)
    assert len(_references(expanded)) > 3
    assert expand_dynamic_report(expanded, template_version=PRODUCT_FEEDBACK_DISPLAY_TEMPLATE) == expanded
    html = render_report_html(report, template_version=PRODUCT_FEEDBACK_DISPLAY_TEMPLATE)
    visible = _visible(html)
    assert 'href="#reference-primary-2" aria-label="参考文献2">2</a>' in visible
    assert 'id="reference-primary-2"><span class="reference-index">[2]</span>炎症关联资料' in visible
    assert 'id="reference-primary-3"><span class="reference-index">[3]</span>临床评估资料' in visible
    assert "[评估" not in visible and "[补" not in visible
    assert set(re.findall(r'href="#(reference-[^"]+)"', visible)) <= set(
        re.findall(r'id="(reference-[^"]+)"', visible))
    assert report == original
    assert json.loads(re.search(r'id="report-data">(.*?)</script>', html, re.S)[1]) == original


def test_original_numeric_and_named_sources_cannot_collide_in_new_callback():
    source = _source()
    original_primary = next(block["items"][0] for block in section(source, "appendix")["blocks"]
                            if block["kind"] == "references" and "参考文献" in block["title"])
    next(block for block in section(source, "disease_risks")["blocks"]
         if block.get("columns") == RISK_COLUMNS)["rows"][0][3] = "关联机制。[1][评估4]"
    report = project_callback_report(source)
    assert next(block for block in section(report, "disease_risks")["blocks"]
                if block.get("columns") == RISK_COLUMNS)["rows"][0][3] == "关联机制。[4][2]"
    assert _references(report)[3]["citation"] == original_primary["citation"]
    assert _references(report)[1]["citation"] == "炎症关联资料"


def test_fixed_copy_renumbering_does_not_change_patient_numbers_or_table_labels(monkeypatch):
    from adapters.gut_barrier import frontend_template
    asset = frontend_template.load_static_template(PRODUCT_FEEDBACK_DISPLAY_TEMPLATE)
    asset["fixedSections"][0]["blocks"] = [{"kind": "paragraph", "title": "表 1 与 3.2 小节",
        "text": "一般机制[1]和补充依据[补1]；普通年份[2026]与图 1、表 2 不变。"}]
    primary = next(block for block in asset["appendixBlocks"]
                   if block["kind"] == "references" and "参考文献" in block["title"])
    supplemental = next(block for block in asset["appendixBlocks"]
                        if block["kind"] == "references" and "补充依据" in block["title"])
    first_fixed, first_supplement = primary["items"][0], supplemental["items"][0]
    supplemental_index = 3 + len(primary["items"]) + 1
    monkeypatch.setattr(frontend_template, "load_static_template", lambda *args, **kwargs: deepcopy(asset))
    report = project_callback_report(_source())
    before = deepcopy(report)
    expanded = expand_dynamic_report(report, template_version=PRODUCT_FEEDBACK_DISPLAY_TEMPLATE)
    text = section(expanded, "repair_plan")["blocks"][0]
    assert text == {"kind": "paragraph", "title": "表 1 与 3.2 小节",
        "text": f"一般机制[4]和补充依据[{supplemental_index}]；普通年份[2026]与图 1、表 2 不变。"}
    assert _references(expanded)[3] == first_fixed
    assert _references(expanded)[supplemental_index - 1] == first_supplement
    assert section(expanded, "disease_risks") == section(report, "disease_risks")
    assert report == before


def test_v9_reference_aliases_from_untrusted_callback_cannot_redirect_links():
    report = project_callback_report(_source())
    expected = expand_dynamic_report(report, template_version=PRODUCT_FEEDBACK_DISPLAY_TEMPLATE)
    report["_referenceAliases"] = {"primary": {2: 99999}}
    actual = expand_dynamic_report(report, template_version=PRODUCT_FEEDBACK_DISPLAY_TEMPLATE)
    assert actual == expected


@pytest.mark.parametrize("identity", ["url", "doi"])
def test_fixed_duplicate_publications_reuse_callback_index_without_moving_patient_rows(monkeypatch, identity):
    from adapters.gut_barrier import frontend_template
    asset = frontend_template.load_static_template(PRODUCT_FEEDBACK_DISPLAY_TEMPLATE)
    groups = [block for block in asset["appendixBlocks"] if block["kind"] == "references"]
    fixed = {"citation": "固定文献的另一标题", "url": "https://example.test/fixed-edition",
             "doi": "10.1234/synthetic-reference", "year": 2025, "publisher": None, "location": None}
    groups[0]["items"] = [fixed]
    groups[1]["items"] = [{**fixed, "citation": "补充列表中的同一文献"}]
    asset["fixedSections"][0]["blocks"] = [{"kind": "paragraph", "title": "固定依据",
                                              "text": "机制依据[1]与补充依据[补1]。"}]
    monkeypatch.setattr(frontend_template, "load_static_template", lambda *args, **kwargs: deepcopy(asset))
    report = project_callback_report(_source())
    _references(report)[1][identity] = fixed[identity]
    original = deepcopy(report)
    expanded = expand_dynamic_report(report, template_version=PRODUCT_FEEDBACK_DISPLAY_TEMPLATE)
    assert _references(expanded) == _references(report)
    assert section(expanded, "repair_plan")["blocks"][0]["text"] == "机制依据[2]与补充依据[2]。"
    assert expand_dynamic_report(expanded, template_version=PRODUCT_FEEDBACK_DISPLAY_TEMPLATE) == expanded
    assert report == original


def test_chart_reuses_callback_labels_and_excludes_independent_zero_model():
    report = project_callback_report(_source(model=True))
    blocks = section(report, "disease_risks")["blocks"]
    chart = next(block for block in blocks if block["title"] == ATTENTION_OVERVIEW_TITLE)
    assert chart["rows"] == [["消化系统", "消化道相关问题", 3, "基础关注"]]
    models = next(block["rows"] for block in blocks if block.get("columns") == RISK_COLUMNS)[1]
    assert models == ["独立模型结果", 0, "基础关注", "血压相关因素参与血管负担。[3]"]
    assert next(block for block in blocks if block["title"] == "独立模型说明")["kind"] == "callout"
    html = _visible(render_report_html(report, template_version=PRODUCT_FEEDBACK_DISPLAY_TEMPLATE))
    chart_html = next(fragment for fragment in html.split("</figure>") if "本次健康关注指数" in fragment)
    assert "基础关注" in chart_html and "独立模型结果" not in chart_html
    assert "高风险" not in chart_html and "中风险" not in chart_html and "低风险" not in chart_html


@pytest.mark.parametrize("symptom", ["腹泻伴便血", "无便血", "既往便血已缓解"])
def test_questionnaire_symptoms_do_not_generate_risk_chapter_content(symptom):
    report = project_callback_report(_source(symptom=symptom))
    blocks = section(report, "disease_risks")["blocks"]
    warnings = [block for block in blocks if block["title"] == "便血就医提示"]
    assert warnings == []
    mechanism = next(block["rows"][0][3] for block in blocks if block.get("columns") == RISK_COLUMNS)
    assert "就医" not in mechanism and mechanism.count("。") == 1


def test_frozen_receipt_and_v9_preview_never_regenerate_clinical_copy():
    report = project_callback_report(_source())
    blocks = section(report, "disease_risks")["blocks"]
    blocks[0]["text"] = "已冻结导语"
    blocks[-2]["text"] = "已冻结小结"
    next(block for block in blocks if block["title"] == ATTENTION_OVERVIEW_TITLE)["rows"] = []
    original = deepcopy(report)
    assert project_callback_report(report) == original
    view = project_display_report(report, template_version=PRODUCT_FEEDBACK_DISPLAY_TEMPLATE)
    assert section(view, "disease_risks")["blocks"] == blocks
    assert view["sourceReport"] == report == original
