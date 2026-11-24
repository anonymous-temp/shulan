import asyncio
from types import SimpleNamespace

import httpx
import pytest

from adapters.report.callback import CallbackClient, CallbackDeliveryError
from modules.report.fallback_policy import ReportFallbackPolicy
from modules.report.parsing import parse_request_source
from modules.report.pipeline import ReportPipeline, _empty_partial_payload
from modules.report.schemas import ReportRequest
from modules.report.task_coordinator import (
    ReportExecutionResult,
    ReportTaskCoordinator,
    report_request_fingerprint,
)


@pytest.mark.asyncio
async def test_all_optional_sources_failed_returns_pending_report():
    class FailedSourceClient:
        async def fetch_file(self, source):
            raise RuntimeError("download failed")

        async def fetch_test_data(self, source):
            raise RuntimeError("download failed")

    request = ReportRequest.model_validate(
        {
            "userId": "u",
            "sessionId": "s",
            "name": "患者",
            "fileList": [
                {"url": "https://example.com/report.pdf", "fileType": "pdf"}
            ],
        }
    )
    parsed = await parse_request_source(request, source_client=FailedSourceClient())
    assert parsed.source_type == "pending"
    assert parsed.subject.name == "患者"
    assert parsed.taxa == []
    assert parsed.disease_results == []
    assert parsed.source_errors == ["fileList[0](pdf): RuntimeError"]


@pytest.mark.asyncio
async def test_evaluate_delivery_failure_does_not_block_followups():
    request = ReportRequest.model_validate({"userId": "u", "sessionId": "s"})

    class Callback:
        def build_body(self, payload):
            return {
                "userId": "u",
                "sessionId": "s",
                "status": "partial_success",
            }

        async def deliver(self, body):
            raise RuntimeError("receiver unavailable")

    followup_calls = []

    async def followup(data, user_id, session_id):
        followup_calls.append((data, user_id, session_id))
        return SimpleNamespace(payloads=({"type": "opinion"},))

    async def no_session(user_id, session_id):
        return {}

    pipeline = ReportPipeline(
        source_client=object(),
        evidence_client=object(),
        callback_client=Callback(),
        session_loader=no_session,
        narrative_client=None,
        followup_report_handler=followup,
    )
    pipeline.generate = lambda request: asyncio.sleep(
        0,
        result=_empty_partial_payload(request, reasons=["TEST_DATA_UNAVAILABLE"]),
    )

    result = await pipeline.execute_and_callback(request)
    assert len(followup_calls) == 1
    assert followup_calls[0][0]["status"] == "partial_success"
    assert result.followup_payloads == ({"type": "opinion"},)


@pytest.mark.asyncio
async def test_callback_does_not_retry_permanent_401():
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"message": "unauthorized"})

    client = CallbackClient(
        url="https://callback.example/report",
        transport=httpx.MockTransport(handler),
        policy=ReportFallbackPolicy(retry_backoff_seconds=()),
    )
    try:
        with pytest.raises(CallbackDeliveryError):
            await client.deliver({"status": "partial_success"})
        assert calls == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_identical_report_tasks_share_generation_and_replay_completed_result():
    policy = ReportFallbackPolicy(
        retry_backoff_seconds=(), completed_result_ttl_seconds=30
    )
    coordinator = ReportTaskCoordinator(policy=policy)
    request = ReportRequest.model_validate({"userId": "u", "sessionId": "s"})
    fingerprint = report_request_fingerprint(request)
    runs = 0
    replays = 0

    async def runner():
        nonlocal runs
        runs += 1
        await asyncio.sleep(0.01)
        return ReportExecutionResult(evaluate_payload={"status": "success"})

    async def replay(result):
        nonlocal replays
        replays += 1

    concurrent_results = await asyncio.gather(
        *(
            coordinator.execute(fingerprint, runner=runner, replay=replay)
            for _ in range(20)
        )
    )
    third = await coordinator.execute(fingerprint, runner=runner, replay=replay)
    assert all(result == third for result in concurrent_results)
    assert runs == 1
    assert replays == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [2, None])
async def test_distinct_evaluate_generations_wait_for_process_local_admission_slots(limit):
    async def load_session(_user_id, _session_id):
        return None

    pipeline = ReportPipeline(
        source_client=SimpleNamespace(),
        evidence_client=SimpleNamespace(),
        callback_client=SimpleNamespace(),
        session_loader=load_session,
        narrative_client=None,
        **({} if limit is None else {"max_concurrent_evaluate_reports": limit}),
    )
    active = 0
    maximum_active = 0
    lock = asyncio.Lock()
    request = ReportRequest.model_validate({"userId": "u", "sessionId": "s"})

    async def generate(_request):
        nonlocal active, maximum_active
        async with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        async with lock:
            active -= 1
        return SimpleNamespace(status="success")

    pipeline.generate = generate

    await asyncio.gather(
        *(
            pipeline._generate_evaluate_payload(request)
            for _index in range(25)
        )
    )

    assert maximum_active == (20 if limit is None else limit)


@pytest.mark.asyncio
async def test_followup_integration_does_not_hold_evaluate_admission_slot():
    async def load_session(_user_id, _session_id):
        return None

    class Callback:
        def build_body(self, _payload):
            return {
                "status": "success",
                "diagnostic_basis": {"abnormal_findings": []},
            }

        async def deliver(self, _body):
            return None

    followup_started = asyncio.Event()
    release_followup = asyncio.Event()

    async def followup(_data, _user_id, _session_id):
        followup_started.set()
        await release_followup.wait()
        return SimpleNamespace(payloads=(), module_statuses={})

    pipeline = ReportPipeline(
        source_client=SimpleNamespace(),
        evidence_client=SimpleNamespace(),
        callback_client=Callback(),
        session_loader=load_session,
        narrative_client=None,
        followup_report_handler=followup,
        max_concurrent_evaluate_reports=1,
    )
    generated = 0

    async def generate(_request):
        nonlocal generated
        generated += 1
        await asyncio.sleep(0.01)
        return SimpleNamespace(status="success")

    pipeline.generate = generate
    first_request = ReportRequest.model_validate(
        {"userId": "u1", "sessionId": "s1"}
    )
    second_request = ReportRequest.model_validate(
        {"userId": "u2", "sessionId": "s2"}
    )
    first = asyncio.create_task(pipeline.execute_and_callback(first_request))
    await asyncio.wait_for(followup_started.wait(), timeout=0.2)

    await asyncio.wait_for(
        pipeline._generate_evaluate_payload(second_request),
        timeout=0.2,
    )
    release_followup.set()
    await first

    assert generated == 2


@pytest.mark.asyncio
async def test_completed_report_replay_cache_has_a_fixed_process_local_bound():
    policy = ReportFallbackPolicy(
        retry_backoff_seconds=(),
        completed_result_ttl_seconds=30,
        completed_result_max_entries=2,
    )
    coordinator = ReportTaskCoordinator(policy=policy)

    async def runner():
        return ReportExecutionResult(evaluate_payload={"status": "success"})

    async def replay(result):
        return None

    for index in range(4):
        await coordinator.execute(
            f"unique-{index}",
            runner=runner,
            replay=replay,
        )
    assert len(coordinator._entries) == 2


@pytest.mark.asyncio
async def test_source_failures_are_processed_with_bounded_concurrency():
    class BoundedFailedSourceClient:
        policy = ReportFallbackPolicy(source_parse_concurrency=2)

        def __init__(self):
            self.active = 0
            self.maximum_active = 0
            self.lock = asyncio.Lock()

        async def fetch_test_data(self, source):
            async with self.lock:
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
            await asyncio.sleep(0.01)
            async with self.lock:
                self.active -= 1
            raise RuntimeError("download failed")

        async def fetch_file(self, source):
            raise AssertionError("not used")

    client = BoundedFailedSourceClient()
    request = ReportRequest.model_validate(
        {
            "userId": "u",
            "sessionId": "s",
            "testData": [
                {"resultDetailUrl": f"https://example.com/{index}.json"}
                for index in range(8)
            ],
        }
    )

    parsed = await parse_request_source(request, source_client=client)

    assert parsed.source_type == "pending"
    assert client.maximum_active == 2


@pytest.mark.asyncio
async def test_followup_modules_retry_and_degrade_independently(monkeypatch):
    from modules.report import reports

    calls = {"info": 0, "explain": 0}

    async def failed_info(data):
        calls["info"] += 1
        raise RuntimeError("model unavailable")

    async def failed_explain(data, user_id, session_id):
        calls["explain"] += 1
        raise RuntimeError("model unavailable")

    async def good_opinion(data, user_id, session_id):
        return {
            "type": "opinion",
            "userId": user_id,
            "sessionId": session_id,
            "diagnosis_summary": {"conclusion": "已生成"},
            "diagnostic_basis": {"abnormal_findings": []},
            "diagnostic_advice": {"report_interpretation": ["已生成"]},
        }

    async def catalog(policy):
        return "", [], [], {}, {}, {}

    async def delivered(payloads, *, policy):
        return {str(item["type"]): "success" for item in payloads}

    monkeypatch.setattr(reports, "_info", failed_info)
    monkeypatch.setattr(reports, "explain_report", failed_explain)
    monkeypatch.setattr(reports, "opinion_report", good_opinion)
    monkeypatch.setattr(reports, "fetch_catalog", catalog)
    monkeypatch.setattr(reports, "replay_followup_reports", delivered)

    result = await reports._push_report(
        {
            "subject": {},
            "sample": {},
            "diagnostic_basis": {"abnormal_findings": [{"problem": "血脂异常"}]},
        },
        "u",
        "s",
        policy=ReportFallbackPolicy(retry_backoff_seconds=()),
    )
    by_type = {item["type"]: item for item in result.payloads}
    assert set(by_type) == {"instruction", "opinion", "explain"}
    assert by_type["instruction"]["recommend"]["MDF"] == []
    assert by_type["instruction"]["recommend"]["combination_rationale"] is None
    assert by_type["instruction"]["info"]["unbalance_type"] is None
    assert by_type["opinion"]["diagnosis_summary"] == {"conclusion": "已生成"}
    assert set(by_type["explain"]) >= {"preface", "sections", "closing"}
    assert "diagnostic_summary" not in by_type["explain"]
    assert by_type["explain"]["status"] == "failed"
    assert result.module_statuses["opinion"]["generation"] == "success"
    assert result.module_statuses["explain"]["generation"] == "fallback_empty"
    assert calls == {"info": 2, "explain": 2}


@pytest.mark.asyncio
async def test_empty_evaluate_and_unavailable_models_preserve_three_neutral_reports(monkeypatch):
    from modules.report import reports

    async def must_not_run(*args, **kwargs):
        raise RuntimeError("model unavailable")

    async def delivered(payloads, *, policy):
        return {str(item["type"]): "success" for item in payloads}

    monkeypatch.setattr(reports, "_info", must_not_run)
    monkeypatch.setattr(reports, "opinion_report", must_not_run)
    monkeypatch.setattr(reports, "explain_report", must_not_run)
    monkeypatch.setattr(reports, "fetch_catalog", must_not_run)
    monkeypatch.setattr(reports, "replay_followup_reports", delivered)

    result = await reports._push_report(
        {"subject": {}, "sample": {}, "diagnostic_basis": {"abnormal_findings": []}},
        "u",
        "s",
        policy=ReportFallbackPolicy(retry_backoff_seconds=()),
    )
    assert {payload["type"] for payload in result.payloads} == {"instruction", "opinion", "explain"}
    assert len(result.payloads) == 3
    assert all(
        status["generation"] == "fallback_empty"
        for name, status in result.module_statuses.items() if name != "dependencies"
    )
    assert result.module_statuses["dependencies"] == {
        "info": "fallback_empty", "catalog": "fallback_empty", "recommend": "fallback_empty",
    }
    assert all(payload["status"] == "failed" for payload in result.payloads)
