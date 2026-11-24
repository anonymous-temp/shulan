"""Offline failure-boundary checks for the final-input report workflow.

All remote adapters, persistence and callback receivers are local fakes. These
checks do not load deployment credentials or contact external services.
"""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from adapters.gut_barrier import sources as sources_module
from adapters.gut_barrier.sources import GutBarrierSources
from modules.gut_barrier import pipeline as pipeline_module
from modules.gut_barrier.pipeline import GutBarrierPipeline
from modules.gut_barrier.publication import project_callback_report
from modules.gut_barrier.schemas import GutBarrierRequest
from modules.report.durable_ports import CallbackResponse
from tests.unit.gut_barrier.test_service import environment


EVALUATION_DATE = "2026-09-24"
SECRET = "private-patient-token-must-not-leak"


def request(*, indicators=True, **extra):
    payload = {
        "userId": "fallback-test", "sessionId": "fallback-session",
        "name": "离线测试", "age": "50", "sex": "男",
        "fileList": [], "testData": [], "hospitalData": [],
    }
    if indicators:
        payload["gutHealthIndicator"] = {
            "dLacticAcid": {"value": "26", "unit": "mg/L"},
            "diamineOxidase": {"value": "17", "unit": "U/L"},
            "bacterialEndotoxin": {"value": "23", "unit": "U/L"},
            "hsCRP": {"value": "2.7", "unit": "mg/L"},
        }
    payload.update(extra)
    return GutBarrierRequest.model_validate(payload)


async def generate(sources, payload=None, **dependencies):
    return await GutBarrierPipeline(sources=sources, **dependencies).generate(
        payload or request(), report_id="gbr_fallback_test", evaluation_date=EVALUATION_DATE)


def assert_direct_results(report):
    assessed = report["assessment"]
    assert assessed["barrier"]["score"] == 9.5
    assert assessed["barrier"]["grade"] == "V"
    assert assessed["barrier"]["pattern"] == "+++"
    assert "inflammation" not in assessed
    assert {row["code"] for row in assessed["markers"]} == {"D_LACTATE", "DAO", "LPS"}
    assert [section["id"] for section in project_callback_report(report)["sections"]] == [
        "overview", "disease_risks", "appendix"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ConnectionError, TimeoutError, ValueError])
async def test_collection_exception_falls_back_to_direct_indicators_without_exception_text(error):
    sources = SimpleNamespace(collect=AsyncMock(side_effect=error(SECRET)))
    report, audit = await generate(sources)
    assert_direct_results(report)
    assert audit["partial"] is True
    assert audit["sources"] == []
    assert audit["issues"] == [{"code": "SOURCE_COLLECTION_UNAVAILABLE", "errorType": error.__name__}]
    assert SECRET not in json.dumps([report, audit], ensure_ascii=False)
    sources.collect.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [
    None, [], "wrong-source-shape", {},
    {"observations": {}, "sources": [], "issues": []},
    {"observations": [None], "sources": [], "issues": []},
    {"observations": [], "sources": ["unsafe"], "issues": []},
    {"observations": [], "sources": [], "issues": ["unsafe"]},
    {"observations": [], "sources": [], "issues": [], "context": None},
    {"observations": [], "sources": [], "issues": [], "context": []},
])
async def test_invalid_collection_shape_discards_all_untrusted_source_data(malformed):
    sources = SimpleNamespace(collect=AsyncMock(return_value=malformed))
    report, audit = await generate(sources)
    assert_direct_results(report)
    assert audit["partial"] is True
    assert audit["sources"] == []
    assert audit["issues"] == [{"code": "SOURCE_COLLECTION_UNAVAILABLE", "errorType": "ValueError"}]


@pytest.mark.asyncio
async def test_collection_context_is_optional_but_invalid_result_cannot_supply_context_facts():
    valid = SimpleNamespace(collect=AsyncMock(return_value={"observations": [], "sources": [], "issues": []}))
    report, audit = await generate(valid)
    assert_direct_results(report)
    assert audit["issues"] == []

    invalid = SimpleNamespace(collect=AsyncMock(return_value={
        "observations": [], "sources": "invalid", "issues": [],
        "context": {"acuteInfection": True, "malformedContextFact": SECRET},
    }))
    report, audit = await generate(invalid)
    assert_direct_results(report)
    assert not report["assessment"]["context"].get("acuteInfection")
    assert SECRET not in json.dumps([report, audit], ensure_ascii=False)


@pytest.mark.asyncio
async def test_collection_outage_preserves_independent_hospital_safety_signal():
    sources = SimpleNamespace(collect=AsyncMock(side_effect=ConnectionError(SECRET)))
    payload = request(hospitalData=[{
        "dataType": "EXAM", "itemName": "腹部检查", "examResult": "肠梗阻",
        "checkTime": EVALUATION_DATE, "reportTime": EVALUATION_DATE,
    }])
    report, audit = await generate(sources, payload)
    assert_direct_results(report)
    assert "fiber_hold" in {item["code"] for item in report["assessment"]["context"]["hospitalSafetySignals"]}
    assert report["interventions"]["nutrition"][0]["status"] == "safety_hold"
    assert audit["partial"] is True


@pytest.mark.asyncio
async def test_collection_outage_without_direct_results_keeps_missing_values_not_normal_values():
    sources = SimpleNamespace(collect=AsyncMock(side_effect=ConnectionError(SECRET)))
    report, audit = await generate(sources, request(indicators=False))
    assert audit["partial"] is True
    assert report["assessment"]["barrier"]["score"] is None
    assert "inflammation" not in report["assessment"]
    assert all(marker["status"] == "missing" and marker["value"] is None
               for marker in report["assessment"]["markers"])
    assert set(report["assessment"]["missingData"]) >= {"D_LACTATE", "DAO", "LPS"}


@pytest.mark.asyncio
async def test_collection_cancellation_is_not_swallowed_as_partial_report():
    sources = SimpleNamespace(collect=AsyncMock(side_effect=asyncio.CancelledError()))
    products = SimpleNamespace(fetch=AsyncMock())
    with pytest.raises(asyncio.CancelledError):
        await generate(sources, products=products)
    products.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_single_source_failure_keeps_good_source_and_direct_results():
    good_document = json.dumps({
        "patientName": "离线测试", "sampleDate": EVALUATION_DATE,
        "items": [{"name": "hs-CRP", "value": "2.7", "unit": "mg/L"}],
    }, ensure_ascii=False).encode()

    async def download(url):
        if url.endswith("bad"):
            raise ConnectionError(SECRET)
        return good_document

    downloader = SimpleNamespace(download=AsyncMock(side_effect=download))
    sources = GutBarrierSources(downloader=downloader)
    report, audit = await generate(sources, request(testData=[{
        "resultDetailUrl": "https://offline.invalid/good",
        "resultSummaryUrl": "https://offline.invalid/bad",
    }]))
    assert_direct_results(report)
    assert audit["partial"] is True
    assert {item["code"] for item in audit["issues"]} == {"SOURCE_UNAVAILABLE"}
    assert {item["status"] for item in audit["sources"]} == {"parsed", "unavailable"}
    assert downloader.download.await_count == 2
    assert SECRET not in json.dumps([report, audit], ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("ocr_configured", [False, True])
async def test_missing_or_failing_ocr_only_marks_attachment_unavailable(monkeypatch, ocr_configured):
    downloader = SimpleNamespace(download=AsyncMock(return_value=b"\xff\xd8\xffoffline-image"))
    parser = SimpleNamespace() if ocr_configured else None
    monkeypatch.setattr(sources_module, "prepare_document", Mock(side_effect=ValueError(SECRET)))
    sources = GutBarrierSources(downloader=downloader, parsing_client=parser)
    report, audit = await generate(sources, request(fileList=[{
        "url": "https://offline.invalid/image.jpg", "fileType": "image",
    }]))
    assert_direct_results(report)
    assert audit["partial"] is True
    expected = "SOURCE_UNAVAILABLE" if ocr_configured else "OCR_UNAVAILABLE"
    assert {item["code"] for item in audit["issues"]} == {expected}
    assert SECRET not in json.dumps([report, audit], ensure_ascii=False)


@pytest.mark.asyncio
async def test_single_source_timeout_does_not_cancel_entire_generation():
    async def blocked_download(url):
        await asyncio.Event().wait()

    sources = GutBarrierSources(
        downloader=SimpleNamespace(download=AsyncMock(side_effect=blocked_download)), timeout_seconds=0.01)
    report, audit = await generate(sources, request(fileList=[{
        "url": "https://offline.invalid/timeout.pdf", "fileType": "pdf",
    }]))
    assert_direct_results(report)
    assert audit["partial"] is True
    assert audit["issues"][0]["code"] == "SOURCE_UNAVAILABLE"
    assert audit["issues"][0]["errorType"] == "TimeoutError"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [ConnectionError, TimeoutError])
async def test_catalog_outage_keeps_measured_results_and_withholds_product_selection(error):
    products = SimpleNamespace(fetch=AsyncMock(side_effect=error(SECRET)))
    report, audit = await generate(None, products=products)
    assert_direct_results(report)
    assert audit["issues"] == [{"code": "PRODUCT_CATALOG_UNAVAILABLE", "errorType": error.__name__}]
    assert audit["partial"] is True
    assert SECRET not in json.dumps([report, audit], ensure_ascii=False)
    assert report["interventions"]["microbiome"]["mdf"]["product"] is None


@pytest.mark.asyncio
async def test_narrative_failure_retains_deterministic_report(monkeypatch):
    original_build = pipeline_module.build_report

    def with_legacy_candidates(*args, **kwargs):
        report = original_build(*args, **kwargs)
        report["narrativeCandidates"] = [{"id": "verified", "text": "已核验段落"}]
        return report

    monkeypatch.setattr(pipeline_module, "build_report", with_legacy_candidates)
    narrative = SimpleNamespace(arrange=AsyncMock(side_effect=ValueError(SECRET)))
    report, audit = await generate(None, narrative=narrative)
    assert_direct_results(report)
    assert report["meta"]["narrativeStatus"] == "deterministic_fallback"
    assert audit["issues"] == [{"code": "NARRATIVE_FALLBACK", "errorType": "ValueError"}]
    assert audit["partial"] is True
    assert SECRET not in json.dumps([report, audit], ensure_ascii=False)


@pytest.mark.asyncio
async def test_core_assessment_error_is_failed_and_retries_same_frozen_failure(monkeypatch):
    monkeypatch.setattr(pipeline_module, "assess_case", Mock(side_effect=RuntimeError(SECRET)))
    pipeline = GutBarrierPipeline()
    pipeline.generate = AsyncMock(wraps=pipeline.generate)
    transport = SimpleNamespace(post_once=AsyncMock(return_value=CallbackResponse(503)))
    env = environment(pipeline=pipeline, callback_url="https://offline.invalid/callback", transport=transport)
    job = await env.service.submit(request())
    await env.service.drain_generation()
    frozen = deepcopy(env.collection.documents[job["_id"]]["body"])
    assert frozen["status"] == "failed" and frozen["report"] is None
    assert frozen["error"]["code"] == "GENERATION_FAILED"
    assert SECRET not in json.dumps(frozen, ensure_ascii=False)
    await env.service.drain_delivery()
    env.clock.advance(31)
    transport.post_once.return_value = CallbackResponse(204)
    await env.service.drain_delivery()
    assert pipeline.generate.await_count == 1
    assert env.collection.documents[job["_id"]]["deliveryStatus"] == "delivered"
    first, second = transport.post_once.await_args_list
    assert first.args[1] == second.args[1] == frozen
    assert first.kwargs["idempotency_key"] == second.kwargs["idempotency_key"]


@pytest.mark.asyncio
async def test_overall_timeout_during_collection_is_failed_not_source_partial():
    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()

    sources = SimpleNamespace(collect=AsyncMock(side_effect=blocked))
    env = environment(pipeline=GutBarrierPipeline(sources=sources))
    env.service.timeout_seconds = 0.01
    job = await env.service.submit(request())
    await env.service.drain_generation()
    document = env.collection.documents[job["_id"]]
    assert document["body"]["status"] == "failed"
    assert document["body"]["report"] is None
    assert document["body"]["error"]["code"] == "GENERATION_TIMEOUT"
    assert "SOURCE_COLLECTION_UNAVAILABLE" not in json.dumps(document["body"])


@pytest.mark.asyncio
async def test_save_failure_never_delivers_unpersisted_report_then_recovers_expired_lease():
    pipeline = GutBarrierPipeline()
    pipeline.generate = AsyncMock(wraps=pipeline.generate)
    transport = SimpleNamespace(post_once=AsyncMock(return_value=CallbackResponse(204)))
    env = environment(pipeline=pipeline, callback_url="https://offline.invalid/callback", transport=transport)
    original_complete = env.store.complete
    env.store.complete = AsyncMock(side_effect=ConnectionError(SECRET))
    job = await env.service.submit(request())
    with pytest.raises(ConnectionError):
        await env.service.drain_generation()
    await env.service.drain_delivery()
    transport.post_once.assert_not_called()
    assert "body" not in env.collection.documents[job["_id"]]
    env.store.complete = original_complete
    env.clock.advance(121)
    assert await env.service.recover_pending() == 1
    await env.service.drain_delivery()
    document = env.collection.documents[job["_id"]]
    assert document["body"]["error"]["code"] == "GENERATION_INTERRUPTED"
    assert document["deliveryStatus"] == "delivered"
    assert pipeline.generate.await_count == 1
