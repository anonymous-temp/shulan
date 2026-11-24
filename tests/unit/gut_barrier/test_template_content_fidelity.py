"""Independent acceptance anchors for the supplied template's fixed content.

Expectations below are curated from source paragraphs 28–44, 63–83 and the
gap-plan review, not imported from the production content constants.
"""
import hashlib
import json
from itertools import product

import pytest

from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.interventions import build_interventions
from modules.gut_barrier.presentation import build_report
from modules.gut_barrier.publication import project_public_report
from modules.gut_barrier.schemas import GutBarrierRequest


def report_for(pattern="+++", *, age=50, missing=False, context=None):
    indicators = {
        key: {"value": str(high if flag == "+" else 5), "unit": unit}
        for key, high, flag, unit in zip(
            ("dLacticAcid", "diamineOxidase", "bacterialEndotoxin"),
            (26, 17, 23), pattern, ("mg/L", "U/L", "U/L"))
    }
    indicators["hsCRP"] = {"value": "2.7", "unit": "mg/L"}
    request = GutBarrierRequest.model_validate({
        "userId": "fidelity-user", "sessionId": "fidelity-session", "name": "模板内容验收",
        "age": age, "sex": "男", "gutHealthIndicator": {} if missing else indicators,
        "fileList": [], "testData": [], "hospitalData": [],
    })
    assessed = assess_case(request, context={"currentDate": "2026-09-24", **(context or {})})
    return project_public_report(build_report(assessed, build_interventions(assessed), report_id="gbr-fidelity"))


def section(report, identifier):
    return next(item for item in report["sections"] if item["id"] == identifier)


def paragraph(report, prefix):
    return next(block["text"] for item in report["sections"] for block in item["blocks"]
                if (block.get("title") or "").startswith(prefix))


def fixed_body(report):
    return {identifier: [block for block in section(report, identifier)["blocks"] if block["kind"] != "references"]
            for identifier in ("repair_plan", "education", "appendix")}


@pytest.mark.parametrize("prefix,anchors", [
    ("3.2 ", ["消化道症状", "近期感染", "饮食结构", "持续腹痛", "不自行停药"]),
    ("3.3 ", ["原料—能量—信号—抗炎", "谷氨酰胺", "锌", "维生素D", "ω-3", "AMPK",
              "鱼油脂肪乳用于静脉营养", "不能与普通口服鱼油互换", "肠内途径不可行", "狭窄、梗阻风险"]),
    ("3.4 ", ["供者", "病原体", "重建阶段", "衔接阶段", "菌群重建→MDF功能支持→恢复期维持",
              "复发性艰难梭菌感染", "并非移植后必须", "维持阶段"]),
    ("3.5 ", ["路径一，紧密连接修复", "路径二，肠上皮修复", "路径三，内毒素相关干预",
              "IAP", "去磷酸化", "菌株编号", "活菌、灭活菌和后生元", "乳果糖"]),
    ("3.6 ", ["地中海式膳食", "全谷", "发酵食品", "有氧", "抗阻", "体重、腰围和肌力",
              "入睡、起床及进餐", "压力", "非甾体抗炎药", "排便变化"]),
    ("4.1 ", ["机械屏障", "化学屏障", "生物屏障", "免疫屏障", "occludin", "claudin", "ZO-1",
              "黏液", "抗菌肽", "分泌型IgA", "短链脂肪酸", "结肠上皮"]),
    ("4.2 ", ["zonulin", "黏液保护", "菌群生态", "定位变化", "上皮细胞脱落", "并非每个人都按固定顺序"]),
    ("4.3 ", ["LPS", "CD14", "TLR4/MD-2", "NF-κB", "TNF-α", "IL-1β", "IL-6", "机体免疫反应", "持续性"]),
    ("4.4 ", ["MLCK", "细胞骨架", "内吞", "短链脂肪酸", "屏障保护减弱→抗原接触增加→免疫激活",
              "而非仅", "复评"]),
    ("4.5 ", ["门静脉", "肠–肝轴", "肠–代谢轴", "IKK/JNK", "胰岛素", "肠–心血管轴", "肠–脑轴",
              "血脑屏障", "遗传易感性", "个人风险指数"]),
    ("4.6 ", ["inflammaging", "细胞衰老", "慢性抗原刺激", "19名", "18名", "小样本横断面关联",
              "功能状态", "不能将年龄等同于损伤程度"]),
])
def test_restored_information_matches_independent_source_anchors(prefix, anchors):
    body = paragraph(report_for(), prefix)
    # These statements are checked individually rather than just counting six headings.
    for anchor in anchors:
        assert anchor in body, (prefix, anchor)


def test_five_principles_match_the_actual_five_interventions():
    items = section(report_for(), "repair_plan")["blocks"][0]["items"]
    assert [item.split("：")[0] for item in items] == ["去除", "改善", "重建", "修复", "平衡"]
    assert "健康评估" in items[0] and "肠道营养" in items[1]
    assert "PFMT" in items[2] and "MDF" in items[3]
    assert "生活方式" not in items[3] and "运动" in items[4]


@pytest.mark.parametrize("pattern", ["".join(items) for items in product("+-", repeat=3)])
@pytest.mark.parametrize("age", [8, 50, None])
def test_fixed_content_unchanged_for_all_patterns_and_age_groups(pattern, age):
    assert fixed_body(report_for(pattern, age=age)) == fixed_body(report_for())


@pytest.mark.parametrize("context", [
    {"allergies": "鱼类过敏"}, {"liverAndKidneyFunction": "肾功能不全"},
    {"immuneStatus": "免疫抑制"}, {"acuteInfection": True},
])
def test_fixed_mechanisms_remain_fixed_but_do_not_assert_patient_treatment(context):
    report = report_for(context=context, missing=True)
    assert fixed_body(report) == fixed_body(report_for())
    text = json.dumps(fixed_body(report), ensure_ascii=False)
    for invalid_claim in ("已完成治疗", "已匹配PFMT", "每日每公斤体重 0.3", "0.3–0.5 g/kg", "150–300"):
        assert invalid_claim not in text
    assert "严重免疫抑制" in text and "不能默认改用后生元即可安全" in text
    assert "狭窄、梗阻风险或明显不耐受时须另定饮食形式" in text


def test_verified_source_groups_keep_existing_order_fields_and_no_public_work_log():
    blocks = section(report_for(), "appendix")["blocks"]
    base = next(block["items"] for block in blocks if block.get("title") == "5.5 参考文献")
    assert len(base) == 14
    assert base[1]["publisher"] == "PLOS Biology"
    assert base[8]["doi"] == "10.1016/j.jamda.2017.05.018"
    assert base[9]["doi"] == "10.3350/cmh.2024.0431"
    assert base[10]["doi"] == "10.1016/j.cgh.2024.03.006"
    # Unresolved combined records are retained, without invented identifiers.
    for index in (11, 13):
        assert base[index]["doi"] is None and base[index]["url"] is None
    for row in base:
        assert set(row) == {"citation", "year", "publisher", "doi", "url", "location"}
        for phrase in ("待补齐", "待核验", "未唯一核验", "不用于本次数值评估"):
            assert phrase not in (row["location"] or "")


def test_supplementary_citations_keep_stable_independent_numbering():
    blocks = section(report_for(), "appendix")["blocks"]
    supplement = next(block["items"] for block in blocks
                      if block["kind"] == "references" and block.get("title") != "5.5 参考文献")
    assert [row["doi"] for row in supplement[:3]] == [
        "10.3945/jn.109.104638", "10.1073/pnas.0712140105", "10.1083/jcb.200902153"]
    assert supplement[4]["doi"] == "10.1053/j.gastro.2024.01.008"
    assert supplement[7]["doi"] == "10.1126/scitranslmed.3009759"
    assert "[补4,6]" in paragraph(report_for(), "3.3 ")
    assert "[补3]" in paragraph(report_for(), "4.4 ")
    assert "[补8]" in paragraph(report_for(), "4.5 ")


def test_fixed_qc_and_method_copy_never_fabricates_actual_lab_results():
    report = report_for()
    appendix = section(report, "appendix")
    quality = next(block for block in appendix["blocks"] if block.get("title") == "5.1 质量控制校验")
    assert len(quality["rows"]) == 4
    assert "实际" in json.dumps(quality, ensure_ascii=False)
    assert not any(block.get("title") == "本次样本与质控记录" for block in section(report, "overview")["blocks"])
    assert "本机构不承担" not in json.dumps(appendix, ensure_ascii=False)


def test_curated_fixed_text_snapshot():
    """Freeze reviewed paragraphs independently of production constants."""
    serialized = json.dumps(fixed_body(report_for()), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(serialized.encode()).hexdigest() == "2b4f7b5cff4bfc6377d8ec28b6d72919756656543d39b1881c062e8c7f843c69"
