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

def test_each_public_disease_mechanism_has_a_resolvable_public_source():
    from adapters.gut_barrier.rendering import render_report_html

    report = public_for(assessed_for())
    tables = [b for s in report["sections"] if s["id"] == "disease_risks" for b in s["blocks"]
              if b.get("columns") == ["疾病", "风险指数", "风险等级", "机制关联"]]
    refs = block(report, "5.5 参考文献")["items"]
    supplement = block(report, "营养与微生态干预补充依据")["items"]
    assert len(refs) == 14 and sum(len(t["rows"]) for t in tables) == 22
    html = render_report_html(report)
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
                    assert f'href="#reference-{namespace}-{index}"' in html
                    assert html.count(f'id="reference-{namespace}-{index}"') == 1
