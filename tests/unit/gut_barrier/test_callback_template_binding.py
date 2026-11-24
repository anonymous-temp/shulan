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
from modules.gut_barrier.display_versions import LEGACY_DISPLAY_TEMPLATE, XU_DISPLAY_TEMPLATE
from tests.unit.report.durable_fakes import Clock, Collection


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


@pytest.mark.asyncio
async def test_completion_atomically_freezes_body_hash_and_template_context():
    store, collection, body, key = await frozen()
    saved = deepcopy(collection.documents[body["finalReportId"]])
    context = await store.delivery_render_context(body, idempotency_key=key)
    assert context == saved["renderContext"] == callback_render_context(body, XU_DISPLAY_TEMPLATE)
    assert saved["body"] == body and saved["contentHash"] == _content_hash(body)
    assert set(body) == {"type", "schemaVersion", "finalReportId", "userId", "sessionId", "status", "error", "report"}
    assert set(body["report"]["meta"]) == {"title", "reportId"}
    assert collection.documents[body["finalReportId"]] == saved


@pytest.mark.asyncio
async def test_retry_reuses_identical_body_headers_and_idempotency_key():
    store, collection, body, key = await frozen()
    calls = []

    async def receive(request):
        calls.append((request.content, dict(request.headers)))
        return httpx.Response(503 if len(calls) == 1 else 204, headers={"Retry-After": "7"})

    transport = GutBarrierCallbackTransport(store=store, transport=httpx.MockTransport(receive))
    try:
        first = await transport.post_once("https://receiver.invalid/callback", body, idempotency_key=key)
        # The frozen binding wins even if incidental audit data later differs.
        collection.documents[body["finalReportId"]]["audit"]["displayTemplateVersion"] = LEGACY_DISPLAY_TEMPLATE
        second = await transport.post_once("https://receiver.invalid/callback", body, idempotency_key=key)
    finally:
        await transport.aclose()
    assert first.status == 503 and first.retry_after == "7" and second.status == 204
    assert calls[0] == calls[1] and json.loads(calls[0][0]) == body
    assert calls[0][1]["idempotency-key"] == key
    context = render_context_from_headers(body, calls[0][1])
    assert context["templateVersion"] == XU_DISPLAY_TEMPLATE
    assert context == collection.documents[body["finalReportId"]]["renderContext"]


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [None, LEGACY_DISPLAY_TEMPLATE, XU_DISPLAY_TEMPLATE])
async def test_preupgrade_receipts_only_use_their_saved_audit_version_without_writes(version):
    body = receipt()
    stored = {"audit": {"displayTemplateVersion": version}} if version else {}
    collection = SimpleNamespace(find_one=AsyncMock(return_value=stored), update_one=AsyncMock())
    store = MongoGutBarrierStore(collection)
    context = await store.delivery_render_context(body, idempotency_key="gbr_binding:gutBarrier:v2")
    assert context == (callback_render_context(body, version) if version else None)
    collection.update_one.assert_not_called()
    assert collection.find_one.await_args.args[1] == {
        "renderContext": 1, "audit.displayTemplateVersion": 1, "_id": 0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["body", "key", "binding", "database"])
async def test_invalid_or_unavailable_binding_never_sends_http(mutation):
    store, collection, body, key = await frozen()
    sent = AsyncMock(return_value=httpx.Response(204))
    if mutation == "body":
        body["report"]["meta"]["title"] = "changed"
    elif mutation == "key":
        key += "-wrong"
    elif mutation == "binding":
        collection.documents[body["finalReportId"]]["renderContext"]["reportSha256"] = "0" * 64
    else:
        collection.unavailable = True
    transport = GutBarrierCallbackTransport(store=store, transport=httpx.MockTransport(sent))
    try:
        with pytest.raises((ValueError, ConnectionError)):
            await transport.post_once("https://receiver.invalid/callback", body, idempotency_key=key)
    finally:
        await transport.aclose()
    sent.assert_not_called()


@pytest.mark.parametrize("mutation", ["missing", "id", "digest", "unknown", "unsafe", "extra_context"])
def test_receiver_rejects_mixed_incomplete_and_unknown_bindings(mutation):
    body = receipt()
    headers = render_context_headers(body, callback_render_context(body, XU_DISPLAY_TEMPLATE))
    if mutation == "missing":
        headers.pop(RENDER_HEADERS["reportId"])
    elif mutation == "id":
        headers[RENDER_HEADERS["reportId"]] = "gbr_other"
    elif mutation == "digest":
        headers[RENDER_HEADERS["reportSha256"]] = "0" * 64
    elif mutation == "unknown":
        headers[RENDER_HEADERS["templateVersion"]] = "unregistered"
    elif mutation == "unsafe":
        headers[RENDER_HEADERS["templateVersion"]] = XU_DISPLAY_TEMPLATE + "\r\nInvalid: header"
    else:
        context = callback_render_context(body, XU_DISPLAY_TEMPLATE)
        context["extra"] = "unsupported"
        with pytest.raises(ValueError):
            render_context_headers(body, context)
        return
    with pytest.raises(ValueError):
        render_context_from_headers(body, headers)


@pytest.mark.asyncio
async def test_failed_receipt_has_no_report_template_headers():
    body = receipt()
    body.update(status="failed", report=None, error={"code": "GENERATION_FAILED", "message": "failed"})
    store = SimpleNamespace(delivery_render_context=AsyncMock(return_value=None))
    calls = []

    async def receive(request):
        calls.append(request)
        return httpx.Response(204)

    transport = GutBarrierCallbackTransport(store=store, transport=httpx.MockTransport(receive))
    try:
        await transport.post_once("https://receiver.invalid/callback", body, idempotency_key="gbr_binding:gutBarrier:v2")
    finally:
        await transport.aclose()
    assert json.loads(calls[0].content) == body
    assert all(header not in calls[0].headers for header in RENDER_HEADERS.values())


def test_saved_callback_headers_resolve_v3_without_changing_report():
    body = receipt()
    original = deepcopy(body)
    headers = render_context_headers(body, callback_render_context(body, XU_DISPLAY_TEMPLATE))
    assert render_context_from_headers(body, headers) == callback_render_context(body, XU_DISPLAY_TEMPLATE)
    assert body == original


def test_no_headers_has_no_explicit_display_binding():
    body = receipt()
    assert render_context_from_headers(body, {"Content-Type": "application/json"}) is None


@pytest.mark.asyncio
async def test_parallel_callbacks_never_share_another_reports_headers():
    async def resolve(body, *, idempotency_key):
        await asyncio.sleep(0)
        version = XU_DISPLAY_TEMPLATE if int(body["finalReportId"].split("_")[-1]) % 2 else LEGACY_DISPLAY_TEMPLATE
        return callback_render_context(body, version)

    seen = []

    async def receive(request):
        await asyncio.sleep(0)
        body = json.loads(request.content)
        seen.append(render_context_from_headers(body, request.headers))
        assert request.headers["Idempotency-Key"] == body["finalReportId"] + ":gutBarrier:v2"
        return httpx.Response(204)

    transport = GutBarrierCallbackTransport(store=SimpleNamespace(delivery_render_context=resolve),
                                           transport=httpx.MockTransport(receive), concurrency=20)
    try:
        await asyncio.gather(*(transport.post_once("https://receiver.invalid/callback", receipt(f"gbr_{index}"),
            idempotency_key=f"gbr_{index}:gutBarrier:v2") for index in range(20)))
    finally:
        await transport.aclose()
    assert {context["reportId"] for context in seen} == {f"gbr_{index}" for index in range(20)}
    assert all(context["templateVersion"] == (XU_DISPLAY_TEMPLATE if int(context["reportId"].split("_")[-1]) % 2
                                             else LEGACY_DISPLAY_TEMPLATE) for context in seen)
