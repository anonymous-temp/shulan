"""Joint-debugging feedback through the production route and local HTTP callback.

All identities and source data are synthetic; source retrieval stays offline.
Set SHULAN_FEEDBACK_ARTIFACT_ROOT to a new directory to retain actual requests,
receipts, callback bytes and headers for a manual preview without overwriting an
earlier run. Ordinary test runs do not write artifacts.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import re

import tornado.testing

from adapters.gut_barrier.render_context import render_context_from_headers
from modules.gut_barrier.display_versions import BARRIER_ONLY_DISPLAY_TEMPLATE
from modules.gut_barrier.publication import project_callback_report
from tests.unit.gut_barrier import test_api as api_fixture
from tests.unit.gut_barrier.final_input_fixture import current_health_input
from tests.unit.gut_barrier.template_risk_assertions import assert_template_risk_contract
from tests.unit.gut_barrier.test_complete_input_contract import (
    assert_public_contract, complete_input, offline_sources,
)


RISK_COLUMNS = ["疾病", "风险指数", "风险等级", "机制关联"]
OVERVIEW_COLUMNS = ["系统", "疾病", "风险指数", "风险等级"]


def _section(report, identifier):
    return next(section for section in report["sections"] if section["id"] == identifier)


def _marker_table(report):
    return next(block for block in _section(report, "overview")["blocks"]
                if block["title"] == "1.2 检测项目与结果总览")


def _save_artifact(case, payload, receipt, received_bytes, headers, binding):
    root = os.environ.get("SHULAN_FEEDBACK_ARTIFACT_ROOT")
    if not root:
        return
    directory = Path(root) / case
    directory.mkdir(parents=True, exist_ok=False)
    for filename, value in (("request.json", payload), ("receipt.json", receipt),
                            ("callback-headers.json", headers), ("render-context.json", binding)):
        with (directory / filename).open("x", encoding="utf-8") as target:
            json.dump(value, target, ensure_ascii=False, indent=2)
            target.write("\n")
    with (directory / "callback.json").open("xb") as target:
        target.write(received_bytes)


class TestIntegrationFeedbackAPI(tornado.testing.AsyncHTTPTestCase):
    # Reuse the real production app, service and durable workers without
    # subclassing and re-collecting every existing TestGutBarrierAPI test.
    get_app = api_fixture.TestGutBarrierAPI.get_app
    post = api_fixture.TestGutBarrierAPI.post

    def tearDown(self):
        self.io_loop.run_sync(self.transport.aclose)
        super().tearDown()

    async def _submit_and_replay(self, case, payload):
        self.service.callback_url = self.get_url("/test-callback")
        self._app.settings["receiver_ready"] = True
        before = deepcopy(payload)
        received_count = len(self._app.settings["received"])
        generated_count = self.pipeline.generate.await_count
        response = await self.post("/api/v1/gut-barrier/report", payload)
        assert response.code == 200
        receipt = json.loads(response.body)["data"]
        assert set(receipt) == {"userId", "sessionId", "finalReportId", "status"}
        assert receipt["status"] == "processing"
        await self.supervisor.wait_idle()
        assert len(self._app.settings["received"]) == received_count + 1
        key, body = self._app.settings["received"][received_count]
        frozen_bytes = self._app.settings["received_bytes"][received_count]
        headers = self._app.settings["received_headers"][received_count]
        assert key == receipt["finalReportId"] + ":gutBarrier:v2"
        assert set(body) == {"type", "schemaVersion", "finalReportId", "userId", "sessionId", "status", "report", "error"}
        assert body["type"] == "gutBarrier" and body["schemaVersion"] == "2.0"
        assert body["userId"] == payload["userId"] and body["sessionId"] == payload["sessionId"]
        assert body["status"] in {"completed", "partial"} and body["error"] is None
        assert_public_contract(body["report"])
        assert project_callback_report(body["report"]) == body["report"]
        saved = self.collection.documents[body["finalReportId"]]
        binding = render_context_from_headers(body, headers)
        assert binding == saved["renderContext"]
        assert binding["templateVersion"] == BARRIER_ONLY_DISPLAY_TEMPLATE
        assert saved["body"] == body and saved["deliveryStatus"] == "delivered"
        assert saved["generationAttempts"] == 1
        assert self.pipeline.generate.await_count == generated_count + 1

        repeated = await self.post("/api/v1/gut-barrier/report", payload)
        assert repeated.code == 200 and json.loads(repeated.body)["data"] == receipt
        await self.supervisor.wait_idle()
        assert await self.service.drain_generation() == 0
        assert await self.service.drain_delivery() == 0
        assert self.pipeline.generate.await_count == generated_count + 1
        assert self._app.settings["received"][received_count:] == [(key, body), (key, body)]
        assert self._app.settings["received_bytes"][received_count:] == [frozen_bytes, frozen_bytes]
        assert render_context_from_headers(body, self._app.settings["received_headers"][-1]) == binding
        assert payload == before
        _save_artifact(case, payload, receipt, frozen_bytes, headers, binding)
        return body["report"], saved["audit"]["reportDocument"]

    def _assert_chapter_feedback(self, public, internal):
        blocks = _section(public, "disease_risks")["blocks"]
        introduction = blocks[0]
        assert set(introduction) == {"kind", "title", "text"}
        assert introduction["kind"] == "paragraph" and introduction["title"] is None
        assert introduction["text"] == (
            "肠道屏障受损与慢性低度炎症是多种慢性疾病与机体衰老的共同上游机制。"
            "本章基于本次检测结果与相关健康评估，评估相关疾病风险。"
            "风险等级（低、中、高）为基于检测指标的健康风险评估推断，"
            "用于风险提示与预防参考，不构成疾病诊断。")
        chart = next(block for block in blocks if block["title"] == "本次疾病风险评估总览")
        assert chart["kind"] == "table" and chart["columns"] == OVERVIEW_COLUMNS
        tables = [block for block in blocks if block.get("columns") == RISK_COLUMNS]
        if tables:
            assert blocks.index(chart) < blocks.index(tables[0])
        # The full template is a review directory, not 22 invented diagnoses.
        # Model and versioned-rule numeric results enter the same overview.
        expected_rows = []
        for table in tables:
            system = re.sub(r"(?:疾病)?风险评估$", "", re.sub(r"^2\.\d+\s+", "", table["title"]))
            expected_rows.extend([system, row[0], row[1], row[2]] for row in table["rows"] if row[1] is not None)
        assert chart["rows"] == expected_rows
        summaries = [block for block in blocks if "本章小结" in (block.get("title") or "")]
        assert len(summaries) == 1 and summaries[0] == blocks[-1]
        summary = summaries[0]
        assert not any("个体临床背景" in (block.get("title") or "") for block in blocks)
        assert not any(block.get("columns") == ["检查日期", "项目", "时间属性", "原始检查摘要"] for block in blocks)
        title = f"2.{len(tables) + 1} 本章小结"
        assert set(summary) == {"kind", "title", "text"}
        assert summary["kind"] == "paragraph" and summary["title"] == title and summary["text"]
        assert "逐项复核" not in summary["text"]
        assert "hs-CRP" not in summary["text"]
        assert not any(block["title"] in {"本次相关因素与复核依据", "影像检查待核实事项"} for block in blocks)
        assert all(term in summary["text"] for term in ("内毒素", "血液循环", "慢性低度炎症"))
        paragraphs = summary["text"].split("\n\n")
        assert len(paragraphs) == 2 and all(part and "\n" not in part for part in paragraphs)
        assert summary["text"].endswith("修复肠道屏障功能，恢复肠道菌群稳态。")
        assert "本次试行规则下" not in summary["text"]
        original = next(block for block in _section(internal, "disease_risks")["blocks"]
                        if block["kind"] == "list" and block["title"] == "本章小结")
        assert original["items"] and all(item not in summary["text"] for item in original["items"])
        assert_template_risk_contract(public, internal["assessment"])
        assert not any(block["title"] in {"状态下限说明", "风险指数说明", "风险评估资料提示"} for block in blocks)
        marker_table = _marker_table(public)
        assert marker_table["columns"] == ["检测项目", "检测指标", "检测结果", "参考区间", "结果评价", "反映维度"]
        assert len(marker_table["rows"]) == 3
        encoded = json.dumps(public, ensure_ascii=False)
        assert "crp" not in encoded.lower() and "C反应蛋白" not in encoded
        return chart, summary, marker_table

    @tornado.testing.gen_test
    async def test_questionnaire_does_not_add_risk_directions_to_real_callback(self):
        self.clock.advance(19 * 24 * 60 * 60)
        payload = current_health_input()
        payload.update(userId="feedback-numeric-user", sessionId="feedback-numeric-session", name="合成数值受检者")
        payload["healthSurvey"].update(name=payload["name"], age=payload["age"], gender=payload["sex"])
        payload["gutHealthIndicator"]["hsCRP"]["value"] = "1.8"
        public, internal = await self._submit_and_replay("numeric-multisystem", payload)
        chart, summary, marker_table = self._assert_chapter_feedback(public, internal)
        assert "inflammation" not in internal["assessment"]
        tables = [block for block in _section(public, "disease_risks")["blocks"] if block.get("columns") == RISK_COLUMNS]
        assert len(tables) == 8 and len(chart["rows"]) == 23
        # Questionnaire answers do not create extra disease directions. The
        # measured barrier markers still produce their existing follow-up.
        assert all(row[1] is not None for table in tables for row in table["rows"])
        assert {row["code"] for row in internal["assessment"]["diseaseRisks"]} == {"intestinal_barrier_followup"}
        by_name = {row[0]: row for table in tables for row in table["rows"]}
        from modules.gut_barrier.risk_index_rules import confirmed_rule_result
        assert by_name["肠道炎症与功能紊乱"][1:3] == list(
            confirmed_rule_result(internal["assessment"], "intestinal_barrier_followup"))
        assert "糖脂代谢异常" not in by_name
        internal_rows = {row["code"]: row for row in internal["assessment"]["diseaseRisks"]}
        assert internal_rows["intestinal_barrier_followup"]["riskIndex"] == 5
        assert "metabolic_barrier_followup" not in internal_rows
        markers = {marker["code"]: marker for marker in internal["assessment"]["markers"]}
        assert markers["DAO"]["score"] > 0 and "HS_CRP" not in markers
        assert markers["D_LACTATE"]["score"] == markers["LPS"]["score"] == 0
        assert summary["text"].endswith("修复肠道屏障功能，恢复肠道菌群稳态。")
        assert "DAO等肠道屏障相关指标异常" in summary["text"]
        assert "hs-CRP指标异常" not in summary["text"] and "健康问卷" not in summary["text"]

    @tornado.testing.gen_test
    async def test_range_result_has_conservative_rule_scores_and_preserves_real_callback_values(self):
        self.clock.advance(19 * 24 * 60 * 60)
        payload = complete_input()
        payload.update(userId="feedback-range-user", sessionId="feedback-range-session", name="合成区间受检者")
        payload["healthSurvey"].update(name=payload["name"], age=payload["age"], gender=payload["sex"])
        self.pipeline.sources, downloader = offline_sources(payload["name"])
        public, internal = await self._submit_and_replay("range-qualitative", payload)
        chart, summary, marker_table = self._assert_chapter_feedback(public, internal)
        assert downloader.download.await_count == 4
        assert "inflammation" not in internal["assessment"]
        assert internal["assessment"]["riskRuleAssessment"]["panelCoverage"] == {"available": 3, "required": 3}
        details = [row for block in _section(public, "disease_risks")["blocks"]
                   if block.get("columns") == RISK_COLUMNS for row in block["rows"]]
        assert len(details) == 23
        assert "既往胃炎随访" not in {row[0] for row in details}
        # The source is retained internally without becoming an extra risk row.
        from modules.gut_barrier.risk_index_rules import confirmed_rule_result
        assert confirmed_rule_result(internal["assessment"], "gastritis_followup") is None
        assert {row["code"] for row in internal["assessment"]["diseaseRisks"]} == {
            "hyperlipidemia", "atherosclerosis", "gastritis_followup"}
        from modules.gut_barrier.risk_model import build_disease_risks
        catalog, _ = build_disease_risks()
        expected_codes = {row["code"] for row in catalog}
        assert len(chart["rows"]) == len(expected_codes) == 23
        assert all(1 <= row[2] <= 10 and row[3] in {"低", "中", "高"} for row in chart["rows"])
        assert internal["assessment"]["riskRuleAssessment"]["notices"] == []
        assert "屏障三项未见评分异常，IBFC为0分" in summary["text"]
        assert "结合hs-CRP指标异常，提示存在炎症相关因素" not in summary["text"]
        assert "不能据此确定慢性炎症" not in summary["text"]
        assert "该范围不能排除 >10 mg/L" not in json.dumps(_section(public, "overview"), ensure_ascii=False)
        assert "提示需关注肠源性慢性低度炎症相关因素" not in summary["text"]
        assert "损伤等级为" not in summary["text"]
        assert summary["text"].endswith("修复肠道屏障功能，恢复肠道菌群稳态。")
        assert internal["assessment"]["barrier"]["score"] == 0

    @tornado.testing.gen_test
    async def test_empty_optional_inputs_keep_full_review_directory_without_invented_values(self):
        for style in ("omitted", "null", "empty"):
            payload = {"userId": f"feedback-{style}-user", "sessionId": f"feedback-{style}-session"}
            if style != "omitted":
                payload.update({key: None if style == "null" else []
                                for key in ("fileList", "testData", "hospitalData")})
                payload.update({key: None if style == "null" else {}
                                for key in ("gutHealthIndicator", "healthSurvey", "microbiomeData")})
            public, internal = await self._submit_and_replay(f"empty-{style}", payload)
            chart, summary, marker_table = self._assert_chapter_feedback(public, internal)
            assert chart["rows"] == [] and internal["assessment"]["diseaseRisks"] == []
            rows, _, _ = assert_template_risk_contract(public, internal["assessment"])
            assert len(rows) == 23 and all(row[1:3] == [None, None] for row in rows.values())
            assert not any(block["title"] == "风险评估资料提示"
                           for block in _section(public, "disease_risks")["blocks"])
            assert "尚不足以形成具体疾病关注方向" not in summary["text"]
            assert all(row[2] is None for row in marker_table["rows"])
            assert internal["assessment"]["barrier"]["score"] is None

    @tornado.testing.gen_test
    async def test_meaningful_zero_and_false_survive_callback_and_its_frozen_replay(self):
        payload = {
            "userId": "feedback-zero-user", "sessionId": "feedback-zero-session",
            "name": "合成零值受检者", "age": 28, "sex": "女", "healthSurvey": {},
            "fileList": [], "testData": [], "hospitalData": [], "microbiomeData": None,
            "gutHealthIndicator": {
                "bacterialEndotoxin": {"value": "0", "unit": "U/L"},
                "diamineOxidase": {"value": "0", "unit": "U/L"},
                "dLacticAcid": {"value": "0", "unit": "mg/L"},
                "hsCRP": {"value": "0", "unit": "mg/L"}, "complete": False,
            },
        }
        public, internal = await self._submit_and_replay("zero-values", payload)
        chart, _, marker_table = self._assert_chapter_feedback(public, internal)
        assert len(chart["rows"]) == 23
        assert all(row[2] in {"0 mg/L", "0 U/L"} for row in marker_table["rows"])
        rows, _, _ = assert_template_risk_contract(public, internal["assessment"])
        assert len(rows) == 23
        # Zero IBFC keeps 23 low grades and retained barrier measurement zeros.
        assert rows["炎症性肠病"][1:3] == [3.2, "低"]
        assert rows["2型糖尿病"][1:3] == [2.7, "低"]
        assert {row[2] for row in rows.values()} == {"低"}
        assert sorted(row[1] for row in rows.values()) == [n / 10 for n in range(10, 33)]
        assert len({row[1] for row in rows.values()}) == 23
        assert all(marker["value"] == 0 for marker in internal["assessment"]["markers"])
        assert internal["assessment"]["barrier"]["score"] == 0
        restored = self.pipeline.generate.call_args.args[0]
        assert restored.gut_health_indicator.complete is False
        cards = [item for block in _section(public, "overview")["blocks"] if block["kind"] == "cards"
                 for item in block["items"]]
        assert next(item["value"] for item in cards if item["label"] == "IBFC评分") == 0
        bands = [item for block in _section(public, "overview")["blocks"] if block["kind"] == "bands"
                 for item in block["items"]]
        assert any(item["selected"] is False for item in bands)

    @tornado.testing.gen_test
    async def test_v27_final_scores_take_priority_over_internal_model_zero_in_detail_and_overview(self):
        from modules.gut_barrier.risk_evaluator import RiskModelRegistry
        from tests.unit.gut_barrier.test_risk_model_adapter import evaluator, specification

        spec = specification(codes=("obesity",), disease_inputs=(("obesity", ("age",)),))
        model = evaluator(results=[{"code": "obesity", "riskIndex": 0, "riskLevel": "TEST_LOW",
                                    "trace": {"fixture": "Technical fixture only; no clinical validity"}}])
        registry = RiskModelRegistry()
        registry.register(spec, model)
        self.pipeline.risk_models = registry
        self.pipeline.risk_model_id = spec.model_id
        self.pipeline.risk_model_version = spec.version
        self.clock.advance(19 * 24 * 60 * 60)
        payload = current_health_input()
        payload.update(userId="feedback-model-user", sessionId="feedback-model-session", name="合成模型受检者")
        payload["healthSurvey"].update(name=payload["name"], age=payload["age"], gender=payload["sex"])
        public, internal = await self._submit_and_replay("registered-model-zero", payload)
        model.evaluate.assert_awaited_once()
        disease = next(row for row in internal["assessment"]["diseaseRisks"] if row["code"] == "obesity")
        assert disease["status"] == "evaluated" and disease["riskIndex"] == 0
        blocks = _section(public, "disease_risks")["blocks"]
        detail = next(row for block in blocks if block.get("columns") == RISK_COLUMNS
                      for row in block["rows"] if row[0] == "肥胖")
        from modules.gut_barrier.risk_index_rules import confirmed_rule_result
        expected = list(confirmed_rule_result(internal["assessment"], "obesity"))
        assert detail[1:3] == expected
        assert "不与本章自定义关注指数比较" not in detail[3]
        assert not any(block.get("title") == "独立模型说明" for block in blocks)
        assert disease["riskLevel"] == "TEST_LOW"
        assert "TEST_LOW" not in json.dumps(public, ensure_ascii=False)
        assert not any(block["title"] == "风险评估资料提示" for block in blocks)
        chart = next(block for block in blocks if block["title"] == "本次疾病风险评估总览")
        assert chart["columns"] == OVERVIEW_COLUMNS and chart["rows"]
        assert [row[1:] for row in chart["rows"] if row[1] == "肥胖"] == [["肥胖", *expected]]
        assert all(row[2] >= 1 and row[3] in {"低", "中", "高"} for row in chart["rows"] if row[1] != "肥胖")
        assert_template_risk_contract(public, internal["assessment"])
        summary = blocks[-1]
        assert summary["kind"] == "paragraph" and "本章小结" in summary["title"]
        assert set(summary) == {"kind", "title", "text"} and summary["text"]
        assert sum("本章小结" in (block.get("title") or "") for block in blocks) == 1
        assert "肥胖：" not in summary["text"]
