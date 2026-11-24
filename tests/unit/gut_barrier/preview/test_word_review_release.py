"""The current callback and preview share dynamic content, with static copy local."""
from copy import deepcopy
import json
import re

import pytest
import pytest_asyncio

from adapters.gut_barrier.frontend_template import expand_dynamic_report, load_static_template, project_display_report
from adapters.gut_barrier.rendering import render_report_html
from modules.gut_barrier.display_versions import (
    CANONICAL_DISPLAY_TEMPLATE, LATEST_DISPLAY_TEMPLATE, WORD_REVIEW_DISPLAY_TEMPLATE,
    PRODUCT_FEEDBACK_DISPLAY_TEMPLATE, ORDERED_REFERENCE_DISPLAY_TEMPLATE,
)
from modules.gut_barrier.pipeline import GutBarrierPipeline
from modules.gut_barrier.integration_content import ATTENTION_OVERVIEW_COLUMNS, ATTENTION_OVERVIEW_TITLE
from modules.gut_barrier.publication import normalize_current_dynamic_report, project_callback_report
from modules.gut_barrier.schemas import GutBarrierRequest
from tests.unit.gut_barrier.preview.support import Parsed, visible_source
from tests.unit.gut_barrier.test_dynamic_publication import clinical_report


MARKER_COLUMNS = ["检测项目", "检测指标", "检测结果", "参考区间", "结果评价", "反映维度"]
RISK_COLUMNS = ["疾病", "风险指数", "风险等级", "机制关联"]
QC_COLUMNS = ["校验项目", "本次记录", "校验项目", "本次记录"]


@pytest_asyncio.fixture
async def internal_document():
    request = GutBarrierRequest.model_validate({
        "userId": "view-test", "sessionId": "view-test", "name": "合成测试样本", "age": 28, "sex": "男",
        "gutHealthIndicator": {"diamineOxidase": {"value": "10.5", "unit": "U/L"},
                                "bacterialEndotoxin": {"value": "15.9", "unit": "U/L"},
                                "dLacticAcid": {"value": "17.42", "unit": "mg/L"},
                                "hsCRP": {"value": "2.66", "unit": "mg/L"}},
    })
    full, _ = await GutBarrierPipeline().generate(request, report_id="view-test", evaluation_date="2026-10-01")
    return full


@pytest.fixture
def document(internal_document):
    return project_callback_report(internal_document)


def blocks(report, section):
    return next(s["blocks"] for s in report["sections"] if s["id"] == section)


def embedded(html):
    return json.loads(re.search(r'id="report-data">(.*?)</script>', html, re.S)[1])


def quality_records(block):
    assert block["columns"] == QC_COLUMNS
    assert all(len(row) == 4 for row in block["rows"])
    return {row[index]: row[index + 1] for row in block["rows"] for index in (0, 2)}


@pytest.mark.asyncio
async def test_callback_view_and_html_share_current_dynamic_headings_columns_and_values(document):
    before = deepcopy(document)
    view = project_display_report(document, template_version=LATEST_DISPLAY_TEMPLATE)
    overview = blocks(view, "overview")
    table = next(b for b in overview if b["title"] == "1.2 检测项目与结果总览")
    assert table == next(b for b in blocks(document, "overview") if b["title"] == table["title"])
    assert table["columns"] == MARKER_COLUMNS
    assert [r[1] for r in table["rows"]] == ["二胺氧化酶（DAO）", "细菌脂多糖（LPS）", "D-乳酸（D-Lactate）"]
    assert [r[5] for r in table["rows"]] == ["肠上皮细胞损伤", "内毒素入血、炎症级联激活",
                                           "紧密连接损伤、肠通透性增加"]
    for report in (document, view):
        assert not any(re.match(r"1\.[345]", b.get("title") or "") for b in blocks(report, "overview"))
        conclusion = next(b for b in blocks(report, "overview") if (b["title"] or "").startswith("1.1 "))
        assert conclusion["kind"] == "paragraph"
        assert conclusion["title"] == "1.1 综合评估指数（IBFC · 肠屏障功能评分）"
    fixed_overview = [item["block"] for item in load_static_template(LATEST_DISPLAY_TEMPLATE)["overviewInsertions"]]
    assert [b for b in overview if b["kind"] == "paragraph" and b not in fixed_overview] == [
        b for b in blocks(document, "overview") if b["kind"] == "paragraph"]
    html = render_report_html(document, template_version=LATEST_DISPLAY_TEMPLATE)
    assert all(r[2] in html and r[5] in html for r in table["rows"])
    assert '>健康</span>' in html and '>亚健康</span>' in html and '>疾病</span>' in html
    assert "68%" not in html and 'aria-current="true"' not in html
    assert embedded(html) == document == before
    assert view["sourceReport"] == document
    assert {"facts", "cards", "bands"} <= {b["kind"] for b in blocks(document, "overview")}


@pytest.mark.asyncio
async def test_confirmed_qc_copy_and_dynamic_identity_match_html_without_inventing_signatures(document):
    before = deepcopy(document)
    view = project_display_report(document, template_version=LATEST_DISPLAY_TEMPLATE)
    qc = next(b for b in blocks(view, "appendix") if b["title"] == "4.1 质控校验")
    assert qc == next(b for b in blocks(document, "appendix") if b["title"] == "4.1 质控校验")
    rows = quality_records(qc)
    assert rows["姓名"] == "合成测试样本" and rows["年龄"] == "28 岁" and rows["性别"] == "男"
    assert rows["样本类型"] == "血液" and rows["送检项目"] == "肠道屏障三项"
    assert all(rows[k] == "合格" for k in ("样本状态", "样本制备质控", "检测数据质控"))
    assert rows["检验医师"] is rows["审核医师"] is None
    facts = next(b for b in blocks(document, "overview") if b["title"] == "受检者信息")
    assert next(item["value"] for item in facts["items"] if item["label"] == "样本类型") == rows["样本类型"]
    # The shared asset stays patient-independent; callback owns this report's QC.
    asset_qc = next(b for b in load_static_template(LATEST_DISPLAY_TEMPLATE)["appendixBlocks"] if b["title"] == "4.1 质控校验")
    assert all(value is None for value in quality_records(asset_qc).values())
    assert [b["title"] for b in blocks(document, "appendix")] == ["4.1 质控校验", "4.4 参考文献"]
    assert all(set(item) == {"citation", "url", "year", "publisher", "doi", "location"}
               for block in blocks(document, "appendix") if block["kind"] == "references" for item in block["items"])
    html = render_report_html(document, template_version=LATEST_DISPLAY_TEMPLATE)
    assert visible_source(html).count("<h3>4.1 质控校验</h3>") == 1
    qc_html = visible_source(html).split("<h3>4.1 质控校验</h3>", 1)[1].split("</table>", 1)[0]
    assert 'data-label="本次记录">血液</td>' in qc_html
    assert 'data-label="本次记录">肠道屏障三项</td>' in qc_html
    assert qc_html.count('data-label="本次记录">合格</td>') == 3
    assert embedded(html) == document == before


@pytest.mark.asyncio
async def test_display_bundle_retains_zero_false_null_and_does_not_share_source(document):
    cards = next(b for b in blocks(document, "overview") if b["kind"] == "cards")
    cards["items"][0]["value"] = 0
    cards["items"][0]["detail"] = None
    # Preserve an explicit hidden section without invalidating the v10 frozen
    # bibliography required by the still-visible fixed chapter's citations.
    next(section for section in document["sections"] if section["id"] == "disease_risks")["visible"] = False
    before = deepcopy(document)
    view = project_display_report(document, template_version=LATEST_DISPLAY_TEMPLATE)
    assert view["sourceReport"] == before == document
    assert view["sourceReport"] is not document
    source_cards = next(b for b in blocks(view["sourceReport"], "overview") if b["kind"] == "cards")
    source_cards["items"][0]["value"] = 99
    assert document == before


@pytest.mark.asyncio
async def test_v10_hidden_reference_appendix_fails_explicitly_without_rebinding_or_mutating(document):
    next(section for section in document["sections"] if section["id"] == "appendix")["visible"] = False
    before = deepcopy(document)
    for compose in (project_display_report, render_report_html):
        with pytest.raises(ValueError, match="v10 display requires a frozen main reference block"):
            compose(document, template_version=LATEST_DISPLAY_TEMPLATE)
    assert document == before
    assert next(section for section in document["sections"] if section["id"] == "appendix")["visible"] is False


@pytest.mark.asyncio
async def test_incomplete_chapters_keep_cover_and_score_blocks_like_html(document):
    document["sections"] = [s for s in document["sections"] if s["id"] != "disease_risks"]
    expanded = expand_dynamic_report(document, template_version=LATEST_DISPLAY_TEMPLATE)
    view = project_display_report(document, template_version=LATEST_DISPLAY_TEMPLATE)
    assert all(b in blocks(expanded, "overview") for b in blocks(view, "overview"))
    html = render_report_html(document, template_version=LATEST_DISPLAY_TEMPLATE)
    assert embedded(html) == document == view["sourceReport"]
    assert "合成测试样本" in visible_source(html)
    assert any(b["kind"] == "cards" for b in blocks(view["sourceReport"], "overview"))


@pytest.mark.asyncio
async def test_conflicting_cover_facts_do_not_choose_an_identity_for_qc(internal_document):
    facts = next(b for b in blocks(internal_document, "overview") if b["title"] == "受检者信息")
    facts["items"].append({"label": "姓名", "value": "另一个人"})
    document = project_callback_report(internal_document)
    view = project_display_report(document, template_version=LATEST_DISPLAY_TEMPLATE)
    qc = next(b for b in blocks(view, "appendix") if b["title"] == "4.1 质控校验")
    assert quality_records(qc)["姓名"] is None


@pytest.mark.asyncio
async def test_hidden_overview_cannot_reveal_identity_in_visible_qc(internal_document):
    next(s for s in internal_document["sections"] if s["id"] == "overview")["visible"] = False
    document = project_callback_report(internal_document)
    view = project_display_report(document, template_version=LATEST_DISPLAY_TEMPLATE)
    qc = next(b for b in blocks(view, "appendix") if b["title"] == "4.1 质控校验")
    rows = quality_records(qc)
    assert rows["样本类型"] == "血液" and rows["送检项目"] == "肠道屏障三项"
    assert all(rows[k] == "合格" for k in ("样本状态", "样本制备质控", "检测数据质控"))
    assert all(rows[k] is None for k in ("姓名", "性别", "年龄", "检验医师", "审核医师"))


@pytest.mark.asyncio
async def test_word_principle_captions_are_not_overwritten_by_refined_release(document):
    assert LATEST_DISPLAY_TEMPLATE == "gut-barrier-static.v14"
    assert PRODUCT_FEEDBACK_DISPLAY_TEMPLATE == "gut-barrier-static.v9"
    assert CANONICAL_DISPLAY_TEMPLATE == "gut-barrier-static.v8"
    assert WORD_REVIEW_DISPLAY_TEMPLATE == "gut-barrier-static.v7"
    html = render_report_html(document, template_version=LATEST_DISPLAY_TEMPLATE)
    for text in ("去除诱因，阻止持续性损伤。", "补充营养，改善肠上皮细胞能量代谢与黏膜微循环。",
                 "重建肠道生态，黏液层快速修复与再生。", "靶向调节，巩固定植，修复紧密连接与免疫屏障。",
                 "维持肠道稳态，实现机体整体健康。"):
        assert text in html
    assert "图示为五阶段干预目标" in html
    body = visible_source(html)
    assert "第四章 附录" in body and "4.4 参考文献" in body
    assert "PFMT" not in body and "FMT" in body
    assert not re.search(r"(?:第五章|第五部分|5\.[1-5] )", body)
    section_ids = [attrs["id"] for tag, attrs in Parsed(body).tags if tag == "section" and "id" in attrs]
    assert section_ids == ["overview", "disease_risks", "repair_plan", "appendix"]
    assert 'id="education"' not in body
    assert [s["id"] for s in document["sections"]] == ["overview", "disease_risks", "appendix"]
    assert [b["title"] for b in blocks(document, "appendix")] == ["4.1 质控校验", "4.4 参考文献"]


@pytest.mark.asyncio
async def test_no_disease_data_keeps_conclusion_without_fake_zero_risk():
    full, _ = await GutBarrierPipeline().generate(GutBarrierRequest.model_validate({
        "userId": "empty-test", "sessionId": "empty-test", "age": 28,
        "gutHealthIndicator": {"complete": True}}), report_id="empty-test")
    report = project_callback_report(full)
    disease = blocks(report, "disease_risks")
    assert disease[-1]["title"] == "2.9 本章小结"
    from tests.unit.gut_barrier.template_risk_assertions import assert_template_risk_contract
    rows, overview, tables = assert_template_risk_contract(report, full["assessment"])
    assert len(rows) == 23 and len(tables) == 8
    assert overview["rows"] == [] and all(row[1:3] == [None, None] for row in rows.values())
    html = render_report_html(report, template_version=LATEST_DISPLAY_TEMPLATE)
    assert "本章小结" in html and 'class="risk-plot-row"' not in html
    assert embedded(html) == report
    table = next(b for b in blocks(report, "overview") if b["title"] == "1.2 检测项目与结果总览")
    assert table["columns"] == MARKER_COLUMNS and len(table["rows"]) == 3
    assert all(row[2] is None for row in table["rows"])
    qc = next(b for b in blocks(report, "appendix") if b["title"] == "4.1 质控校验")
    assert quality_records(qc) == {
        "姓名": None, "性别": None, "年龄": "28 岁", "样本类型": "血液",
        "送检项目": "肠道屏障三项", "样本状态": "合格",
        "样本制备质控": "合格", "检测数据质控": "合格", "检验医师": None, "审核医师": None,
    }
    view = project_display_report(report, template_version=LATEST_DISPLAY_TEMPLATE)
    assert [b for b in blocks(view, "appendix") if b["title"] == "4.1 质控校验"] == [qc]
    qc_html = visible_source(html).split("<h3>4.1 质控校验</h3>", 1)[1].split("</table>", 1)[0]
    assert "血液" in qc_html and "肠道屏障三项" in qc_html
    assert qc_html.count('data-label="本次记录">合格</td>') == 3


@pytest.mark.asyncio
async def test_frozen_qc_values_are_not_rebound_or_duplicated_in_html(document):
    qc = next(b for b in blocks(document, "appendix") if b["title"] == "4.1 质控校验")
    qc["rows"] = [["姓名", "已冻结姓名", "性别", False], ["年龄", 0, "样本类型", "已记录样本"],
                  ["送检项目", "已冻结项目", "样本状态", "已记录状态"],
                  ["样本制备质控", "已记录制备", "检测数据质控", "已记录分析"],
                  ["检验医师", "已记录检验人", "审核医师", None]]
    original = deepcopy(document)
    assert project_callback_report(document) == original
    view = project_display_report(document, template_version=LATEST_DISPLAY_TEMPLATE)
    assert [b for b in blocks(view, "appendix") if b["title"] == "4.1 质控校验"] == [qc]
    html = render_report_html(document, template_version=LATEST_DISPLAY_TEMPLATE)
    body = visible_source(html)
    assert body.count("<h3>4.1 质控校验</h3>") == 1
    assert all(value in body for value in ("已冻结姓名", "已冻结项目", "已记录样本", "已记录检验人"))
    assert embedded(html) == document == original


@pytest.mark.asyncio
@pytest.mark.parametrize("score, percentage", [(0, "0.00%"), (5, "33.33%"), (15, "100.00%"), (None, None)])
async def test_score_share_html_keeps_ibfc_and_legacy_grade_only_internally(internal_document, score, percentage):
    original_cards = next(b for b in blocks(internal_document, "overview") if b["kind"] == "cards")
    next(item for item in original_cards["items"] if item["label"] == "IBFC评分")["value"] = score
    original_grade = next(item["value"] for item in original_cards["items"] if item["label"] == "损伤等级")
    document = project_callback_report(internal_document)
    cards = next(b for b in blocks(document, "overview") if b["kind"] == "cards")
    share = next(item for item in cards["items"] if item["label"] == "评分占满分比例")
    assert share == {"label": "评分占满分比例", "value": percentage, "detail": "IBFC 满分为 15 分"}
    assert next(item["value"] for item in cards["items"] if item["label"] == "IBFC评分") == score
    assert next(item for item in cards["items"] if item["label"] == "损伤等级") == {
        "label": "损伤等级", "value": None, "detail": None}
    assert next(item["value"] for item in original_cards["items"] if item["label"] == "损伤等级") == original_grade
    html = render_report_html(document, template_version=LATEST_DISPLAY_TEMPLATE)
    value = re.search(r'<span>评分占满分比例</span><strong>(.*?)</strong>',
                      visible_source(html)).group(1)
    assert value == (percentage if percentage is not None else "未提供")
    assert visible_source(html).count("评分占满分比例") == 1
    assert '<span>损伤等级</span>' not in visible_source(html)
    assert embedded(html) == document


@pytest.mark.parametrize("count", [0, 1, 3])
def test_v8_disease_tables_keep_wire_columns_and_number_summary_after_actual_systems(count, historical_templates):
    internal = clinical_report()
    internal["meta"]["displayTemplateVersion"] = CANONICAL_DISPLAY_TEMPLATE
    disease = next(s for s in internal["sections"] if s["id"] == "disease_risks")
    tables = [{"kind": "table", "title": f"2.{8 - index} 合成系统{index}", "columns": RISK_COLUMNS,
               "rows": [[f"合成方向{index}", index, "基础关注", f"本次资料依据{index}"]]}
              for index in range(count)]
    disease["blocks"] = [{"kind": "callout", "title": "本次关注指数", "text": "关注排序", "tone": "default"},
                         {"kind": "list", "title": "本章小结", "items": ["合成总结正文"]}, *tables,
                         {"kind": "paragraph", "title": "临床背景", "text": "保留临床背景"}]
    report = project_callback_report(internal)
    published = blocks(report, "disease_risks")
    assert published[-1] == {"kind": "list", "title": f"2.{count + 1} 本章小结", "items": ["合成总结正文"]}
    expected_tables = [{**table, "title": f"2.{index + 1} 合成系统{index}"} for index, table in enumerate(tables)]
    assert [b for b in published if b["kind"] == "table" and b["columns"] == RISK_COLUMNS] == expected_tables
    expected_overview = {"kind": "table", "title": ATTENTION_OVERVIEW_TITLE,
                         "columns": ATTENTION_OVERVIEW_COLUMNS,
                         "rows": [[f"合成系统{index}", f"合成方向{index}", index, "基础关注"]
                                  for index in range(count)]}
    assert [b for b in published if b["kind"] == "table" and b["columns"] == ATTENTION_OVERVIEW_COLUMNS] == [expected_overview]
    assert published[-2]["kind"] == "paragraph" and published[-2]["title"] == published[-1]["title"]
    assert published[-2]["text"]
    view = project_display_report(report, template_version=CANONICAL_DISPLAY_TEMPLATE)
    assert [b for b in blocks(view, "disease_risks") if b["kind"] == "table" and b["columns"] == RISK_COLUMNS] == expected_tables
    assert [b for b in blocks(view, "disease_risks") if b["kind"] == "table" and b["columns"] == ATTENTION_OVERVIEW_COLUMNS] == [expected_overview]
    html = render_report_html(report, template_version=CANONICAL_DISPLAY_TEMPLATE)
    assert embedded(html) == report
    assert visible_source(html).count(f"<h3>2.{count + 1} 本章小结</h3>") == 1
    assert "合成总结正文" in visible_source(html)
    for table in expected_tables:
        assert table["title"] in visible_source(html)
        assert all(f"data-label=\"{column}\"" in visible_source(html) for column in RISK_COLUMNS)


@pytest.mark.asyncio
async def test_current_publication_is_idempotent_and_composition_preserves_input(document):
    original = deepcopy(document)
    assert project_callback_report(document) == document
    assert project_callback_report(project_callback_report(document)) == document
    expanded = expand_dynamic_report(document, template_version=LATEST_DISPLAY_TEMPLATE)
    assert expand_dynamic_report(document, template_version=LATEST_DISPLAY_TEMPLATE) == expanded
    assert expand_dynamic_report(expanded, template_version=LATEST_DISPLAY_TEMPLATE)["sections"] == expanded["sections"]
    expanded["sections"][0]["blocks"].clear()
    assert document == original


@pytest.mark.asyncio
async def test_previous_v10_normalizer_is_idempotent_for_frozen_receipts(
        internal_document, historical_ordered_release):
    internal_document["meta"]["displayTemplateVersion"] = ORDERED_REFERENCE_DISPLAY_TEMPLATE
    document = project_callback_report(internal_document, archive_directory=historical_ordered_release)
    original = deepcopy(document)
    normalized = normalize_current_dynamic_report(document, product_feedback=True)
    assert normalize_current_dynamic_report(normalized, product_feedback=True) == normalized == original
    normalized["sections"][0]["blocks"].clear()
    assert document == original


@pytest.mark.asyncio
async def test_v8_callback_composition_has_stable_correct_and_unforgeable_reference_aliases(internal_document, historical_templates):
    internal_document["meta"]["displayTemplateVersion"] = CANONICAL_DISPLAY_TEMPLATE
    document = project_callback_report(internal_document)
    static = next(b for b in load_static_template(CANONICAL_DISPLAY_TEMPLATE)["appendixBlocks"]
                  if b["title"] == "营养与微生态干预补充依据")["items"]
    who_index = next(index for index, item in enumerate(static, 1)
                     if item.get("url") == "https://www.who.int/publications/i/item/9789240015128")
    first_dynamic = len(static) + 1
    next(b for b in blocks(document, "appendix") if b["kind"] == "references")["items"] = [
        {"citation": "WHO重复版本", "url": "https://iris.who.int/bitstream/handle/10665/336656/9789240015128-eng.pdf"},
        {"citation": "追加新来源", "url": "https://evidence.invalid/new"},
    ]
    blocks(document, "overview").append({"kind": "paragraph", "title": "引用定位测试",
        "text": f"已知重复来源[补{first_dynamic}]与后续来源[补{first_dynamic + 1}]。"})
    original = deepcopy(document)
    expected = expand_dynamic_report(document, template_version=CANONICAL_DISPLAY_TEMPLATE)
    assert expected["_referenceAliases"]["supplement-1"][first_dynamic] == who_index
    assert expected["_referenceAliases"]["supplement-1"][first_dynamic + 1] == first_dynamic
    assert expand_dynamic_report(document, template_version=CANONICAL_DISPLAY_TEMPLATE) == expected
    injected = deepcopy(document)
    injected["_referenceAliases"] = {"supplement-1": {1: 99999}}
    assert expand_dynamic_report(injected, template_version=CANONICAL_DISPLAY_TEMPLATE) == expected
    html = render_report_html(document, template_version=CANONICAL_DISPLAY_TEMPLATE)
    paragraph = re.search(r'<h3>引用定位测试</h3><p>(.*?)</p>', visible_source(html), re.S)[1]
    assert f'href="#reference-supplement-1-{who_index}"' in paragraph
    assert f'href="#reference-supplement-1-{first_dynamic}"' in paragraph
    assert embedded(html) == original == document


def test_pre_v8_callback_keeps_frozen_titles_and_values_when_reprojected_or_rendered(historical_templates):
    report = project_callback_report(clinical_report())
    original = deepcopy(report)
    assert any(b["title"] == "三项检测指标" for b in blocks(report, "overview"))
    assert blocks(report, "appendix")[0]["title"] == "5.5 参考文献"
    # Even an untrusted public metadata field cannot request migration.
    report["meta"]["displayTemplateVersion"] = CANONICAL_DISPLAY_TEMPLATE
    assert project_callback_report(report) == original
    report["meta"].pop("displayTemplateVersion")
    html = render_report_html(report, template_version=WORD_REVIEW_DISPLAY_TEMPLATE)
    assert embedded(html) == original == report
    assert "5.4 参考文献" in visible_source(html)
    assert 'id="education"' in visible_source(html)
