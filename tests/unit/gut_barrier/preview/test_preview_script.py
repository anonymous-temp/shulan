import copy
import json

import pytest

from scripts.render_gut_barrier_callback import render_callback


@pytest.mark.parametrize("callback_type", ["gutBarrier", "gut_barrier"])
@pytest.mark.parametrize("status", ["success", "partial"])
def test_preview_is_rendered_from_exact_callback_report_without_mutation(tmp_path, callback_type, status):
    report = {"meta": {"reportId": "gbr_test"}, "sections": [
        {"id": "markers", "title": "检测结果", "blocks": [
            {"kind": "paragraph", "text": "hs-CRP >3.0 mg/L"}]}]}
    payload = {"type": callback_type, "status": status, "report": report}
    original = copy.deepcopy(payload)
    target = render_callback(payload, tmp_path)
    assert "&gt;3.0 mg/L" in target.read_text(encoding="utf-8")
    assert json.loads((tmp_path / "report.json").read_text(encoding="utf-8")) == original["report"]
    assert payload == original
    assert "previewOnly" not in payload["report"]["meta"]


@pytest.mark.parametrize("payload", [
    {"type": "evaluate", "status": "success", "report": {}},
    {"type": "gut_barrier", "status": "processing", "report": {}},
    {"type": "gut_barrier", "status": "failed", "report": None},
    {"type": "gut_barrier", "status": "partial", "report": {}},
    {"type": "gutBarrier", "status": "processing", "report": {}},
    {"type": "gutBarrier", "status": "failed", "report": None},
    {"type": "gutBarrier", "status": "partial", "report": {}},
    {"data": {"type": "gut_barrier", "status": "success", "report": {}}},
])
def test_nonterminal_or_nonreport_payload_cannot_be_presented_as_completed_report(tmp_path, payload):
    with pytest.raises(ValueError):
        render_callback(payload, tmp_path)
    assert not (tmp_path / "report.html").exists()
