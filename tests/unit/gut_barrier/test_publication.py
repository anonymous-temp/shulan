"""Public callback projection preserves patient content, not internal objects."""

from copy import deepcopy
import json

import pytest

from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.interventions import build_interventions
from modules.gut_barrier.presentation import build_report
from modules.gut_barrier.publication import project_public_report
from modules.gut_barrier.schemas import GutBarrierRequest


PATTERNS = ("+--", "-+-", "--+", "++-", "+-+", "-++", "+++", "---")
PRIVATE_KEYS = {
    "assessment", "subject", "interventions", "summary", "references", "sourceSummary",
    "microbiomeSources", "narrativeCandidates", "interpretation", "provenance", "rawData",
    "audit", "sourcePath", "localPath", "signedUrl", "internalOnly",
}


def clinical_report(pattern="+++", *, age=35, crp=4.2, missing=False):
    payload = {"userId": "PUBLIC-USER", "sessionId": "PUBLIC-SESSION", "name": "受检者",
               "age": age, "sex": "男"}
    if not missing:
        payload["gutHealthIndicator"] = {
            key: {"value": high if sign == "+" else low, "unit": unit}
            for key, high, low, unit, sign in zip(
                ("dLacticAcid", "diamineOxidase", "bacterialEndotoxin"),
                (19, 13, 24), (5, 5, 10), ("mg/L", "U/L", "U/L"), pattern)
        }
        payload["gutHealthIndicator"]["hsCRP"] = {"value": crp, "unit": "mg/L"}
    assessed = assess_case(GutBarrierRequest.model_validate(payload), context={"currentDate": "2026-09-23"})
    plans = build_interventions(assessed, context=assessed.get("context", {}))
    return build_report(assessed, plans, report_id="gbr_public_test")


@pytest.mark.parametrize("pattern", PATTERNS)
def test_all_seven_patterns_and_normal_preserve_public_content_contract(pattern):
    report = clinical_report(pattern)
    before = deepcopy(report)
    public = project_public_report(report)
    assert set(public) == {"schemaVersion", "meta", "sections"}
    assert public["schemaVersion"] == "gut-barrier-public.v1"
    assert set(public["meta"]) == {"title", "reportId"}
    assert report == before
    assert project_public_report(public) == public
    assert len(json.dumps(public, ensure_ascii=False)) < len(json.dumps(report, ensure_ascii=False))
    assert [section["id"] for section in public["sections"]] == [
        "overview", "disease_risks", "repair_plan", "education", "appendix"]
    assert all(set(section) == {"id", "title", "subtitle", "visible", "blocks"}
               for section in public["sections"])
    assert report["assessment"]["barrier"]["pattern"] == pattern


@pytest.mark.parametrize("kwargs", [{"missing": True}, {"age": 17}, {"age": None}, {"crp": 12}])
def test_missing_minor_unknown_age_and_high_crp_safety_paths_survive_projection(kwargs):
    report = clinical_report(**kwargs)
    public = project_public_report(report)
    body = json.dumps(public, ensure_ascii=False)
    if kwargs.get("missing"):
        assert "暂不能评分" in body and "尚需补充" in body
    elif "age" in kwargs:
        assert "不套用成人" in body and "年龄" in body
        assert "hs-CRP 项目功能分层" not in body
    else:
        assert "12 mg/L" not in body and "hs-CRP" not in body
        assert "inflammation" not in report["assessment"]



@pytest.mark.parametrize("selected", [
    [{"id": "second", "text": "INJECTED DIAGNOSIS"}, {"id": "unknown", "text": "UNAPPROVED"}, "second"],
    {"paragraphIds": ["second", "second", "unknown"]},
    [], None,
])
def test_interpretation_resolves_whitelist_order_deduplication_and_required_paragraphs(selected):
    report = {
        "meta": {"title": "结果解析", "reportId": "gbr_narrative"},
        "sections": [{"id": "interpretation", "title": "结果解析", "blocks": [
            {"kind": "paragraph", "text": "UNRESOLVED FALLBACK"}]}],
        "narrativeCandidates": [
            {"id": "first", "text": "必选且经过审核的结论", "required": True},
            {"id": "second", "text": "经过审核的机制介绍", "required": False},
            {"id": "third", "text": "可选内容", "required": False}],
        "interpretation": selected,
    }
    public = project_public_report(report)
    texts = [block["text"] for block in public["sections"][0]["blocks"]]
    assert "必选且经过审核的结论" in texts
    assert len(texts) == len(set(texts))
    assert "INJECTED" not in json.dumps(public) and "UNAPPROVED" not in json.dumps(public)
    if selected and selected is not None:
        assert texts[0] == "经过审核的机制介绍"
        assert texts[-1] == "必选且经过审核的结论"


def all_block_document():
    attack = '</script><img src=x onerror="alert(1)"><script>alert(2)</script>'
    return {
        "meta": {"title": "所有呈现类型", "reportId": "gbr_all_blocks", "pipelineVersion": "PRIVATE-META"},
        "sections": [{"id": "results", "title": "结果", "subtitle": "副标题", "visible": True, "blocks": [
            {"kind": "paragraph", "title": "解释", "text": attack},
            {"kind": "table", "columns": ["项目", "结果"], "rows": [
                {"cells": ["当前行", {"text": "已审结果", "rawData": "PRIVATE-CELL"}], "current": True},
                {"cells": [False, 0], "current": False}, [None, ""]]},
            {"kind": "facts", "items": [{"label": "缺失项", "value": None},
                {"label": "零值", "value": 0}, {"label": "布尔值", "value": False}]},
            {"kind": "cards", "items": [{"label": "卡片", "value": 0, "detail": None}]},
            {"kind": "bands", "items": [{"range": "0–1", "label": "未选中", "selected": False},
                {"range": "1–2", "label": "选中", "selected": True}]},
            {"kind": "list", "items": ["列表", {"label": "嵌套展示文字", "audit": "PRIVATE-LIST"}, False, 0, None]},
            {"kind": "callout", "tone": "caution", "text": "需及时就医"},
            {"kind": "references", "items": [{"citation": "已审核文献", "title": "标题", "year": 2026,
                "publisher": "机构", "doi": "10.test/demo", "location": "第1页", "url": "https://example.org/evidence"},
                {"name": "其他参考", "link": "javascript:alert(3)"}]},
            {"kind": "future-fallback", "text": "未知类型也有可见正文"},
        ]}, {"id": "hidden", "title": "不显示的章节", "visible": False, "blocks": []}],
        "assessment": {"secret": "PRIVATE-ASSESSMENT"}, "audit": {"secret": "PRIVATE-AUDIT"},
        "sourceSummary": [{"signedUrl": "https://source.invalid/?token=PRIVATE-SIGNATURE"}],
    }


@pytest.mark.parametrize("candidates", [None, "not-a-list"])
def test_interpretation_without_candidate_list_keeps_existing_safe_blocks(candidates):
    report = {"meta": {"title": "结论", "reportId": "gbr_fallback"},
              "narrativeCandidates": candidates,
              "sections": [{"id": "interpretation", "title": "结论", "blocks": [
                  {"kind": "paragraph", "text": "已生成的解析正文"}]}]}
    before = deepcopy(report)
    public = project_public_report(report)
    assert public["sections"][0]["blocks"] == [
        {"kind": "paragraph", "title": None, "text": "已生成的解析正文"}]
    assert report == before


def _inject_hidden_keys(value):
    if isinstance(value, dict):
        for item in tuple(value.values()):
            _inject_hidden_keys(item)
        value["internalOnly"] = {"sourcePath": "/private/PRIVATE-PATH", "rawData": "PRIVATE-RAW"}
    elif isinstance(value, list):
        for item in value:
            _inject_hidden_keys(item)


def _keys(value):
    if isinstance(value, dict):
        yield from value
        for item in value.values():
            yield from _keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _keys(item)


def test_all_blocks_nested_whitelists_and_nonmutation():
    report = all_block_document()
    _inject_hidden_keys(report)
    before = deepcopy(report)
    public = project_public_report(report)
    assert report == before
    serialized = json.dumps(public, ensure_ascii=False)
    assert "PRIVATE-" not in serialized
    assert not PRIVATE_KEYS.intersection(_keys(public))
    public["sections"][0]["blocks"][1]["rows"][0]["cells"][0] = "修改副本"
    assert report == before


def test_explicit_null_empty_false_and_zero_values_are_not_filtered_out():
    public = project_public_report(all_block_document())
    blocks = {block["kind"]: block for block in public["sections"][0]["blocks"]}
    assert blocks["facts"]["items"] == [
        {"label": "缺失项", "value": None}, {"label": "零值", "value": 0},
        {"label": "布尔值", "value": False}]
    assert blocks["table"]["rows"][1] == {"cells": [False, 0], "current": False}
    assert blocks["table"]["rows"][2] == [None, ""]
    assert blocks["bands"]["items"][0]["selected"] is False
    assert blocks["cards"]["items"][0]["value"] == 0
    assert blocks["cards"]["items"][0]["detail"] is None


@pytest.mark.parametrize("url", [
    {"rawData": "PRIVATE-REFERENCE", "url": "https://example.org"},
    [{"audit": "PRIVATE-REFERENCE"}], "javascript:alert(1)", "file:///private/patient.json",
    "https://example.org/\nPRIVATE-REFERENCE", "http://[malformed",
])
def test_invalid_reference_links_cannot_carry_nested_or_local_internal_data(url):
    report = {"meta": {}, "sections": [{"id": "evidence", "title": "依据", "blocks": [
        {"kind": "references", "items": [{"title": "安全文献名称", "url": url}]}]}]}
    public = project_public_report(report)
    entry = public["sections"][0]["blocks"][0]["items"][0]
    assert entry == {"citation": "安全文献名称", "url": None, "year": None,
                     "publisher": None, "doi": None, "location": None}
    assert "PRIVATE-" not in json.dumps(public)


def test_deeply_nested_text_wrappers_never_stringify_private_objects():
    value = {"text": [{"text": {"label": "可展示的数值", "audit": "PRIVATE-INNER"},
                        "sourcePath": "PRIVATE-PATH"}, {"rawData": "PRIVATE-RAW"}],
             "internalOnly": "PRIVATE-OUTER"}
    report = {"meta": {}, "sections": [{"id": "summary", "blocks": [
        {"kind": "paragraph", "text": value},
        {"kind": "table", "columns": ["项目"], "rows": [[value]]},
        {"kind": "facts", "items": [{"label": "字段", "value": value}]}]}]}
    original = deepcopy(report)
    public = project_public_report(report)
    assert "PRIVATE-" not in json.dumps(public)
    assert "可展示的数值" in json.dumps(public, ensure_ascii=False)
    assert report == original
    assert not PRIVATE_KEYS.intersection(_keys(public))


def test_hidden_and_nonobject_sections_preserve_later_section_numbers_without_their_data():
    report = {"meta": {}, "sections": [
        {"id": "first", "title": "第一节", "blocks": []},
        {"id": "hidden", "visible": False, "title": "PRIVATE-HIDDEN", "blocks": [
            {"kind": "paragraph", "text": "PRIVATE-HIDDEN-BODY"}]},
        "PRIVATE-NONSECTION",
        {"id": "last", "title": "第四节", "blocks": []},
    ]}
    public = project_public_report(report)
    hidden = {"id": None, "title": None, "subtitle": None, "blocks": [], "visible": False}
    assert public["sections"][1:3] == [hidden, hidden]
    assert "PRIVATE-" not in json.dumps(public)


def test_every_kind_has_fixed_keys_even_when_all_values_are_missing():
    kinds = ["paragraph", "table", "facts", "cards", "bands", "list", "callout", "references"]
    report = {"meta": {}, "sections": [{"blocks": [{"kind": kind} for kind in kinds]}]}
    public = project_public_report(report)
    assert public["meta"] == {"title": None, "reportId": None}
    section = public["sections"][0]
    assert set(section) == {"id", "title", "subtitle", "blocks", "visible"}
    expected = {"paragraph": {"text"}, "table": {"columns", "rows"}, "callout": {"text", "tone"}}
    for block in section["blocks"]:
        assert set(block) == {"kind", "title"} | expected.get(block["kind"], {"items"})
        assert block["title"] is None
    assert project_public_report(public) == public


def test_nested_kind_items_are_defaulted_without_erasing_false_zero_or_row_union():
    report = {"meta": {"title": 0, "reportId": False}, "sections": [{"blocks": [
        {"kind": "facts", "items": [{}]}, {"kind": "cards", "items": [{}]},
        {"kind": "bands", "items": [{}]}, {"kind": "references", "items": [{}, {"year": 0, "publisher": False}]},
        {"kind": "table", "rows": [{"cells": [0, False, None]}, [0, False, None]]},
    ]}]}
    public = project_public_report(report)
    assert public["meta"] == {"title": 0, "reportId": False}
    blocks = public["sections"][0]["blocks"]
    assert blocks[0]["items"] == [{"label": None, "value": None}]
    assert blocks[1]["items"] == [{"label": None, "value": None, "detail": None}]
    assert blocks[2]["items"] == [{"range": None, "label": None, "selected": False}]
    assert blocks[3]["items"][0] == dict.fromkeys(["citation", "url", "year", "publisher", "doi", "location"])
    assert blocks[3]["items"][1]["year"] == 0 and blocks[3]["items"][1]["publisher"] is False
    assert blocks[4]["rows"] == [{"cells": [0, False, None], "current": False}, [0, False, None]]


def test_mixed_interpretation_never_drops_a_table_or_titled_subsection():
    report = {"meta": {}, "narrativeCandidates": [
        {"id": "first", "text": "已审核结论一", "required": True},
        {"id": "second", "text": "已审核结论二"}],
        "interpretation": ["second", "first"], "sections": [{"id": "interpretation", "blocks": [
            {"kind": "paragraph", "title": "固定机制小节", "text": "必须保留的科普"},
            {"kind": "paragraph", "text": "已审核结论一"},
            {"kind": "table", "columns": ["检测"], "rows": [[19]]},
            {"kind": "paragraph", "text": "已审核结论二"},
            {"kind": "list", "items": ["必要执行条件"]},
        ]}]}
    public = project_public_report(report)
    blocks = public["sections"][0]["blocks"]
    assert [block["kind"] for block in blocks] == ["paragraph", "paragraph", "table", "paragraph", "list"]
    assert blocks[0]["text"] == "必须保留的科普" and blocks[2]["rows"] == [[19]]
    assert blocks[1]["text"] == "已审核结论二" and blocks[3]["text"] == "已审核结论一"
    assert blocks[-1]["items"] == ["必要执行条件"]


def test_empty_candidates_cannot_erase_mixed_interpretation_chapter():
    report = {"meta": {}, "narrativeCandidates": [], "interpretation": [], "sections": [
        {"id": "interpretation", "blocks": [{"kind": "paragraph", "title": "固定小节", "text": "固定正文"},
                                               {"kind": "table", "columns": ["结果"], "rows": [[0]]}]}]}
    blocks = project_public_report(report)["sections"][0]["blocks"]
    assert len(blocks) == 2 and blocks[1]["rows"] == [[0]]


def test_legacy_reference_aliases_and_zero_false_citations_remain_visible_after_projection():
    report = {"sections": [{"blocks": [{"kind": "references", "items": [
        {"citation": None, "title": None, "name": "旧名字段"},
        {"citation": "", "title": "旧题名字段"},
        {"citation": 0, "year": 0}, {"citation": False},
    ]}]}]}
    public = project_public_report(report)
    assert [item["citation"] for item in public["sections"][0]["blocks"][0]["items"]] == ["旧名字段", "旧题名字段", 0, False]
