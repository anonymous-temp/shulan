"""Removed CRP values never create patient-facing rows or review prompts."""
from copy import deepcopy
import json
import re
import pytest
from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.presentation import build_report
from modules.gut_barrier.schemas import GutBarrierRequest


def _case(raw=">3.0", *, unit="mg/L", age=28, abnormal=False, context=None, observations=None):
    values = {
        "dLacticAcid": {"value": "26" if abnormal else "0.06", "unit": "mg/L"},
        "diamineOxidase": {"value": "17" if abnormal else "3.8", "unit": "U/L"},
        "bacterialEndotoxin": {"value": "23" if abnormal else "12.5", "unit": "U/L"},
    }
    if raw is not None:
        values["hsCRP"] = {"value": raw, "unit": unit}
    request = GutBarrierRequest.model_validate({"userId": "removed-marker", "sessionId": "display",
                                               "age": age, "gutHealthIndicator": values})
    return assess_case(request, observations=observations,
                       context={"currentDate": "2026-09-24", **(context or {})})


def _visible(assessment):
    before = deepcopy(assessment)
    report = build_report(assessment, {}, report_id="three-markers")
    assert assessment == before
    return report["sections"]


@pytest.mark.parametrize("raw", [None, "0", "0.5", "1", "3", "10", "100", ">3", ">10", "<0.5", "<=1", ">=3"])
@pytest.mark.parametrize("abnormal", [False, True])
def test_crp_variants_do_not_change_patient_sections(raw, abnormal):
    sections = _visible(_case(raw, abnormal=abnormal))
    assert sections == _visible(_case(None, abnormal=abnormal))
    assert not re.search(r"crp|C反应蛋白|C-reactive", json.dumps(sections, ensure_ascii=False), re.I)
    overview = next(s for s in sections if s["id"] == "overview")
    table = next(b for b in overview["blocks"] if b.get("title") == "三项检测指标")
    assert len(table["rows"]) == 3
    assert [r[0] for r in table["rows"]] == ["D-乳酸（D-Lactate）", "二胺氧化酶（DAO）", "细菌脂多糖（LPS）"]


@pytest.mark.parametrize("age", [None, 0, 17, 28])
@pytest.mark.parametrize("unit", ["mg/L", "mg/dL", "invalid"])
def test_removed_crp_unit_or_age_does_not_add_a_display_warning(age, unit):
    assert _visible(_case(">3", age=age, unit=unit)) == _visible(_case(None, age=age))


@pytest.mark.parametrize("flag", ["acuteInfection", "acuteInflammation"])
def test_real_acute_context_is_preserved_without_crp(flag):
    sections = _visible(_case(None, context={flag: True}))
    assert "急性感染或炎症背景" in json.dumps(sections, ensure_ascii=False)
    assert sections == _visible(_case("100", context={flag: True}))


def test_crp_source_conflict_does_not_affect_barrier_display():
    observations = [{"code": "HS_CRP", "value": "100", "unit": "mg/L", "isCurrent": True,
                     "source": {"kind": "file", "location": "test.pdf"}}]
    assert _visible(_case("1", observations=observations)) == _visible(_case(None))
