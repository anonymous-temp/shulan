"""Backend release bindings remain covered without importing preview tooling."""

from copy import deepcopy
import json

import pytest

from adapters.gut_barrier.render_context import make_render_context, resolve_render_context
from modules.gut_barrier.display_versions import (
    CANONICAL_DISPLAY_TEMPLATE, LATEST_DISPLAY_TEMPLATE, LEGACY_DISPLAY_TEMPLATE,
    ORDERED_REFERENCE_DISPLAY_TEMPLATE, PRODUCT_FEEDBACK_DISPLAY_TEMPLATE,
    CLOSED_BOUNDARY_DISPLAY_TEMPLATE,
    SOURCE_STAGE_DISPLAY_TEMPLATE, WORD_REVIEW_DISPLAY_TEMPLATE,
    display_capabilities, resolve_display_template,
)
from modules.gut_barrier.pipeline import GutBarrierPipeline
from modules.gut_barrier.publication import project_callback_report
from modules.gut_barrier.schemas import GutBarrierRequest
from tests.unit.gut_barrier.final_input_fixture import final_input
from tests.unit.gut_barrier.test_dynamic_publication import clinical_report


def test_public_metadata_cannot_override_trusted_display_release_binding():
    assert LATEST_DISPLAY_TEMPLATE == "gut-barrier-static.v14"
    assert PRODUCT_FEEDBACK_DISPLAY_TEMPLATE == "gut-barrier-static.v9"
    assert CLOSED_BOUNDARY_DISPLAY_TEMPLATE == "gut-barrier-static.v13"
    assert display_capabilities(LATEST_DISPLAY_TEMPLATE).barrier_only_layout
    assert not display_capabilities(CLOSED_BOUNDARY_DISPLAY_TEMPLATE).barrier_only_layout
    assert display_capabilities(CLOSED_BOUNDARY_DISPLAY_TEMPLATE).closed_boundary_layout
    assert display_capabilities(CLOSED_BOUNDARY_DISPLAY_TEMPLATE).pinned_reference_layout
    assert display_capabilities(CLOSED_BOUNDARY_DISPLAY_TEMPLATE).template_risk_layout
    assert display_capabilities(CLOSED_BOUNDARY_DISPLAY_TEMPLATE).ordered_reference_layout
    assert ORDERED_REFERENCE_DISPLAY_TEMPLATE == "gut-barrier-static.v10"
    assert not display_capabilities(ORDERED_REFERENCE_DISPLAY_TEMPLATE).template_risk_layout
    assert display_capabilities(ORDERED_REFERENCE_DISPLAY_TEMPLATE).ordered_reference_layout
    assert not display_capabilities(PRODUCT_FEEDBACK_DISPLAY_TEMPLATE).ordered_reference_layout
    assert CANONICAL_DISPLAY_TEMPLATE == "gut-barrier-static.v8"
    assert display_capabilities(PRODUCT_FEEDBACK_DISPLAY_TEMPLATE).product_feedback_layout
    assert not display_capabilities(CANONICAL_DISPLAY_TEMPLATE).product_feedback_layout
    assert WORD_REVIEW_DISPLAY_TEMPLATE == "gut-barrier-static.v7"
    assert display_capabilities(CANONICAL_DISPLAY_TEMPLATE).canonical_dynamic_layout
    assert not display_capabilities(WORD_REVIEW_DISPLAY_TEMPLATE).canonical_dynamic_layout
    assert SOURCE_STAGE_DISPLAY_TEMPLATE == "gut-barrier-static.v6"
    report = project_callback_report(clinical_report())
    report["meta"]["displayTemplateVersion"] = LATEST_DISPLAY_TEMPLATE
    assert resolve_display_template(report) == LEGACY_DISPLAY_TEMPLATE
    assert resolve_display_template(report, LATEST_DISPLAY_TEMPLATE) == LATEST_DISPLAY_TEMPLATE


@pytest.mark.parametrize("version", ["gut-barrier-static.v99", "../../secret", [], False, ""])
def test_unknown_bound_display_version_is_rejected(version):
    with pytest.raises(ValueError, match="unsupported"):
        resolve_display_template({}, version)


def test_binding_verifies_report_digest_identifier_and_required_version_without_mutation():
    report = project_callback_report(clinical_report())
    before = deepcopy(report)
    # This minimal historical receipt predates the v10 citation contract.
    context = make_render_context(report, PRODUCT_FEEDBACK_DISPLAY_TEMPLATE)
    assert resolve_render_context(report, context) == PRODUCT_FEEDBACK_DISPLAY_TEMPLATE
    assert report == before
    wrong = deepcopy(report)
    wrong["meta"]["reportId"] = "another"
    with pytest.raises(ValueError, match="does not match"):
        resolve_render_context(wrong, context)
    context["templateVersion"] = None
    with pytest.raises(ValueError):
        resolve_render_context(report, context)


@pytest.mark.asyncio
async def test_current_pipeline_freezes_internal_release_and_public_summary_contract():
    payload = final_input()
    payload["fileList"] = []
    internal, audit = await GutBarrierPipeline().generate(
        GutBarrierRequest.model_validate(payload), report_id="gbr_release_contract",
        evaluation_date="2026-09-25")
    public = project_callback_report(internal)
    assert audit["displayTemplateVersion"] == internal["meta"]["displayTemplateVersion"] == LATEST_DISPLAY_TEMPLATE
    assert "displayTemplateVersion" not in json.dumps(public)
    disease = next(section for section in public["sections"] if section["id"] == "disease_risks")
    system_count = sum(block["kind"] == "table" and block["columns"] ==
                       ["疾病", "风险指数", "风险等级", "机制关联"] for block in disease["blocks"])
    summary = disease["blocks"][-1]
    assert summary["title"] == f"2.{system_count + 1} 本章小结"
    assert summary["kind"] == "paragraph" and isinstance(summary["text"], str) and summary["text"]
    assert set(summary) == {"kind", "title", "text"}
    paragraphs = summary["text"].split("\n\n")
    assert len(paragraphs) == 2 and all(part and "\n" not in part for part in paragraphs)
    assert summary["text"].endswith("修复肠道屏障功能，恢复肠道菌群稳态。")
    assert "本次试行规则下" not in summary["text"]
    assert sum("本章小结" in (block.get("title") or "") for block in disease["blocks"]) == 1
    assert not any("个体临床背景" in (block.get("title") or "") for block in disease["blocks"])
    assert project_callback_report(public) == public
