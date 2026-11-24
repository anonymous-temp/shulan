from tests.unit.gut_barrier.preview.archive_support import historical_asset
"""XU fixed content is an independent, patient-free v3 snapshot."""
from hashlib import sha256
import json
from pathlib import Path
import re

import pytest


_HISTORICAL_HASHES = {
    "v1": "62b4a6ebc0b9f0c7c8e4b09e05bfeaebbb876bd3e20431bcf9b41e18983ce9a0",
    "v2": "5162b8268c709db333b3a76e4e52daf75ef6e0db01db203102f14243e056036c",
}
_PRINCIPLES = [
    "去除：去除诱因，阻止持续性损伤。",
    "改善：补充营养，改善肠上皮细胞能量代谢与黏膜微循环。",
    "重建：重建肠道生态，黏液层快速修复与再生。",
    "修复：靶向调节，巩固定植，修复紧密连接与免疫屏障。",
    "平衡：维持肠道稳态，实现机体整体健康。",
]


def _asset(version="v3"):
    return json.loads(historical_asset(version).read_text(encoding="utf-8"))


def _section(identifier, asset=None):
    return next(section for section in (asset or _asset())["fixedSections"] if section["id"] == identifier)


def _prose(value):
    if isinstance(value, dict):
        return "\n".join(_prose(item) for item in value.values())
    if isinstance(value, list):
        return "\n".join(_prose(item) for item in value)
    return value if isinstance(value, str) else ""


def _repair_block(number):
    return next(block for block in _section("repair_plan")["blocks"] if (block.get("title") or "").startswith(f"3.{number} "))


@pytest.mark.parametrize("version,expected", _HISTORICAL_HASHES.items())
def test_historical_asset_bytes_remain_frozen(version, expected):
    assert sha256(historical_asset(version).read_bytes()).hexdigest() == expected


def test_v3_keeps_resource_shape_and_patient_callback_contract():
    current, previous = _asset(), _asset("v2")
    assert set(current) == set(previous)
    assert current["templateVersion"] == "gut-barrier-static.v3"
    assert current["reportSchemaVersion"] == previous["reportSchemaVersion"] == "gut-barrier-public.v2"
    assert set(current["sourceVersions"]) == set(previous["sourceVersions"])
    assert current["sourceVersions"] != previous["sourceVersions"]
    assert current["sectionOrder"] == previous["sectionOrder"]
    assert current["overviewSlots"] == previous["overviewSlots"]
    for insertion, old in zip(current["overviewInsertions"], previous["overviewInsertions"], strict=True):
        assert insertion["anchor"] == old["anchor"]
        assert insertion["position"] == old["position"]
        assert set(insertion) == set(old)
        assert set(insertion["block"]) == set(old["block"])
    for section, old in zip(current["fixedSections"], previous["fixedSections"], strict=True):
        assert set(section) == set(old)
        for block, old_block in zip(section["blocks"], old["blocks"], strict=True):
            assert set(block) == set(old_block)


def test_indicator_meanings_follow_the_accepted_dao_edit():
    current = _asset()["overviewInsertions"][1]["block"]
    previous = _asset("v2")["overviewInsertions"][1]["block"]
    assert len(current["items"]) == 4
    assert current["items"][0] == previous["items"][0]
    assert current["items"][2:] == previous["items"][2:]
    assert "本项指标用于观察黏膜上皮完整性" in current["items"][1]
    assert "并非内镜或组织病理损伤程度的直接测量" in current["items"][1]


def test_five_stage_copy_matches_comment_eight_without_a_treatment_guarantee():
    repair = _section("repair_plan")
    titles = [block["title"] for block in repair["blocks"] if block.get("title")]
    assert titles == [
        "3.1 修复总原则：五阶段原则", "3.2 肠道营养补充",
        "3.3 肠道菌群移植（FMT）", "3.4 靶向功能菌群调节（MDF）", "3.5 生活方式干预",
    ]
    assert _repair_block(1)["items"] == _PRINCIPLES
    assert "不是所有受检者都必须依次接受的治疗" in repair["blocks"][1]["text"]
    assert "不表示个人一定获得快速修复或定植改善" in repair["blocks"][1]["text"]
    assert not any(block["kind"] == "table" for block in repair["blocks"])
    assert "方案要点" not in _prose(repair)


def test_nutrition_additions_keep_indications_separate_from_mechanisms():
    text = _repair_block(2)["text"]
    for required in (
        "精氨酸", "核苷酸", "重大肿瘤手术", "不能直接外推", "[补26]",
        "静脉还原型谷胱甘肽", "不据此形成静脉处方", "不能仅由屏障分值选择给药途径",
        "不依据IBFC等级统一启动输注", "肝肾功能", "过敏", "妊娠", "梗阻",
    ):
        assert required in text
    assert not re.search(r"\d+(?:[.–-]\d+)?\s*(?:g/kg|mg/kg|mL/kg)", text)


def test_fmt_and_mdf_keep_strain_and_immune_safety_constraints():
    full = _prose(_asset())
    assert "PFMT" not in full
    fmt, mdf = _repair_block(3)["text"], _repair_block(4)["text"]
    for term in ("供者", "病原体", "免疫", "适应证", "不建议在临床试验之外常规实施"):
        assert term in fmt
    for term in (
        "Microbiota-Directed Function modulation", "本项目", "不作为国际统一技术名称",
        "没有菌群检测资料时", "具体配方另行进行个体化确认", "免疫抑制", "粒细胞减少",
        "不能默认改用后生元即可安全", "不是无需复核即可承诺的疗效",
    ):
        assert term in mdf
    assert "FMT" in _repair_block(5)["text"]


def test_liver_display_name_changes_without_replacing_its_stable_code():
    rows = _asset()["diseaseMechanisms"]
    liver = [row for row in rows if row["diseaseCode"] == "nafld"]
    assert len(liver) == 1
    assert liver[0]["diseaseName"] == "代谢相关脂肪性肝病"
    assert len(rows) == len(_asset("v2")["diseaseMechanisms"])
    assert "不据此新增疾病、指数或等级" in _asset()["composition"]["disease_risks"]


def test_education_and_corrected_reference_identities_are_preserved():
    latest, previous = _asset(), _asset("v2")
    assert _section("education", latest) == _section("education", previous)
    groups = [block for block in latest["appendixBlocks"] if block["kind"] == "references"]
    old_groups = [block for block in previous["appendixBlocks"] if block["kind"] == "references"]
    assert groups[0] == old_groups[0]
    assert groups[1]["items"][:25] == old_groups[1]["items"]
    assert [len(group["items"]) for group in groups] == [14, 26]
    assert groups[1]["items"][25]["doi"] == "10.1016/j.clnu.2025.08.029"
    for supplement, numbers in re.findall(r"\[(补?)([\d,]+)\]", _prose(latest)):
        assert all(1 <= int(number) <= len(groups[bool(supplement)]["items"]) for number in numbers.split(","))


@pytest.mark.parametrize("forbidden", [
    "陈先生", "张学斌", "测试小陈", "高于68%", "WHO国际标准", "IBFC 3.5",
    "该处文字由AI填写", "这个数值是怎样得出来的", "如果上图中评分有循证支持",
    "均显示出较好的临床疗效", "内毒素消除蛋白的乳酸菌", "三联活菌联合低聚果糖与乳果糖",
])
def test_source_patients_comments_and_deleted_prescriptions_do_not_leak(forbidden):
    assert forbidden not in _prose(_asset())


def test_asset_does_not_embed_patient_or_new_callback_fields():
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
