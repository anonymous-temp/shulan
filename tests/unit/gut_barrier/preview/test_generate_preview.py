import json
import re
from types import SimpleNamespace

import pytest

from scripts.generate_gut_barrier_report import generate


@pytest.mark.asyncio
async def test_pipeline_preview_exports_same_public_report_as_callback(tmp_path, capsys):
    source = tmp_path / "request.json"
    source.write_text(json.dumps({"userId": "preview-test", "sessionId": "preview-test", "age": "50",
                                 "gutHealthIndicator": {"hsCRP": {"value": ">3.0", "unit": "mg/L"}}}),
                      encoding="utf-8")
    directory = tmp_path / "preview"
    await generate(SimpleNamespace(input=str(source), output=str(directory), offline=True,
                                   evaluation_date="2026-09-24"))
    public = json.loads((directory / "report.json").read_text(encoding="utf-8"))
    callback = json.loads((directory / "callback.json").read_text(encoding="utf-8"))
    audit = json.loads((directory / "audit.json").read_text(encoding="utf-8"))
    html = (directory / "report.html").read_text(encoding="utf-8")
    data = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', html, re.S)
    assert public == callback["report"] == json.loads(data.group(1))
    assert set(public) == {"schemaVersion", "meta", "sections"}
    assert callback["type"] == "gutBarrier" and callback["schemaVersion"] == "2.0"
    assert callback["finalReportId"] == public["meta"]["reportId"]
    assert callback["status"] == "partial" and callback["error"] is None
    assert audit["previewOnly"] is True and audit["callbackSent"] is False
    assert json.loads(capsys.readouterr().out)["callbackSent"] is False


@pytest.mark.asyncio
async def test_preview_never_overwrites_an_existing_directory(tmp_path):
    directory = tmp_path / "historical"
    directory.mkdir()
    original = directory / "report.json"
    original.write_text('{"frozen": true}', encoding="utf-8")
    with pytest.raises(FileExistsError, match="must be new"):
        await generate(SimpleNamespace(input=str(tmp_path / "not-read.json"), output=str(directory),
                                       offline=True, evaluation_date="2026-09-25"))
    assert original.read_text(encoding="utf-8") == '{"frozen": true}'
