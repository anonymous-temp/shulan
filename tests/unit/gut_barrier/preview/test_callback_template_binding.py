"""Frozen release binding crosses HTTP without changing public JSON fields."""

from copy import deepcopy

import asyncio

import json

from types import SimpleNamespace

from unittest.mock import AsyncMock

import httpx

import pytest

from adapters.gut_barrier.delivery import GutBarrierCallbackTransport

from adapters.gut_barrier.render_context import (
    RENDER_HEADERS, callback_render_context, render_context_headers, render_context_from_headers,
)

from adapters.gut_barrier.store import MongoGutBarrierStore, _content_hash

from modules.gut_barrier.display_versions import LEGACY_DISPLAY_TEMPLATE, XU_DISPLAY_TEMPLATE, LATEST_DISPLAY_TEMPLATE

from scripts.render_gut_barrier_callback import render_callback

from tests.unit.report.durable_fakes import Clock, Collection
from tests.unit.gut_barrier.preview.archive_support import historical_archive
from tests.unit.gut_barrier.test_dynamic_publication import clinical_report
from modules.gut_barrier.publication import project_callback_report

def receipt(report_id="gbr_binding"):
    return {"type": "gutBarrier", "schemaVersion": "2.0", "finalReportId": report_id,
            "userId": "test", "sessionId": "binding", "status": "partial", "error": None,
            "report": {"schemaVersion": "gut-barrier-public.v2",
                       "meta": {"title": "模板绑定测试", "reportId": report_id},
                       "sections": [{"id": "overview", "title": "第一章", "subtitle": None,
                                     "visible": True, "blocks": []}]}}

async def frozen(version=XU_DISPLAY_TEMPLATE):
    collection, clock = Collection(), Clock()
    store = MongoGutBarrierStore(collection, clock=clock)
    job = await store.admit(fingerprint="binding", request={"userId": "test", "sessionId": "binding"},
                            callback_url="https://receiver.invalid/callback", version="binding-release")
    job = await store.claim_generation()
    body = receipt(job["_id"])
    audit = {"displayTemplateVersion": version} if version else {}
    assert await store.complete(job, body=body, audit=audit)
    return store, collection, body, f"{job['_id']}:gutBarrier:v2"

@pytest.mark.parametrize("version", [LATEST_DISPLAY_TEMPLATE, XU_DISPLAY_TEMPLATE])
def test_saved_callback_headers_render_bound_version_without_changing_report(tmp_path, version):
    archive = historical_archive() if version != LATEST_DISPLAY_TEMPLATE else None
    body = receipt()
    if version == LATEST_DISPLAY_TEMPLATE:
        # A current binding requires a genuinely published current callback,
        # including the frozen main reference inventory.
        internal = clinical_report()
        internal["meta"].update(reportId=body["finalReportId"], displayTemplateVersion=version)
        body["report"] = project_callback_report(internal)
    original = deepcopy(body)
    headers = render_context_headers(body, callback_render_context(body, version))
    target = render_callback(body, tmp_path / "preview", callback_headers=headers,
                             template_archive_dir=archive)
    assert target.exists() and body == original
    context = json.loads((target.parent / "render-context.json").read_text(encoding="utf-8"))
    assert context["templateVersion"] == version
    assert json.loads((target.parent / "report.json").read_text(encoding="utf-8")) == body["report"]
    with pytest.raises(ValueError, match="disagree"):
        render_callback(body, tmp_path / "conflict", callback_headers=headers, template_version=LEGACY_DISPLAY_TEMPLATE)
    assert not (tmp_path / "conflict").exists()

def test_no_headers_preserves_legacy_default(tmp_path, historical_templates):
    body = receipt()
    assert render_context_from_headers(body, {"Content-Type": "application/json"}) is None
    target = render_callback(body, tmp_path / "legacy", callback_headers={})
    context = json.loads((target.parent / "render-context.json").read_text(encoding="utf-8"))
    assert context["templateVersion"] == LEGACY_DISPLAY_TEMPLATE
