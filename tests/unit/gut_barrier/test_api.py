import json
import os
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
import tornado.testing
import tornado.web

from adapters.gut_barrier.store import MongoGutBarrierStore
from adapters.gut_barrier.rendering import render_report_html
from adapters.gut_barrier.delivery import GutBarrierCallbackTransport
from bootstrap.container import AppContainer
from modules.gut_barrier.pipeline import GutBarrierPipeline
from modules.gut_barrier.publication import project_callback_report
from modules.gut_barrier.service import GutBarrierService
from modules.report.durable import ReportCallbackWorker
from runtime import ApplicationLifecycle, TaskSupervisor
from scripts.artifact_paths import artifact_output
from tests.unit.report.durable_fakes import Clock, Collection
from web.routes import build_routes
from tests.unit.gut_barrier.final_input_fixture import current_health_input, final_input


class Receiver(tornado.web.RequestHandler):
    async def post(self):
        key = self.request.headers["Idempotency-Key"]
        self.application.settings["received"].append((key, json.loads(self.request.body)))
        self.application.settings["received_bytes"].append(self.request.body)
        self.application.settings["received_headers"].append(dict(self.request.headers))
        self.set_status(204 if self.application.settings["receiver_ready"] else 503)


class TestGutBarrierAPI(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        self.clock, self.collection = Clock(), Collection()
        self.supervisor = TaskSupervisor()
        store = MongoGutBarrierStore(self.collection, clock=self.clock)
        self.transport = GutBarrierCallbackTransport(store=store, transport=httpx.AsyncHTTPTransport(trust_env=False))
        self.pipeline = GutBarrierPipeline()
        self.pipeline.generate = AsyncMock(wraps=self.pipeline.generate)
        self.callback_worker = ReportCallbackWorker(
            store=store, transport=self.transport, clock=self.clock, jitter=lambda: 1)
        self.service = GutBarrierService(
            store=store, pipeline=self.pipeline, transport=self.transport,
            callback_worker=self.callback_worker,
        )
        lifecycle = ApplicationLifecycle()
        lifecycle.mark_ready()
        dependency = SimpleNamespace()
        self.container = AppContainer(
            agent_runtime=dependency, analysis_service_factory=lambda: dependency,
            record_service=dependency, chat_service=dependency, health_profile_service=dependency,
            health_report_source=dependency, record_profile_source=dependency, chat_profile_source=dependency,
            health_profile_memory_provider=dependency, health_profile_ingestion_worker=dependency,
            report_pipeline=dependency, report_task_coordinator=dependency,
            lifecycle=lifecycle, task_supervisor=self.supervisor, gut_barrier_service=self.service,
        )
        return tornado.web.Application([*build_routes(self.container), ("/test-callback", Receiver)],
                                       lifecycle=lifecycle, task_supervisor=self.supervisor,
                                       received=[], received_bytes=[], received_headers=[], receiver_ready=False)

    @tornado.testing.gen_test
    async def test_production_route_generates_only_main_report_and_replays_its_frozen_callback(self):
        from tests.unit.gut_barrier.test_complete_input_contract import (
            assert_public_contract, complete_input, offline_sources,
        )

        # Exercise the production handler, service, durable worker and real
        # local HTTP receiver. Gut-barrier must never enter report.followup.
        self.service.callback_url = self.get_url("/test-callback")
        self._app.settings["receiver_ready"] = True
        payload = complete_input()
        payload.update(userId="main-http", sessionId="main-session")
        self.clock.advance(14 * 24 * 60 * 60)
        self.pipeline.sources, downloader = offline_sources(payload["name"])

        with patch("agents.AgentHub.invoke", new_callable=AsyncMock) as invoke, patch(
            "adapters.report.agent_followup.FollowupModelGatewayFactory.create"
        ) as create_followup:
            response = await self.post("/api/v1/gut-barrier/report", payload)
            assert response.code == 200
            receipt = json.loads(response.body)["data"]
            assert set(receipt) == {"userId", "sessionId", "finalReportId", "status"}
            assert receipt["status"] == "processing"
            await self.supervisor.wait_idle()
            assert len(self._app.settings["received"]) == 1
            key, body = self._app.settings["received"][0]
            original_bytes = self._app.settings["received_bytes"][0]
            assert key == receipt["finalReportId"] + ":gutBarrier:v2"
            assert set(body) == {"type", "schemaVersion", "finalReportId", "userId", "sessionId", "status", "report", "error"}
            assert body["type"] == "gutBarrier" and body["schemaVersion"] == "2.0"
            assert body["finalReportId"] == receipt["finalReportId"]
            assert body["userId"] == payload["userId"] and body["sessionId"] == payload["sessionId"]
            assert body["status"] == "partial" and body["error"] is None
            assert_public_contract(body["report"])
            assert downloader.download.await_count == 4
            assert set(self.collection.documents) == {receipt["finalReportId"]}
            saved = self.collection.documents[receipt["finalReportId"]]
            assert saved["deliveryStatus"] == "delivered" and saved["generationAttempts"] == 1
            assert saved.get("jobType") != "gutBarrierExplain"
            assert self.service.concurrency == 20

            repeated = await self.post("/api/v1/gut-barrier/report", payload)
            assert repeated.code == 200 and json.loads(repeated.body)["data"] == receipt
            await self.supervisor.wait_idle()
            assert await self.service.drain_generation() == 0
            assert await self.service.recover_pending() == 0
            assert await self.service.drain_delivery() == 0
            invoke.assert_not_called()
            create_followup.assert_not_called()

        assert downloader.download.await_count == 4
        self.pipeline.generate.assert_awaited_once()
        assert set(self.collection.documents) == {receipt["finalReportId"]}
        assert self._app.settings["received"] == [(key, body), (key, body)]
        assert self._app.settings["received_bytes"] == [original_bytes, original_bytes]

    @tornado.testing.gen_test
    async def test_failed_main_report_only_delivers_and_replays_its_failure_callback(self):
        self.service.callback_url = self.get_url("/test-callback")
        self._app.settings["receiver_ready"] = True
        self.pipeline.generate.side_effect = RuntimeError("private generation detail")
        payload = self.payload()

        with patch("agents.AgentHub.invoke", new_callable=AsyncMock) as invoke, patch(
            "adapters.report.agent_followup.FollowupModelGatewayFactory.create"
        ) as create_followup:
            response = await self.post("/api/v1/gut-barrier/report", payload)
            assert response.code == 200
            receipt = json.loads(response.body)["data"]
            await self.supervisor.wait_idle()
            assert len(self._app.settings["received"]) == 1
            key, body = self._app.settings["received"][0]
            original_bytes = self._app.settings["received_bytes"][0]
            assert key == receipt["finalReportId"] + ":gutBarrier:v2"
            assert body == {
                "type": "gutBarrier", "schemaVersion": "2.0", "finalReportId": receipt["finalReportId"],
                "userId": payload["userId"], "sessionId": payload["sessionId"], "status": "failed",
                "report": None,
                "error": {"code": "GENERATION_FAILED", "message": "报告生成未完成，请核对资料或联系服务方"},
            }
            assert b"private generation detail" not in original_bytes
            assert set(self.collection.documents) == {receipt["finalReportId"]}
            saved = self.collection.documents[receipt["finalReportId"]]
            assert saved["status"] == "failed" and saved["deliveryStatus"] == "delivered"
            assert saved["generationAttempts"] == 1

            repeated = await self.post("/api/v1/gut-barrier/report", payload)
            assert repeated.code == 200 and json.loads(repeated.body)["data"] == receipt
            await self.supervisor.wait_idle()
            assert await self.service.drain_generation() == 0
            assert await self.service.recover_pending() == 0
            assert await self.service.drain_delivery() == 0
            invoke.assert_not_called()
            create_followup.assert_not_called()

        self.pipeline.generate.assert_awaited_once()
        assert set(self.collection.documents) == {receipt["finalReportId"]}
        assert self._app.settings["received"] == [(key, body), (key, body)]
        assert self._app.settings["received_bytes"] == [original_bytes, original_bytes]

    @tornado.testing.gen_test
    async def test_production_route_delivers_frozen_v14_binding_over_real_http(self):
        from adapters.gut_barrier.render_context import render_context_from_headers
        self.service.callback_url = self.get_url("/test-callback")
        self._app.settings["receiver_ready"] = True
        response = await self.post("/api/v1/gut-barrier/report", {
            "userId": "binding-http", "sessionId": "binding-http", "age": 26,
        })
        assert response.code == 200
        await self.supervisor.wait_idle()
        assert len(self._app.settings["received"]) == 1
        key, body = self._app.settings["received"][0]
        headers = self._app.settings["received_headers"][0]
        binding = render_context_from_headers(body, headers)
        assert binding["templateVersion"] == "gut-barrier-static.v14"
        assert binding == self.collection.documents[body["finalReportId"]]["renderContext"]
        assert key == body["finalReportId"] + ":gutBarrier:v2"
        assert set(body["report"]["meta"]) == {"title", "reportId"}

    @tornado.testing.gen_test
    async def test_complete_input_contract_reaches_frozen_v14_callback_with_all_submitted_sources(self):
        from adapters.gut_barrier.render_context import render_context_from_headers
        from tests.unit.gut_barrier.test_complete_input_contract import (
            assert_public_contract, complete_input, offline_sources,
        )

        self.clock.advance(14 * 24 * 60 * 60)  # The fixture's 2026-09-28 assessment date.
        payload = complete_input()
        source, downloader = offline_sources(payload["name"])
        self.pipeline.sources = source
        self.service.callback_url = self.get_url("/test-callback")
        self._app.settings["receiver_ready"] = True

        response = await self.post("/api/v1/gut-barrier/report", payload)
        assert response.code == 200
        receipt = json.loads(response.body)["data"]
        assert set(receipt) == {"userId", "sessionId", "finalReportId", "status"}
        assert receipt["status"] == "processing"
        await self.supervisor.wait_idle()

        assert self.service.concurrency == 20
        self.pipeline.generate.assert_awaited_once()
        assert downloader.download.await_count == 4
        assert {call.args[0] for call in downloader.download.await_args_list} == {
            item["url"] for item in payload["fileList"]
        } | {payload["testData"][0][key] for key in ("resultDetailUrl", "resultSummaryUrl")}
        source._parse_page.assert_called_once()
        assert len(self._app.settings["received"]) == 1
        key, body = self._app.settings["received"][0]
        frozen_bytes = self._app.settings["received_bytes"][0]
        headers = self._app.settings["received_headers"][0]
        saved = self.collection.documents[receipt["finalReportId"]]
        assert saved["deliveryStatus"] == "delivered" and saved["generationAttempts"] == 1
        assert len(saved["audit"]["sources"]) == 4
        assert all(item["status"] == "parsed" for item in saved["audit"]["sources"])
        assert set(body) == {"type", "schemaVersion", "finalReportId", "userId", "sessionId", "status", "report", "error"}
        assert body["type"] == "gutBarrier" and body["schemaVersion"] == "2.0"
        assert body["status"] == "partial" and body["error"] is None
        assert body["finalReportId"] == receipt["finalReportId"]
        assert body["userId"] == payload["userId"] and body["sessionId"] == payload["sessionId"]
        assert_public_contract(body["report"])
        binding = render_context_from_headers(body, headers)
        assert binding == saved["renderContext"]
        assert binding["templateVersion"] == "gut-barrier-static.v14"
        assert key == body["finalReportId"] + ":gutBarrier:v2"
        assert set(body["report"]["meta"]) == {"title", "reportId"}

        assessment = saved["audit"]["reportDocument"]["assessment"]
        assert assessment["subject"]["name"] == payload["name"]
        assert assessment["subject"]["age"] == 28 and assessment["subject"]["sex"] == "男"
        survey_issues = assessment["context"]["healthSurveyIssues"]
        assert not any("IDENTITY_CONFLICT" in item["code"] for item in survey_issues)
        assert not any("IDENTITY_CONFLICT" in item["code"] for item in saved["audit"]["issues"])
        assert "bmi" not in assessment["context"]
        assert "35.2 kg/m²" in dict(assessment["context"]["healthSurveyRows"])["BMI"]
        assert set(assessment["context"]["symptoms"]) == {"消化不良", "时常腹泻"}
        assert "obesity" not in {item["code"] for item in assessment["diseaseRisks"]}
        assert assessment["barrier"]["score"] == 0
        assert "inflammation" not in assessment
        findings = assessment["context"]["clinicalFindings"]
        assert len(findings) == 11 and all(item["temporalStatus"] == "historical" for item in findings)
        assert {(item["date"], item["value"]) for item in findings if item["code"] == "LDL_C"} == {
            ("2025-10-29", "3.44"), ("2025-12-15", "3.70")}
        assert {(item["date"], item["value"]) for item in findings if item["code"] == "TOTAL_CHOLESTEROL"} == {
            ("2025-10-29", "6.09"), ("2025-12-15", "6.48")}
        assert "html" not in saved and "html" not in body and b"<!DOCTYPE" not in frozen_bytes

        repeated = await self.post("/api/v1/gut-barrier/report", payload)
        assert repeated.code == 200 and json.loads(repeated.body)["data"] == receipt
        await self.supervisor.wait_idle()
        self.pipeline.generate.assert_awaited_once()
        assert downloader.download.await_count == 4 and len(self.collection.documents) == 1
        # Repeated admission intentionally redelivers the frozen callback; it
        # must not parse sources or generate a second clinical document.
        assert self._app.settings["received"] == [(key, body), (key, body)]
        assert self._app.settings["received_bytes"] == [frozen_bytes, frozen_bytes]
        assert render_context_from_headers(body, self._app.settings["received_headers"][1]) == binding
        assert saved["body"] == body and saved["renderContext"] == binding

    def tearDown(self):
        self.io_loop.run_sync(self.transport.aclose)
        super().tearDown()

    @tornado.testing.gen_test
    async def test_canonical_four_marker_payload_reaches_main_callback_and_word_view(self):
        from adapters.gut_barrier.frontend_template import project_display_report
        from adapters.gut_barrier.render_context import render_context_from_headers
        payload = {
            "userId": "legacy-four-http", "sessionId": "legacy-four-http", "name": "合成受检者", "age": "28", "sex": "男",
            "fileList": [], "testData": [], "hospitalData": [], "microbiomeData": None,
            "healthSurvey": {"name": "另一个人", "age": "26", "gender": "女", "heightCm": "160", "weightKg": "90"},
            "gutHealthIndicator": {
                "bacterialEndotoxin": {"value": "15.9", "unit": "U/L"},
                "diamineOxidase": {"value": "10.5", "unit": "U/L"},
                "hsCRP": {"value": "2.66", "unit": "mg/L"},
                "dLacticAcid": {"value": "17.42", "unit": "mg/L"}, "complete": True},
        }
        self.service.callback_url = self.get_url("/test-callback")
        self._app.settings["receiver_ready"] = True
        response = await self.post("/api/v1/gut-barrier/report", payload)
        assert response.code == 200
        await self.supervisor.wait_idle()
        assert len(self._app.settings["received"]) == 1
        _, body = self._app.settings["received"][0]
        binding = render_context_from_headers(body, self._app.settings["received_headers"][0])
        assessment = self.collection.documents[body["finalReportId"]]["audit"]["reportDocument"]["assessment"]
        assert {m["code"]: m["value"] for m in assessment["markers"]} == {
            "D_LACTATE": 17.42, "DAO": 10.5, "LPS": 15.9}
        assert assessment["barrier"]["score"] == 3.0 and assessment["barrier"]["grade"] == "II"
        assert assessment["subject"]["name"] == payload["name"]
        assert "bmi" not in assessment["context"]
        assert "35.2 kg/m²" in dict(assessment["context"]["healthSurveyRows"])["BMI"]
        assert "healthSurvey" in assessment["context"]
        assert not any("IDENTITY_CONFLICT" in issue["code"] for issue in
                       self.collection.documents[body["finalReportId"]]["audit"]["issues"])
        assert body["type"] == "gutBarrier" and body["schemaVersion"] == "2.0"
        assert set(body) == {"type", "schemaVersion", "finalReportId", "userId", "sessionId", "status", "report", "error"}
        view = project_display_report(body["report"], template_version=binding["templateVersion"])
        table = next(b for s in view["sections"] for b in s["blocks"] if b["title"] == "1.2 检测项目与结果总览")
        assert len(table["columns"]) == 6 and table["columns"][-1] == "反映维度"
        assert [r[2] for r in table["rows"]] == ["10.5 U/L", "15.9 U/L", "17.42 mg/L"]

    async def post(self, path, body):
        return await self.http_client.fetch(self.get_url(path), method="POST", body=json.dumps(body),
                                           headers={"Content-Type": "application/json"}, raise_error=False)

    @tornado.testing.gen_test
    async def test_noncanonical_input_is_rejected_before_queuing_or_callback(self):
        cases = []
        payload = current_health_input()
        payload["gutBarrierIndicator"] = payload.pop("gutHealthIndicator")
        cases.append(payload)
        payload = current_health_input()
        payload["gutHealthIndicator"]["dlacticAcid"] = payload["gutHealthIndicator"].pop("dLacticAcid")
        cases.append(payload)
        payload = current_health_input()
        payload["gutHealthIndicator"]["dLacticAcid"] = {"Value": "1.02", "Unit": "mg/L"}
        cases.append(payload)
        payload = current_health_input()
        payload["fileList"] = [{"url": "https://example.com/report.pdf", "file_type": "pdf"}]
        cases.append(payload)
        for payload in cases:
            assert (await self.post("/api/v1/gut-barrier/report", payload)).code == 400
        assert not self.collection.documents and not self._app.settings["received"]
        self.pipeline.generate.assert_not_called()

    @tornado.testing.gen_test
    async def test_current_health_input_keeps_four_values_and_all_survey_answers_through_callback(self):
        from adapters.gut_barrier.frontend_template import project_display_report
        from adapters.gut_barrier.render_context import render_context_from_headers
        from tests.unit.gut_barrier.test_complete_input_contract import assert_public_contract

        self.clock.advance(17 * 24 * 60 * 60)  # Assess after the submitted questionnaire date.
        payload = current_health_input()
        self.service.callback_url = self.get_url("/test-callback")
        self._app.settings["receiver_ready"] = True
        response = await self.post("/api/v1/gut-barrier/report", payload)
        assert response.code == 200
        receipt = json.loads(response.body)["data"]
        assert receipt["status"] == "processing"
        await self.supervisor.wait_idle()
        assert len(self._app.settings["received"]) == 1
        key, body = self._app.settings["received"][0]
        assert key == receipt["finalReportId"] + ":gutBarrier:v2"
        assert body["type"] == "gutBarrier" and body["schemaVersion"] == "2.0"
        assert body["status"] == "partial" and body["error"] is None
        assert body["userId"] == payload["userId"] and body["sessionId"] == payload["sessionId"]
        assert set(body) == {"type", "schemaVersion", "finalReportId", "userId", "sessionId", "status", "report", "error"}
        assert_public_contract(body["report"])
        saved = self.collection.documents[body["finalReportId"]]
        assessment = saved["audit"]["reportDocument"]["assessment"]
        assert saved["deliveryStatus"] == "delivered" and saved["generationAttempts"] == 1
        assert assessment["subject"]["age"] == 42 and assessment["subject"]["sex"] == "男"
        assert {m["code"]: m["value"] for m in assessment["markers"]} == {
            "D_LACTATE": 12.77, "DAO": 24.9, "LPS": 11.9}
        assert assessment["barrier"]["score"] == 5.0 and assessment["barrier"]["grade"] == "III"
        assert assessment["barrier"]["pattern"] == "-+-"
        assert assessment["subject"]["name"] == payload["name"]
        assert "bmi" not in assessment["context"]
        assert "35.2 kg/m²" in dict(assessment["context"]["healthSurveyRows"])["BMI"]
        assert set(assessment["context"]["symptoms"]) == {"消化不良", "时常腹泻"}
        assert "healthSurvey" in assessment["context"]
        assert "obesity" not in {r["code"] for r in assessment["diseaseRisks"]}
        assert not any("IDENTITY_CONFLICT" in item["code"] for item in saved["audit"]["issues"])
        assert saved["audit"]["sources"] == []
        restored = self.pipeline.generate.call_args.args[0]
        assert restored.gut_health_indicator.complete is True
        binding = render_context_from_headers(body, self._app.settings["received_headers"][0])
        view = project_display_report(body["report"], template_version=binding["templateVersion"])
        table = next(b for s in view["sections"] for b in s["blocks"] if b["title"] == "1.2 检测项目与结果总览")
        assert [r[2] for r in table["rows"]] == ["24.9 U/L", "11.9 U/L", "12.77 mg/L"]
        wire_table = next(b for s in body["report"]["sections"] for b in s["blocks"]
                          if b["title"] == "1.2 检测项目与结果总览")
        assert table == wire_table
        assert wire_table["columns"] == ["检测项目", "检测指标", "检测结果", "参考区间", "结果评价", "反映维度"]
        assert project_callback_report(body["report"]) == body["report"]
        html = render_report_html(body["report"], template_version=binding["templateVersion"])
        embedded = re.search(r'id="report-data">(.*?)</script>', html, re.S)
        assert json.loads(embedded.group(1)) == body["report"]
        assert "第四章 附录" in html and "4.4 参考文献" in html
        appendix = next(s for s in body["report"]["sections"] if s["id"] == "appendix")
        qc = next(b for b in appendix["blocks"] if b["title"] == "4.1 质控校验")
        assert qc == next(b for s in view["sections"] for b in s["blocks"] if b["title"] == "4.1 质控校验")
        assert qc["rows"] == [
            ["姓名", payload["name"], "性别", "男"],
            ["年龄", "42 岁", "样本类型", "血液"],
            ["送检项目", "肠道屏障三项", "样本状态", "合格"],
            ["样本制备质控", "合格", "检测数据质控", "合格"],
            ["检验医师", None, "审核医师", None],
        ]
        facts = next(b for s in body["report"]["sections"] for b in s["blocks"] if b["title"] == "受检者信息")
        assert next(item["value"] for item in facts["items"] if item["label"] == "样本类型") == "血液"
        assert html.count('id="appendix-4-1"') == 1
        cards = next(b for s in body["report"]["sections"] for b in s["blocks"]
                     if b["title"] == "肠道屏障功能评分（IBFC）")
        share = next(item for item in cards["items"] if item["label"] == "评分占满分比例")
        assert share == {"label": "评分占满分比例", "value": "33.33%", "detail": "IBFC 满分为 15 分"}
        assert "评分占满分比例" in html and "33.33%" in html
        assert "第四章 肠道屏障与慢性低度炎症概述" not in html and "第五章" not in html
        for row in wire_table["rows"]:
            assert row[2] in html and row[5] in html
        # The separate interpretation consumer must read the same new callback,
        # not silently lose its renamed table or anonymous clinical paragraphs.
        from modules.report.gut_barrier_context import build_gut_barrier_contexts
        from modules.report.gut_barrier_defaults import default_section
        original_body = json.dumps(body, ensure_ascii=False)
        contexts = build_gut_barrier_contexts(body)
        part1 = contexts["part1"]
        assert part1["indicator_table"]["rows"] == [dict(zip(wire_table["columns"], row))
                                                   for row in wire_table["rows"]]
        assert part1["ibfc"]["score"] == "5"
        assert part1["normal_count"] == 2
        assert {item["name"] for item in part1["abnormal_indicators"]} == {
            "二胺氧化酶（DAO）"}
        assert "inflammation" not in part1
        overview = next(s for s in body["report"]["sections"] if s["id"] == "overview")
        summary = next(b for b in overview["blocks"] if b["title"] == "1.1 综合评估指数（IBFC · 肠屏障功能评分）")
        anonymous = [b["text"] for b in overview["blocks"] if b["kind"] == "paragraph" and b["title"] is None]
        assert part1["summary_table"]["rows"][0]["综合结论"] == summary["text"]
        assert [part1["ibfc"]["paragraph"], part1["damage_paragraph"]] == anonymous
        section = default_section(1, contexts)
        assert [item["result"] for item in section["indicator_talk"]["items"]] == [row[2] for row in wire_table["rows"]]
        assert [item["reference"] for item in section["indicator_talk"]["items"]] == [row[3] for row in wire_table["rows"]]
        assert all(item["anchor"] in {"1.1", "1.2"} for item in section["key_points"])
        assert json.dumps(body, ensure_ascii=False) == original_body
        risk_tables = [b for s in body["report"]["sections"] if s["id"] == "disease_risks"
                       for b in s["blocks"] if b["kind"] == "table" and b["columns"][0] == "疾病"]
        from tests.unit.gut_barrier.template_risk_assertions import assert_template_risk_contract
        assert_template_risk_contract(body["report"], assessment)
        assert len(risk_tables) == 8 and len(assessment["diseaseRisks"]) == 1
        assert all(1 <= row[1] <= 10 and row[2] in {"低", "中", "高"}
                   for block in risk_tables for row in block["rows"] if row[1] is not None)
        assert sum(row[1] is not None for block in risk_tables for row in block["rows"]) == 23
        assert {"intestinal_barrier_followup"} == {
            row["code"] for row in assessment["diseaseRisks"]}
        assert all("本次依据：" not in row[3] and "随访重点：" not in row[3] and row[3].count("。") == 1
                   for block in risk_tables for row in block["rows"])
        assert html.count('class="risk-plot-row"') == 0
        assert 'class="risk-fill"' not in html
        assert any(b["title"] == "2.9 本章小结" for s in body["report"]["sections"] for b in s["blocks"])
        assert "redacted-test-ip" not in json.dumps(body, ensure_ascii=False)
        self.pipeline.generate.assert_awaited_once()

    @tornado.testing.gen_test
    async def test_latest_survey_alias_reaches_v14_callback_and_replays_under_previous_key(self):
        from adapters.gut_barrier.render_context import render_context_from_headers
        from tests.unit.gut_barrier.test_complete_input_contract import assert_public_contract
        from tests.unit.gut_barrier.test_final_input import latest_survey

        self.clock.advance(24 * 24 * 60 * 60)  # Assess on the questionnaire's 2026-10-08 date.
        survey = latest_survey()
        payload = final_input()
        payload.update(userId="latest-survey-http", sessionId="latest-survey-http",
                       name=survey["name"], fileList=[])
        payload.pop("healthSurvey")
        payload["healthSurvery"] = survey
        self.service.callback_url = self.get_url("/test-callback")
        self._app.settings["receiver_ready"] = True

        response = await self.post("/api/v1/gut-barrier/report", payload)
        assert response.code == 200
        receipt = json.loads(response.body)["data"]
        assert set(receipt) == {"userId", "sessionId", "finalReportId", "status"}
        assert receipt["status"] == "processing"
        await self.supervisor.wait_idle()
        assert len(self._app.settings["received"]) == 1
        key, body = self._app.settings["received"][0]
        frozen_bytes = self._app.settings["received_bytes"][0]
        saved = self.collection.documents[receipt["finalReportId"]]
        assert saved["deliveryStatus"] == "delivered" and saved["generationAttempts"] == 1
        assert saved["request"]["healthSurvey"] == survey
        assert "healthSurvery" not in saved["request"]
        assert self.pipeline.generate.call_args.args[0].health_survey == survey

        assessment = saved["audit"]["reportDocument"]["assessment"]
        context = assessment["context"]
        assert assessment["subject"]["name"] == survey["name"]
        assert assessment["subject"]["age"] == 26 and assessment["subject"]["sex"] == "女"
        assert not {"heightCm", "weightKg", "bmi"}.intersection(context)
        assert context["symptoms"] == [] and context["medicalHistory"] == []
        assert context["conditions"] is None and context["familyHistory"] is None
        assert context["medications"] is None and context["medicationHistoryComplete"] is False
        assert context["allergies"] == [] and context["allergyHistoryComplete"] is True
        survey_context = context["healthSurvey"]
        assert survey_context["submittedAt"] == survey["submitTime"]
        assert survey_context["temporalStatus"] == "date_provided"
        answers = survey_context["answers"]
        assert answers["digestion"]["status"] == answers["bowelMovement"]["status"] == "explicit_none"
        assert answers["fourHigh"]["raw"] == "其他 / 无" and answers["fourHigh"]["status"] == "unknown"
        assert all(answers[field]["status"] == "unknown" and answers[field]["raw"] is None
                   for field in ("lifestyle", "skin", "sleep", "familyDisease", "currentMedication"))
        assert len(context["improvementGoals"]) == 6
        assert "obesity" not in {item["code"] for item in assessment["diseaseRisks"]}
        assert not any(item["code"] == "HEALTH_SURVEY_FUTURE_DATE" for item in saved["audit"]["issues"])

        assert set(body) == {"type", "schemaVersion", "finalReportId", "userId", "sessionId", "status", "report", "error"}
        assert body["type"] == "gutBarrier" and body["schemaVersion"] == "2.0"
        assert body["status"] == "partial" and body["error"] is None
        assert body["finalReportId"] == receipt["finalReportId"]
        assert body["userId"] == payload["userId"] and body["sessionId"] == payload["sessionId"]
        assert key == receipt["finalReportId"] + ":gutBarrier:v2"
        assert_public_contract(body["report"])
        assert survey["fromIp"] not in frozen_bytes.decode()
        binding = render_context_from_headers(body, self._app.settings["received_headers"][0])
        assert binding["templateVersion"] == "gut-barrier-static.v14"
        assert binding == saved["renderContext"]

        payload["healthSurvey"] = payload.pop("healthSurvery")
        repeated = await self.post("/api/v1/gut-barrier/report", payload)
        assert repeated.code == 200 and json.loads(repeated.body)["data"] == receipt
        await self.supervisor.wait_idle()
        self.pipeline.generate.assert_awaited_once()
        assert set(self.collection.documents) == {receipt["finalReportId"]}
        assert self._app.settings["received"] == [(key, body), (key, body)]
        assert self._app.settings["received_bytes"] == [frozen_bytes, frozen_bytes]
        assert render_context_from_headers(body, self._app.settings["received_headers"][1]) == binding

    @tornado.testing.gen_test
    async def test_final_input_survey_and_failed_attachment_reach_real_http_callback(self):
        from adapters.gut_barrier.sources import GutBarrierSources

        self.clock.advance(10 * 24 * 60 * 60)
        payload = final_input()
        # Exercise the full final input without contacting the supplied sites.
        # One unavailable attachment must not erase the other one or direct data.
        downloader = SimpleNamespace(download=AsyncMock(side_effect=[ConnectionError("private transport detail"), b"%PDF-fixture"]))
        source = GutBarrierSources(downloader=downloader)
        source._pdf_pages = Mock(return_value=["姓名：测试小陈\n采样日期：2026-09-24\nhs-CRP >3.0 mg/L\nDAO 3.8 U/L"])
        self.pipeline.sources = source
        self.service.callback_url = self.get_url("/test-callback")
        self._app.settings["receiver_ready"] = True
        response = await self.post("/api/v1/gut-barrier/report", payload)
        assert response.code == 200
        report_id = json.loads(response.body)["data"]["finalReportId"]
        await self.supervisor.wait_idle()
        assert self.service.concurrency == 20
        assert downloader.download.await_count == 2
        assert len(self._app.settings["received"]) == 1
        body = self._app.settings["received"][0][1]
        saved = self.collection.documents[report_id]
        assert saved["deliveryStatus"] == "delivered" and body["status"] == "partial"
        assert body["type"] == "gutBarrier" and body["report"]["schemaVersion"] == "gut-barrier-public.v2"
        assert any(issue["code"] == "SOURCE_UNAVAILABLE" for issue in saved["audit"]["issues"])
        assessment = saved["audit"]["reportDocument"]["assessment"]
        assert "bmi" not in assessment["context"]
        assert "35.2 kg/m²" in dict(assessment["context"]["healthSurveyRows"])["BMI"]
        assert assessment["context"]["allergies"] is None and assessment["context"]["medications"] is None
        assert "inflammation" not in assessment
        assert assessment["barrier"]["score"] == 0
        text = json.dumps(body, ensure_ascii=False)
        assert "健康问卷背景（自述）" not in text and "问卷相关管理关注" not in text
        assert "时常腹泻" in str(assessment["context"]["healthSurveyRows"])
        assert "35.2" in str(assessment["context"]["healthSurveyRows"])
        assert "redacted-test-id-card" not in text and "redacted-test-ip" not in text and "private transport detail" not in text
        assert [s["id"] for s in body["report"]["sections"]] == ["overview", "disease_risks", "appendix"]
        output = os.environ.get("GUT_BARRIER_FINAL_INPUT_ACCEPTANCE_OUTPUT")
        if output:
            directory = artifact_output(output, prefix="gut-barrier-final-input")
            directory.mkdir(parents=True, exist_ok=True)
            validation = {"route": "/api/v1/gut-barrier/report", "httpCallback": "localhost",
                          "storage": "in-memory Mongo collection fake", "sourceAdapter": "real adapter with controlled download/page fixtures",
                          "sourceURLsFetched": False, "remoteCallbackSent": False,
                          "generationConcurrency": self.service.concurrency, "status": body["status"],
                          "deliveryStatus": saved["deliveryStatus"], "generationAttempts": saved["generationAttempts"],
                          "deliveryAttempts": saved["deliveryAttempts"], "fixtureIdentifiers": "synthetic user/session; redacted idCard/IP"}
            for filename, value in (("request.json", payload), ("callback.json", body),
                                    ("audit.json", {"validation": validation, "generation": saved["audit"]})):
                (directory / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    @tornado.testing.gen_test
    async def test_final_input_invalid_json_and_survey_shape_return_400_without_job(self):
        response = await self.http_client.fetch(self.get_url("/api/v1/gut-barrier/report"), method="POST",
            body='{"userId":"u","sessionId":"s","sex":""女"}',
            headers={"Content-Type": "application/json"}, raise_error=False)
        assert response.code == 400
        for field in ("healthSurvey", "microbiomeData"):
            payload = final_input()
            payload[field] = "not-an-object"
            assert (await self.post("/api/v1/gut-barrier/report", payload)).code == 400
        payload = final_input()
        payload["fileList"][0]["url"] = "***" + payload["fileList"][0]["url"] + "***"
        assert (await self.post("/api/v1/gut-barrier/report", payload)).code == 400
        assert not self.collection.documents and not self._app.settings["received"]
        self.pipeline.generate.assert_not_called()

    @staticmethod
    def payload():
        return {"userId": "用户唯一ID", "sessionId": "346291b2-2166-430f-8352-ec7d9c11e64d",
                "name": "患者姓名", "age": "28", "sex": "男", "fileList": [], "testData": [],
                "gutHealthIndicator": {"bacterialEndotoxin": {"value": "12.5", "unit": "U/L"},
                    "diamineOxidase": {"value": "3.8", "unit": "U/L"},
                    "dLacticAcid": {"value": "0.06", "unit": "mg/L"}},
                "hospitalData": [{"checkTime": "2025-12-15 09:31:08", "dataType": "INSPECTION",
                    "itemName": "低密度脂蛋白(LDL-c)", "referenceRange": "0.00-3.37",
                    "reportTime": "2025-12-15 09:31:08", "resultStatus": 1,
                    "resultStatusName": "偏高", "testResultValue": "3.70"}]}

    @tornado.testing.gen_test
    async def test_inline_thousand_species_is_accepted_by_formal_http_endpoint(self):
        payload = self.payload()
        payload["microbiomeData"] = {"microbeData": [
            {"latinName": f"Synthetic_{index}", "value": index / 1000} for index in range(1000)]}
        response = await self.post("/api/v1/gut-barrier/report", payload)
        assert response.code == 200
        await self.supervisor.wait_idle()
        job = next(iter(self.collection.documents.values()))
        assert len(job["request"]["microbiomeData"]["microbeData"]) == 1000
        payload["sessionId"] = "oversized"
        payload["microbiomeData"] = {"microbeData": [None] * 30000}
        assert (await self.post("/api/v1/gut-barrier/report", payload)).code == 400
        assert len(self.collection.documents) == 1

    @tornado.testing.gen_test
    async def test_submission_only_acknowledges_and_reuses_durable_json_without_html(self):
        response = await self.post("/api/v1/gut-barrier/report", self.payload())
        assert response.code == 200
        receipt = json.loads(response.body)["data"]
        assert set(receipt) == {"userId", "sessionId", "finalReportId", "status"}
        assert receipt["status"] == "processing" and receipt["finalReportId"].startswith("gbr_")
        await self.supervisor.wait_idle()
        document = self.collection.documents[receipt["finalReportId"]]
        report = document["body"]["report"]
        internal = document["audit"]["reportDocument"]
        assert report == project_callback_report(internal)
        assert set(report) == {"schemaVersion", "meta", "sections"}
        assert document["body"]["schemaVersion"] == "2.0"
        assert document["body"]["type"] == "gutBarrier"
        assert internal["assessment"]["barrier"]["score"] == 0
        assert internal["assessment"]["barrier"]["pattern"] == "---"
        assert "inflammation" not in internal["assessment"]
        assert "html" not in document and "html" not in document["body"]
        assert document["status"] == "partial"
        duplicate = await self.post("/api/v1/gut-barrier/report", self.payload())
        assert json.loads(duplicate.body)["data"] == receipt
        await self.supervisor.wait_idle()
        assert self.pipeline.generate.await_count == 1

    @tornado.testing.gen_test
    async def test_get_and_list_endpoints_are_not_registered(self):
        route_names = {route.name for route in build_routes(self.container)}
        assert {name for name in route_names if name and name.startswith("gut-barrier.")} == {
            "gut-barrier.submit"}
        for path in ("/api/v1/gut-barrier/report/get", "/api/v1/gut-barrier/report/list"):
            assert (await self.post(path, {"userId": "用户唯一ID"})).code == 404

    @tornado.testing.gen_test
    async def test_real_http_callback_retries_identical_body_without_generating_new_report(self):
        self.service.callback_url = self.get_url("/test-callback")
        response = await self.post("/api/v1/gut-barrier/report", self.payload())
        report_id = json.loads(response.body)["data"]["finalReportId"]
        await self.supervisor.wait_idle()
        assert len(self._app.settings["received"]) == 1
        first = self.collection.documents[report_id]
        assert first["deliveryStatus"] == "retry_wait" and first["deliveryAttempts"] == 1
        assert await self.service.drain_delivery() == 0
        self.clock.advance(30)
        self._app.settings["receiver_ready"] = True
        await self.service.drain_delivery()
        await self.service.drain_delivery()
        received = self._app.settings["received"]
        assert len(received) == 2 and received[0] == received[1]
        assert received[0][0] == report_id + ":gutBarrier:v2"
        row = self.collection.documents[report_id]
        assert row["generationAttempts"] == 1 and row["deliveryStatus"] == "delivered"
        assert row["body"] == received[0][1]
        assert received[0][1]["type"] == "gutBarrier" and "data" not in received[0][1]
        assert received[0][1]["schemaVersion"] == "2.0"
        assert set(received[0][1]["report"]) == {"schemaVersion", "meta", "sections"}
        assert not {"audit", "request", "reportDocument"}.intersection(received[0][1])
        assert "html" not in received[0][1] and "html" not in row
        assert self.pipeline.generate.await_count == 1

    @tornado.testing.gen_test
    async def test_report_content_contract_survives_real_http_callback(self):
        """Exercise the formal route and a real local HTTP callback, not a transport mock."""
        self.clock.advance(10 * 24 * 60 * 60)  # Freeze this acceptance artifact on 2026-09-24.
        payload = {
            "userId": "acceptance-user", "sessionId": "five-chapter-20260924",
            "name": "模拟受检者（动态回调验收）", "age": "50", "sex": "男",
            "gutHealthIndicator": {
                "dLacticAcid": {"value": "26.0", "unit": "mg/L"},
                "diamineOxidase": {"value": "17.00", "unit": "U/L"},
                "bacterialEndotoxin": {"value": "23.00", "unit": "U/L"},
                "hsCRP": {"value": "2.7", "unit": "mg/L"},
            },
            "fileList": [], "testData": [], "hospitalData": [],
        }
        self.service.callback_url = self.get_url("/test-callback")
        response = await self.post("/api/v1/gut-barrier/report", payload)
        assert response.code == 200
        receipt = json.loads(response.body)["data"]
        assert receipt["status"] == "processing"
        report_id = receipt["finalReportId"]
        await self.supervisor.wait_idle()
        assert len(self._app.settings["received"]) == 1
        assert self.collection.documents[report_id]["deliveryStatus"] == "retry_wait"
        assert await self.service.drain_delivery() == 0
        self.clock.advance(30)
        self._app.settings["receiver_ready"] = True
        assert await self.service.drain_delivery() == 1
        assert await self.service.drain_delivery() == 0
        received = self._app.settings["received"]
        assert len(received) == 2 and received[0] == received[1]
        assert self._app.settings["received_bytes"][0] == self._app.settings["received_bytes"][1]
        assert received[0][0] == report_id + ":gutBarrier:v2"
        body = received[-1][1]
        stored = self.collection.documents[report_id]
        assert stored["body"] == body
        assert stored["deliveryStatus"] == "delivered" and stored["deliveryAttempts"] == 2
        assert stored["generationAttempts"] == 1
        assert self.pipeline.generate.await_count == 1
        assert set(body) == {"type", "schemaVersion", "finalReportId", "userId", "sessionId", "status", "report", "error"}
        assert body["type"] == "gutBarrier" and body["schemaVersion"] == "2.0"
        assert body["status"] == "partial" and body["error"] is None
        public = body["report"]
        assert public["schemaVersion"] == "gut-barrier-public.v2"
        assert set(public) == {"schemaVersion", "meta", "sections"}
        assert set(public["meta"]) == {"title", "reportId"}
        assert public == project_callback_report(stored["audit"]["reportDocument"])
        assert [s["id"] for s in public["sections"]] == [
            "overview", "disease_risks", "appendix"]
        block_keys = {
            "paragraph": {"kind", "title", "text"}, "table": {"kind", "title", "columns", "rows"},
            "facts": {"kind", "title", "items"}, "cards": {"kind", "title", "items"},
            "bands": {"kind", "title", "items"}, "list": {"kind", "title", "items"},
            "callout": {"kind", "title", "text", "tone"}, "references": {"kind", "title", "items"},
        }
        item_keys = {
            "facts": {"label", "value"}, "cards": {"label", "value", "detail"},
            "bands": {"range", "label", "selected"},
            "references": {"citation", "url", "year", "publisher", "doi", "location"},
        }
        for section in public["sections"]:
            assert set(section) == {"id", "title", "subtitle", "blocks", "visible"}
            assert section["visible"] is True
            for block in section["blocks"]:
                assert set(block) == block_keys[block["kind"]]
                if block["kind"] in item_keys:
                    assert all(set(item) == item_keys[block["kind"]] for item in block["items"])

        def block(section_id, title):
            return next(item for section in public["sections"] if section["id"] == section_id
                        for item in section["blocks"] if item.get("title") == title)

        markers = block("overview", "1.2 检测项目与结果总览")
        assert [row[2] for row in markers["rows"]] == ["17.00 U/L", "23.00 U/L", "26.0 mg/L"]
        assert [row[-1] for row in markers["rows"]] == ["肠上皮细胞损伤", "内毒素入血、炎症级联激活",
                                                       "紧密连接损伤、肠通透性增加"]
        cards = block("overview", "肠道屏障功能评分（IBFC）")["items"]
        assert [item["value"] for item in cards] == [9.5, None, None, "63.33%"]
        assert cards[1] == {"label": "损伤等级", "value": None, "detail": None}
        assert stored["audit"]["reportDocument"]["assessment"]["barrier"]["grade"] == "V"
        assert not any(item["label"] == "受损类型" for item in cards)
        assert stored["audit"]["reportDocument"]["assessment"]["barrier"]["type"] == "混合型受损"
        assert cards[2] == {"label": "总体阶段", "value": None, "detail": None}
        assert cards[0]["detail"] is None  # Static scale annotation belongs to the client.
        risk_tables = [item for section in public["sections"] if section["id"] == "disease_risks"
                       for item in section["blocks"] if item.get("columns") == ["疾病", "风险指数", "风险等级", "机制关联"]]
        from tests.unit.gut_barrier.template_risk_assertions import assert_template_risk_contract
        assert_template_risk_contract(public, stored["audit"]["reportDocument"]["assessment"])
        assert len(risk_tables) == 8 and sum(len(item["rows"]) for item in risk_tables) == 23
        findings = {row[0]: row for item in risk_tables for row in item["rows"]}
        from modules.gut_barrier.risk_index_rules import confirmed_rule_result
        assessment = stored["audit"]["reportDocument"]["assessment"]
        assert findings["动脉粥样硬化"][1:3] == list(confirmed_rule_result(assessment, "atherosclerosis"))
        assert findings["肠道炎症与功能紊乱"][1:3] == list(confirmed_rule_result(assessment, "intestinal_barrier_followup"))
        assert "糖脂代谢异常" not in findings
        for name in ("肠道炎症与功能紊乱",):
            assert "屏障" in findings[name][3]
        original_scores = {row["code"]: row["riskIndex"] for row in stored["audit"]["reportDocument"]["assessment"]["diseaseRisks"]}
        assert original_scores == {"intestinal_barrier_followup": 4}
        assert all(row[3].count("。") == 1 and "本次依据：" not in row[3] and "随访重点：" not in row[3]
                   for row in findings.values())
        assert "本次疾病风险评估总览" in json.dumps(public, ensure_ascii=False)
        assert "本次健康关注指数总览" not in json.dumps(public, ensure_ascii=False)
        assert not any(b["title"] == "本次样本与质控记录" for s in public["sections"] for b in s["blocks"])
        references = block("appendix", "4.4 参考文献")["items"]
        assert len(references) == 15
        reference_audit = stored["audit"]["referencePublication"]
        assert reference_audit["displayedCount"] == 15
        assert reference_audit["originalCitedCount"] > 15
        assert any(item["selection"] == "budget_omitted" for item in reference_audit["inventory"])
        assert not any(b["title"] == "4.5 补充参考资料" for s in public["sections"] for b in s["blocks"])
        assert all(not re.match(r"^\[评估\d+\]", item["citation"]) for item in references)
        for row in findings.values():
            numbers = [int(value) for group in re.findall(r"\[(\d+(?:,\d+)*)\]", row[3])
                       for value in group.split(",")]
            assert all(1 <= number <= len(references) for number in numbers)
            # Selected display citations may be absent, but no original
            # mechanism/source is discarded by the real HTTP callback flow.
            citation_free = lambda text: re.sub(r"\[\d+(?:,\d+)*\]", "", text).strip()
            claim = next(item for item in reference_audit["claims"]
                         if item.get("disease", row[0]) == row[0] and item.get("cellIndex") == 3
                         and citation_free(item["text"]) == citation_free(row[3]))
            assert claim["sourceRoots"]
            assert list(dict.fromkeys(numbers)) == claim["selectedBodyNumbers"]
            assert set(claim["sourceRoots"]) <= {
                item["sourceRoot"] for item in reference_audit["inventory"]
            }
        assert not any(b["title"] == "指标意义" for s in public["sections"] for b in s["blocks"])
        assert "四重城墙" not in json.dumps(public, ensure_ascii=False)
        # Render exact HTTP JSON using its frozen binding, not an unbound legacy view.
        from adapters.gut_barrier.render_context import render_context_from_headers
        binding = render_context_from_headers(body, self._app.settings["received_headers"][-1])
        assert binding == stored["renderContext"]
        html = render_report_html(public, template_version=binding["templateVersion"])
        embedded = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', html, re.S)
        assert embedded is not None and json.loads(embedded.group(1)) == public
        visible = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", html, flags=re.S)
        first_citations = list(dict.fromkeys(map(int, re.findall(r'href="#reference-primary-(\d+)"', visible))))
        from modules.gut_barrier.reference_pins import pinned_references
        from modules.gut_barrier.reference_order import reference_keys
        prefix = pinned_references()
        assert len(prefix) == 8
        assert references[:len(prefix)] == prefix
        assert len({tuple(sorted(reference_keys(item))) for item in references}) == len(references)
        assert set(first_citations) <= set(range(1, len(references) + 1))
        assert [number for number in first_citations if number > len(prefix)] == list(
            range(len(prefix) + 1, len(references) + 1))
        assert set(re.findall(r'href="#(reference-[^"]+)"', visible)) <= set(re.findall(r'id="(reference-[^"]+)"', visible))
        assert re.findall(r'id="figure-(\d+)"', html) == ["1", "2", "3", "4"]
        assert "第四章 肠道屏障与慢性低度炎症概述" not in html
        assert "第三章 肠道屏障分层修复方案" in html and "第四章 附录" in html
        assert 'class="risk-fill"' not in html
        assert html.count('class="risk-number"') == 23
        assert sum(row[1] is not None for item in risk_tables for row in item["rows"]) == 23
        assert "html" not in body and "html" not in stored
        # Opt in explicitly; normal unit tests never write workspace artifacts.
        output = os.environ.get("GUT_BARRIER_ACCEPTANCE_OUTPUT")
        if output:
            directory = artifact_output(output, prefix="gut-barrier-acceptance")
            directory.mkdir(parents=True, exist_ok=True)
            audit = {
                "validation": {
                    "route": "/api/v1/gut-barrier/report", "httpCallback": "localhost",
                    "storage": "in-memory Mongo collection fake", "sample": "latest-document acceptance values",
                    "deliveryAttempts": 2, "firstResponse": 503, "retryResponse": 204,
                    "retryBytesIdentical": True, "generationAttempts": 1,
                    "source": "captured HTTP callback.report", "remoteCallbackSent": False,
                },
                "generation": stored["audit"],
            }
            for name, value in (("request.json", payload), ("callback.json", body), ("audit.json", audit)):
                (directory / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            (directory / "report.html").write_text(html, encoding="utf-8")

    @tornado.testing.gen_test
    async def test_hospital_exam_safety_reaches_final_http_callback_without_extra_input_fields(self):
        payload = self.payload()
        payload["gutHealthIndicator"] = {
            "dLacticAcid": {"value": "26", "unit": "mg/L"},
            "diamineOxidase": {"value": "17", "unit": "U/L"},
            "bacterialEndotoxin": {"value": "23", "unit": "U/L"},
            "hsCRP": {"value": "2.7", "unit": "mg/L"},
        }
        payload["hospitalData"] = [{"itemName": "院内检查", "dataType": "EXAM",
            "examResult": "肠梗阻；急性感染。", "checkTime": "2026-09-14 08:00:00"}]
        self.service.callback_url = self.get_url("/test-callback")
        self._app.settings["receiver_ready"] = True
        response = await self.post("/api/v1/gut-barrier/report", payload)
        assert response.code == 200
        await self.supervisor.wait_idle()
        body = self._app.settings["received"][0][1]
        internal = self.collection.documents[body["finalReportId"]]["audit"]["reportDocument"]
        codes = {item["code"] for item in internal["assessment"]["context"]["hospitalSafetySignals"]}
        assert codes == {"fiber_hold", "acute_infection"}
        assert internal["assessment"]["context"]["acuteInfection"] is True
        assert internal["interventions"]["nutrition"][0]["status"] == "safety_hold"
        assert body["report"] == project_callback_report(internal)
        assert "25–35" not in json.dumps(body["report"], ensure_ascii=False)
        assert body["status"] == "partial"

    @tornado.testing.gen_test
    async def test_current_range_reaches_callback_and_four_lipid_records_remain_internal(self):
        payload = self.payload()
        payload["gutHealthIndicator"]["hsCRP"] = {"value": ">3.0", "unit": "mg/L"}
        payload["hospitalData"] = [
            {"checkTime": date, "dataType": "INSPECTION", "itemName": name,
             "referenceRange": reference, "reportTime": date, "resultStatus": 1,
             "resultStatusName": "偏高", "testResultValue": value}
            for date, name, value, reference in (
                ("2025-12-15 09:31:08", "低密度脂蛋白（LDL-c）", "3.70", "0.00-3.37"),
                ("2025-12-15 09:31:08", "总胆固醇（TC）", "6.39", "0.00-5.20"),
                ("2025-11-10 09:31:08", "低密度脂蛋白(LDL-c)", "3.60", "0.00-3.37"),
                ("2025-11-10 09:31:08", "总胆固醇(TC)", "6.20", "0.00-5.20"),
            )
        ]
        self.service.callback_url = self.get_url("/test-callback")
        self._app.settings["receiver_ready"] = True
        response = await self.post("/api/v1/gut-barrier/report", payload)
        assert response.code == 200
        await self.supervisor.wait_idle()
        assert len(self._app.settings["received"]) == 1
        assert await self.service.drain_delivery() == 0
        body = self._app.settings["received"][0][1]
        assert body["type"] == "gutBarrier"
        assert body["status"] == "partial" and body["error"] is None
        internal = self.collection.documents[body["finalReportId"]]["audit"]["reportDocument"]
        public = body["report"]
        assert public == project_callback_report(internal)
        assert set(public) == {"schemaVersion", "meta", "sections"}
        assert public["schemaVersion"] == "gut-barrier-public.v2"
        assert set(public["meta"]) == {"title", "reportId"}
        patient_text = json.dumps(public["sections"], ensure_ascii=False)
        assert "hs-CRP" not in patient_text
        for value in ("0.06", "3.8", "12.5"):
            assert value in patient_text
        assert "hs-CRP指标异常" not in patient_text and "定量复测" not in patient_text
        assert not any("个体临床背景" in (block.get("title") or "")
                       for section in public["sections"] for block in section["blocks"])
        assert len(json.dumps(public)) < len(json.dumps(internal))
        assessment = internal["assessment"]
        markers = {marker["code"]: marker for marker in assessment["markers"]}
        assert [(markers[code]["value"], markers[code]["unit"]) for code in ("D_LACTATE", "DAO", "LPS")] == [
            (0.06, "mg/L"), (3.8, "U/L"), (12.5, "U/L")]
        assert "HS_CRP" not in markers and "inflammation" not in assessment
        findings = assessment["context"]["clinicalFindings"]
        assert len(findings) == 4 and all(item["temporalStatus"] == "historical" for item in findings)
        assert [item["value"] for item in findings] == ["3.70", "6.39", "3.60", "6.20"]
        assert [item["date"] for item in findings] == ["2025-12-15", "2025-12-15", "2025-11-10", "2025-11-10"]
        cardiovascular = next(item for item in assessment["risks"] if item["code"] == "cardiovascular")
        basis = " ".join(cardiovascular["basis"])
        assert all(value in basis for value in ("3.70", "6.39", "3.60", "6.20"))
        assert "2025-12-15" in basis and "2025-11-10" in basis
        assert cardiovascular["riskIndex"] is None and cardiovascular["probability"] is None

    @tornado.testing.gen_test
    async def test_auxiliary_questionnaire_compatibility_keeps_callback_contract(self):
        from tests.unit.gut_barrier.test_complete_input_contract import assert_public_contract

        self.service.callback_url = self.get_url("/test-callback")
        self._app.settings["receiver_ready"] = True
        risks = []
        for index, timestamp in enumerate(("2030/01/01 12:00:00", "时间未知", None)):
            payload = final_input()
            payload.update(userId="compat-http", sessionId=f"compat-http-{index}",
                           age="28岁", fileList="N/A", testData=None, hospitalData="未提供")
            # Questionnaire demographics remain auxiliary, even when the age
            # differs from registration; aliases describe the same questionnaire.
            payload["healthSurvey"] = {"name": "兼容测试", "age": "26", "gender": "女",
                                       "digestion": "消化不良", "heightCm": "160", "weightKg": "90"}
            survey = {"name": "兼容测试", "age": "26", "gender": "女",
                      "submitTime": timestamp, "bowelMovement": "时常腹泻"}
            payload["healthSurvery"] = survey
            # The submitted latest template repeats identical questionnaire keys.
            raw = json.dumps(payload)[:-1] + ',"healthSurvery":' + json.dumps(survey) + "}"
            response = await self.http_client.fetch(
                self.get_url("/api/v1/gut-barrier/report"), method="POST", body=raw,
                headers={"Content-Type": "application/json"}, raise_error=False)
            assert response.code == 200
            receipt = json.loads(response.body)["data"]
            await self.supervisor.wait_idle()
            saved = self.collection.documents[receipt["finalReportId"]]
            assert saved["deliveryStatus"] == "delivered"
            assessment = saved["audit"]["reportDocument"]["assessment"]
            assert assessment["subject"]["age"] == 28
            assert "bmi" not in assessment["context"]
            assert "35.2 kg/m²" in dict(assessment["context"]["healthSurveyRows"])["BMI"]
            assert set(assessment["context"]["symptoms"]) == {"消化不良", "时常腹泻"}
            assert "obesity" not in {item["code"] for item in assessment["diseaseRisks"]}
            assert not any(item["code"] in {"HEALTH_SURVEY_FUTURE_DATE", "HEALTH_SURVEY_INVALID_DATE"}
                           for item in saved["audit"]["issues"])
            risks.append(assessment["riskRuleAssessment"])
            _, callback = self._app.settings["received"][-1]
            assert callback["error"] is None
            assert set(callback) == {"type", "schemaVersion", "finalReportId", "userId", "sessionId",
                                     "status", "report", "error"}
            assert_public_contract(callback["report"])
            titles = {block.get("title") for section in callback["report"]["sections"]
                      for block in section["blocks"]}
            assert not titles.intersection({"风险指数说明", "状态下限说明", "风险评估资料提示"})
        assert risks[0] == risks[1] == risks[2]

    @tornado.testing.gen_test
    async def test_missing_marker_unit_is_accepted_without_inventing_measurements(self):
        from tests.unit.gut_barrier.test_complete_input_contract import assert_public_contract

        payload = current_health_input()
        payload.update(userId="missing-unit-http", sessionId="missing-unit-http", fileList=[])
        payload["gutHealthIndicator"]["dLacticAcid"] = {"value": "19"}
        self.service.callback_url = self.get_url("/test-callback")
        self._app.settings["receiver_ready"] = True
        response = await self.post("/api/v1/gut-barrier/report", payload)
        assert response.code == 200
        await self.supervisor.wait_idle()
        receipt = json.loads(response.body)["data"]
        saved = self.collection.documents[receipt["finalReportId"]]
        assert saved["request"]["gutHealthIndicator"]["dLacticAcid"] == {"value": "19"}
        markers = {item["code"]: item for item in saved["audit"]["reportDocument"]["assessment"]["markers"]}
        assert markers["D_LACTATE"]["status"] == "needs_review"
        assert markers["D_LACTATE"]["score"] is None
        assert markers["DAO"]["status"] == "evaluated" and markers["LPS"]["status"] == "evaluated"
        _, callback = self._app.settings["received"][-1]
        assert callback["status"] == "partial" and callback["error"] is None
        assert_public_contract(callback["report"])

    @tornado.testing.gen_test
    async def test_invalid_empty_and_storage_unavailable_contracts(self):
        for payload in ({}, {"userId": "u"}):
            response = await self.post("/api/v1/gut-barrier/report", payload)
            assert response.code == 400
        response = await self.post("/api/v1/gut-barrier/report", {
            "userId": "u", "sessionId": "s", "fileList": None, "testData": None,
            "hospitalData": None, "gutHealthIndicator": {"dLacticAcid": {}, "diamineOxidase": None}})
        assert response.code == 200
        await self.supervisor.wait_idle()
        self.collection.unavailable = True
        response = await self.post("/api/v1/gut-barrier/report", self.payload())
        assert response.code == 503

    def test_existing_report_route_dependencies_and_types_unchanged(self):
        from modules.report.artifacts import REPORT_TYPES
        from web.handlers.report import ReportGenerateHandler
        old = next(route for route in build_routes(self.container) if route.name == "report.submit")
        assert old.handler_class is ReportGenerateHandler
        assert set(old.kwargs) == {"pipeline", "task_supervisor", "task_coordinator"}
        # Match the report subsystem already merged into develop; this endpoint
        # must not remove or add any of the other subsystem's report types.
        assert REPORT_TYPES == ("evaluate", "instruction", "opinion", "explain", "explain_gutBarrier")

    @tornado.testing.gen_test
    async def test_invalid_json_numbers_and_sensitive_nested_fields_are_safe_client_errors(self):
        payload = self.payload()
        payload["gutHealthIndicator"]["dLacticAcid"]["value"] = 10 ** 30
        response = await self.post("/api/v1/gut-barrier/report", payload)
        assert response.code == 400 and not self.collection.documents
        secret_key = "https://files.example.test/patient-Zhang?token=SECRET"
        payload = self.payload()
        payload["hospitalData"] = [{secret_key + "x" * 201: "value"}]
        with self.assertLogs(level="WARNING") as captured:
            response = await self.post("/api/v1/gut-barrier/report", payload)
        assert response.code == 400
        assert secret_key not in response.body.decode()
        assert "SECRET" not in " ".join(captured.output)


@pytest.mark.parametrize("canonical", [False, True])
@pytest.mark.parametrize("object_row", [False, True])
@pytest.mark.parametrize("value,evaluation,normal", [
    ("0.2 mg/L", "符合功能医学标准值", True),
    ("2.66 mg/L", "超出功能医学标准范围", False),
    (">3.0 mg/L", "超出功能医学标准范围", False),
])
def test_new_interpretation_ignores_crp_from_current_and_frozen_tables_without_rewriting_source(canonical, object_row, value, evaluation, normal):
    from modules.report.gut_barrier_context import build_gut_barrier_contexts
    from modules.report.gut_barrier_defaults import default_section
    columns = (["检测项目", "检测指标", "检测结果", "参考区间", "结果评价", "反映维度"] if canonical else
               ["检测指标", "检测结果", "参考范围", "结果评价", "单项评分"])
    cells = (["炎症指标", "超敏C反应蛋白（hs-CRP）", value, "＜0.5 mg/L", evaluation, "慢性低度炎症"] if canonical else
             ["超敏C反应蛋白（hs-CRP）", value, "＜0.5 mg/L", evaluation, None])
    data = {"report": {"sections": [{"id": "overview", "visible": True, "blocks": [
        {"kind": "table", "title": "1.2 检测项目与结果总览" if canonical else "四项检测指标",
         "columns": columns, "rows": [{"cells": cells, "current": False} if object_row else cells]},
        {"kind": "cards", "title": "肠道屏障功能评分（IBFC）", "items": [
            {"label": "IBFC评分", "value": 0, "detail": None}]},
    ]}]}}
    before = json.dumps(data, ensure_ascii=False)
    contexts = build_gut_barrier_contexts(data)
    part1 = contexts["part1"]
    assert "inflammation" not in part1
    assert part1["normal_count"] == 0
    assert part1["ibfc"]["score"] == "0"
    manual = default_section(1, contexts)
    assert manual["indicator_talk"]["items"] == []
    assert manual["inflammation_talk"] == {"kind": "script", "text": "", "must_say": ""}
    if canonical:
        assert all(item["anchor"] in {"1.1", "1.2"} for item in manual["key_points"])
    assert json.dumps(data, ensure_ascii=False) == before
