"""Offline previews save the same selected bibliography and full private audit."""
import json
import re
from types import SimpleNamespace

import pytest

from scripts import generate_gut_barrier_report as preview


@pytest.mark.asyncio
async def test_preview_keeps_full_reference_sources_separate_from_callback_and_html(tmp_path):
    source = tmp_path / "input.json"
    source.write_text(json.dumps({"userId": "budget-preview", "sessionId": "budget-preview", "age": "28",
                                 "gutHealthIndicator": {
                                     "diamineOxidase": {"value": "11.77", "unit": "U/L"},
                                     "dLacticAcid": {"value": "7.18", "unit": "mg/L"},
                                     "bacterialEndotoxin": {"value": "15.04", "unit": "U/L"},
                                     "hsCRP": {"value": "2.2", "unit": "mg/L"}}}), encoding="utf-8")
    target = tmp_path / "preview"
    await preview.generate(SimpleNamespace(input=str(source), output=str(target), offline=True,
                                           evidence=False, ocr=False, evaluation_date="2026-10-07"))
    callback = json.loads((target / "callback.json").read_text(encoding="utf-8"))
    audit = json.loads((target / "audit.json").read_text(encoding="utf-8"))
    ledger = audit["referencePublication"]
    assert ledger["displayedCount"] == 15 and ledger["originalCitedCount"] > 15
    assert any(item["selection"] == "budget_omitted" for item in ledger["inventory"])
    html = (target / "report.html").read_text(encoding="utf-8")
    anchors = set(re.findall(r'id="reference-primary-(\d+)"', html))
    links = set(re.findall(r'href="#reference-primary-(\d+)"', html))
    assert anchors == set(map(str, range(1, 16))) and links <= anchors
    assert "referencePublication" not in json.dumps(callback) and "selected_display_budget" not in html
    assert audit["callbackSent"] is False
