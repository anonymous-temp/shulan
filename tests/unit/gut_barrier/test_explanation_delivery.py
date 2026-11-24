"""Retired explain receipts must never become a second gutBarrier callback."""
from copy import deepcopy
from datetime import timedelta
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from adapters.gut_barrier.delivery import GutBarrierCallbackTransport
from adapters.gut_barrier.render_context import render_context_from_headers
from adapters.gut_barrier.store import MongoGutBarrierStore, _content_hash
from modules.gut_barrier.display_versions import LATEST_DISPLAY_TEMPLATE
from modules.gut_barrier.policy import EXPLANATION_TYPE
from modules.report.durable import ReportCallbackWorker
from tests.unit.report.durable_fakes import Clock, Collection


async def frozen_pair(*, legacy=False, delivery_status="pending"):
    collection, clock = Collection(), Clock()
    store = MongoGutBarrierStore(collection, clock=clock)
    request = {"userId": "owner-a", "sessionId": "session-a"}
    parent = await store.admit(fingerprint="delivery-parent", request=request,
        callback_url="https://receiver.invalid/callback", version="delivery-main-v1")
    parent = await store.claim_generation(version="delivery-main-v1")
    report_id = parent["_id"]
    main = {"type": "gutBarrier", "schemaVersion": "2.0", "finalReportId": report_id,
        **request, "status": "partial", "error": None,
        "report": {"schemaVersion": "gut-barrier-public.v2",
            "meta": {"title": "投递契约测试", "reportId": report_id},
            "sections": [{"id": "overview", "title": "结果", "subtitle": None,
                "visible": True, "blocks": [{"kind": "facts", "title": None, "items": [
                    {"label": "零值", "value": 0}, {"label": "空值", "value": None},
                    {"label": "标记", "value": False}]}]}]}}
    assert await store.complete(parent, body=main, audit={
        "displayTemplateVersion": LATEST_DISPLAY_TEMPLATE, "privateAudit": "never-send-audit"})
    child_body = {"type": EXPLANATION_TYPE if legacy else "gutBarrier",
        "finalReportId": report_id, **request, "diagnostic_summary": {"conclusion": None},
        "diagnostic_basis": {"abnormal_findings": []}, "diagnostic_advice": {},
        "evidence": [], "status": "partial_success", "error": {"error_code": None, "message": None}}
    # Seed an actual pre-upgrade receipt instead of resurrecting the retired generator.
    child_id = report_id + ":" + EXPLANATION_TYPE
    collection.documents[child_id] = {"_id": child_id, "jobType": EXPLANATION_TYPE,
        "parentReportId": report_id, **request, "version": "delivery-main-v1",
        "body": child_body, "contentHash": _content_hash(child_body), "status": "partial",
        "callbackUrl": "https://receiver.invalid/callback", "deliveryStatus": delivery_status,
        "nextDeliveryAt": clock(), "deliveryLeaseUntil": clock() - timedelta(seconds=1),
        "deliveryAttempts": 1, "generationAttempts": 1, "deliveryReplayPending": True}
    collection.documents[report_id].update(explanationStatus="pending",
        explanationVersion="retired-v1", explanationReplayPending=True)
    return store, collection, clock, main, child_body, child_id


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("delivery_status", ["pending", "retry_wait", "sending", "delivered"])
async def test_main_retry_is_frozen_and_historical_explanation_is_never_sent(legacy, delivery_status):
    store, collection, clock, main, child, child_id = await frozen_pair(
        legacy=legacy, delivery_status=delivery_status)
    child_before = deepcopy(collection.documents[child_id])
    received = []

    async def receive(request):
        received.append((request.content, dict(request.headers)))
        return httpx.Response(503 if len(received) == 1 else 204)

    transport = GutBarrierCallbackTransport(store=store, transport=httpx.MockTransport(receive))
    worker = ReportCallbackWorker(store=store, transport=transport, clock=clock, jitter=lambda: 1)
    try:
        assert await worker.drain_pending() == 1
        assert collection.documents[main["finalReportId"]]["deliveryStatus"] == "retry_wait"
        clock.advance(31)
        assert await worker.drain_pending() == 1
        assert await worker.drain_pending() == 0
    finally:
        await transport.aclose()
    assert len(received) == 2 and received[0] == received[1]
    raw, headers = received[0]
    assert json.loads(raw) == main
    assert headers["idempotency-key"] == f"{main['finalReportId']}:gutBarrier:v2"
    assert render_context_from_headers(main, headers)["templateVersion"] == LATEST_DISPLAY_TEMPLATE
    assert b"never-send-audit" not in raw and b"diagnostic_summary" not in raw
    assert collection.documents[child_id] == child_before
    assert collection.documents[main["finalReportId"]]["body"] == main
    assert collection.documents[main["finalReportId"]]["contentHash"] == _content_hash(main)


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_duplicate_admission_only_replays_main_and_keeps_old_child_unchanged(legacy):
    store, collection, clock, main, _, child_id = await frozen_pair(legacy=legacy)
    child_before = deepcopy(collection.documents[child_id])
    received = []

    async def receive(request):
        received.append(request.content)
        return httpx.Response(204)

    transport = GutBarrierCallbackTransport(store=store, transport=httpx.MockTransport(receive))
    worker = ReportCallbackWorker(store=store, transport=transport, clock=clock)
    try:
        assert await worker.drain_pending() == 1
        replay = await store.admit(fingerprint="delivery-parent", request={
            "userId": "owner-a", "sessionId": "session-a"},
            callback_url="https://receiver.invalid/callback", version="delivery-main-v1")
        assert replay["_id"] == main["finalReportId"]
        assert await worker.drain_pending() == 1
    finally:
        await transport.aclose()
    assert len(received) == 2 and received[0] == received[1]
    assert collection.documents[child_id] == child_before
    assert len(collection.documents) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("remaining_key", ["diagnostic_summary", "diagnostic_basis", "diagnostic_advice"])
async def test_even_stale_direct_transport_calls_cannot_send_explanations(legacy, remaining_key):
    body = {"type": EXPLANATION_TYPE if legacy else "gutBarrier", "finalReportId": "gbr_old",
            remaining_key: {}, "status": "partial_success"}
    receive = AsyncMock(return_value=httpx.Response(204))
    store = AsyncMock()
    transport = GutBarrierCallbackTransport(store=store, transport=httpx.MockTransport(receive))
    try:
        with pytest.raises(ValueError, match="explanation delivery is disabled"):
            await transport.post_once("https://receiver.invalid/callback", body,
                idempotency_key=f"gbr_old:{body['type']}:v1")
    finally:
        await transport.aclose()
    receive.assert_not_called()
    store.delivery_render_context.assert_not_awaited()
