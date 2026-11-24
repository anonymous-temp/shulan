"""A complete-input preview can use production OCR without delivering a callback."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from adapters.gut_barrier.render_context import render_context_from_headers
from scripts import generate_gut_barrier_report as preview


@pytest.mark.asyncio
async def test_online_ocr_is_explicitly_injected_and_headers_match_frozen_json(tmp_path, monkeypatch):
    from adapters import analysis
    client = object()
    factory = Mock(return_value=client)
    monkeypatch.setattr(analysis, "create_application_document_parsing_client", factory)
    sources = SimpleNamespace(collect=AsyncMock(return_value={
        "observations": [], "sources": [], "issues": [], "context": {},
    }))
    source_factory = Mock(return_value=sources)
    monkeypatch.setattr(preview, "GutBarrierSources", source_factory)
    path = tmp_path / "request.json"
    path.write_text(json.dumps({"userId": "preview", "sessionId": "preview", "age": 28,
                               "fileList": [{"url": "https://documents.invalid/image.jpg", "fileType": "image"}]}), encoding="utf-8")
    target = tmp_path / "result"
    await preview.generate(SimpleNamespace(input=str(path), output=str(target), offline=False,
                                           ocr=True, evidence=False, evaluation_date="2026-09-28"))
    factory.assert_called_once_with()
    source_factory.assert_called_once_with(parsing_client=client)
    sources.collect.assert_awaited_once()
    callback = json.loads((target / "callback.json").read_text(encoding="utf-8"))
    context = json.loads((target / "render-context.json").read_text(encoding="utf-8"))
    headers = json.loads((target / "callback-headers.json").read_text(encoding="utf-8"))
    assert render_context_from_headers(callback, headers) == context
    assert json.loads((target / "audit.json").read_text(encoding="utf-8"))["callbackSent"] is False


@pytest.mark.asyncio
async def test_offline_mode_cannot_silently_submit_documents_to_ocr(tmp_path):
    path = tmp_path / "request.json"
    path.write_text('{"userId":"offline","sessionId":"offline"}', encoding="utf-8")
    target = tmp_path / "result"
    with pytest.raises(ValueError, match="cannot combine"):
        await preview.generate(SimpleNamespace(input=str(path), output=str(target), offline=True, ocr=True))
    assert not target.exists()
