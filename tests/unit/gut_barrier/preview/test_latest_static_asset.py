from tests.unit.gut_barrier.preview.archive_support import historical_asset
"""The latest fixed copy is a new, patient-independent frontend snapshot.

These tests deliberately read the two assets directly: choosing or expanding a
template is the adapter's concern, and must never rebuild historical prose.
"""
from hashlib import sha256
import json
from pathlib import Path
import re

import pytest


_ROOT = Path(__file__).resolve().parents[3]
_OLD_SHA256 = "62b4a6ebc0b9f0c7c8e4b09e05bfeaebbb876bd3e20431bcf9b41e18983ce9a0"


def _asset(version="v2"):
    return json.loads(historical_asset(version).read_text(encoding="utf-8"))


def _section(identifier, asset=None):
    return next(section for section in (asset or _asset())["fixedSections"] if section["id"] == identifier)


def _numbered(blocks):
    return [block["title"] for block in blocks if re.match(r"^[1-5]\.\d+\s", block.get("title") or "")]


def _strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, str):
        yield value


def test_new_asset_is_explicitly_versioned_without_changing_existing_keys():
    latest, legacy = _asset(), _asset("v1")
    assert set(latest) == set(legacy)
    assert latest["templateVersion"] == "gut-barrier-static.v2"
    assert latest["reportSchemaVersion"] == legacy["reportSchemaVersion"] == "gut-barrier-public.v2"
    assert set(latest["sourceVersions"]) == set(legacy["sourceVersions"])
    assert latest["sourceVersions"] != legacy["sourceVersions"]
    assert latest["sectionOrder"] == legacy["sectionOrder"]
    assert [section["id"] for section in latest["fixedSections"]] == ["repair_plan", "education"]
    for section, previous in zip(latest["fixedSections"], legacy["fixedSections"]):
        assert set(section) == set(previous)
        assert section["visible"] is True
        for block, old_block in zip(section["blocks"], previous["blocks"]):
            assert set(block) == set(old_block)


def test_previous_asset_bytes_are_frozen():
    assert sha256(historical_asset("v1").read_bytes()).hexdigest() == _OLD_SHA256


def test_overview_anchors_still_address_existing_callback_blocks():
    latest, legacy = _asset(), _asset("v1")
    assert latest["overviewSlots"] == legacy["overviewSlots"]
    for insertion, previous in zip(latest["overviewInsertions"], legacy["overviewInsertions"]):
        assert set(insertion) == set(previous)
        assert insertion["anchor"] == previous["anchor"]
        assert insertion["position"] == previous["position"]
    assert latest["overviewInsertions"][0]["block"]["title"] == "1.2 检测项目与结果总览"
    assert latest["overviewInsertions"][1]["block"] == legacy["overviewInsertions"][1]["block"]


def test_repair_plan_has_five_subsections_without_losing_indication_and_safety_copy():
    repair = _section("repair_plan")
    assert repair["title"] == "第三章 肠道屏障分层修复方案"
    assert _numbered(repair["blocks"]) == [
        "3.1 修复总原则：五原则框架",
        "3.2 肠道营养补充",
        "3.3 精准肠道菌群移植（PFMT）",
        "3.4 靶向功能菌群调节（MDF）",
        "3.5 生活方式干预",
    ]
    legacy = _section("repair_plan", _asset("v1"))
    # The removal principle is nested under the overview, not deleted.
    removal = next(block for block in legacy["blocks"] if (block.get("title") or "").startswith("3.2 "))
    assert {"kind": "paragraph", "title": None, "text": removal["text"]} in repair["blocks"]
    assert [block.get("text") for block in repair["blocks"]] == [block.get("text") for block in legacy["blocks"]]
    text = "\n".join(_strings(repair))
    for safety in ("不依据IBFC等级统一启动输注", "不建议在临床试验之外常规实施", "不能默认改用后生元即可安全", "具体配方另行进行个体化确认"):
        assert safety in text


def test_education_retains_six_detailed_sections_and_nonpersonal_mechanisms():
    education = _section("education")
    assert education["title"] == "第四章 肠道屏障与慢性低度炎症概述"
    titles = _numbered(education["blocks"])
    assert [title.split()[0] for title in titles] == [f"4.{index}" for index in range(1, 7)]
    assert titles[3] == "4.4 互为因果：炎症反噬屏障的恶性循环"
    legacy = _section("education", _asset("v1"))
    assert [block["text"] for block in education["blocks"]] == [block["text"] for block in legacy["blocks"]]
    assert all(len(block["text"]) > 300 for block in education["blocks"])
    assert "不能把示意图当作个人检测曲线" in education["blocks"][-1]["text"]


def test_appendix_has_four_subsections_and_keeps_attention_scale_limitations():
    blocks = _asset()["appendixBlocks"]
    assert _numbered(blocks) == [
        "5.1 质量控制校验", "5.2 检测方法学介绍", "5.3 免责声明", "5.4 参考文献",
    ]
    disclaimer = next(index for index, block in enumerate(blocks) if block.get("title") == "5.3 免责声明")
    scale = blocks[disclaimer + 1]
    assert scale["kind"] == "paragraph" and scale["title"] is None
    for boundary in ("尚未经过临床验证", "不表示疾病风险高低", "不能用于诊断和治疗决策", "包括0分", "不与自定义关注指数合并排序"):
        assert boundary in scale["text"]
    assert "本次样本已经取得某项质控结论" in "\n".join(_strings(blocks))


def test_disease_mechanisms_are_compatibility_copy_not_a_new_patient_catalog():
    latest, legacy = _asset(), _asset("v1")
    assert latest["diseaseMechanisms"] == legacy["diseaseMechanisms"]
    assert {item["columnIndex"] for item in latest["diseaseMechanisms"]} == {3}
    assert "动态关注模式不回填通用机制" in latest["composition"]["disease_risks"]
    assert "不据此新增疾病、指数或等级" in latest["composition"]["disease_risks"]
    assert "不按标题、日期或患者内容猜版本" in latest["composition"]["data"]


def test_citation_numbers_remain_in_bounds_and_references_preserve_identity():
    latest, legacy = _asset(), _asset("v1")
    current_groups = [block for block in latest["appendixBlocks"] if block["kind"] == "references"]
    old_groups = [block for block in legacy["appendixBlocks"] if block["kind"] == "references"]
    assert [len(group["items"]) for group in current_groups] == [14, 25]
    for current, old in zip(current_groups, old_groups):
        for item, previous in zip(current["items"], old["items"]):
            assert set(item) == set(previous)
            assert {key: value for key, value in item.items() if key != "location"} == {
                key: value for key, value in previous.items() if key != "location"
            }
    # Check all prose, including historical mechanism copy; citations must keep
    # their numbered source group when appendix section numbers change.
    prose = "\n".join(_strings({key: value for key, value in latest.items() if key != "appendixBlocks"}))
    for supplement, numbers in re.findall(r"\[(补?)([\d,]+)\]", prose):
        limit = len(current_groups[bool(supplement)]["items"])
        assert all(1 <= int(number) <= limit for number in numbers.split(","))
    locations = "\n".join(item["location"] or "" for item in current_groups[1]["items"])
    assert "3.6" not in locations
    assert "3.2、3.4、4.1" in locations
    assert "3.3移植适应证" in locations


@pytest.mark.parametrize("unverified", [
    "陈先生", "张学斌", "测试小陈", "IBF-2026-0918", "IBF-R-2026-0087",
    "高于68%", "WHO国际标准", "0.3–0.5g/kg", "2–4倍", "升高22%",
])
def test_no_template_patient_or_unsupported_precision_is_frozen(unverified):
    assert unverified not in json.dumps(_asset(), ensure_ascii=False)


def test_static_asset_contains_no_callback_identity_or_patient_measurement_keys():
    forbidden = {"userId", "sessionId", "subject", "reportId", "riskIndex", "qualityControlRecords"}

    def inspect(value):
        if isinstance(value, dict):
            assert not forbidden.intersection(value)
            for child in value.values():
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    inspect(_asset())
