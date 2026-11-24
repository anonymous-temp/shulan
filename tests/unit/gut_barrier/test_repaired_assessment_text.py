"""Independent patient-text expectations for the template repair release."""
import json
import re

import pytest

from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.interventions import build_interventions
from modules.gut_barrier.presentation import build_report
from modules.gut_barrier.publication import project_public_report
from modules.gut_barrier.schemas import GutBarrierRequest


def assessed_for(*, dates=(), pattern="+++", hs="2.7", age=50, context=None):
    payload = {
        "userId": "repair-text", "sessionId": "repair-text", "age": age,
        "gutHealthIndicator": {
            key: {"value": high if flag == "+" else "5", "unit": unit}
            for key, high, flag, unit in zip(
                ("dLacticAcid", "diamineOxidase", "bacterialEndotoxin"),
                ("26", "17", "23"), pattern, ("mg/L", "U/L", "U/L"))
        },
        "hospitalData": [
            {"checkTime": when, "itemName": "低密度脂蛋白(LDL-c)", "dataType": "INSPECTION",
             "testResultValue": "4.10", "resultStatusName": "偏高", "unit": "mmol/L"}
            for when in dates
        ],
    }
    if hs is not None:
        payload["gutHealthIndicator"]["hsCRP"] = {"value": hs, "unit": "mg/L"}
    return assess_case(GutBarrierRequest.model_validate(payload),
                       context={"currentDate": "2026-09-24", **(context or {})})


def public_for(assessment):
    return project_public_report(build_report(
        assessment, build_interventions(assessment), report_id="gbr_repaired_text"))


def block(report, title):
    return next(b for s in report["sections"] for b in s["blocks"] if b.get("title") == title)


@pytest.mark.parametrize("dates,expected,forbidden", [
    (["2026-09-24 08:00:00"], "本次血脂存在升高记录", "既往血脂"),
    (["2025-12-15 08:00:00"], "既往血脂异常记录", "本次血脂"),
    (["2026-09-24", "2025-12-15"], "本次与既往均有血脂升高记录", "既往血脂异常记录不等于当前诊断"),
])
def test_lipid_timepoint_controls_recommendation(dates, expected, forbidden):
    assessed = assessed_for(dates=dates)
    cv = next(r for r in assessed["risks"] if r["code"] == "cardiovascular")
    text = json.dumps(cv, ensure_ascii=False)
    assert expected in text and forbidden not in text
    assert expected in json.dumps(public_for(assessed), ensure_ascii=False)


@pytest.mark.parametrize("date", [None, "not-a-date", "2026-09-25"])
def test_undated_or_future_lipids_remain_visible_without_becoming_current_risk(date):
    assessed = assessed_for(dates=[date])
    assert not any(r["code"] == "cardiovascular" for r in assessed["risks"])
    rows = block(public_for(assessed), "个体临床背景")["rows"]
    assert rows[0][2] == "日期待核对" and "4.10" in rows[0][3]


def test_inflammation_without_lipids_does_not_invent_lipid_history():
    assessed = assessed_for(hs="4.2")
    assert not any(r["code"] == "cardiovascular" for r in assessed["risks"])


@pytest.mark.parametrize("pattern,knowledge", [
    ("+--", ("D-乳酸指标异常", "通透性", "清除")),
    ("-+-", ("DAO指标异常", "上皮", "营养")),
    ("--+", ("LPS指标异常", "免疫", "来源")),
    ("++-", ("D-乳酸与DAO", "跨屏障", "上皮")),
    ("+-+", ("D-乳酸与LPS", "细菌产物", "炎症")),
    ("-++", ("DAO与LPS", "上皮", "内毒素")),
    ("+++", ("D-乳酸、DAO与LPS", "通透性", "上皮", "内毒素")),
    ("---", ("未见评分异常", "养护")),
])
def test_each_pattern_has_its_own_mechanistic_readout(pattern, knowledge):
    assessed = assessed_for(pattern=pattern)
    text = block(public_for(assessed), "1.4 肠道屏障受损类型与受损状况")["text"]
    assert all(term in text for term in knowledge)
    assert "评估重点覆盖" not in text
    assert assessed["barrier"]["pattern"] == pattern


def test_known_case_scoring_is_unchanged_and_conclusion_is_result_first():
    assessed = assessed_for()
    assert assessed["barrier"]["score"] == 9.5 and assessed["barrier"]["grade"] == "V"
    assert [m["score"] for m in assessed["markers"][:3]] == [3, 3.5, 3]
    report = public_for(assessed)
    assert "hs-CRP" not in json.dumps(report,ensure_ascii=False)
    assert "inflammation" not in assessed
    assert "已形成代谢性内毒素血症" not in json.dumps(report, ensure_ascii=False)


@pytest.mark.parametrize("kwargs", [{"hs": ">3.0"}, {"hs": None}, {"age": 10}, {"age": None}, {"context": {"acuteInfection":True}}])
def test_crp_is_absent_while_age_and_acute_context_remain(kwargs):
    report = public_for(assessed_for(**kwargs))
    text = json.dumps(report, ensure_ascii=False)
    assert "hs-CRP" not in text and "1.5 慢性低度炎症评估" not in text
    if "age" in kwargs:
        assert "不套用成人" in text
    if "context" in kwargs:
        assert "急性感染或炎症背景" in text



@pytest.mark.parametrize("hs", ["1", "3", "100", ">10"])
def test_old_crp_endpoints_do_not_change_current_patient_text(hs):
    assessed = assessed_for(hs=hs)
    report = public_for(assessed)
    assert report == public_for(assessed_for(hs=None))
    assert "inflammation" not in assessed



def test_reference_locations_are_sources_not_internal_review_logs():
    report = public_for(assessed_for())
    for section in report["sections"]:
        for entry in section["blocks"]:
            if entry["kind"] != "references":
                continue
            for reference in entry["items"]:
                assert set(reference) == {"citation", "url", "year", "publisher", "doi", "location"}
                assert not any(term in (reference["location"] or "") for term in (
                    "待补齐", "待唯一核验", "不能用于计算", "不等于产品疗效"))


def test_each_public_disease_mechanism_has_a_resolvable_public_source():

    report = public_for(assessed_for())
    tables = [b for s in report["sections"] if s["id"] == "disease_risks" for b in s["blocks"]
              if b.get("columns") == ["疾病", "风险指数", "风险等级", "机制关联"]]
    refs = block(report, "5.5 参考文献")["items"]
    supplement = block(report, "营养与微生态干预补充依据")["items"]
    assert len(refs) == 14 and sum(len(t["rows"]) for t in tables) == 22
    for table in tables:
        for row in table["rows"]:
            citations = re.findall(r"\[(补?)([0-9,]+)\]", row[3])
            assert citations, row[0]
            assert row[1] is None  # Citation completeness is not a risk model.
            for prefix, numbers in citations:
                for token in numbers.split(","):
                    index = int(token)
                    source = (supplement if prefix else refs)[index - 1]
                    assert source["url"] and source["citation"]
                    namespace = "supplement-1" if prefix else "primary"


def test_citation_numbers_follow_actual_supplement_list_with_extra_intervention_source():
    from modules.gut_barrier.chapters import _supplemental_evidence

    plans = {"references": [{"type": "official_guideline", "title": "测试来源", "url": "https://example.org/source"}]}
    actual, citations = _supplemental_evidence(plans)
    assert actual[8]["citation"] == "测试来源"
    # An extra management reference cannot shift citations to the wrong paper.
    first_supplement_number = int(re.search(r"\[补(\d+)", citations["nafld"]).group(1))
    assert actual[first_supplement_number - 1]["url"] == "https://pubmed.ncbi.nlm.nih.gov/17952090/"
