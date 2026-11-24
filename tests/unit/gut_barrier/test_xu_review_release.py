"""New reference/readout semantics without changing historical scoring."""
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.gut_barrier.assessment import assess_case
from modules.gut_barrier.interventions import build_interventions
from modules.gut_barrier.pipeline import GutBarrierPipeline
from modules.gut_barrier.presentation import build_report
from modules.gut_barrier.publication import project_callback_report
from modules.gut_barrier.review_release import apply_review_release
from modules.gut_barrier.schemas import GutBarrierRequest
from modules.gut_barrier.service import GutBarrierService
from tests.unit.gut_barrier.test_service import environment, request as service_request


def request(crp="1.8", *, age=45, unit="mg/L"):
    values = {"diamineOxidase": ("9.6", "U/L"), "bacterialEndotoxin": ("19.8", "U/L"),
              "dLacticAcid": ("15.6", "mg/L"), "hsCRP": (crp, unit)}
    return GutBarrierRequest.model_validate({
        "userId": "xu-test", "sessionId": "xu-session", "age": age,
        "gutHealthIndicator": {key: {"value": value, "unit": unit} for key, (value, unit) in values.items()
                               if value is not None},
    })


def assessment(crp="1.8", **kwargs):
    return assess_case(request(crp, **kwargs), context={"currentDate": "2026-09-25"})


def public(assessed):
    return project_callback_report(build_report(assessed, build_interventions(assessed), report_id="xu-test"))


def block(report, title):
    return next(b for s in report["sections"] for b in s["blocks"] if b["title"] == title)














@pytest.mark.parametrize("raw", [None, "1.8", ">10", "100"])
def test_review_projection_preserves_barrier_and_omits_crp(raw):
    original = assessment(raw)
    before = deepcopy(original)
    revised = apply_review_release(original)
    assert original == before
    assert revised["barrier"] == original["barrier"]
    assert revised["markers"] == original["markers"]
    assert revised["barrier"]["score"] == 3.5
    assert "hsCRP" not in revised["reportReview"]
    assert "inflammation" not in revised
    rows = block(public(revised), "三项检测指标")["rows"]
    assert [row[3] for row in rows] == ["D-乳酸指标异常", "DAO指标异常", "LPS指标异常"]
    assert [row[4] for row in rows] == [1, 1, 1.5]



def test_unreviewed_current_builder_also_uses_three_marker_copy():
    report = public(assessment())
    assert len(block(report, "三项检测指标")["rows"]) == 3
    assert "hs-CRP" not in json.dumps(report, ensure_ascii=False)



@pytest.mark.parametrize("crp", [None, "0.2", "1.8", "4.2", "100"])
def test_acute_context_remains_independent_of_removed_measurement(crp):
    original = assess_case(request(crp), context={"acuteInfection": True, "currentDate": "2026-09-25"})
    report = public(apply_review_release(original))
    text = json.dumps(block(report, "1.1 检测综合评估结论"),ensure_ascii=False)
    assert "急性感染" in text
    assert "hs-CRP" not in text and "高于 10" not in text



def test_existing_public_keys_and_rows_survive_internal_review_projection():
    original = assessment()
    old, new = public(original), public(apply_review_release(original))
    assert set(new) == set(old) == {"schemaVersion", "meta", "sections"}
    for previous, current in zip(old["sections"], new["sections"]):
        assert set(previous) == set(current)
        assert previous["id"] == current["id"]
        for a, b in zip(previous["blocks"], current["blocks"]):
            assert set(a) == set(b) and a["kind"] == b["kind"] and a["title"] == b["title"]
            if a["kind"] == "table":
                assert a["columns"] == b["columns"] and len(a["rows"]) == len(b["rows"])
    card = block(new, "肠道屏障功能评分（IBFC）")["items"][-1]
    assert card == {"label": "总体阶段", "value": None, "detail": None}
    encoded = json.dumps(new, ensure_ascii=False)
    for private in ("reportReview", "pendingRules", "overall_stage_policy", "sha256", "XU修改标注", "68%", "WHO国际标准"):
        assert private not in encoded
    assert [s["id"] for s in new["sections"]] == ["overview", "disease_risks", "appendix"]


@pytest.mark.asyncio
async def test_pipeline_freezes_review_version_and_records_display_only_stage():
    report, audit = await GutBarrierPipeline().generate(request(">3.0"), report_id="xu-pipeline", evaluation_date="2026-09-25")
    assert audit["overallStage"]["status"] == "display_only"
    assert "overall_stage_mapping" not in audit["reportReview"]["pendingRules"]
    assert audit["reportReview"] == report["assessment"]["reportReview"]
    assert report["meta"]["pipelineVersion"] == GutBarrierPipeline.version
    assert "hs-CRP" not in json.dumps(public(report["assessment"]),ensure_ascii=False)



@pytest.mark.asyncio
async def test_new_and_old_version_workers_drain_only_their_own_queue():
    old_pipeline = SimpleNamespace(version="gut-barrier-pipeline-v2.20260925-template-release",
        generate=AsyncMock(return_value=({"meta": {"title": "旧报告"}}, {"partial": False})))
    env = environment(pipeline=old_pipeline)
    old_job = await env.service.submit(service_request())
    env.service.stop_admission()
    new_pipeline = SimpleNamespace(version=GutBarrierPipeline.version,
        generate=AsyncMock(return_value=({"meta": {"title": "新版报告"}}, {"partial": True})))
    new_service = GutBarrierService(store=env.store, pipeline=new_pipeline)
    new_job = await new_service.submit(service_request())
    assert old_job["_id"] != new_job["_id"]
    assert await new_service.drain_generation() == 1
    assert env.collection.documents[old_job["_id"]]["status"] == "queued"
    frozen_new = deepcopy(env.collection.documents[new_job["_id"]]["body"])
    assert await env.service.drain_generation() == 1
    assert old_pipeline.generate.await_count == new_pipeline.generate.await_count == 1
    assert env.collection.documents[old_job["_id"]]["body"]["report"]["meta"]["title"] == "旧报告"
    assert env.collection.documents[new_job["_id"]]["body"] == frozen_new
