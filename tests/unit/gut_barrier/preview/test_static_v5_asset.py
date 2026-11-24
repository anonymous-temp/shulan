from tests.unit.gut_barrier.preview.archive_support import historical_asset
"""The v5 static asset refines XU chapter 3 without rewriting v1-v4."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import re


V4_SHA256 = "d54b46dbd95d6d305aa4d62eb2d1a1ac7e5830e23d6ce68962f45de64851697d"


def _asset(version: str) -> dict:
    return json.loads(
        historical_asset(version).read_text(encoding="utf-8")
    )


def _section(asset: dict, identifier: str) -> dict:
    return next(section for section in asset["fixedSections"] if section["id"] == identifier)


def _repair_blocks(asset: dict) -> dict[str, dict]:
    return {
        block["title"]: block
        for block in _section(asset, "repair_plan")["blocks"]
    }


def _reference_groups(asset: dict) -> tuple[list[dict], list[dict]]:
    groups = {
        block["title"]: block["items"]
        for block in asset["appendixBlocks"]
        if block["kind"] == "references"
    }
    return groups["5.4 参考文献"], groups["营养与微生态干预补充依据"]


def test_v5_is_a_new_asset_and_v4_bytes_remain_frozen() -> None:
    assert sha256(historical_asset("v4").read_bytes()).hexdigest() == V4_SHA256
    old, current = _asset("v4"), _asset("v5")
    assert current["templateVersion"] == "gut-barrier-static.v5"
    assert current["reportSchemaVersion"] == old["reportSchemaVersion"] == "gut-barrier-public.v2"
    assert current["sectionOrder"] == old["sectionOrder"]
    assert set(current) == set(old)


def test_v5_changes_only_versioned_chapter_three_and_reviewed_references() -> None:
    old, current = _asset("v4"), _asset("v5")
    normalized = deepcopy(current)
    normalized["templateVersion"] = old["templateVersion"]
    normalized["sourceVersions"] = deepcopy(old["sourceVersions"])
    normalized["composition"]["displayOnly"] = old["composition"]["displayOnly"]
    _section(normalized, "repair_plan")["blocks"] = deepcopy(
        _section(old, "repair_plan")["blocks"]
    )
    normalized["appendixBlocks"][0]["rows"][3][0] = old["appendixBlocks"][0]["rows"][3][0]
    normalized["appendixBlocks"][5]["text"] = old["appendixBlocks"][5]["text"]

    old_primary, old_supplemental = _reference_groups(old)
    primary, supplemental = _reference_groups(normalized)
    primary[11] = deepcopy(old_primary[11])
    primary[13] = deepcopy(old_primary[13])
    for index in (0, 1, 2, 3, 4, 5, 6, 7, 25):
        supplemental[index]["location"] = old_supplemental[index]["location"]
    assert normalized == old


def test_five_stage_and_three_nutrition_lines_follow_xu_structure() -> None:
    blocks = _repair_blocks(_asset("v5"))
    assert list(blocks) == [
        "3.1 修复总原则：五阶段原则",
        "3.2 肠道营养补充",
        "3.3 肠道菌群移植（FMT）",
        "3.4 靶向功能菌群调节（MDF）",
        "3.5 生活方式干预",
    ]
    principle = blocks["3.1 修复总原则：五阶段原则"]["text"]
    for stage in ("去除阶段", "改善阶段", "重建阶段", "修复阶段", "平衡阶段"):
        assert stage in principle
    assert "通用的分层干预框架" in principle

    nutrition = blocks["3.2 肠道营养补充"]["text"]
    for phrase in (
        "原料—能量—信号—抗炎",
        "一、上皮能量代谢",
        "二、细胞合成与连接更新",
        "三、抗氧化防御与黏膜环境",
        "丙氨酰-谷氨酰胺",
        "精氨酸",
        "核苷酸",
        "静脉还原型谷胱甘肽",
        "肝肾功能",
        "由医师确定制剂、剂量和疗程",
    ):
        assert phrase in nutrition
    assert "不能仅凭机制作出疗效承诺" in nutrition
    assert not re.search(r"\d+(?:[.–-]\d+)?\s*(?:g/kg|mg/kg|mL/kg)", nutrition)


def test_fmt_and_mdf_are_professional_descriptions_not_product_promises() -> None:
    blocks = _repair_blocks(_asset("v5"))
    fmt = blocks["3.3 肠道菌群移植（FMT）"]["text"]
    for phrase in (
        "经严格筛查供者",
        "复发性艰难梭菌感染",
        "对溃疡性结肠炎、克罗恩病、储袋炎及肠易激综合征，不建议在临床试验之外常规实施",
        "供者、病原体和制备质量筛查",
        "严重免疫抑制",
    ):
        assert phrase in fmt
    for forbidden in (
        "均显示出较好的临床疗效",
        "自闭症、焦虑、抑郁症和帕金森病",
        "糖尿病、肥胖症、脂肪肝和高脂血症",
    ):
        assert forbidden not in fmt

    mdf = blocks["3.4 靶向功能菌群调节（MDF）"]["text"]
    assert "靶向功能菌群调节（Microbiota-Directed Function modulation，MDF）" in mdf
    for development_wording in (
        "本项目所称",
        "该英文展开为项目定义",
        "并非国际统一技术名称",
    ):
        assert development_wording not in mdf
    mechanism = (
        "Caco-2细胞研究显示，丁酸可通过AMPK促进紧密连接组装，"
        "为“短链脂肪酸—上皮连接”路径提供机制依据。[补1]"
    )
    assert mechanism in mdf
    assert mdf.count("[补1]") == 1
    assert "该细胞研究不等同于某一菌株、产品或人群已证实疗效" in mdf
    assert "后生元应依ISAPP共识定义及具体产品证据解读" in mdf
    assert "[14]" in mdf


def test_lifestyle_is_actionable_without_using_caco2_as_a_blanket_citation() -> None:
    lifestyle = _repair_blocks(_asset("v5"))["3.5 生活方式干预"]["text"]
    for phrase in (
        "每天至少25克食物来源纤维",
        "有氧活动和主要肌群抗阻练习",
        "稳定的睡眠、进餐与排便节律",
        "处方药由医师调整",
    ):
        assert phrase in lifestyle
    assert "[补1]" not in lifestyle
    assert "[13][补6]" in lifestyle and "[补7]" in lifestyle


def test_placeholder_reference_groups_are_replaced_with_verifiable_sources() -> None:
    primary, supplemental = _reference_groups(_asset("v5"))
    atlas, postbiotic = primary[11], primary[13]
    assert atlas == {
        "citation": "International Diabetes Federation. IDF Diabetes Atlas, 10th ed.",
        "url": "https://www.ncbi.nlm.nih.gov/books/NBK581934/",
        "year": 2021,
        "publisher": "International Diabetes Federation",
        "doi": None,
        "location": None,
    }
    assert postbiotic["url"] == "https://pubmed.ncbi.nlm.nih.gov/33948025/"
    assert postbiotic["doi"] == "10.1038/s41575-021-00440-6"
    assert postbiotic["publisher"] == "Nature Reviews Gastroenterology & Hepatology"
    assert postbiotic["location"] == "18(9):649–667"
    assert "原稿列引" not in json.dumps((atlas, postbiotic), ensure_ascii=False)

    butyrate = supplemental[0]
    assert butyrate["doi"] == "10.3945/jn.109.104638"
    assert butyrate["location"] == "139(9):1619–1625"
    assert [item["location"] for item in supplemental[1:8]] == [
        "105(9):3551–3556",
        "189(1):111–126",
        "Recommendations 1.3、1.6–1.8",
        "166(3):409–434",
        None,
        None,
        "6(263):263ra158",
    ]
    assert supplemental[25]["location"] == "53:222–261"

    public_locations = json.dumps(
        [item["location"] for item in primary + supplemental], ensure_ascii=False
    )
    for internal_note in (
        "不用于个体风险换算",
        "不等同于具体MDF",
        "不支持MDF产品",
        "机制句",
        "本次报告",
        "普通屏障检测异常者通用方案",
    ):
        assert internal_note not in public_locations


def test_patient_facing_appendix_keeps_only_necessary_score_boundaries() -> None:
    appendix = _asset("v5")["appendixBlocks"]
    assert appendix[0]["rows"][3][0] == "结果与报告审核"
    assert len(appendix[0]["rows"][3][0]) < len("结果审核与报告出具")

    scale = appendix[5]
    assert scale["title"] == "健康关注指数标尺说明"
    for necessary in (
        "自定义0–10分标尺",
        "尚未经过临床验证",
        "不代表患病概率",
        "IBFC、健康关注指数及其他评估工具",
        "不能直接换算或比较",
    ):
        assert necessary in scale["text"]
    for implementation_detail in (
        "已注册模型",
        "合并排序",
        "健康关注图仅展示",
        "空值表示",
        "1–3分为基础关注",
    ):
        assert implementation_detail not in scale["text"]


def test_v5_static_copy_stays_patient_free_and_not_an_individual_prescription() -> None:
    text = json.dumps(_asset("v5"), ensure_ascii=False)
    for forbidden in (
        "患者姓名",
        "测试小陈",
        "本次受检者应",
        "自动开具",
        "均显示出较好的临床疗效",
    ):
        assert forbidden not in text
