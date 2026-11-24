"""The v2 callback owns patient values; a pinned frontend asset owns fixed copy."""
from copy import deepcopy
import json
import re
from unittest.mock import patch

import pytest

from adapters.gut_barrier.frontend_template import (
    BUNDLED_STATIC_TEMPLATE_PATH, STATIC_TEMPLATE_VERSION,
    expand_dynamic_report, load_static_template,
)
from adapters.gut_barrier.rendering import render_report_html
from modules.gut_barrier.display_versions import LATEST_DISPLAY_TEMPLATE
from modules.gut_barrier.publication import project_callback_report, project_public_report
from tests.unit.gut_barrier.test_five_chapters import report_for, sample
from tests.unit.gut_barrier.preview.archive_support import historical_asset


def _block(report, section_id, title):
    return next(block for section in report["sections"] if section["id"] == section_id
                for block in section["blocks"] if block.get("title") == title)


def _dynamic():
    return project_callback_report(report_for(sample()))


def test_versioned_asset_contains_all_fixed_copy_without_patient_or_scores(historical_templates):
    asset = load_static_template()
    assert asset["templateVersion"] == STATIC_TEMPLATE_VERSION == "gut-barrier-static.v1"
    assert asset["reportSchemaVersion"] == "gut-barrier-public.v2"
    assert asset["sectionOrder"] == ["overview", "disease_risks", "repair_plan", "education", "appendix"]
    assert [section["id"] for section in asset["fixedSections"]] == ["repair_plan", "education"]
    assert len(asset["diseaseMechanisms"]) == 21
    assert len({item["diseaseName"] for item in asset["diseaseMechanisms"]}) == 21
    assert {item["columnIndex"] for item in asset["diseaseMechanisms"]} == {3}
    refs = [block for block in asset["appendixBlocks"] if block["kind"] == "references"]
    assert [len(block["items"]) for block in refs] == [14, 25]
    text = json.dumps(asset, ensure_ascii=False)
    for private in ("新版验收样本", "acceptance-user", "gbr_five_chapter_test", '"subject"', '"riskIndex"', '"sessionId"', "26.0 mg/L"):
        assert private not in text
    assert json.loads(historical_asset("v1").read_text(encoding="utf-8")) == asset


def test_loading_current_release_does_not_require_or_evaluate_a_patient():
    with patch("modules.gut_barrier.assessment.assess_case", side_effect=AssertionError("no clinical calculation")):
        asset = load_static_template(LATEST_DISPLAY_TEMPLATE)
    assert asset["templateVersion"] == LATEST_DISPLAY_TEMPLATE


def test_v2_expansion_restores_five_chapters_and_twenty_nine_subsections(historical_templates):
    report = _dynamic()
    before = deepcopy(report)
    expanded = expand_dynamic_report(report)
    assert [s["id"] for s in expanded["sections"]] == ["overview", "disease_risks", "repair_plan", "education", "appendix"]
    titles = [block.get("title", "") or "" for section in expanded["sections"] for block in section["blocks"]]
    assert len([title for title in titles if re.match(r"^[1-5]\.\d+\s", title)]) == 29
    assert len(_block(expanded, "overview", "指标意义")["items"]) == 4
    assert len(_block(expanded, "appendix", "5.5 参考文献")["items"]) == 14
    assert report == before
    assert _block(expanded, "overview", "三项检测指标") == _block(report, "overview", "三项检测指标")
    assert _block(expanded, "overview", "肠道屏障功能评分（IBFC）")["items"][0]["value"] == 9.5
    for block in expanded["sections"][1]["blocks"]:
        if block["kind"] == "table" and len(block.get("columns", [])) == 4 and block["columns"][0] == "疾病":
            original = _block(report, "disease_risks", block["title"])
            assert [row[:3] for row in block["rows"]] == [row[:3] for row in original["rows"]]
            assert all(row[3] and "[" in row[3] for row in block["rows"])


def test_dynamic_nonempty_values_and_meaningful_zero_false_are_never_overwritten(historical_templates):
    report = _dynamic()
    facts = _block(report, "overview", "受检者信息")["items"]
    next(item for item in facts if item["label"] == "检测项目")["value"] = "保留本次实际项目"
    cards = _block(report, "overview", "肠道屏障功能评分（IBFC）")["items"]
    cards[0].update(value=0, detail=False)
    risk = _block(report, "disease_risks", "2.1 消化系统疾病风险评估")["rows"][0]
    risk[1:] = [0, False, "非空动态内容必须保留"]
    expanded = expand_dynamic_report(report)
    assert next(item for item in _block(expanded, "overview", "受检者信息")["items"] if item["label"] == "检测项目")["value"] == "保留本次实际项目"
    assert _block(expanded, "overview", "肠道屏障功能评分（IBFC）")["items"][0] == cards[0]
    assert _block(expanded, "disease_risks", "2.1 消化系统疾病风险评估")["rows"][0] == risk


def test_static_numeric_reference_positions_remain_stable_when_dynamic_sources_change(historical_templates):
    report = _dynamic()
    fixed = load_static_template()["appendixBlocks"]
    main = next(block for block in fixed if block.get("title") == "5.5 参考文献")
    extra = next(block for block in fixed if block.get("title") == "营养与微生态干预补充依据")
    dynamic = _block(report, "appendix", "5.5 参考文献")
    unique = {"citation": "个体补充来源", "url": "https://example.org/unique", "year": 2026,
              "publisher": None, "doi": None, "location": None}
    dynamic["items"] = [deepcopy(main["items"][0]), deepcopy(extra["items"][0]), unique, deepcopy(unique)]
    expanded = expand_dynamic_report(report)
    assert _block(expanded, "appendix", "5.5 参考文献") == main
    assert _block(expanded, "appendix", "营养与微生态干预补充依据")["items"] == extra["items"] + [unique]
    body = render_report_html(report).split('<script type="application/json"')[0]
    anchors = set(re.findall(r'id="(reference-[^"]+)"', body))
    targets = set(re.findall(r'href="#(reference-[^"]+)"', body))
    assert targets <= anchors
    assert "reference-supplement-1-26" in anchors


def test_renderer_expands_only_view_but_data_island_remains_exact_dynamic_payload(historical_templates):
    report = _dynamic()
    before = deepcopy(report)
    html = render_report_html(report)
    body, island = html.split('<script type="application/json" id="report-data">')
    assert 'id="repair_plan"' in body and 'id="education"' in body
    assert 'id="figure-8"' in body
    assert "5.4 免责声明" in body
    assert json.loads(island.split("</script>")[0]) == report == before
    assert [section["id"] for section in report["sections"]] == ["overview", "disease_risks", "appendix"]


def test_expansion_is_idempotent_for_static_text_and_dynamic_references(historical_templates):
    report = _dynamic()
    expanded = expand_dynamic_report(report)
    assert expand_dynamic_report(expanded) == expanded


def test_non_risk_tables_and_unhashable_names_are_left_untouched(historical_templates):
    report = _dynamic()
    disease = next(section for section in report["sections"] if section["id"] == "disease_risks")
    clinical = {"kind": "table", "title": "2.1 消化系统疾病风险评估",
                "columns": ["日期", "项目", "结论", "备注"], "rows": [["非酒精性脂肪性肝病", 0, False, None]]}
    disease["blocks"].append(clinical)
    risk = _block(report, "disease_risks", "2.1 消化系统疾病风险评估")
    risk["rows"].append([["列表名称"], None, None, None])
    expanded = expand_dynamic_report(report)
    blocks = next(section for section in expanded["sections"] if section["id"] == "disease_risks")["blocks"]
    assert blocks[-1] == clinical
    assert _block(expanded, "disease_risks", "2.1 消化系统疾病风险评估")["rows"][-1] == [["列表名称"], None, None, None]


def test_runtime_uses_pinned_snapshot_not_current_python_prose(historical_templates):
    report = _dynamic()
    with patch("modules.gut_barrier.content_catalog.PRINCIPLES", ("不可用于旧版渲染的新文案",)):
        expanded = expand_dynamic_report(report)
    assert "不可用于旧版渲染的新文案" not in json.dumps(expanded, ensure_ascii=False)
    assert "3.1 肠道屏障修复 5 原则总览" in json.dumps(expanded, ensure_ascii=False)


@pytest.mark.parametrize("version", ["gut-barrier-public.v1", "gut-barrier-report.v1", None])
def test_legacy_reports_are_not_enriched_or_changed(version):
    report = project_public_report(report_for(sample()))
    report["schemaVersion"] = version
    report["sections"] = report["sections"][:1]
    before = deepcopy(report)
    with patch("adapters.gut_barrier.frontend_template.load_static_template", side_effect=AssertionError("legacy must not load static asset")):
        assert expand_dynamic_report(report) == before
        html = render_report_html(report)
    assert 'id="repair_plan"' not in html
    assert report == before


@pytest.mark.parametrize("version", ["gut-barrier-public.v3", "gut-barrier-public.future", "gut-barrier-public."])
def test_unknown_public_versions_fail_instead_of_silently_using_latest_copy(version):
    report = {"schemaVersion": version, "meta": {}, "sections": []}
    with pytest.raises(ValueError, match="unsupported"):
        render_report_html(report)


def test_asset_binding_rejects_wrong_version(tmp_path):
    asset = load_static_template(LATEST_DISPLAY_TEMPLATE)
    asset["templateVersion"] = "gut-barrier-static.v99"
    with patch("adapters.gut_barrier.frontend_template.BUNDLED_STATIC_TEMPLATE_PATH") as path:
        path.read_text.return_value = json.dumps(asset)
        with pytest.raises(ValueError, match="version mismatch"):
            load_static_template(LATEST_DISPLAY_TEMPLATE)
