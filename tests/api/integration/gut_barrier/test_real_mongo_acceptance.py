"""Opt-in acceptance against a disposable, task-owned local Mongo database.

Run only with GUT_BARRIER_TEST_MONGO_URI=mongodb://127.0.0.1:<temporary-port>.
Each test allocates a unique database and drops only that database in finally.
The test never imports deployment .env, production DB adapters, OCR or a model.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import ipaddress
import json
import os
import re
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
import pytest_asyncio


def _temporary_local_uri():
    value = os.environ.get("GUT_BARRIER_TEST_MONGO_URI")
    if not value:
        pytest.skip("set GUT_BARRIER_TEST_MONGO_URI for disposable local Mongo acceptance")
    try:
        parsed = urlsplit(value)
        address = ipaddress.ip_address(parsed.hostname or "")
        valid = (
            parsed.scheme == "mongodb" and address.is_loopback
            and parsed.port is not None and 0 < parsed.port < 65536
            and not parsed.username and not parsed.password
            and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        pytest.fail("acceptance Mongo URI must be a single numeric loopback host:port without credentials, DB path or options")
    return value


@pytest.fixture
def isolated_settings(monkeypatch):
    uri = _temporary_local_uri()
    name = "gut_barrier_acceptance_" + uuid4().hex
    assert re.fullmatch(r"gut_barrier_acceptance_[0-9a-f]{32}", name)
    # Apply before lazily importing the real application route. These are only
    # test placeholders; no inherited production environment is printed/read.
    settings = {
        "SHULAN_ENVIRONMENT": "test", "SHULAN_LOG_LEVEL": "CRITICAL",
        "SHULAN_LOG_FILE_ENABLED": "false", "SHULAN_LLM_API_KEY": "unused-local-test-key",
        "SHULAN_LLM_PROVIDER": "", "SHULAN_LLM_DEFAULT_MODEL": "unused-local-test-model",
        "SHULAN_ALIYUN_DOCMIND_ACCESSKEY_ID": "unused-local-test-id",
        "SHULAN_ALIYUN_DOCMIND_ACCESSKEY_SECRET": "unused-local-test-secret",
        "SHULAN_ALIYUN_DOCMIND_ENDPOINT": "https://unit-test.invalid",
        "SHULAN_MONGODB_URI": uri, "SHULAN_MONGODB_DATABASE": name,
        "SHULAN_MONGODB_COLL_SESSIONS": "test-sessions",
        "SHULAN_MONGODB_COLL_HEALTH_REPORTS": "test-health-reports",
        "SHULAN_REDIS_HOST": "127.0.0.1", "SHULAN_REDIS_PORT": "1", "SHULAN_REDIS_PASSWORD": "",
        "SHULAN_KNOWLEDGE_API_KEY": "", "SHULAN_EVIMED_API_KEY": "",
        "SHULAN_REPORT_CALLBACK_URL": "http://127.0.0.1:1/disabled-callback",
        "SHULAN_REPORT_PRODUCT_URL": "http://127.0.0.1:1/disabled-products",
    }
    for key, value in settings.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("loongspears.utils.env.load_env", lambda *args, **kwargs: None)
    return SimpleNamespace(uri=uri, name=name)


@pytest_asyncio.fixture
async def mongo(isolated_settings):
    from pymongo import AsyncMongoClient
    from pymongo.write_concern import WriteConcern

    config = isolated_settings
    client = AsyncMongoClient(
        config.uri, directConnection=True, serverSelectionTimeoutMS=2000,
        connectTimeoutMS=2000, socketTimeoutMS=5000, tz_aware=True,
    )
    owns_database = False
    try:
        await client.admin.command("ping")
        # Never take cleanup ownership of an existing database, even in the
        # astronomically unlikely event of a UUID collision.
        assert config.name not in await client.list_database_names()
        owns_database = True
        database = client[config.name]
        collection = database["gut_barrier_report_jobs"].with_options(
            write_concern=WriteConcern(w="majority", j=True, wtimeout=2000))
        yield SimpleNamespace(client=client, database=database, collection=collection)
    finally:
        try:
            if owns_database:
                assert re.fullmatch(r"gut_barrier_acceptance_[0-9a-f]{32}", config.name)
                await client.drop_database(config.name)
                assert config.name not in await client.list_database_names()
        finally:
            await client.close()


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 24, 3, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


def payload(user="integration-synthetic-user"):
    return {
        "userId": user, "sessionId": "integration-synthetic-session",
        "name": "集成验收模拟样本", "age": "26", "sex": "女",
        "fileList": [], "testData": [], "hospitalData": [], "microbiomeData": None,
        "healthSurvey": {"name": "集成验收模拟样本", "age": "26", "gender": "女",
                         "heightCm": "160", "weightKg": "60", "allergy": "无",
                         "currentMedication": "无", "specialMedication": "无"},
        "gutHealthIndicator": {
            "dLacticAcid": {"value": "0.06", "unit": "mg/L"},
            "diamineOxidase": {"value": "3.8", "unit": "U/L"},
            "bacterialEndotoxin": {"value": "12.5", "unit": "U/L"},
            "hsCRP": {"value": ">3.0", "unit": "mg/L"},
        },
    }


async def bounded(awaitable):
    return await asyncio.wait_for(awaitable, 5)


@pytest.mark.asyncio
async def test_real_mongo_formal_http_submission_and_frozen_callback_retries(mongo):
    import httpx
    from tornado.httpserver import HTTPServer
    from tornado.netutil import bind_sockets
    from tornado.web import Application, RequestHandler
    from adapters.gut_barrier.store import MongoGutBarrierStore
    from adapters.report.delivery import ReportCallbackTransport
    from bootstrap.container import AppContainer
    from modules.gut_barrier.pipeline import GutBarrierPipeline
    from modules.gut_barrier.service import GutBarrierService
    from modules.report.durable import ReportCallbackWorker
    from runtime import ApplicationLifecycle, TaskSupervisor
    from web.routes import build_routes

    class Receiver(RequestHandler):
        async def post(self):
            self.application.settings["received"].append({
                "key": self.request.headers["Idempotency-Key"], "bytes": self.request.body,
            })
            self.set_status(503 if len(self.application.settings["received"]) == 1 else 204)

    clock, supervisor = Clock(), TaskSupervisor()
    store = MongoGutBarrierStore(mongo.collection, clock=clock)
    real_pipeline = GutBarrierPipeline()  # deterministic: no sources, catalog or model adapters
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def generate(*args, **kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return await real_pipeline.generate(*args, **kwargs)

    pipeline = SimpleNamespace(version=real_pipeline.version, generate=generate)
    transport = ReportCallbackTransport(
        transport=httpx.AsyncHTTPTransport(trust_env=False), timeout_seconds=2, concurrency=4)
    worker = ReportCallbackWorker(store=store, transport=transport, clock=clock, jitter=lambda: 1)
    service = GutBarrierService(store=store, pipeline=pipeline, transport=transport, callback_worker=worker)
    lifecycle = ApplicationLifecycle()
    lifecycle.mark_ready()
    dependency = SimpleNamespace()
    container = AppContainer(
        agent_runtime=dependency, analysis_service_factory=lambda: dependency,
        record_service=dependency, chat_service=dependency, health_profile_service=dependency,
        health_report_source=dependency, record_profile_source=dependency, chat_profile_source=dependency,
        health_profile_memory_provider=dependency, health_profile_ingestion_worker=dependency,
        report_pipeline=dependency, report_task_coordinator=dependency,
        lifecycle=lifecycle, task_supervisor=supervisor, gut_barrier_service=service,
    )
    app = Application([*build_routes(container), (r"/integration-callback", Receiver)],
                      lifecycle=lifecycle, task_supervisor=supervisor, received=[])
    sockets = bind_sockets(0, "127.0.0.1")
    port = sockets[0].getsockname()[1]
    server = HTTPServer(app)
    server.add_sockets(sockets)
    service.callback_url = f"http://127.0.0.1:{port}/integration-callback"
    url = f"http://127.0.0.1:{port}/api/v1/gut-barrier/report"
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            first = await client.post(url, json=payload())
            assert first.status_code == 200
            report_id = first.json()["data"]["finalReportId"]
            await bounded(entered.wait())
            persisted = await mongo.collection.find_one({"_id": report_id})
            assert persisted is not None and persisted["request"]["userId"] == payload()["userId"]
            assert persisted["status"] == "generating" and "body" not in persisted
            duplicate = await client.post(url, json=payload())
            assert duplicate.status_code == 200 and duplicate.json()["data"]["finalReportId"] == report_id
            assert await mongo.collection.count_documents({}) == 1 and calls == 1

            release.set()
            await bounded(supervisor.wait_idle())
            failed_delivery = await mongo.collection.find_one({"_id": report_id})
            assert failed_delivery["deliveryStatus"] == "retry_wait"
            assert failed_delivery["status"] == "partial" and failed_delivery["generationAttempts"] == 1
            assert failed_delivery["deliveryAttempts"] == 1
            frozen = deepcopy(failed_delivery["body"])
            assert frozen["type"] == "gutBarrier" and frozen["report"]["schemaVersion"] == "gut-barrier-public.v2"
            assert len(app.settings["received"]) == 1
            assert json.loads(app.settings["received"][0]["bytes"]) == frozen

            clock.advance(31)
            assert await bounded(service.drain_delivery()) == 1
            delivered = await mongo.collection.find_one({"_id": report_id})
            assert delivered["deliveryStatus"] == "delivered" and delivered["deliveryAttempts"] == 2
            assert delivered["body"] == frozen and calls == 1
            replay = await client.post(url, json=payload())
            assert replay.status_code == 200 and replay.json()["data"]["finalReportId"] == report_id
            await bounded(supervisor.wait_idle())
            completed = await mongo.collection.find_one({"_id": report_id})
            assert completed["deliveryStatus"] == "delivered" and completed["deliveryAttempts"] == 3
            assert completed["body"] == frozen and completed["generationAttempts"] == 1 and calls == 1
            callbacks = app.settings["received"]
            assert len(callbacks) == 3 and callbacks[0] == callbacks[1] == callbacks[2]
            assert callbacks[0]["key"] == report_id + ":gutBarrier:v2"
    finally:
        release.set()
        service.stop_accepting()
        supervisor.stop_accepting()
        try:
            try:
                await bounded(supervisor.wait_idle())
            except TimeoutError:
                # This supervisor belongs only to this test. Do not cancel
                # unrelated pytest/application tasks when an assertion fails.
                pending = tuple(supervisor._tasks)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                raise
        finally:
            server.stop()
            try:
                await server.close_all_connections()
            finally:
                await transport.aclose()


@pytest.mark.asyncio
async def test_two_real_stores_claim_once_and_preflight_uses_read_only_real_aggregate(mongo):
    from adapters.gut_barrier.store import MongoGutBarrierStore
    from modules.gut_barrier.schemas import GutBarrierRequest
    from modules.gut_barrier.service import GutBarrierService, SERVICE_VERSION
    from scripts.check_gut_barrier_queue import summarize_snapshot

    clock = Clock()
    first, second = [MongoGutBarrierStore(mongo.collection, clock=clock) for _ in range(2)]
    pipeline = SimpleNamespace(version="real-claim-acceptance")
    services = [GutBarrierService(store=store, pipeline=pipeline) for store in (first, second)]
    request = GutBarrierRequest.model_validate(payload())
    left, right = await asyncio.gather(*(service.submit(request) for service in services))
    assert left["_id"] == right["_id"]
    for index in range(9):
        clock.advance(1)
        await services[index % 2].submit(GutBarrierRequest.model_validate(payload(f"claim-{index}")))
    version = SERVICE_VERSION + ":" + pipeline.version
    claims = await asyncio.gather(*(
        (first if index % 2 else second).claim_generation(version=version) for index in range(20)
    ))
    claimed = [job for job in claims if job is not None]
    assert len(claimed) == 10 and len({job["_id"] for job in claimed}) == 10
    assert await mongo.collection.count_documents({"generationAttempts": 1, "status": "generating"}) == 10

    started = claimed[0]
    assert await first.mark_generation_started(started)
    assert not await second.mark_generation_started(started)
    assert not await second.release_generation(started, known_not_started=True)
    assert await first.release_generation(claimed[1], known_not_started=True)
    old = await first.admit(fingerprint=uuid4().hex, request=payload("old-version"),
                            callback_url="", version="retired-test-version")
    before = await mongo.collection.find({}).sort("_id", 1).to_list(length=20)
    snapshot = await second.generation_queue_snapshot()
    result = summarize_snapshot(snapshot, expected_version=version)
    assert result["status"] == "blocked" and result["blockingVersions"] == ["retired-test-version"]
    assert result["totals"] == {"queued": 2, "generating": 9, "recovering": 0}
    assert "request" not in str(result) and "integration-synthetic" not in str(result)
    assert await mongo.collection.find({}).sort("_id", 1).to_list(length=20) == before
    assert (await mongo.collection.find_one({"_id": old["_id"]}))["version"] == "retired-test-version"


@pytest.mark.asyncio
async def test_real_mongo_twenty_rolling_generators_and_twenty_first_slot(mongo):
    from adapters.gut_barrier.store import MongoGutBarrierStore
    from modules.gut_barrier.schemas import GutBarrierRequest
    from modules.gut_barrier.service import GutBarrierService

    clock = Clock()
    store = MongoGutBarrierStore(mongo.collection, clock=clock)
    entered = [asyncio.Event() for _ in range(21)]
    release = [asyncio.Event() for _ in range(21)]
    active = peak = calls = 0

    async def generate(request, *, report_id, **kwargs):
        nonlocal active, peak, calls
        index = int(request.user_id.rsplit("-", 1)[1])
        active += 1
        calls += 1
        peak = max(peak, active)
        entered[index].set()
        try:
            await release[index].wait()
            return {"meta": {"title": "并发集成验收", "reportId": report_id}, "sections": []}, {"partial": False}
        finally:
            active -= 1

    service = GutBarrierService(store=store, pipeline=SimpleNamespace(version="real-rolling-acceptance", generate=generate))
    await service.submit(GutBarrierRequest.model_validate(payload("rolling-0")))
    owner = asyncio.create_task(service.drain_generation())
    try:
        await bounded(entered[0].wait())
        for index in range(1, 21):
            clock.advance(1)
            await service.submit(GutBarrierRequest.model_validate(payload(f"rolling-{index}")))
        await bounded(asyncio.gather(*(event.wait() for event in entered[:20])))
        assert active == peak == calls == 20 and not entered[20].is_set()
        assert await mongo.collection.count_documents({"status": "generating"}) == 20
        assert await mongo.collection.count_documents({"status": "queued"}) == 1
        assert await asyncio.gather(*(service.drain_generation() for _ in range(30))) == [0] * 30
        release[3].set()
        await bounded(entered[20].wait())
        assert active == peak == 20 and calls == 21 and not owner.done()
    finally:
        for event in release:
            event.set()
        await bounded(owner)
        service.stop_accepting()
    assert owner.result() == 21 and peak == 20 and active == 0
    assert await mongo.collection.count_documents({"status": "success", "generationAttempts": 1,
                                                   "generationStartedAt": {"$exists": True}}) == 21
    assert await store.generation_queue_snapshot() == []
