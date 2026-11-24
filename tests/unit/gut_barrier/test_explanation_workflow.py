"""Main-report-only workflow and non-mutating retirement of explanation jobs."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from adapters.gut_barrier.store import _content_hash
from modules.gut_barrier.policy import EXPLANATION_TYPE
from modules.gut_barrier.service import SERVICE_VERSION
from modules.report.durable_ports import CallbackResponse
from tests.unit.gut_barrier.test_service import environment, request
from tests.unit.report.durable_fakes import matches


def historical_explanation(env, *, status="queued", delivery_status="waiting_generation",
                           body_type="gutBarrier", tagged=True, diagnostic="diagnostic_summary"):
    body = {
        "type": body_type, "finalReportId": "old-parent", "userId": "u", "sessionId": "s",
        diagnostic: {}, "status": "success",
    }
    job = {
        "_id": "old-parent:" + EXPLANATION_TYPE, "parentReportId": "old-parent",
        "userId": "u", "sessionId": "s", "version": SERVICE_VERSION + ":" + env.pipeline.version,
        "explanationVersion": "retired-explain-v1", "status": status,
        "createdAt": env.clock() - timedelta(days=1), "completedAt": env.clock(),
        "generationAttempts": 1, "leaseToken": "old-generation-token",
        "leaseUntil": env.clock() - timedelta(seconds=1),
        "body": body, "contentHash": _content_hash(body),
        "callbackUrl": "https://receiver.invalid/callback", "deliveryStatus": delivery_status,
        "deliveryAttempts": 2, "nextDeliveryAt": env.clock(),
        "deliveryLeaseUntil": env.clock() - timedelta(seconds=1),
        "deliveryToken": "old-delivery-token", "deliveryRequestRevision": 0,
    }
    if tagged:
        job["jobType"] = EXPLANATION_TYPE
    if delivery_status == "delivered":
        job["deliveryReplayPending"] = True
    # An unfinished tagged child has no frozen body yet.
    if tagged and status in {"queued", "generating", "recovering"}:
        job.pop("body")
        job.pop("contentHash")
    return job


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["success", "partial", "failed"])
async def test_new_request_generates_and_delivers_only_one_main_report(status):
    transport = SimpleNamespace(post_once=AsyncMock(return_value=CallbackResponse(204)))
    env = environment(callback_url="https://receiver.invalid/callback", transport=transport)
    if status == "partial":
        env.pipeline.generate.return_value = ({"meta": {"title": "测试报告"}}, {"partial": True})
    elif status == "failed":
        env.pipeline.generate.side_effect = RuntimeError("main generation failed")
    parent = await env.service.submit(request())

    assert await env.service.drain_generation() == 1
    assert await env.service.drain_delivery() == 1
    assert await env.service.drain_generation() == 0
    assert await env.service.drain_delivery() == 0
    assert len(env.collection.documents) == 1
    saved = env.collection.documents[parent["_id"]]
    assert saved["status"] == status
    assert saved["body"]["type"] == "gutBarrier"
    assert saved["body"]["schemaVersion"] == "2.0"
    assert "report" in saved["body"]
    assert not any(key.startswith("explanation") for key in saved)
    assert env.pipeline.generate.await_count == 1
    assert transport.post_once.await_count == 1
    assert transport.post_once.await_args.args[1] == saved["body"]
    assert transport.post_once.await_args.kwargs["idempotency_key"] == parent["_id"] + ":gutBarrier:v2"
    assert not any("explanation" in options["name"] for _, options in env.collection.indexes)


@pytest.mark.asyncio
@pytest.mark.parametrize("intent", ["pending", "scheduled"])
@pytest.mark.parametrize("has_replay_intent", [False, True])
async def test_repeated_parent_admission_replays_only_frozen_main_report(intent, has_replay_intent):
    transport = SimpleNamespace(post_once=AsyncMock(return_value=CallbackResponse(204)))
    env = environment(callback_url="https://receiver.invalid/callback", transport=transport)
    parent = await env.service.submit(request())
    assert await env.service.drain_generation() == 1
    assert await env.service.drain_delivery() == 1
    row = env.collection.documents[parent["_id"]]
    frozen = deepcopy(row["body"])
    row.update(explanationStatus=intent, explanationVersion="retired-explain-v1")
    if has_replay_intent:
        row["explanationReplayPending"] = True
    if intent == "scheduled":
        child = historical_explanation(env, status="success", delivery_status="delivered")
        child.update(_id=parent["_id"] + ":" + EXPLANATION_TYPE, parentReportId=parent["_id"])
        child["body"]["finalReportId"] = parent["_id"]
        child["contentHash"] = _content_hash(child["body"])
        await env.collection.insert_one(child)
    else:
        child = None

    assert (await env.service.submit(request()))["_id"] == parent["_id"]
    assert await env.service.drain_generation() == 0
    assert await env.service.recover_pending() == 0
    assert await env.service.drain_delivery() == 1
    assert await env.service.drain_delivery() == 0
    saved = env.collection.documents[parent["_id"]]
    assert saved["body"] == frozen
    assert saved["deliveryRequestRevision"] == 1
    assert saved.get("explanationReplayPending", False) is has_replay_intent
    assert saved["explanationStatus"] == intent
    assert env.pipeline.generate.await_count == 1
    assert [call.args[1] for call in transport.post_once.await_args_list] == [frozen, frozen]
    assert len(env.collection.documents) == (2 if child else 1)
    if child:
        assert env.collection.documents[child["_id"]] == child


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["queued", "generating", "recovering"])
@pytest.mark.parametrize("tagged", [False, True])
async def test_retired_generation_jobs_are_never_claimed_or_recovered(status, tagged):
    env = environment()
    child = historical_explanation(env, status=status, tagged=tagged)
    await env.collection.insert_one(child)
    parent = await env.service.submit(request())

    assert await env.service.recover_pending() == 0
    assert await env.service.drain_generation() == 1
    assert await env.service.drain_generation() == 0
    assert await env.store.claim_generation(expired=True) is None
    assert env.collection.documents[child["_id"]] == child
    assert env.collection.documents[parent["_id"]]["status"] == "success"
    assert env.pipeline.generate.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery_status", ["pending", "retry_wait", "sending", "delivered"])
@pytest.mark.parametrize("body_type,tagged,diagnostic", [
    ("gutBarrier", True, "diagnostic_summary"),
    ("gutBarrierExplain", False, "diagnostic_summary"),
    ("gutBarrier", False, "diagnostic_summary"),
    ("gutBarrier", False, "diagnostic_basis"),
    ("gutBarrier", False, "diagnostic_advice"),
])
async def test_retired_callback_jobs_never_claim_or_block_main_delivery(
        delivery_status, body_type, tagged, diagnostic):
    transport = SimpleNamespace(post_once=AsyncMock(return_value=CallbackResponse(204)))
    env = environment(callback_url="https://receiver.invalid/callback", transport=transport)
    child = historical_explanation(env, status="success", delivery_status=delivery_status,
                                   body_type=body_type, tagged=tagged, diagnostic=diagnostic)
    await env.collection.insert_one(child)
    parent = await env.service.submit(request())
    assert await env.service.drain_generation() == 1

    assert (await env.store.backlog())["pending"] == 1
    assert await env.service.drain_delivery() == 1
    assert await env.store.claim_delivery() is None
    assert await env.store.backlog() == {"pending": 0, "oldestPendingAt": None, "oldestWaitSeconds": 0}
    assert env.collection.documents[child["_id"]] == child
    assert transport.post_once.await_count == 1
    assert transport.post_once.await_args.args[1] == env.collection.documents[parent["_id"]]["body"]


@pytest.mark.asyncio
async def test_pre_upgrade_generation_lease_cannot_complete_or_renew_retired_child():
    env = environment()
    child = historical_explanation(env, status="generating")
    child["leaseUntil"] = env.clock() + timedelta(seconds=120)
    await env.collection.insert_one(child)

    assert not await env.store.renew_generation(child)
    assert not await env.store.mark_generation_started(child)
    assert not await env.store.release_generation(child, known_not_started=True)
    assert not await env.store.complete(child, body={"status": "failed"}, audit={})
    assert env.collection.documents[child["_id"]] == child


@pytest.mark.asyncio
@pytest.mark.parametrize("tagged", [False, True])
async def test_pre_upgrade_delivery_lease_cannot_read_ack_or_retry_retired_callback(tagged):
    env = environment()
    child = historical_explanation(env, status="success", delivery_status="sending", tagged=tagged)
    child["deliveryLeaseUntil"] = env.clock() + timedelta(seconds=120)
    await env.collection.insert_one(child)
    lease = {"jobId": child["_id"], "userId": "u", "contentHash": child["contentHash"],
             "deliveryToken": child["deliveryToken"], "deliveryRequestRevision": 0}

    with pytest.raises(ValueError, match="immutable gut-barrier artifact"):
        await env.store.delivery_body(lease)
    assert not await env.store.acknowledge_delivery(lease)
    assert not await env.store.retry_delivery(lease, next_retry_at=env.clock(), error="not sent")
    assert env.collection.documents[child["_id"]] == child


def test_fake_nested_query_preserves_missing_versus_null_and_boolean_composition():
    row = {"body": {"type": "gutBarrier", "report": None, "diagnostic_summary": {}}}
    assert matches(row, {"body.report": {"$exists": True}})
    assert not matches(row, {"body.report": {"$exists": False}})
    assert matches(row, {"body.schemaVersion": {"$exists": False}})
    assert matches(row, {"$and": [{"body.type": "gutBarrier"}, {"body.report": {"$exists": True}}]})
    assert matches(row, {"$nor": [{"body.type": "gutBarrierExplain"}, {"body.report": {"$exists": False}}]})
    assert not matches(row, {"$nor": [{"body.diagnostic_summary": {"$exists": True}}]})
