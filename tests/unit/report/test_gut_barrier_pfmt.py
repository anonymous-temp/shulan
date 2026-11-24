"""gutBarrier 的临床医嘱必须给出 PFMT：肠型优先、性别其次配型，缺失时由程序按目录补全。"""

import json
from unittest.mock import AsyncMock

import pytest

from modules.report import reports

CANDIDATES = [
    {"productCode": "M-1", "productName": "移植胶囊（肠型I·男）", "productStatus": "启用",
     "applicableGender": "男", "applicableEnterotype": "type_1", "applicableEnterotypeName": "拟杆菌型"},
    {"productCode": "F-3", "productName": "移植胶囊（肠型III·女）", "productStatus": "启用",
     "applicableGender": "女", "applicableEnterotype": "type_3", "applicableEnterotypeName": "瘤胃球菌型"},
    {"productCode": "M-3", "productName": "移植胶囊（肠型III·男）", "productStatus": "启用",
     "applicableGender": "男", "applicableEnterotype": "type_3", "applicableEnterotypeName": "瘤胃球菌型"},
    {"productCode": "OFF", "productName": "停用胶囊（肠型III·男）", "productStatus": "停用",
     "applicableGender": "男", "applicableEnterotype": "type_3", "applicableEnterotypeName": "瘤胃球菌型"},
]
CAPSULE = {item["productCode"]: item["productName"] for item in CANDIDATES}
DETAILS = {code: {"viableCountPerCapsule": "1", "totalViableCountPerBox": "2", "usageInstructions": "用法"}
           for code in CAPSULE}


@pytest.mark.parametrize("text,code", [
    ("肠型I（拟杆菌型）", "I"), ("肠型II", "II"), ("肠型Ⅲ", "III"), ("ET-B 拟杆菌型", "I"),
    ("type_3", "III"), ("普雷沃氏菌型", "II"), ("已纳入来源核对", ""), ("", ""),
])
def test_enterotype_code_normalizes_common_spellings(text, code):
    assert reports._enterotype_code(text) == code


@pytest.mark.parametrize("gender,enterotype,preferred,expected,basis", [
    ("男", "I", "", "M-3", (True, True)),      # 肠型I -> 供体 III，同性别
    ("女", "I", "", "F-3", (True, True)),
    ("女", "II", "", "M-1", (True, False)),    # 肠型II -> 供体 I；无女性 I 型，肠型优先于性别
    ("女", "III", "", "M-1", (True, False)),   # 肠型III -> 供体 I（同上）
    ("", "I", "", "F-3", (True, False)),       # 有肠型无性别 -> 第一款供体 III
    ("男", "", "", "M-1", (False, True)),      # 只有性别 -> 第一款同性别启用产品
    ("", "", "", "M-1", (False, False)),       # 都没有 -> 第一款启用产品
    ("男", "I", "F-3", "M-3", (True, True)),   # 模型选的不符合配型 -> 纠正
    ("男", "I", "M-3", "M-3", (True, True)),   # 模型选的符合 -> 保留
    ("男", "I", "OFF", "M-3", (True, True)),   # 停用产品不可选
])
def test_select_pfmt_prefers_enterotype_over_gender(gender, enterotype, preferred, expected, basis):
    assert reports._select_pfmt(CANDIDATES, CAPSULE, gender=gender, enterotype=enterotype,
                                preferred=preferred) == (expected, basis)


def test_select_pfmt_avoids_same_enterotype_when_rule_donor_missing():
    only_ii_and_iii = [
        {"productCode": "P-2", "productStatus": "启用", "applicableGender": "女", "applicableEnterotypeName": "普雷沃氏菌型"},
        {"productCode": "P-3", "productStatus": "启用", "applicableGender": "男", "applicableEnterotypeName": "瘤胃球菌型"},
    ]
    capsule = {"P-2": "II", "P-3": "III"}
    # 肠型II 没有供体 I：避开同肠型，宁可跨性别选 III
    assert reports._select_pfmt(only_ii_and_iii, capsule, gender="女", enterotype="II") == ("P-3", (False, False))


def gut_input(*, enterotype="", gender="男"):
    return {"type": "gutBarrier", "subject": {"gender": gender}, "enterotype": enterotype}


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    json.dumps({"recommended_combination_method": "生活方式干预"}, ensure_ascii=False),
    "not json",
])
async def test_gut_barrier_recommend_always_contains_pfmt(monkeypatch, response):
    monkeypatch.setattr(reports, "safe_complete_async", AsyncMock(return_value=response))
    result = await reports._recommend(gut_input(), "", [], [], CAPSULE, {}, DETAILS, {}, CANDIDATES)
    assert "健康人源肠道菌群移植胶囊(PFMT)" in result["recommended_combination_method"]
    assert result["pfmt_code"] == "M-1" and result["PFMT"] == CAPSULE["M-1"]
    assert result["pfmt_usageInstructions"] == reports.PFMT_USAGE_INSTRUCTIONS  # 产品固定用法，不取目录


@pytest.mark.asyncio
async def test_gut_barrier_recommend_uses_enterotype_and_gender_rule(monkeypatch):
    response = json.dumps({"recommended_combination_method": "生活方式干预"}, ensure_ascii=False)
    monkeypatch.setattr(reports, "safe_complete_async", AsyncMock(return_value=response))
    result = await reports._recommend(gut_input(enterotype="肠型I（拟杆菌型）"), "", [], [], CAPSULE, {},
                                      DETAILS, {}, CANDIDATES)
    assert result["pfmt_code"] == "M-3"


@pytest.mark.asyncio
async def test_evaluate_recommend_is_not_forced(monkeypatch):
    response = json.dumps({"recommended_combination_method": "生活方式干预"}, ensure_ascii=False)
    monkeypatch.setattr(reports, "safe_complete_async", AsyncMock(return_value=response))
    result = await reports._recommend({"subject": {"gender": "男"}}, "", [], [], CAPSULE, {}, DETAILS, {}, CANDIDATES)
    assert result["recommended_combination_method"] == "生活方式干预"
    assert not result["pfmt_code"]
    monkeypatch.setattr(reports, "safe_complete_async", AsyncMock(return_value="not json"))
    with pytest.raises(ValueError, match="no usable selection"):
        await reports._recommend({}, "", [], [], CAPSULE, {}, DETAILS, {}, CANDIDATES)


def test_gut_barrier_adapter_carries_enterotype_from_auxiliary_table():
    data = {"type": "gutBarrier", "report": {"sections": [{"id": "overview", "blocks": [
        {"kind": "facts", "title": "受检者信息", "items": [{"label": "性别", "value": "男"}]},
        {"kind": "table", "title": "辅助资料摘要", "columns": ["采样日期", "资料类型", "原报告肠型 / 摘要"],
         "rows": [["2026-09-01", "菌群资料", "肠型II（普雷沃氏菌型）"]]},
    ]}]}}
    followup_input = reports._gut_barrier_followup_input(data)
    assert followup_input["enterotype"] == "肠型II（普雷沃氏菌型）"
    assert followup_input["diagnosis_summary"]["clinical_context"]["肠型"] == "肠型II（普雷沃氏菌型）"
    assert "肠型II（普雷沃氏菌型）" in reports._prompt_scope_note(data)
    assert "也没有肠型信息" in reports._prompt_scope_note({"type": "gutBarrier", "report": {"sections": []}})


@pytest.mark.asyncio
@pytest.mark.parametrize("enterotype,gender,code,note", [
    ("肠型I", "男", "M-3", None),
    ("肠型II", "女", "M-1", "（按您的肠型匹配）"),
    ("", "男", "M-1", "（按您的性别匹配）"),
    ("", "", "M-1", "（按产品目录匹配）"),
])
async def test_recommend_carries_match_note_for_instruction_only(monkeypatch, enterotype, gender, code, note):
    response = json.dumps({"recommended_combination_method": "生活方式干预"}, ensure_ascii=False)
    monkeypatch.setattr(reports, "safe_complete_async", AsyncMock(return_value=response))
    recommend = await reports._recommend(gut_input(enterotype=enterotype, gender=gender), "", [], [], CAPSULE, {},
                                         DETAILS, {}, CANDIDATES)
    assert recommend["pfmt_code"] == code
    cleaned, carried = reports._take_pfmt_match_note(recommend)
    assert carried == note
    assert reports._PFMT_MATCH_NOTE_KEY not in cleaned
    assert reports._PFMT_MATCH_NOTE_KEY in recommend  # 不改原对象，instruction 重试时仍可取到


@pytest.mark.parametrize("note,expected", [
    (None, "（按您的肠型与性别一对一配型）"),
    ("（按您的性别匹配）", "（按您的性别匹配）"),
])
def test_adjust_pfmt_note_rewrites_product_card(note, expected):
    ndi = {"gut_microbiota_regulation": {"probiotics": {"products": [
        {"type": "PFMT", "rationale_note": "（按您的肠型与性别一对一配型）"}]}}}
    reports._adjust_gut_barrier_pfmt_note(ndi, note)
    assert ndi["gut_microbiota_regulation"]["probiotics"]["products"][0]["rationale_note"] == expected


def test_evaluate_recommend_has_no_match_note():
    recommend = {"pfmt_code": "M-1"}
    assert reports._take_pfmt_match_note(recommend) == (recommend, None)
