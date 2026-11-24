from copy import deepcopy

import json

import re

from types import SimpleNamespace

from unittest.mock import AsyncMock

import pytest

from adapters.gut_barrier.rendering import render_report_html

from modules.gut_barrier.assessment import assess_case

from modules.gut_barrier.content_catalog import CHAPTERS, CONTENT_VERSION, TEMPLATE_VERSION

from modules.gut_barrier.interventions import build_interventions

from modules.gut_barrier.pipeline import GutBarrierPipeline

from modules.gut_barrier.presentation import build_report

from modules.gut_barrier.publication import project_public_report

from modules.gut_barrier.schemas import GutBarrierRequest

def sample(**updates):
    value = {"userId": "acceptance-user", "sessionId": "acceptance-session", "name": "新版验收样本", "age": "50", "sex": "男",
             "gutHealthIndicator": {"dLacticAcid": {"value": "26.0", "unit": "mg/L"},
                "diamineOxidase": {"value": "17.00", "unit": "U/L"},
                "bacterialEndotoxin": {"value": "23.00", "unit": "U/L"},
                "hsCRP": {"value": "2.7", "unit": "mg/L"}},
             "fileList": [], "testData": [], "hospitalData": []}
    value.update(updates)
    return GutBarrierRequest.model_validate(value)

def report_for(request):
    assessment = assess_case(request, context={"currentDate": "2026-09-24"})
    return build_report(assessment, build_interventions(assessment), report_id="gbr_five_chapter_test")

def block(report, section, title):
    return next(b for s in report["sections"] if s["id"] == section for b in s["blocks"] if b.get("title") == title)

def test_public_json_and_html_are_same_frozen_document_with_all_eight_figures():
    report = project_public_report(report_for(sample()))
    before = deepcopy(report)
    body = render_report_html(report)
    data = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', body, re.S)
    assert json.loads(data.group(1)) == before and report == before
    assert len(re.findall(r'id="figure-[1-8]"', body)) == 8
    assert all(f'href="#{identifier}"' in body for identifier, _ in CHAPTERS)
    assert 'class="risk-fill"' not in body
